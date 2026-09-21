// ESP32-S3 sensor + Ethernet bridge (Zenoh pub/sub).
//
// All zenoh-pico / ETH / SPI code lives in zenoh_bridge.cpp, not here.
// Arduino's sketch preprocessor runs a `-CC` (comments-preserved) pass over
// the .ino to auto-insert function prototypes, and that pass corrupts
// zenoh-pico's `##`-heavy channel-handler macros (comments placed between
// macro arguments survive substitution and land next to `##`, which GCC
// then rejects). Plain .cpp files in the sketch folder are compiled
// normally (no -CC pass), so keeping zenoh-pico.h out of this file avoids
// the whole problem.
#include <Arduino.h>
#include "zenoh_bridge.h"

void setup() {
  zenohBridgeSetup();
}

void loop() {
  zenohBridgeLoop();
}
