# ESP32 T1S Bridge

ESP32-S3 as the center of the network, not a side character: Ethernet
(W5500 today, 10BASE-T1S planned) + sensors, talking [Zenoh](https://zenoh.io/)
pub/sub over the wire. Split off from
[`aurix-tc4d7`](https://github.com/hwkim3330/aurix-tc4d7), whose AURIX side
is separately blocked on an immature OpenOCD dev build — this repo drops
AURIX and builds around the ESP32 instead.

## What works now

- `firmware/esp32_bridge/`: ESP32-S3 + W5500 (SPI Ethernet), zenoh-pico as
  a Zenoh peer. Verified on real hardware over a Kontron D10 switch: the
  board gets a static IP, links at 100 Mbit full-duplex, and exchanges
  pub/sub messages with a Zenoh peer on a PC in both directions.
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

- **10BASE-T1S**: swap or add to the W5500 for a single-pair automotive/
  industrial Ethernet link. No transceiver hardware confirmed on hand yet
  for this repo — pin mapping and chip choice TBD once the part is picked.
- **Sensors**: not yet specified.

## Origin

The AURIX side of the original project (debug access, TAS/OpenOCD,
tricore-elf-gcc) lives on in `aurix-tc4d7` — see that repo's `STATUS.md`
for why it's currently stuck (an OpenOCD dev build with a ~32-byte
memory-transfer ceiling) and how the two could be reconnected later.
