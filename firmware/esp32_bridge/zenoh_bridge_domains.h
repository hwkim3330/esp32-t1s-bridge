#pragma once
#include <stdint.h>

// Implemented in zenoh_bridge.cpp (needs the live zenoh session/publisher),
// called from csi_link.h so that file doesn't have to pull in zenoh-pico.h.
void publishCabin(int8_t rssi, float amplitudeMean, float amplitudeVar, uint32_t frames, bool present);
