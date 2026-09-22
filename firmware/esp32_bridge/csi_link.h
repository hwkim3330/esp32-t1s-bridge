// Cabin domain (5th virtual domain): WiFi CSI across the boards, used as a
// coarse direction-of-motion sensor. The boards are aimed in different
// directions, so each radio path between them is a separate "beam", and
// something moving through a path perturbs that path's channel. Which path
// wobbles tells you roughly where the motion is.
//
// WHAT THIS CAN AND CANNOT DO -- worth being blunt, because the display looks
// like a radar and radars imply range:
//
//   * Direction: yes, at the resolution of "which path". Each path is a
//     bistatic link, and a body in its Fresnel zone changes the multipath
//     that link sees.
//   * Range: no. Range resolution is c/2B, and B here is one 20 MHz WiFi
//     channel -> 7.5 m. A room is one range bin. There is no range axis to
//     read, blurry or otherwise, and the dashboard must not draw one.
//   * Angle of arrival: no. One antenna per board, and the boards have
//     independent clocks, so there is no phase coherence to beamform with.
//     Direction here comes from path geometry, not from phase.
//
// Real range and angle need bandwidth or an array: UWB (500 MHz -> ~30 cm) or
// 60 GHz mmWave. This is deliberately neither.
//
// THE RADIO CARRIES NO INFORMATION. It exists only to put frames in the air
// for the other boards to measure the channel of -- a path can only be
// measured if one board physically receives a frame another transmitted, and
// no amount of Ethernet can create that. Everything else travels over the LAN
// via Zenoh: each board reports what it measured against a raw source MAC and
// publishes its own MAC alongside, and the PC does all the naming. No board
// needs to know which board is at the other end of a path.
//
// WHY ASSOCIATED WiFi AND NOT ESP-NOW. ESP-NOW would be tidier -- no AP, all
// boards identical, and it would give the third path (2<->3) that an
// AP-at-the-centre arrangement cannot, since the AP relays station traffic
// rather than letting stations hear each other directly. It was built and
// tried on the real boards, and it does not work here: with all three boards
// broadcasting and unicasting ESP-NOW probes, the CSI callback fired 1432
// times on other networks' traffic and exactly 0 times on our own frames.
// An unassociated station produces no CSI for its own ESP-NOW frames on this
// chip, and enabling promiscuous mode (MGMT|DATA) did not change that. So:
// association, which is the arrangement that demonstrably produces CSI here.
//
// Promiscuous mode is kept on anyway. It costs one MAC compare per frame and
// it is what lets a station also hear the OTHER station's uplink to the AP.
// Where that works it yields the station-to-station path for free; where it
// does not, the path simply never appears and the dashboard draws what
// exists rather than a path we wish were there.
//
// Still NOT a validated presence detector -- the threshold below is a guess,
// not a calibration. What this task needs is a domain with a real physical
// sensing path (RF -> CSI -> Zenoh -> Ethernet -> D10 -> HPC); accuracy is
// somebody's later problem, against real data.
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <string.h>

#include "zenoh_bridge_domains.h"  // publishCabin() -- see zenoh_bridge.cpp

#define CSI_WIFI_SSID "ivn-cabin-link"
#define CSI_WIFI_PASS "ivnivnivn1"  // WPA2 needs >=8 chars

// Generous, because promiscuous mode means neighbouring networks land in here
// too. Only the busiest few are reported, and the PC keeps the ones whose MAC
// it recognises as a board.
#define CSI_MAX_PEERS 12
#define CSI_REPORT_PEERS 4

namespace {

struct PeerLink {
  uint8_t mac[6];
  bool used;
  volatile int8_t rssi;
  volatile float ampMean;
  volatile float ampVar;
  volatile uint32_t frames;
};
PeerLink gLinks[CSI_MAX_PEERS];

// Runs in the WiFi task; the reporting side only reads, so no lock.
int linkSlot(const uint8_t *mac) {
  int freeSlot = -1;
  for (int i = 0; i < CSI_MAX_PEERS; i++) {
    if (gLinks[i].used) {
      if (memcmp(gLinks[i].mac, mac, 6) == 0) return i;
    } else if (freeSlot < 0) {
      freeSlot = i;
    }
  }
  if (freeSlot < 0) return -1;
  memcpy(gLinks[freeSlot].mac, mac, 6);
  gLinks[freeSlot].frames = 0;
  gLinks[freeSlot].used = true;
  return freeSlot;
}

void IRAM_ATTR csiRxCallback(void *, wifi_csi_info_t *info) {
  if (!info || !info->buf || info->len == 0) return;
  // Deliberately not filtered by MAC here: this board has no idea which MACs
  // are its siblings and does not need to -- it reports what it measured
  // against whoever transmitted, and the PC names them.
  int slot = linkSlot(info->mac);
  if (slot < 0) return;

  // buf is signed 8-bit I/Q pairs per subcarrier; ||(I,Q)|| is that
  // subcarrier's amplitude. Variance across them is a coarse "how much is
  // this path wobbling" number -- a body in the path changes multipath,
  // which shows up here.
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
  gLinks[slot].ampMean = mean;
  gLinks[slot].ampVar = (float)sumSq / n - mean * mean;
  gLinks[slot].rssi = info->rx_ctrl.rssi;
  gLinks[slot].frames++;
}

bool gCsiUp = false;

void csiEnable() {
  wifi_promiscuous_filter_t filt = {};
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA;
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous(true);

  wifi_csi_config_t csiConfig = {};
  csiConfig.lltf_en = true;
  csiConfig.htltf_en = true;
  csiConfig.stbc_htltf2_en = true;
  csiConfig.ltf_merge_en = true;
  csiConfig.channel_filter_en = true;
  csiConfig.manu_scale = false;
  esp_err_t e1 = esp_wifi_set_csi_config(&csiConfig);
  esp_err_t e2 = esp_wifi_set_csi_rx_cb(&csiRxCallback, nullptr);
  esp_err_t e3 = esp_wifi_set_csi(true);
  Serial.printf("[csi] config=%s rx_cb=%s enable=%s\n", esp_err_to_name(e1), esp_err_to_name(e2),
                esp_err_to_name(e3));
  gCsiUp = (e3 == ESP_OK);
}

void csiClearLinks() {
  for (int i = 0; i < CSI_MAX_PEERS; i++) gLinks[i] = PeerLink{{0}, false, -100, 0, 0, 0};
}

}  // namespace

#if NODE_ID == 1
// The AP. Its beacons and its replies to both stations are what those
// stations measure, and it measures both stations' uplinks itself.
inline void csiLinkSetup() {
  csiClearLinks();
  WiFi.mode(WIFI_AP);
  WiFi.softAP(CSI_WIFI_SSID, CSI_WIFI_PASS);
  Serial.printf("[csi] AP up: %s, IP %s, mac %s\n", CSI_WIFI_SSID, WiFi.softAPIP().toString().c_str(),
                WiFi.softAPmacAddress().c_str());
  csiEnable();
}

#else
static WiFiUDP gCsiPingSock;
static bool gCsiStaConnected = false;

inline void csiLinkSetup() {
  csiClearLinks();
  WiFi.mode(WIFI_STA);
  WiFi.begin(CSI_WIFI_SSID, CSI_WIFI_PASS);
  Serial.print("[csi] connecting to AP");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    Serial.print(".");
    delay(300);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(" FAILED (cabin domain disabled, everything else unaffected)");
    return;
  }
  gCsiStaConnected = true;
  Serial.printf("\n[csi] STA connected, IP %s, mac %s\n", WiFi.localIP().toString().c_str(),
                WiFi.macAddress().c_str());
  csiEnable();
  gCsiPingSock.begin(51000);
}
#endif

inline void csiLinkLoop() {
  if (!gCsiUp) return;
  uint32_t now = millis();

#if NODE_ID != 1
  // Keeps frames flowing both ways: the AP needs something of ours to
  // measure, and our own link gets sampled at a steady rate rather than
  // whenever the AP happens to beacon.
  static uint32_t tPing = 0;
  if (gCsiStaConnected && now - tPing >= 100) {  // 10 Hz
    tPing = now;
    gCsiPingSock.beginPacket(WiFi.gatewayIP(), 51000);
    gCsiPingSock.write((const uint8_t *)"csi-probe", 9);
    gCsiPingSock.endPacket();
  }
#endif

  static uint32_t tReport = 0;
  if (now - tReport < 500) return;  // 2 Hz, matching the other domains
  tReport = now;

  uint8_t self[6];
#if NODE_ID == 1
  esp_wifi_get_mac(WIFI_IF_AP, self);
#else
  esp_wifi_get_mac(WIFI_IF_STA, self);
#endif
  char line[320];
  int off = snprintf(line, sizeof(line), "node=%d,self=%02x:%02x:%02x:%02x:%02x:%02x", NODE_ID, self[0],
                     self[1], self[2], self[3], self[4], self[5]);

  bool present = false;
  bool taken[CSI_MAX_PEERS] = {false};
  for (int rank = 0; rank < CSI_REPORT_PEERS; rank++) {
    int best = -1;
    for (int i = 0; i < CSI_MAX_PEERS; i++) {
      if (!gLinks[i].used || taken[i]) continue;
      if (best < 0 || gLinks[i].frames > gLinks[best].frames) best = i;
    }
    if (best < 0 || gLinks[best].frames == 0) break;
    taken[best] = true;
    // Threshold is a guess, not a calibration: near-zero variance on a strong
    // link reads as an undisturbed path, clearly above that as something in
    // the way. Revisit against a person actually walking.
    if (gLinks[best].rssi > -75 && gLinks[best].ampVar > 5.0f) present = true;
    if (off <= 0 || off >= (int)sizeof(line)) break;
    off += snprintf(line + off, sizeof(line) - off,
                    ";peer=%02x:%02x:%02x:%02x:%02x:%02x,rssi=%d,var=%.1f,mean=%.1f,frames=%lu",
                    gLinks[best].mac[0], gLinks[best].mac[1], gLinks[best].mac[2], gLinks[best].mac[3],
                    gLinks[best].mac[4], gLinks[best].mac[5], (int)gLinks[best].rssi, gLinks[best].ampVar,
                    gLinks[best].ampMean, (unsigned long)gLinks[best].frames);
  }
  publishCabin(line, present);
}
