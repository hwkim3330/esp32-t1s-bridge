# ESP32 T1S Bridge

An Edge/HPC testbed for KETI's IVN 3세부 과제 (SDV network architecture:
Multi-GigE backbone + 10Mbps lightweight edge Ethernet, DDS, QoS/TSN,
virtual domains). ESP32-S3 boards stand in for virtual ECUs on the edge
side, talking [Zenoh](https://zenoh.io/) over Ethernet (W5500 now,
10BASE-T1S planned) through a Kontron D10 switch to a PC/HPC that will run
the Zenoh↔DDS bridge and Fast DDS. Split off from
[`aurix-tc4d7`](https://github.com/hwkim3330/aurix-tc4d7), whose AURIX side
is separately blocked on an immature OpenOCD dev build.

Task alignment, roughly:

| Task needs | This repo |
|---|---|
| 10Mbps edge Ethernet | RJ45/W5500 now → 10BASE-T1S later |
| Pub/sub middleware | zenoh-pico on ESP32 |
| DDS (mandatory per task doc) | planned: `zenoh-plugin-dds` bridge + Fast DDS on the PC (already installed via ROS2 Jazzy) |
| Edge→HPC delay/jitter test | `pc/zenoh_peer.py` RTT echo + firmware ping/pong — see below |
| Virtual domains (target 8) | not started |
| QoS/TSN before/after on D10 | not started |
| Multiplexing (target 2) | not started |

## What works now

- `firmware/esp32_bridge/`: ESP32-S3 + W5500 (SPI Ethernet), zenoh-pico as
  a Zenoh peer. Two boards run the same firmware (`NODE_ID` build flag
  picks IP `.60`/`.61` and keyexpr `bridge/esp32-1`/`bridge/esp32-2`),
  verified together on a Kontron D10 switch: both link at 100 Mbit
  full-duplex and exchange pub/sub with a Zenoh peer on the PC. The two
  ESP32s don't see each other directly yet (peer mode, no router) — only
  via the PC's session.
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

## Planned

- **DDS bridge**: `zenoh-plugin-dds` (or `zenoh-bridge-dds`) on the PC,
  fronting Fast DDS (already present via `ros-jazzy-fastrtps`) — this is
  what the task document actually requires on the HPC side, not Zenoh
  alone.
- **Virtual domains**: tag traffic into 4 (then 8) logical domains,
  probably as VLANs on the D10 plus a keyexpr namespace per domain.
- **QoS/TSN before/after**: configure the D10's QoS/TSN and re-run the
  RTT probe under load to show the delta.
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
