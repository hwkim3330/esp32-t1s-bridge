// Simple LED control, common to all boards (NODE_ID 1/2/3...).
//
// NOT on the onboard addressable WS2812 (GPIO48 on this esp32s3 variant,
// LED_BUILTIN/RGB_BUILTIN) -- that pin is already our W5500 SPI SCK line
// (kSck in zenoh_bridge.cpp). Bit-banging the WS2812 protocol on the same
// pin used for real-time SPI clocking would corrupt both, intermittently
// and hard to reproduce -- not worth it for a status LED. GPIO38 per the
// user's own board (silkscreen-confirmed, not a guess like the original
// GPIO4 pick was) -- a plain on/off output, no timing-critical protocol.
#pragma once
#include <Arduino.h>

#ifndef LED_CTRL_PIN
#define LED_CTRL_PIN 38
#endif

static bool gLedState = false;

inline void ledControlSetup() {
  pinMode(LED_CTRL_PIN, OUTPUT);
  digitalWrite(LED_CTRL_PIN, LOW);
  gLedState = false;
}

inline void ledControlSet(bool on) {
  gLedState = on;
  digitalWrite(LED_CTRL_PIN, on ? HIGH : LOW);
}

inline bool ledControlGet() { return gLedState; }

// Shared parser for both the serial (UART, right now) and zenoh (LAN,
// later) command paths -- same three words either way: "on", "off",
// "toggle". Returns true if `cmd` was a recognized LED command.
inline bool ledControlHandleCommand(const char *cmd) {
  if (!strcasecmp(cmd, "on")) {
    ledControlSet(true);
  } else if (!strcasecmp(cmd, "off")) {
    ledControlSet(false);
  } else if (!strcasecmp(cmd, "toggle")) {
    ledControlSet(!gLedState);
  } else {
    return false;
  }
  return true;
}

// Non-blocking: call every loop() iteration. Accepts lines typed into the
// Arduino Serial Monitor (or any serial terminal) at 115200 baud, e.g.
// "led on" / "led off" / "led toggle" -- this is the "지금은 유아트로"
// path; ledControlHandleCommand() itself doesn't care whether it was
// called from here or from a zenoh subscriber, which is the "나중에는
// 랜으로" path (see LED_KEYEXPR / the subscriber declared in
// zenoh_bridge.cpp).
inline void ledControlSerialPoll() {
  static char buf[32];
  static size_t len = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (len > 0) {
        buf[len] = '\0';
        char *cmd = buf;
        if (!strncasecmp(buf, "led ", 4)) cmd = buf + 4;
        if (ledControlHandleCommand(cmd)) {
          Serial.printf("[led] -> %s\n", gLedState ? "on" : "off");
        } else {
          Serial.printf("[led] unrecognized command: '%s' (try: led on / led off / led toggle)\n", buf);
        }
        len = 0;
      }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}
