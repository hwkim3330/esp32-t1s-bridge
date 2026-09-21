# Congestion before/after: floods one ESP32's port with UDP traffic to show
# what contention does to the RTT probe's latency/jitter -- the problem QoS
# on the D10 exists to solve. (qos_before_after.py in this same directory
# attempted the *protective* half -- applying a queueShaper to hold the RTT
# probe steady under this same flood -- but tuning the switch's
# qos.config.interface.queueShaper/scheduler far enough to see a clean
# effect via JSON-RPC didn't land in the time available; this script is the
# honest, verified half.)
import socket
import threading
import time

import zenoh

TARGET_IP = "192.168.100.60"  # esp32-1
FLOOD_PORT = 9999  # nothing listens here; the point is link/switch load, not app-level delivery
FLOOD_DURATION_S = 10


def flood():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = b"x" * 1400
    end = time.time() + FLOOD_DURATION_S
    sent = 0
    while time.time() < end:
        sock.sendto(payload, (TARGET_IP, FLOOD_PORT))
        sent += 1
    print(f"flood: sent {sent} x 1400B packets in {FLOOD_DURATION_S}s "
          f"(~{sent * 1400 * 8 / FLOOD_DURATION_S / 1e6:.1f} Mbit/s offered)", flush=True)


def collect_stats(duration_s, label):
    latest = {}

    def on_sample(sample):
        text = sample.payload.to_string()
        parts = dict(p.split("=") for p in text.split())
        node = str(sample.key_expr).rsplit("/", 1)[-1]
        latest[node] = {"avg_ms": float(parts["avg"].rstrip("ms")), "jitter_ms": float(parts["jitter"].rstrip("ms"))}

    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", '["udp/192.168.100.50:7447"]')
    with zenoh.open(conf) as session:
        session.declare_subscriber("test/stats/**", on_sample)
        time.sleep(duration_s)
    print(f"[{label}] {latest}", flush=True)
    return latest


print("=== baseline (no flood) ===", flush=True)
before = collect_stats(8, "before")

print(f"=== flooding {TARGET_IP}:{FLOOD_PORT} for {FLOOD_DURATION_S}s (no QoS protection) ===", flush=True)
t = threading.Thread(target=flood)
t.start()
during = collect_stats(FLOOD_DURATION_S, "during")
t.join()

print("=== recovery (flood stopped) ===", flush=True)
time.sleep(1)
after = collect_stats(8, "after")

print("\n=== summary (esp32-1 is the flooded node; esp32-2 is the control) ===")
for node in sorted(set(before) | set(during) | set(after)):
    b, d, a = before.get(node, {}), during.get(node, {}), after.get(node, {})
    print(f"{node}: before avg={b.get('avg_ms')}ms jitter={b.get('jitter_ms')}ms | "
          f"during avg={d.get('avg_ms')}ms jitter={d.get('jitter_ms')}ms | "
          f"after avg={a.get('avg_ms')}ms jitter={a.get('jitter_ms')}ms")
