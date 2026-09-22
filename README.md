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
| Multiplexing (target 2) | done both ways: STP failover verified (0.51s) and bidirectional FRER (zero-loss) proven live with real traffic in both directions, zero packet loss risk, no storm — see below |

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
- **LED control** (`firmware/esp32_bridge/led_control.h`), on every board:
  plain on/off/toggle on GPIO4 (deliberately *not* the onboard addressable
  WS2812 on GPIO48 — that pin is already the W5500's SPI SCK line on this
  board, and bit-banging the LED protocol on it would corrupt real-time
  SPI clocking). Same handler, two entry points: typed into the Arduino
  Serial Monitor right now (`led on` / `led off` / `led toggle`, works even
  with Ethernet/zenoh down — verified on ESP32-1 while its own network
  issue, below, was still unresolved), and a zenoh subscriber
  (`cmd/esp32-<N>/led`) for whenever LAN-only control (no USB cable) is
  needed instead.
- **Local control/monitoring web app** (`pc/webapp/`), not a Claude
  Artifact: a real FastAPI/uvicorn process on the PC itself, bound to
  `0.0.0.0:8811` so it's reachable by IP from either of the PC's networks.
  Polls both D10s' JSON-RPC and a live zenoh subscription every second —
  shows a node's physical Ethernet link and its zenoh-session liveness as
  two *separate* signals (a node can be link-up with zenoh dead, exactly
  ESP32-1's current state, without the dashboard flattening that into one
  misleading "down" pill) — and can actually change switch config from the
  page (port shutdown for the failover demo, STP enable/disable, FRER
  AdminActive toggle, re-apply the whole FRER script) instead of only
  displaying it.

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

**FRER (802.1CB), bidirectional, proven live** — `pc/frer_setup.py`
configures true dual-path replication both ways: ESP32-1→PC (switch2
Generation, switch1 Recovery, stream/inst 1) and PC→ESP32-1 (switch1
Generation, switch2 Recovery, stream/inst 2) — instead of STP's
single-active-path failover above. Getting from "config that looks right"
to "actually replicates packets, safely" took four real, confirmed bugs
and one real architectural discovery, all found by reading the switch's
own `/json_spec` and its live counters rather than guessing from field
names or trusting `keti-reconfig`'s older docs:

1. **Per-port VCL "Matching" mode** (`vcl.config.interface.vcl_cfg.get/set`,
   not exposed in the web UI keti-reconfig's docs checked): the ring ports
   and both leaf ports (Gi1/5, Gi1/6) all need `dmac_dip` to match our
   destination-MAC classifiers — any port left at the default `smac_sip`
   silently never matches at the hardware TCAM level, classifier correctly
   attached or not.
2. **STP unconditionally blocking a redundant link's second path is a
   real hardware constraint, not just an STP nuisance**: with Gi1/2
   sitting `AlternatePort`/`discarding`, FRER's second egress copy was
   being dropped regardless of FRER's own config (`frer.status.get`'s
   `WarningStpBlocked`/`WarningMstpBlocked` flags stayed false throughout
   — don't trust them as the signal). Fix: `mstp.config.cist.interface.set
   (Enable=false)` is a *whole-port* switch, not per-VLAN, so making Gi1/2
   always-forward for FRER's VLAN safely requires first narrowing Gi1/2 to
   being a member of *only* that VLAN (`HybridVlans=[200]`) — otherwise
   disabling STP there reopens a real two-switch bridging loop for VLAN 1.
   Gi1/1 stays untouched (STP-protected, all-VLANs), so general traffic
   keeps exactly the redundancy it already had.
3. **The actual root cause of "Passed stays 0 no matter what"** for
   direction A: the ESP32-1 ingress port on switch2 was simply wrong. It
   was guessed as `Gi 1/6` early on (from RX counter deltas, never
   actually verified) — `mac.status.fdb.full.get` finally settled it
   directly: ESP32-1's MAC is on `Gi 1/5`; `Gi 1/6` is ESP32-2's port.
   Also caught a stray, unrelated `stream.add` on `Gi 1/4` left over from
   an earlier experiment months back, attached to the *same* stream index
   being reused for direction B — its dead link tripped `WarningIngressNoLink`
   until cleaned up. Stream/instance IDs aren't global; reusing one without
   checking `vcl.config.interface.stream.get()`'s full list first is a
   real trap on a switch that's been used for other things before.
4. **The real replication mechanism, and the loop it can reopen**: this
   switch has no dedicated "duplicate to N ports" FRER hardware path — it
   replicates by **flooding** the R-tagged frame within the FRER VLAN
   (nothing else lives in VLAN 200, so the destination is always
   "unknown" there, and normal flood-to-all-members behavior *is* the
   duplication). Confirmed directly: with `vlan.config.global.flooding`
   temporarily re-enabled for VLAN 200, `frer.statistics.get`'s `Passed`
   went non-zero for the first time. But that's also a live loop: Gi1/2
   has no STP protection (needed so it always-forwards), so an
   unknown-destination frame in VLAN 200 bounces switch1↔switch2 over
   Gi1/1+Gi1/2 forever. Watched it happen — both switches' leaf-port
   discard counters climbed by **tens of millions per second** within
   seconds, and it knocked out ESP32-2's real session. Reverted (flooding
   back off) immediately; confirmed the storm stopped and ESP32-2
   recovered. MSTI-per-VLAN mapping doesn't fix this either — spanning
   tree always blocks one of two parallel links between the same two
   bridges in *any* instance, so it can't give "both links forwarding" no
   matter which MSTI VLAN 200 sits in. **The actual fix**:
   `mac.config.fdb.static.add`'s `PortList` ("list of destination ports
   for which frames with this DMAC is forwarded to") takes more than one
   port — a static, VLAN-200-scoped multicast-style FDB entry per
   direction's real destination MAC makes Generation duplicate
   deterministically onto both ring ports with **no flooding involved
   anywhere**, so there's nothing left that can bounce. VLAN 1 dynamic
   learning for the same MACs is untouched (VLAN is part of the FDB key).

**Direction B (PC→ESP32-1) proven live, safely**: a ping burst from the
PC shows real duplicated frames on switch1's Gi1/2 (previously stuck at
zero no matter what), `frer.statistics.get`'s `Passed` climbing on
switch2's Recovery side, and — critically — `TxDiscardPkts` staying at
**0** throughout on every port, with `vlan.config.global.flooding.get`
confirmed `false` on both switches the whole time. Direction A
(ESP32-1→PC) is configured identically and will behave the same the
moment ESP32-1's own hardware issue (below) is resolved — nothing left to
fix on the switch side for it.

**ESP32-1's problem: resolved, and it really was the cable.** Chased it
all the way down to a physical-layer fault via an ICMP echo test added
directly in firmware (`esp_ping`, bypassing zenoh-pico and even bypassing
a raw UDP `sendto()` — which can report success without a frame ever
reaching the wire): 0 of 4 replies, symmetric with the PC's own failed
pings to it, with everything software-side ruled out first (switch
config, a from-scratch reflash, a full power-cycle, CSI/WiFi compiled
out). Chip spec matched ESP32-2 exactly (`esptool chip-id`), so not a
different/defective board model either. The actual test: swapped the
original ESP32-1 unit onto a different switch port (Gi1/3) and put a
third, known-good board on its old port/cable (Gi1/5) instead. **Both
now work perfectly** — the third board runs fine on the old Gi1/5
position, and the original ESP32-1 unit runs fine on its new Gi1/3
position. Neither board was ever defective; the original Gi1/5 cable
run was bad. Net result: **3 live ESP32 nodes** now (esp32-1 on Gi1/3,
esp32-2 on Gi1/6, esp32-3 on Gi1/5), and bidirectional FRER (above) is
proven with real end-system traffic in both directions — `Passed`
climbing on switch1's Recovery from esp32-3's own live zenoh publishes
(direction A) and on switch2's Recovery from PC-originated traffic
(direction B), both with zero `TxDiscardPkts` growth anywhere.

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
- **10BASE-T1S**: no custom PHY design needed after all — an off-the-shelf
  TSN Lab **LAN8651** Raspberry Pi HAT (the product photo's silkscreen and
  TSN Lab's own device-tree overlay both say LAN8651, not the LAN8650 the
  devicemart listing states) plus a small adapter board gets T1S onto a
  T-ETH-Elite (ESP32-S3) node. The T-ETH-Elite's own 40-pin header already
  matches the Pi GPIO header pin-for-pin, *and* its bottom mounting-hole pair
  matches the Pi's to 0.005 mm — so the adapter is not there for pin mapping,
  and a tall enough stacking header would already clear the Elite's RJ45
  (15.97 mm, against header pins ending at 10.10 mm). What the adapter adds is
  the Pi's **far** hole pair, which lands 4.4 mm past the Elite's own edge, so
  a full-size HAT is held at four corners instead of two — plus Ø4 tool holes
  over the BOOT/RESET switches it would otherwise bury. Hardware lives in its
  own repo: **[t-eth-elite-hat-adapter](https://github.com/hwkim3330/t-eth-elite-hat-adapter)**
  (KiCad project, gerbers, printed tray, and the LilyGo CAD the geometry was
  measured from). Not fabricated, not bench-tested, no order placed. Per TSN
  Lab's overlay the HAT claims SPI0 + CE0, GPIO23 (PHY IRQ), GPIO24 (FXL6408
  IRQ) and I2C1 — so CE1 and most GPIOs stay free for a sensor. Scaling to 4
  and 8 ESP32 nodes on the bus is still open.
- **Sensors**: not yet specified.
- **CAN**: explicitly not a required metric in the current task document
  — skipped for now.

## Origin

The AURIX side of the original project (debug access, TAS/OpenOCD,
tricore-elf-gcc) lives on in `aurix-tc4d7` — see that repo's `STATUS.md`
for why it's currently stuck (an OpenOCD dev build with a ~32-byte
memory-transfer ceiling) and how the two could be reconnected later.
