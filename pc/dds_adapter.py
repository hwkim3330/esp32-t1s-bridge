# Zenoh<->DDS adapter: re-publishes this project's plain-text zenoh keys
# (bridge/**, test/stats/**, ivn/**) as CDR-encoded std_msgs/String under
# "rt/<same path>", which is the naming zenoh-bridge-dds
# (https://github.com/eclipse-zenoh/zenoh-plugin-dds) uses for a DDS topic
# "/<same path>" of type std_msgs::msg::String. Run zenoh-bridge-dds
# alongside this (e.g. `zenoh-bridge-dds -e udp/192.168.100.50:7447`), then
# any DDS participant (`ros2 topic echo /ivn/chassis/wheel_speed`, a Fast
# DDS subscriber, rosbag record, ...) sees the ESP32 boards' data as native
# DDS -- the bridge only creates the route once something on the DDS side
# is actually listening, so nothing shows up in `ros2 topic list` until a
# reader does.
#
# The reverse (DDS -> our plain zenoh keys) isn't done here: this project's
# own consumers (this script, the dashboard) read the plain keys directly,
# there's no need to round-trip them through DDS first.
#
# Payloads stay plain std_msgs/String (task-value text, same as the raw
# zenoh payload) rather than typed messages -- proving the bridge path
# works end-to-end matters more here than a finished message schema.
import struct
import time

import zenoh


def cdr_string_msg(s: str) -> bytes:
    header = b"\x00\x01\x00\x00"  # CDR_LE encapsulation, no options
    data = s.encode("utf-8") + b"\x00"
    length = struct.pack("<I", len(data))
    payload = header + length + data
    pad = (-len(payload)) % 4
    return payload + b"\x00" * pad


conf = zenoh.Config()
conf.insert_json5("mode", '"client"')  # connects to the zenohd router, not peer-to-peer
conf.insert_json5("connect/endpoints", '["udp/192.168.100.50:7447"]')

with zenoh.open(conf) as session:
    dds_pubs = {}  # source key -> Publisher on "rt/" + key, created lazily

    def on_sample(sample):
        key = str(sample.key_expr)
        if key not in dds_pubs:
            dds_pubs[key] = session.declare_publisher(f"rt/{key}")
            print(f"bridging {key} -> rt/{key} (std_msgs/String)", flush=True)
        dds_pubs[key].put(cdr_string_msg(sample.payload.to_string()))

    sub_bridge = session.declare_subscriber("bridge/**", on_sample)
    sub_stats = session.declare_subscriber("test/stats/**", on_sample)
    sub_ivn = session.declare_subscriber("ivn/**", on_sample)
    print("dds_adapter up: bridge/**,test/stats/**,ivn/** -> rt/<key> (std_msgs/String)", flush=True)

    while True:
        time.sleep(1)
