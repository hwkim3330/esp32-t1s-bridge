import json
import time
import urllib.request

import zenoh

D10 = "http://192.168.100.1/json_rpc"
AUTH = urllib.request.HTTPPasswordMgrWithDefaultRealm()
AUTH.add_password(None, D10, "admin", "")
opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(AUTH))

PORTS = ["Gi 1/4", "Gi 1/5"]
QUEUES = list(range(8))
LOW_CIR_KBPS = 100  # the switch's minimum shaper granularity (8 kbps: "QOS parameter error")


def rpc(method, params=None):
    body = json.dumps({"method": method, "params": params or [], "id": 1}).encode()
    req = urllib.request.Request(D10, data=body, headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=5) as r:
        return json.loads(r.read())


def set_shaper(enable, cir=LOW_CIR_KBPS):
    for port in PORTS:
        for q in QUEUES:
            # params are 3 flat args (port, queue, value) -- a [[port, queue], value]
            # shape looks natural but the switch's JSON-RPC rejects it as
            # "argument-cnt-expect 3, actual 2" (learned the hard way, with no
            # revert needed since the original all-false state was never touched).
            resp = rpc(
                "qos.config.interface.queueShaper.set",
                [port, q, {"Enable": enable, "Excess": False, "Cir": cir, "RateType": "line", "Credit": False}],
            )
            if resp.get("error"):
                raise RuntimeError(f"queueShaper.set({port}, {q}) failed: {resp['error']}")


def collect_stats(duration_s, label):
    latest = {}

    def on_sample(sample):
        key = str(sample.key_expr)
        node = key.rsplit("/", 1)[-1]
        text = sample.payload.to_string()
        # "n=NN avg=X.XXms min=... max=... jitter=Y.YYms"
        parts = dict(p.split("=") for p in text.split())
        latest[node] = {"avg_ms": float(parts["avg"].rstrip("ms")), "jitter_ms": float(parts["jitter"].rstrip("ms"))}

    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", '["udp/192.168.100.50:7447"]')
    with zenoh.open(conf) as session:
        sub = session.declare_subscriber("test/stats/**", on_sample)
        time.sleep(duration_s)
    print(f"[{label}] {latest}", flush=True)
    return latest


print("=== baseline (shaper off) ===", flush=True)
before = collect_stats(8, "before")

print(f"=== enabling queueShaper Cir={LOW_CIR_KBPS}kbps on Gi1/4, Gi1/5, all queues ===", flush=True)
set_shaper(True)
time.sleep(2)  # let a few RTT samples land under the new condition
during = collect_stats(12, "during")

print("=== reverting (shaper off) ===", flush=True)
set_shaper(False)
time.sleep(2)
after = collect_stats(8, "after")

print("\n=== summary ===")
for node in sorted(set(before) | set(during) | set(after)):
    b = before.get(node, {})
    d = during.get(node, {})
    a = after.get(node, {})
    print(f"{node}: before avg={b.get('avg_ms')}ms jitter={b.get('jitter_ms')}ms | "
          f"during avg={d.get('avg_ms')}ms jitter={d.get('jitter_ms')}ms | "
          f"after avg={a.get('avg_ms')}ms jitter={a.get('jitter_ms')}ms")
