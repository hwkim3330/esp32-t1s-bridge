"""Real, locally-hosted control/monitoring app for the esp32-t1s-bridge rig.

Not a Claude Artifact (sandboxed, no real network access) -- this is a plain
FastAPI/uvicorn process bound to 0.0.0.0 on the PC itself, so it's reachable
by real IP from any device on either of the PC's networks
(192.168.100.50 for the D10/ESP32 segment, or the office LAN NIC). It talks
directly to the two D10 switches' JSON-RPC and to zenohd, and can actually
change switch config (port shutdown, STP enable, FRER admin-active), not
just display numbers.

Two things this deliberately keeps separate that the switch-only view
conflates: a node's *physical* Ethernet link (from the D10's own
port.status.get -- true even while the board's application code is stuck)
and its *zenoh* liveness (whether a bridge/**/test/stats/** sample has
arrived recently). ESP32-1 sitting at "link up, zenoh dead" is a real,
useful distinction the dashboard should show, not flatten into one
"down" pill.
"""
import asyncio
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import zenoh
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

SWITCH1 = "http://192.168.100.1"  # PC side (Gi1/6), FRER Recovery
SWITCH2 = "http://192.168.100.2"  # ESP32 side, FRER Generation
ZENOH_LOCATOR = "udp/192.168.100.50:7447"
FRER_VLAN = 200
# One entry per protected flow. Only the RECOVERY end of a flow has readable
# counters -- Generation rejects a port argument outright ("Ifindex must be
# VTSS_IFINDEX_NONE in generation mode") -- so each entry names the switch
# and egress port where that flow's recovery instance lives.
# Every protected flow, counted at its own recovery end. Downstream
# (PC -> board) shares FRER VLAN 200, since each board is a distinct
# destination MAC; upstream needs one VLAN per board, because all three send
# to the same PC MAC and would otherwise share a recovery instance -- three
# interleaved sequence counters in one instance read out as a huge fake Lost.
FRER_FLOWS = [
    {"id": "d1", "label": "PC \u2192 esp32-1", "node": "esp32-1", "dir": "down",
     "vlan": 200, "inst": 2,  "switch": "switch2", "port": "Gi 1/3"},
    {"id": "u1", "label": "esp32-1 \u2192 PC", "node": "esp32-1", "dir": "up",
     "vlan": 200, "inst": 1,  "switch": "switch1", "port": "Gi 1/6"},
    {"id": "d2", "label": "PC \u2192 esp32-2", "node": "esp32-2", "dir": "down",
     "vlan": 200, "inst": 10, "switch": "switch2", "port": "Gi 1/6"},
    {"id": "u2", "label": "esp32-2 \u2192 PC", "node": "esp32-2", "dir": "up",
     "vlan": 201, "inst": 11, "switch": "switch1", "port": "Gi 1/6"},
    {"id": "d3", "label": "PC \u2192 esp32-3", "node": "esp32-3", "dir": "down",
     "vlan": 200, "inst": 12, "switch": "switch2", "port": "Gi 1/5"},
    {"id": "u3", "label": "esp32-3 \u2192 PC", "node": "esp32-3", "dir": "up",
     "vlan": 202, "inst": 13, "switch": "switch1", "port": "Gi 1/6"},
]

NODES = {
    # esp32-1 moved to Gi1/3 after the cable-vs-board swap test (its
    # original Gi1/5 cable run was bad, not the board); esp32-3 is the
    # board now on Gi1/5, which is the physical port direction A/B's FRER
    # config actually protects. All three are live now.
    "esp32-1": {"ip": "192.168.100.60", "switch": "switch2", "port": "Gi 1/3", "mac": "a6:cb:8f:e7:f0:bd"},
    "esp32-2": {"ip": "192.168.100.61", "switch": "switch2", "port": "Gi 1/6", "mac": "2a:84:85:80:9b:85"},
    "esp32-3": {"ip": "192.168.100.62", "switch": "switch2", "port": "Gi 1/5", "mac": "a6:cb:8f:e9:88:45"},
    "pc": {"ip": "192.168.100.50", "switch": None, "port": None, "mac": "d4:5d:64:b2:5d:c3"},
}
RING_PORTS = ["Gi 1/1", "Gi 1/2"]
ZENOH_ALIVE_WINDOW_S = 6.0

_session_cache: dict[str, requests.Session] = {}


def rpc(base_url: str, method: str, params: Optional[list] = None, timeout=3):
    if base_url not in _session_cache:
        s = requests.Session()
        s.auth = ("admin", "")
        _session_cache[base_url] = s
    s = _session_cache[base_url]
    body = {"method": method, "params": params if params is not None else [], "id": 1}
    r = s.post(f"{base_url}/json_rpc", json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"{method}{params} @ {base_url} -> {data['error']}")
    return data["result"]


# ---------------------------------------------------------------------------
# Background poller: switch state
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_switch_state = {"switch1": {}, "switch2": {}, "updatedAt": None, "error": None}


def _poll_switch(name: str, base_url: str, ports: list[str]):
    out = {"ip": base_url.split("//")[1], "ports": {}, "frer": {}, "reachable": True}
    try:
        for p in ports:
            status = rpc(base_url, "port.status.get", [p])
            out["ports"][p] = {
                "link": status.get("Link"), "speed": status.get("Speed"),
                "fdx": status.get("Fdx"), "linkUpCnt": status.get("LinkUpCnt"),
                "linkDownCnt": status.get("LinkDownCnt"),
            }
        for p in RING_PORTS:
            try:
                mstp = rpc(base_url, "mstp.status.interface.get", [p, 0])
                out["ports"].setdefault(p, {})["mstp"] = {
                    "state": mstp.get("PortState"), "role": mstp.get("PortRole"),
                    "enabled": mstp.get("Enabled"),
                }
            except RuntimeError:
                out["ports"].setdefault(p, {})["mstp"] = None
            try:
                stp_cfg = rpc(base_url, "mstp.config.cist.interface.get", [p])
                out["ports"].setdefault(p, {})["stpEnabled"] = stp_cfg.get("Enable")
            except RuntimeError:
                pass
            try:
                lp = rpc(base_url, "loopProtect.status.interface.get", [p])
                out["ports"].setdefault(p, {})["loopProtect"] = {
                    "disabled": lp.get("Disabled"), "loopDetected": lp.get("LoopDetected"),
                    "loopCount": lp.get("LoopCount"),
                }
            except RuntimeError:
                pass
        out["frer"] = {}
        for flow in FRER_FLOWS:
            if flow["switch"] != name:
                continue
            entry = {"label": flow["label"], "inst": flow["inst"],
                     "node": flow["node"], "dir": flow["dir"], "vlan": flow["vlan"]}
            try:
                entry["status"] = rpc(base_url, "frer.status.get", [flow["inst"]])
            except RuntimeError as e:
                entry["status"] = None
                entry["statusError"] = str(e)
            try:
                entry["statistics"] = rpc(base_url, "frer.statistics.get",
                                          [flow["inst"], flow["port"], 0])
            except RuntimeError as e:
                entry["statistics"] = None
                entry["statisticsError"] = str(e)
            out["frer"][flow["id"]] = entry
    except Exception as e:  # noqa: BLE001 -- switch unreachable, degrade gracefully
        out["reachable"] = False
        out["error"] = str(e)
    return out


def _switch_poll_loop():
    while True:
        s1 = _poll_switch("switch1", SWITCH1, RING_PORTS + ["Gi 1/6"])
        s2 = _poll_switch("switch2", SWITCH2, RING_PORTS + ["Gi 1/3", "Gi 1/5", "Gi 1/6"])
        with _state_lock:
            _switch_state["switch1"] = s1
            _switch_state["switch2"] = s2
            _switch_state["updatedAt"] = datetime.now(timezone.utc).isoformat()
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# Background poller: zenoh liveness + recent samples
# ---------------------------------------------------------------------------
_zenoh_lock = threading.Lock()
_zenoh_recent: dict[str, deque] = {}
_zenoh_last_seen: dict[str, float] = {}
MAX_RECENT = 12


def _zenoh_on_sample(sample):
    key = str(sample.key_expr)
    try:
        payload = sample.payload.to_string()
    except Exception:
        payload = "<binary>"
    now = time.time()
    with _zenoh_lock:
        node = _key_to_node(key)
        _zenoh_recent.setdefault(node, deque(maxlen=MAX_RECENT)).append(
            {"t": now, "key": key, "val": payload}
        )
        _zenoh_last_seen[node] = now


def _key_to_node(key: str) -> str:
    parts = key.split("/")
    for tok in parts:
        if tok in NODES:
            return tok
    return "other"


ZENOH_SESSION_MAX_AGE_S = 45.0  # force a fresh session this often -- the zenoh
# session can go silently orphaned (e.g. after zenohd restarts, or a board
# reset) with no exception raised and no queryable "am I still receiving
# anything" signal on the Python side. Recreating periodically is a crude
# but robust fix: worst case is a few seconds of gap every 45s, far better
# than staying silently dead until someone notices and restarts this
# process by hand (happened repeatedly before this fix).


def _zenoh_thread():
    while True:
        try:
            conf = zenoh.Config()
            conf.insert_json5("mode", '"client"')
            conf.insert_json5("connect/endpoints", f'["{ZENOH_LOCATOR}"]')
            with zenoh.open(conf) as session:
                session.declare_subscriber("bridge/**", _zenoh_on_sample)
                session.declare_subscriber("test/stats/**", _zenoh_on_sample)
                session.declare_subscriber("test/ping/**", _zenoh_on_sample)
                session.declare_subscriber("ivn/**", _zenoh_on_sample)
                time.sleep(ZENOH_SESSION_MAX_AGE_S)
        except Exception:  # noqa: BLE001 -- keep retrying no matter what
            time.sleep(2.0)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="esp32-t1s-bridge control")


@app.on_event("startup")
def _startup():
    threading.Thread(target=_switch_poll_loop, daemon=True).start()
    threading.Thread(target=_zenoh_thread, daemon=True).start()


@app.get("/api/state")
def get_state():
    now = time.time()
    with _state_lock:
        switches = {"switch1": dict(_switch_state["switch1"]), "switch2": dict(_switch_state["switch2"])}
        updated_at = _switch_state["updatedAt"]

    nodes = {}
    with _zenoh_lock:
        for node_id, meta in NODES.items():
            last_seen = _zenoh_last_seen.get(node_id)
            recent = list(_zenoh_recent.get(node_id, []))
            phy_link = None
            if meta["switch"] and meta["port"]:
                sw = switches.get(meta["switch"], {})
                port_info = sw.get("ports", {}).get(meta["port"], {})
                phy_link = port_info.get("link")
            nodes[node_id] = {
                "ip": meta["ip"],
                "phyLink": phy_link,
                "zenohAlive": bool(last_seen and (now - last_seen) < ZENOH_ALIVE_WINDOW_S),
                "lastSeenAgo": round(now - last_seen, 1) if last_seen else None,
                "recent": [{"key": r["key"], "val": r["val"], "ago": round(now - r["t"], 1)} for r in recent],
            }

    return {
        "updatedAt": updated_at,
        "switches": switches,
        "nodes": nodes,
    }


class PortShutdown(BaseModel):
    switch: str
    port: str
    shutdown: bool


@app.post("/api/port/shutdown")
def set_port_shutdown(body: PortShutdown):
    base = SWITCH1 if body.switch == "switch1" else SWITCH2
    cfg = rpc(base, "port.config.get", [body.port])
    cfg["Shutdown"] = body.shutdown
    rpc(base, "port.config.set", [body.port, cfg])
    return {"ok": True}


class StpEnable(BaseModel):
    switch: str
    port: str
    enable: bool


@app.post("/api/stp/enable")
def set_stp_enable(body: StpEnable):
    base = SWITCH1 if body.switch == "switch1" else SWITCH2
    cfg = rpc(base, "mstp.config.cist.interface.get", [body.port])
    cfg["Enable"] = body.enable
    rpc(base, "mstp.config.cist.interface.set", [body.port, cfg])
    return {"ok": True}


class FrerActive(BaseModel):
    switch: str
    active: bool


@app.post("/api/frer/active")
def set_frer_active(body: FrerActive):
    base = SWITCH1 if body.switch == "switch1" else SWITCH2
    cfg = rpc(base, "frer.config.get", [FRER_INST])
    cfg["AdminActive"] = body.active
    rpc(base, "frer.config.set", [FRER_INST, cfg])
    return {"ok": True}


@app.post("/api/frer/reapply")
def frer_reapply():
    script = Path(__file__).resolve().parents[1] / "frer_setup.py"
    proc = subprocess.run(["python3", str(script)], capture_output=True, text=True, timeout=60)
    return {"ok": proc.returncode == 0, "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-4000:]}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
