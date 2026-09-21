// Cabin domain (5th virtual domain): WiFi CSI as a stand-in "presence
// sensor", so the demo doesn't need to buy or wire an actual IMU/PIR --
// the two ESP32-S3 boards already on the bench are the sensor. NODE_ID=1
// runs a small closed WiFi AP just for this (SSID below); NODE_ID=2
// joins it as a station, sends it a tiny UDP ping to keep frames flowing,
// and reads Channel State Information off those frames.
//
// Deliberately NOT a validated presence/motion detector. Espressif's own
// esp-csi examples say as much -- this publishes the raw numbers
// (RSSI, CSI amplitude variance) plus a crude fixed threshold on top,
// because what this task needs is a domain with a real physical sensing
// path (RF -> WiFi CSI -> Zenoh -> Ethernet -> D10 -> HPC), not sensor
// accuracy. Tune or replace the threshold once someone actually stands
// in front of the boards and it's worth calibrating against real data.
//
// WiFi and the W5500/Ethernet+Zenoh session are independent ESP32
// subsystems (different radios/peripherals, different netifs) and are
// expected to coexist -- this is what the stability soak in setup/loop
// below is checking, not assumed.
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>

#include "zenoh_bridge_domains.h"  // publishCabin() -- see zenoh_bridge.cpp

#define CSI_WIFI_SSID "ivn-cabin-link"
#define CSI_WIFI_PASS "ivnivnivn1"  // WPA2 needs >=8 chars

namespace {

struct CsiStats {
  volatile int8_t lastRssi = -100;
  volatile float lastAmplitudeMean = 0;
  volatile float lastAmplitudeVar = 0;
  volatile uint32_t frames = 0;
};
CsiStats gCsi;

void IRAM_ATTR csiRxCallback(void *, wifi_csi_info_t *info) {
  if (!info || !info->buf || info->len == 0) return;
  // buf is signed 8-bit I/Q pairs per subcarrier; ||(I,Q)|| is that
  // subcarrier's amplitude. Mean/variance across them is a coarse
  // "how much is this frame's channel wobbling" number -- a moving
  // body between AP and station changes multipath, which shows up here.
  int64_t sum = 0, sumSq = 0;
  int n = info->len / 2;
  if (n <= 0) return;
  for (int i = 0; i < n; i++) {
    int8_t I = info->buf[2 * i];
    int8_t Q = info->buf[2 * i + 1];
    int amp = (int)sqrtf((float)(I * I + Q * Q));
    sum += amp;
    sumSq += (int64_t)amp * amp;
  }
  float mean = (float)sum / n;
  float var = (float)sumSq / n - mean * mean;
  gCsi.lastAmplitudeMean = mean;
  gCsi.lastAmplitudeVar = var;
  gCsi.lastRssi = info->rx_ctrl.rssi;
  gCsi.frames++;
}

}  // namespace

#if NODE_ID == 1
inline void csiLinkSetup() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP(CSI_WIFI_SSID, CSI_WIFI_PASS);
  Serial.printf("[csi] AP up: %s, IP %s\n", CSI_WIFI_SSID, WiFi.softAPIP().toString().c_str());
}
inline void csiLinkLoop() {}

#elif NODE_ID == 2
static WiFiUDP gCsiPingSock;
static bool gCsiStaConnected = false;

inline void csiLinkSetup() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(CSI_WIFI_SSID, CSI_WIFI_PASS);
  Serial.print("[csi] connecting to AP");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    Serial.print(".");
    delay(300);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(" FAILED (CSI/cabin domain disabled, everything else unaffected)");
    return;
  }
  gCsiStaConnected = true;
  Serial.printf("\n[csi] STA connected, IP %s, gateway %s\n", WiFi.localIP().toString().c_str(),
                WiFi.gatewayIP().toString().c_str());

  wifi_csi_config_t csiConfig = {};
  csiConfig.lltf_en = true;
  csiConfig.htltf_en = true;
  csiConfig.stbc_htltf2_en = true;
  csiConfig.ltf_merge_en = true;
  csiConfig.channel_filter_en = true;
  csiConfig.manu_scale = false;
  esp_wifi_set_csi_config(&csiConfig);
  esp_wifi_set_csi_rx_cb(&csiRxCallback, nullptr);
  esp_wifi_set_csi(true);
  gCsiPingSock.begin(51000);
}

inline void csiLinkLoop() {
  if (!gCsiStaConnected) return;
  static uint32_t tPing = 0;
  uint32_t now = millis();
  if (now - tPing >= 100) {  // 10 Hz: enough frames for a moving-average presence read
    tPing = now;
    gCsiPingSock.beginPacket(WiFi.gatewayIP(), 51000);
    gCsiPingSock.write((const uint8_t *)"csi-probe", 9);
    gCsiPingSock.endPacket();
  }

  static uint32_t tReport = 0;
  if (now - tReport >= 500) {  // 2 Hz, matching the other domains
    tReport = now;
    // Threshold is a guess, not a calibration: near -100 dBm/near-zero
    // variance reads as an empty room, anything clearly above that as
    // "something's there". Revisit once this has actually been tested
    // against a person walking in front of the boards.
    bool present = gCsi.lastRssi > -75 && gCsi.lastAmplitudeVar > 5.0f;
    publishCabin(gCsi.lastRssi, gCsi.lastAmplitudeMean, gCsi.lastAmplitudeVar, gCsi.frames, present);
  }
}

#else
inline void csiLinkSetup() {}
inline void csiLinkLoop() {}
#endif
