# ESP32 T1S Bridge

An Edge/HPC testbed for KETI's IVN 3세부 과제 (SDV network architecture:
Multi-GigE backbone + 10Mbps lightweight edge Ethernet, DDS, QoS/TSN,
virtual domains). ESP32-S3 boards stand in for virtual ECUs on the edge
side, talking [Zenoh](https://zenoh.io/) over Ethernet (W5500 now,
10BASE-T1S planned) through two Kontron D10 switches (dual-linked, for
path redundancy) to a PC/HPC running the Zenoh↔DDS bridge and Fast DDS.
Split off from
[`aurix-tc4d7`](https://github.com/hwkim3330/aurix-tc4d7), whose AURIX side
is separately blocked on an immature OpenOCD dev build.

Task alignment, roughly:

| Task needs | This repo |
|---|---|
| 10Mbps edge Ethernet | RJ45/W5500 now → 10BASE-T1S later |
| Pub/sub middleware | zenoh-pico on ESP32 |
| DDS (mandatory per task doc) | done: `zenoh-bridge-dds` + `pc/dds_adapter.py` in front of Fast DDS (ROS2 Jazzy) — see below |
| Edge→HPC delay/jitter test | `pc/zenoh_peer.py` RTT echo + firmware ping/pong — see below |
| Virtual domains (target 8) | 5 done (chassis/body/cabin/diag×2), 3 more to go |
| QoS/TSN before/after on D10 | control channel works, no clean delta yet — see below |
| Multiplexing (target 2) | done, STP-level: 2 D10s dual-linked, automatic failover verified — see below |

## What works now

- `firmware/esp32_bridge/`: ESP32-S3 + W5500 (SPI Ethernet), zenoh-pico as
  a Zenoh client. Two boards run the same firmware (`NODE_ID` build flag
  picks IP `.60`/`.61` and keyexpr `bridge/esp32-1`/`bridge/esp32-2`),
  verified together on a Kontron D10 switch: both link at 100 Mbit
  full-duplex and exchange pub/sub through a `zenohd` router on the PC.
  A router, not a peer: two boards each only connected to the PC (not to
  each other) need something that explicitly forwards between them, which
  is a router's actual job — an earlier version tried folding that role
  into whichever script happened to be listening first, and a third
  session (the DDS adapter, below) never saw the ESP32s' samples because
  of it.
- **DDS bridge**, real and verified end-to-end: `pc/dds_adapter.py`
  CDR-encodes each `bridge/**`/`test/stats/**`/`ivn/**` sample as a
  `std_msgs/String` and republishes it on `rt/<key>`, the naming
  [`zenoh-bridge-dds`](https://github.com/eclipse-zenoh/zenoh-plugin-dds)
  uses for DDS topic `/<key>`. Confirmed with a plain
  `ros2 topic echo /ivn/chassis/wheel_speed std_msgs/msg/String` printing
  the ESP32's live sine-sweep values, over Fast DDS (`ros-jazzy-fastrtps`,
  no ROS2-specific code involved on the bridge side). `scripts/06-fetch-dds-tools.sh`
  gets the two binaries (`zenohd`, `zenoh-bridge-dds`), pinned to 1.10.1 to
  match the `eclipse-zenoh` Python version these scripts use.
- **RTT/jitter probe**, live on both boards: each publishes
  `test/ping/esp32-N` at 5 Hz with its own send timestamp; `pc/zenoh_peer.py`
  echoes it back unchanged on `test/pong/esp32-N`; the board computes
  round-trip time and reports `test/stats/esp32-N` at 1 Hz
  (`n=.. avg=..ms min=.. max=.. jitter=..ms`, jitter = mean absolute
  difference between consecutive RTTs). Measured on the bench: **~3.3 ms
  RTT, ~0.1-0.4 ms jitter** — well inside the task's ≤5 ms delay / ≤2.5 ms
  jitter targets (those are one-way Edge→HPC figures; this is round-trip,
  so treat it as a comfortable margin, not the calibrated number).
- The W5500 bring-up (`eth_w5500.h` / `w5500_spi.h`) is carried over from
  `esp32-lidar/firmware/lidar_probe`, not reinvented: raw `esp_eth` +
  `esp_netif` instead of Arduino's `ETH.begin()` (which hardcodes a 10 ms
  MAC poll period with no way to change it), plus a fix for a real bug in
  the IDF W5500 driver at the 16 kB RX buffer wrap. Pin mapping
  (SCK=48, MISO=47, MOSI=21, CS=45, no INT/RST) is that project's measured
  pinout, not a datasheet default.
- zenoh-pico is vendored under `vendor/zenoh-pico` with three local patches
  (`link.c`, `endpoint.c`, `transport/common/tx.c`,
  `transport/multicast/transport.c`): raw-ethernet transport symbols were
  referenced unconditionally instead of behind
  `#if Z_FEATURE_RAWETH_TRANSPORT == 1` like every other transport, which
  broke the link step for the `arduino_esp32` platform (RAWETH is
  unsupported there) with the feature at its own default of 0.
- `pc/zenoh_peer.py`: the PC-side peer (`pip install eclipse-zenoh`). Relays
  `bridge/**`, answers the RTT probe, publishes a `bridge/pc` heartbeat.
- **5 virtual domains live**: chassis (`ivn/chassis/wheel_speed`,
  `vehicle_speed` — synthetic sine sweep, NODE_ID=1), body
  (`ivn/body/control`, a lock/unlock event, NODE_ID=2), diag ×2
  (`ivn/diag/esp32-N`), and **cabin** (`ivn/cabin/csi`, `ivn/cabin/presence`
  — see `firmware/esp32_bridge/csi_link.h`): NODE_ID=1 runs a small closed
  WiFi AP just for this, NODE_ID=2 joins it as a station and reads real
  WiFi Channel State Information off the frames between them. No extra
  sensor hardware — the two boards already on the bench are the sensor.
  Verified stable for 30+s with both radios (WiFi + the W5500/Ethernet
  session) running at once: RTT jitter moved from ~0.1-0.3ms to
  ~0.3-0.5ms but stayed well inside the 2.5ms target, no crashes, no heap
  drift. The presence threshold is an unvalidated guess and currently
  saturates "present" at bench range (RSSI/variance never drop to what an
  empty room looks like when the boards are inches apart) — the point
  proven here is the RF sensing *path*, not sensor accuracy.

## Build

```bash
# One-time: assemble zenoh-pico into an Arduino library
cmake -S vendor/zenoh-pico -B build/zenoh-arduino-config \
    -DZP_PLATFORM=arduino_esp32 -DZ_FEATURE_LINK_UDP_MULTICAST=1
scripts/50-build-zenoh-arduino-lib.sh

# Compile + flash (ESP32-S3, native USB CDC)
FQBN="esp32:esp32:esp32s3:PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc"
arduino-cli compile --fqbn "$FQBN" \
    --library firmware/esp32_bridge/lib/zenoh-pico \
    --build-property "compiler.c.extra_flags=-DZENOH_ARDUINO_ESP32" \
    --build-property "compiler.cpp.extra_flags=-DZENOH_ARDUINO_ESP32" \
    firmware/esp32_bridge
arduino-cli upload -p /dev/ttyACM0 --fqbn "$FQBN" firmware/esp32_bridge
```

`CDCOnBoot=cdc` is not optional: without it the board boots and runs fine,
it just never emits a byte over USB serial — no crash, no error, just
silence. Cost an afternoon to find once already.

The static IP / peer address in `zenoh_bridge.cpp` (`kLocalIP`,
`PC_LOCATOR`) matches whatever segment the board is currently plugged
into — check those before flashing onto a different network.

## Multiplexing — 2 D10s, dual-linked, verified failover

A second Kontron D10 (192.168.100.2) now sits between the ESP32 boards and
the first one (192.168.100.1, which carries the PC), joined by **two**
parallel 1G links (Gi1/1, Gi1/2 on both switches). MSTP already elects one
as active and the other as standby (`AlternatePort`/`discarding`) on its
own — a real second path, not yet FRER's zero-loss kind, but genuine,
safe, and already there.

`pc/redundancy_failover_test.py` admin-shuts the currently active
inter-switch link via `port.config.set` (remotely reversible — no cable
pulling needed, and switch2 stays reachable throughout since its own
management traffic reroutes the same way), watches `test/stats/**`
through the cutover, then restores it. Verified on the real rig:
**longest gap between samples across the entire failover: 0.51s** — at
our ~1s telemetry cadence, that reads as "no observable interruption."
CSI frame counters, RTT stats, all 5 domains kept incrementing straight
through the cutover and the restore.

FRER (true zero-loss, no STP reconvergence wait at all) is the fancier
version of this and is a known, do-able next step —
`keti-reconfig/docs/D10_SWITCH_REFERENCE.md` and `d10-tsn-manager`'s
`96ee0ae` commit have the working recipe (`vcl.config.stream.add` with
`protocol:"ANY"` uppercase, `vcl.config.interface.stream.add` to attach
the ingress classifier, `frer.config.add` with unused `StreamId0..7` set
to `0` not `-1`). Not done here yet because it needs disabling STP on
these same two ports, and with only two switches and two direct links
between them, that's a genuine 2-node loop — worth doing carefully
(storm-control safety net first, console open) rather than blind, per
that doc's own warning from an earlier ring topology.

## D10 QoS/TSN — control channel works, before/after doesn't show a delta yet

`pc/qos_before_after.py` and `pc/congestion_before_after.py` talk to the
D10's WebStaX JSON-RPC (`http://<d10-ip>/json_rpc`, Basic auth
`admin`/blank, same interface `d10-tsn-manager` uses) to actually change
`qos.config.interface.queueShaper` on the ESP32 ports and watch
`test/stats/**` respond. Real, working control path — worth stating
plainly what it did and didn't show:

- The JSON-RPC call shape learned the hard way: `params` is 3 flat
  arguments (`[port, queue, value]`), not `[[port, queue], value]` — the
  latter fails with a clear "argument-cnt-expect 3, actual 2" rather than
  silently doing nothing.
- `Cir` (committed rate, kbps) below the switch's minimum granularity
  rejects with a QOS parameter error; 64-100 worked. But this project's
  actual traffic is tiny (a few kbit/s across all `bridge/**`+`test/**`+
  `ivn/**` combined), so shaping it down to 64-100 kbps barely constrains
  anything — no clean before/after delta.
- `congestion_before_after.py` instead offers a raw UDP flood (~950 Mbit/s
  attempted from the PC's own socket loop, unverified whether that's what
  actually left the NIC) at one ESP32's IP with no QoS protection
  configured, to show what contention does to the RTT probe. Measured:
  **no significant change** in `test/stats/esp32-1`'s avg/jitter. Either
  the flood didn't really load that 100 Mbit egress port the way the
  offered-rate number suggests, or the D10's own default queuing already
  isolates small control traffic well. Not chased further yet — a
  dedicated packet generator (scapy, `pktgen`) instead of a bare Python
  socket loop, and reading the D10's own port counters instead of just
  the RTT probe, would be the next things to check.

## Planned

- **3 more virtual domains** to reach 8 (5 done: chassis/body/cabin/diag×2),
  plus VLANs on the D10 to actually segment them (currently all 5 share
  one flat subnet).
- **QoS/TSN before/after, take two**: see the D10 QoS/TSN section above —
  a real packet generator and the switch's own port counters instead of a
  bare Python flood and the RTT probe alone.
- **Multiplexing**: a second physical path between a node and the D10
  (target: 2), so a path can be cut without losing the session.
- **10BASE-T1S**: swap or add to the W5500 for a single-pair automotive/
  industrial Ethernet link, then scale to 4 and 8 ESP32 nodes on the bus.
  No transceiver hardware confirmed on hand yet — chip choice
  (e.g. LAN8651) and pin mapping TBD once the part is picked.
- **Sensors**: not yet specified.
- **CAN**: explicitly not a required metric in the current task document
  — skipped for now.

## Origin

The AURIX side of the original project (debug access, TAS/OpenOCD,
tricore-elf-gcc) lives on in `aurix-tc4d7` — see that repo's `STATUS.md`
for why it's currently stuck (an OpenOCD dev build with a ~32-byte
memory-transfer ceiling) and how the two could be reconnected later.
