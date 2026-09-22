#pragma once
#include <stdint.h>

// Implemented in zenoh_bridge.cpp (needs the live zenoh session/publisher),
// called from csi_link.h so that file doesn't have to pull in zenoh-pico.h.
// csiLine describes every radio path this board can see, one segment per
// peer, so the dashboard can rebuild the triangle from whichever boards
// are currently talking rather than from a fixed assumed layout.
void publishCabin(const char *csiLine, bool present);
