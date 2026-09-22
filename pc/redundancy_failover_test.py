# Safe redundancy/failover test: switch2 (192.168.100.2) <-> switch1
# (192.168.100.1) now has two parallel 1G links (Gi1/1, Gi1/2). MSTP
# already elected Gi1/1 as active (forwarding) and Gi1/2 as standby
# (discarding, AlternatePort) on switch2's side -- a real, already-safe
# form of path redundancy, just not zero-loss (STP has to reconverge).
#
# This admin-shuts the currently active link via port.config.set (remotely
# reversible, no cable pulling needed) and watches test/stats/** through
# the cutover to see how long the gap is and whether the session recovers
# on its own. FRER (zero-loss, no reconvergence wait) is the fancier
# version of this -- deliberately not attempted here yet, since it needs
# disabling STP on these ports, and this is a live segment: see
# keti-reconfig/docs/D10_SWITCH_REFERENCE.md's storm-risk warning before
# doing that blind.
import json
import time
import urllib.request

import zenoh

SWITCH2 = "http://192.168.100.2/json_rpc"
ACTIVE_PORT = "Gi 1/1"  # switch2's current RootPort/forwarding link (Gi1/2 is the
                        # already-blocked AlternatePort/standby per mstp.status.interface.get)
AUTH = urllib.request.HTTPPasswordMgrWithDefaultRealm()
AUTH.add_password(None, SWITCH2, "admin", "")
opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(AUTH))


def rpc(url, method, params=None):
    body = json.dumps({"method": method, "params": params or [], "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=5) as r:
        return json.loads(r.read())


def set_port_shutdown(port, shutdown):
    resp = rpc(SWITCH2, "port.config.get", [port])
    cfg = resp["result"]
    cfg["Shutdown"] = shutdown
    resp = rpc(SWITCH2, "port.config.set", [port, cfg])
    if resp.get("error"):
        raise RuntimeError(f"port.config.set({port}, Shutdown={shutdown}) failed: {resp['error']}")


def watch_stats(duration_s, label):
    events = []
    t0 = time.time()

    def on_sample(sample):
        node = str(sample.key_expr).rsplit("/", 1)[-1]
        events.append((round(time.time() - t0, 2), node, sample.payload.to_string()))

    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", '["udp/192.168.100.50:7447"]')
    with zenoh.open(conf) as session:
        session.declare_subscriber("test/stats/**", on_sample)
        session.declare_subscriber("bridge/**", on_sample)
        time.sleep(duration_s)
    print(f"--- {label} ({len(events)} samples in {duration_s}s) ---")
    for t, node, val in events:
        print(f"  t+{t:5.2f}s  {node:10s} {val}")
    return events


print("=== baseline (both links up, STP has one active/one standby) ===")
watch_stats(6, "before")

print(f"\n=== admin-shutdown {ACTIVE_PORT} on switch2 (forces STP failover to the other link) ===")
set_port_shutdown(ACTIVE_PORT, True)
events = watch_stats(15, "during failover")

print(f"\n=== restoring {ACTIVE_PORT} ===")
set_port_shutdown(ACTIVE_PORT, False)
watch_stats(8, "after restore")

# Gap = longest silence in the middle of the failover window (a real "how
# long did the session actually go dark for" number, not just "did it come
# back eventually").
if len(events) >= 2:
    gaps = [events[i + 1][0] - events[i][0] for i in range(len(events) - 1)]
    print(f"\nlongest single gap between samples during failover: {max(gaps):.2f}s")
else:
    print("\ntoo few samples during failover to compute a gap (link may not have recovered in time)")
