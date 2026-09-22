"""Bidirectional FRER (802.1CB) between the two Kontron D10s, done properly.

Protects esp32-1 <-> PC in both directions, plus PC -> esp32-2, over the two
D10s' parallel links (Gi1/1 + Gi1/2), with spanning tree off on both ring
ports -- FRER alone provides the redundancy, which is the point: there is no
detection delay and no reconvergence, so a cut costs zero frames.

Everything here was learned the hard way against the real rig. The six
things that actually matter, none of which are obvious from the field
names alone:

1. The per-port VCL "Matching" mode (vcl.config.interface.vcl_cfg) decides
   what the hardware TCAM key contains. On `dmac_dip` -- the mode every
   port here needs -- the key holds the DESTINATION MAC and nothing about
   the source. A stream that asks to match source AND destination can
   therefore never fire on such a port: the source simply isn't in the
   lookup key. This silently cost most of a day; the symptom is a
   perfectly clean config (frerClientAttached true, zero warnings) with
   every FRER counter pinned at zero. Generation streams below match on
   destination MAC only, for exactly this reason.

2. ...but a destination-MAC-only stream on the RECOVERY side is dangerous:
   the recovery classifier sits on the ring ports, where the far switch's
   own management traffic to the PC also arrives, carrying the same
   destination MAC. Matched, it gets pulled into the FRER pipeline and
   eaten -- which cut switch2 off the network entirely the one time it was
   tried. The recovery streams therefore match destination MAC AND the
   FRER VLAN tag. Management traffic is untagged, so it can't collide.

3. For (2) to hold, the FRER frames must actually arrive tagged -- on BOTH
   paths. They don't by default: a ring port whose native VLAN happens to
   be the FRER VLAN egresses those frames untagged, so one of the two
   copies silently fails the recovery match while the other passes. Hence
   HybridEgressTagging=tagAll on the ring ports.

4. The mirror of (3) at the edges: the PC's NIC and the boards have no
   VLAN 200 sub-interface, so anything still tagged when it reaches them
   is dropped by the host, not the switch. Recovery's Terminate strips the
   R-tag but not the VLAN tag -- hence untagAll on every end-host port.

5. Replication itself is the chip's own, via FLOODING inside the FRER VLAN
   -- there is no separate "fan out to EgressPorts" path. An earlier
   attempt to fan out with multi-port static FDB entries instead does
   deliver two copies, but it cannot work bidirectionally: the entry that
   fans direction A out (dst=PC -> both ring ports on switch2) is the same
   MAC that direction B's frames arrive FROM on those ports, and the
   switch then drops them. Both directions broke each other, symmetrically,
   until the static entries were removed entirely.

   Flooding is also why an early attempt looked like a storm: back then
   classification was broken (see 1), so nothing was ever recovered and
   frames just circulated. With recovery actually consuming them
   (Terminate strips the R-tag and forwards to one egress port), a flooded
   frame is absorbed at the far switch instead of being passed on.

   MAC learning is disabled in the FRER VLAN on purpose: if the far side
   learns where a MAC lives, the next frame is unicast to one port and the
   fan-out silently degrades to a single path -- which the reverse
   direction's own traffic would otherwise teach it almost immediately.

6. One recovery instance can only follow ONE sequence counter. Pointing
   several generation instances at a single recovery instance interleaves
   their independent R-tag sequences, and the recovery engine reports the
   gaps as Lost -- 765 of them in one run, while ping showed 0% loss. The
   same shape appears in miniature if HistoryLen is too tight for two paths
   with different delays: at 8 it invented losses, at 32 (with
   ResetTimeoutMsec 1000) Lost stays flat at 0. So: one source per FRER
   VLAN, HistoryLen 32.

Run this with Gi1/2 unplugged if you want zero risk while it applies;
nothing here needs the second path to be live in order to be configured.
"""
import json
import sys
import urllib.request

SWITCH1 = "http://192.168.100.1/json_rpc"  # PC side
SWITCH2 = "http://192.168.100.2/json_rpc"  # ESP32 side

FRER_VLAN = 200
RING_PORTS = ["Gi 1/1", "Gi 1/2"]

PC_MAC = "d4:5d:64:b2:5d:c3"
PC_PORT_SW1 = "Gi 1/6"                     # PC hangs off switch1 here

EDGE_MAC = "a6:cb:8f:e7:f0:bd"             # esp32-1
EDGE_PORT_SW2 = "Gi 1/3"                   # verified against mac.status.fdb.full.get,
# NOT guessed from RX-counter deltas -- an early guess of the wrong port here is
# what made direction A look broken for hours.

# Separate stream ids per role, because generation and recovery need
# genuinely different match criteria (see notes 1 and 2 above). High ids to
# stay clear of streams 1-6, which belong to an unrelated older project
# still configured on these switches.
STREAM_A_GEN, STREAM_A_REC = 10, 11        # direction A: edge -> PC
STREAM_B_GEN, STREAM_B_REC = 12, 13        # direction B: PC -> edge
INST_A, INST_B = 1, 2

# A second protected flow, PC -> esp32-2. Note that only the PC -> board
# direction can be added this way: the board -> PC direction would have to
# reuse the generation instance that already matches dst=PC, and two sources
# feeding one recovery instance interleave two independent R-tag sequences,
# which shows up as a large and entirely fake Lost count (note 6). Protecting
# esp32-2 -> PC as well needs its own FRER VLAN.
EDGE2_MAC = "2a:84:85:80:9b:85"            # esp32-2
EDGE2_PORT_SW2 = "Gi 1/6"
STREAM_C_GEN, STREAM_C_REC = 14, 15
INST_C = 10

_openers = {}


def _opener(url):
    if url not in _openers:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, "admin", "")
        _openers[url] = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
    return _openers[url]


def rpc(url, method, params=None, quiet=False):
    body = json.dumps({"method": method, "params": params if params is not None else [], "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with _opener(url).open(req, timeout=6) as r:
        resp = json.loads(r.read())
    if resp.get("error"):
        if quiet:
            return None
        raise RuntimeError(f"{method} @ {url} -> {resp['error']}")
    return resp["result"]


def stream_conf(dmac, vlan=None):
    """Destination-MAC match, optionally also requiring the FRER VLAN tag.

    Source MAC is deliberately left as don't-care: on a dmac_dip port it is
    not part of the TCAM key at all, and requiring it means the rule never
    matches (note 1).
    """
    return {
        "MulticastDMac": "any", "BroadcastDMac": "any",
        "destinationMacAddress": dmac, "destinationMacMask": "ff:ff:ff:ff:ff:ff",
        "sourceMacAddress": "00:00:00:00:00:00", "sourceMacMask": "00:00:00:00:00:00",
        "outerTag": "one" if vlan else "any", "outerTagIsSTag": "any",
        "outerTagVidValue": vlan or 0, "outerTagVidMask": 4095 if vlan else 0,
        "outerTagPcpValue": 0, "outerTagPcpMask": 0, "outerTagDei": "any",
        "innerTag": "any", "innerTagIsSTag": "any", "innerTagVidValue": 0, "innerTagVidMask": 0,
        "innerTagPcpValue": 0, "innerTagPcpMask": 0, "innerTagDei": "any",
        "protocol": "ANY",  # upper-case; "any" is rejected
    }


def frer_conf(mode, stream_id, egress_ports):
    conf = {
        "Mode": mode,                       # lower-case per /json_spec
        "FrerVlan": FRER_VLAN, "EgressPorts": egress_ports,
        # HistoryLen 8 is too tight for two paths whose copies interleave: the
        # vector algorithm then reports sequence gaps as Lost even though no
        # frame was actually lost (ping stayed at 0% while Lost climbed).
        # 32 (the maximum) makes Lost read 0, matching reality.
        "Algorithm": "vector", "HistoryLen": 32, "ResetTimeoutMsec": 1000,
        "TakeNoSequence": False, "IndividualRecovery": False,
        # Terminate strips the R-tag on the way out -- right for recovery
        # (the end system has no idea what an R-tag is), wrong for
        # generation, whose whole job is to add one.
        "Terminate": mode == "recovery",
        "LaErrDetection": False, "LaErrDifference": 100, "LaErrPeriodMsec": 2000,
        "LaErrPaths": 2, "LaErrResetPeriodMsec": 30000,
        "AdminActive": True,
    }
    for i in range(8):
        conf[f"StreamId{i}"] = stream_id if i == 0 else 0   # unused slots are 0, not -1
    return conf


def put_stream(url, sid, conf):
    rpc(url, "vcl.config.stream.add", [sid, conf], quiet=True)
    rpc(url, "vcl.config.stream.set", [sid, conf])


def attach_stream(url, port, sid):
    # Re-attach rather than assume: a classifier attached BEFORE the port's
    # matching mode was corrected stays programmed with the old key.
    rpc(url, "vcl.config.interface.stream.del", [port, sid], quiet=True)
    rpc(url, "vcl.config.interface.stream.add", [port, sid])


def put_frer(url, inst, conf):
    rpc(url, "frer.config.add", [inst, conf], quiet=True)
    rpc(url, "frer.config.set", [inst, conf])


def set_matching(url, port, mode="dmac_dip"):
    cur = rpc(url, "vcl.config.interface.vcl_cfg.get", [port])
    if cur["Matching"] != mode:
        rpc(url, "vcl.config.interface.vcl_cfg.set", [port, {"Matching": mode}])


def set_vlan(url, port, add_vlans, egress_tagging, native=None):
    cfg = rpc(url, "vlan.config.interface.get", [port])
    cfg["Mode"] = "hybrid"
    cfg["HybridVlans"] = sorted(set(cfg["HybridVlans"]) | set(add_vlans))
    cfg["HybridEgressTagging"] = egress_tagging
    if native is not None:
        cfg["HybridNativeVlan"] = native
    rpc(url, "vlan.config.interface.set", [port, cfg])


def disable_stp(url, port):
    cfg = rpc(url, "mstp.config.cist.interface.get", [port])
    if cfg["Enable"]:
        cfg["Enable"] = False
        rpc(url, "mstp.config.cist.interface.set", [port, cfg])



# ---------------------------------------------------------------------------
def main():
    print("--- ring ports: VLAN 200 tagged, dmac_dip, STP off ---")
    for sw in (SWITCH1, SWITCH2):
        for p in RING_PORTS:
            set_matching(sw, p)
            set_vlan(sw, p, [FRER_VLAN], "tagAll", native=1)   # note 3; native 1 keeps
            # untagged loop-protection probes out of the FRER VLAN, where they would
            # otherwise flood across both links and trip loop protection.
            disable_stp(sw, p)

    print("--- edge/host ports: VLAN 200 member, dmac_dip, untagged egress ---")
    set_matching(SWITCH1, PC_PORT_SW1)
    set_vlan(SWITCH1, PC_PORT_SW1, [1, FRER_VLAN], "untagAll")   # note 4
    set_matching(SWITCH2, EDGE_PORT_SW2)
    set_vlan(SWITCH2, EDGE_PORT_SW2, [1, FRER_VLAN], "untagAll")

    print(f"--- VLAN {FRER_VLAN}: flooding ON (it IS the fan-out), learning OFF (note 5) ---")
    for sw in (SWITCH1, SWITCH2):
        rpc(sw, "vlan.config.global.flooding.set", [FRER_VLAN, True])
        rpc(sw, "mac.config.vlan.learn.set", [FRER_VLAN, {"Mode": False}])

    print(f"\n--- direction A: {EDGE_PORT_SW2} (esp32-1) -> PC ---")
    put_stream(SWITCH2, STREAM_A_GEN, stream_conf(PC_MAC))                    # gen: DMAC only
    attach_stream(SWITCH2, EDGE_PORT_SW2, STREAM_A_GEN)
    put_frer(SWITCH2, INST_A, frer_conf("generation", STREAM_A_GEN, RING_PORTS))

    put_stream(SWITCH1, STREAM_A_REC, stream_conf(PC_MAC, FRER_VLAN))         # rec: DMAC + VLAN
    for p in RING_PORTS:
        attach_stream(SWITCH1, p, STREAM_A_REC)
    put_frer(SWITCH1, INST_A, frer_conf("recovery", STREAM_A_REC, [PC_PORT_SW1]))

    print(f"--- direction B: PC -> {EDGE_PORT_SW2} (esp32-1) ---")
    put_stream(SWITCH1, STREAM_B_GEN, stream_conf(EDGE_MAC))
    attach_stream(SWITCH1, PC_PORT_SW1, STREAM_B_GEN)
    put_frer(SWITCH1, INST_B, frer_conf("generation", STREAM_B_GEN, RING_PORTS))

    put_stream(SWITCH2, STREAM_B_REC, stream_conf(EDGE_MAC, FRER_VLAN))
    for p in RING_PORTS:
        attach_stream(SWITCH2, p, STREAM_B_REC)
    put_frer(SWITCH2, INST_B, frer_conf("recovery", STREAM_B_REC, [EDGE_PORT_SW2]))

    print(f"--- direction C: PC -> {EDGE2_PORT_SW2} (esp32-2) ---")
    set_matching(SWITCH2, EDGE2_PORT_SW2)
    set_vlan(SWITCH2, EDGE2_PORT_SW2, [1, FRER_VLAN], "untagAll")
    put_stream(SWITCH1, STREAM_C_GEN, stream_conf(EDGE2_MAC))
    attach_stream(SWITCH1, PC_PORT_SW1, STREAM_C_GEN)
    put_frer(SWITCH1, INST_C, frer_conf("generation", STREAM_C_GEN, RING_PORTS))

    put_stream(SWITCH2, STREAM_C_REC, stream_conf(EDGE2_MAC, FRER_VLAN))
    for p in RING_PORTS:
        attach_stream(SWITCH2, p, STREAM_C_REC)
    put_frer(SWITCH2, INST_C, frer_conf("recovery", STREAM_C_REC, [EDGE2_PORT_SW2]))

    # Single-port delivery entries on each recovery switch (note 5). Learning is
    # off in this VLAN, so without these the recovery switch does not know where
    # the destination lives and floods the recovered frame straight back across
    # the ring -- which is a genuine broadcast storm, ~3M pkt/s/port, not a
    # theoretical one. One port each: a multi-port entry here breaks the
    # opposite direction, whose frames arrive *from* those same ring ports.
    print("--- static delivery entries (note 5) ---")
    rpc(SWITCH1, "mac.config.fdb.static.add", [FRER_VLAN, PC_MAC, {"PortList": [PC_PORT_SW1]}])
    rpc(SWITCH2, "mac.config.fdb.static.add", [FRER_VLAN, EDGE_MAC, {"PortList": [EDGE_PORT_SW2]}])
    rpc(SWITCH2, "mac.config.fdb.static.add", [FRER_VLAN, EDGE2_MAC, {"PortList": [EDGE2_PORT_SW2]}])

    print("\n--- status ---")
    for sw, name in ((SWITCH1, "switch1"), (SWITCH2, "switch2")):
        for inst, label in ((INST_A, "dirA"), (INST_B, "dirB"), (INST_C, "dirC")):
            st = rpc(sw, "frer.status.get", [inst])
            warns = [k for k, v in st.items() if k.startswith("Warning") and k != "WarningNone" and v]
            print(f"  {name} inst{inst} ({label}): {st['OperState']}, {warns or 'no warnings'}")

    print("\nSave to startup when happy:")
    print("  icfg.control.copy.set runningConfig -> startupConfig")



if __name__ == "__main__":
    main()
