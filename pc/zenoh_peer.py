# PC/HPC-side Zenoh peer for the ESP32 bridge: relays bridge/** samples,
# publishes its own bridge/pc heartbeat, and answers the RTT probe (see
# firmware/esp32_bridge/zenoh_bridge.cpp) by echoing test/ping/<node> back
# on test/pong/<node> unchanged. `pip install eclipse-zenoh` (this was
# tested against 1.10.1). Point it at whatever interface sits on the same
# segment as the ESP32 boards -- 192.168.100.50/enp4s0 on the Kontron D10
# bench this was built against.
import time
import zenoh

conf = zenoh.Config()
# A dedicated `zenohd` router now owns udp/192.168.100.50:7447 (the ESP32
# boards' PC_LOCATOR) -- this script, dds_adapter.py and zenoh-bridge-dds
# all connect to it as clients. Running this script itself in "router" or
# "peer" mode *listening* on that address only relays traffic it
# originates, not traffic between two OTHER sessions attached to it (the
# ESP32 boards vs. this process) -- that's a router's actual job, so it
# gets its own process instead of being folded into whichever script
# happened to be listening first.
conf.insert_json5("mode", '"client"')
conf.insert_json5("connect/endpoints", '["udp/192.168.100.50:7447"]')


def on_sample(sample):
    print(f">> [{sample.key_expr}] {sample.payload.to_string()}", flush=True)


def make_echo(session):
    # RTT probe: bounce test/ping/<node> straight back on test/pong/<node>,
    # payload untouched (it carries the ESP's own send timestamp -- see
    # firmware/esp32_bridge/zenoh_bridge.cpp pongHandler). Stateless on
    # purpose: no per-node table to keep in sync here.
    def on_ping(sample):
        node = str(sample.key_expr).rsplit("/", 1)[-1]
        session.put(f"test/pong/{node}", sample.payload)

    return on_ping


with zenoh.open(conf) as session:
    sub = session.declare_subscriber("bridge/**", on_sample)
    stats_sub = session.declare_subscriber("test/stats/**", on_sample)
    ping_sub = session.declare_subscriber("test/ping/**", make_echo(session))
    domain_sub = session.declare_subscriber("ivn/**", on_sample)  # chassis/body/diag/cabin
    pub = session.declare_publisher("bridge/pc")
    print("zenoh peer up on udp/192.168.100.50:7447, sub=bridge/**,test/**,ivn/** pub=bridge/pc", flush=True)
    i = 0
    while True:
        pub.put(f"[pc {i}] hello from enp4s0")
        i += 1
        time.sleep(2)
