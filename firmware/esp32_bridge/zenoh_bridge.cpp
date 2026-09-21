// ESP32-S3 + W5500, talking Zenoh to a peer on this PC over the Kontron D10
// switch (same 192.168.100.0/24 subnet as the PC's enp4s0 -- see
// afdx-jitter's D10 notes). No DHCP server on that segment, so a static IP
// and an explicit peer locator instead of multicast scouting.
//
// zenoh-pico is vendored under ../lib/zenoh-pico (see
// scripts/50-build-zenoh-arduino-lib.sh at the repo root) and pulled in via
// `arduino-cli compile --library firmware/esp32_bridge/lib/zenoh-pico`.
//
// The W5500 bring-up (eth_w5500.h / w5500_spi.h) and its pin mapping are
// carried over from esp32-lidar/firmware/lidar_probe verbatim: raw
// esp_eth + esp_netif instead of Arduino's ETH class, because ETH.begin
// hardcodes a 10ms MAC poll period with no way to change it afterwards, and
// the IDF driver's own buffer-read has a real bug at the W5500's 16kB
// receive-buffer wrap that w5500_spi.h works around. Do not switch back to
// ETH.begin() to "simplify" this. Pinout is keti-reconfig's measured one,
// not a datasheet default -- see the comment in lidar_probe.ino.
#include "zenoh_bridge.h"

#include <Arduino.h>
#include <cmath>
#include <esp_timer.h>
#include <zenoh-pico.h>

#include "eth_w5500.h"
#include "zenoh_bridge_domains.h"

W5500Spi *gW5500Spi = nullptr;  // defined here; w5500_spi.h only declares it extern

// ---- W5500 SPI wiring (esp32-lidar/firmware/lidar_probe/lidar_probe.ino) ----
constexpr int kSck = 48, kMosi = 21, kCs = 45, kMiso = 47;  // no INT, no RST wired

// ---- Node identity: one board is NODE_ID=1 (192.168.100.60), the other
// NODE_ID=2 (192.168.100.61) -- set via
// `--build-property "compiler.cpp.extra_flags=-DNODE_ID=2"`. Both publish
// under their own keyexpr and subscribe to everyone else's.
#ifndef NODE_ID
#define NODE_ID 1
#endif
#define ESP_IP_LAST (59 + NODE_ID)  // NODE_ID=1 -> .60, NODE_ID=2 -> .61

#include "csi_link.h"  // needs NODE_ID defined above; provides csiLinkSetup()/csiLinkLoop()

// ---- Kontron D10 segment addressing (PC's enp4s0 is 192.168.100.50) ----
static const IPAddress kLocalIP(192, 168, 100, ESP_IP_LAST);
static const IPAddress kMask(255, 255, 255, 0);
static const IPAddress kGateway(192, 168, 100, ESP_IP_LAST);  // no router; loops to self
#define PC_LOCATOR "udp/192.168.100.50:7447"

#define _STR(x) #x
#define STR(x) _STR(x)
#define PUB_KEYEXPR "bridge/esp32-" STR(NODE_ID)
#define SUB_KEYEXPR "bridge/**"

// ---- RTT/latency/jitter probe (KETI IVN 3세부 test metric: Edge->HPC
// delay <=5ms, jitter <=2.5ms). Stateless echo: the timestamp rides in the
// payload, the PC bounces it straight back on the pong topic, so there's
// no pending-ping table to manage on either end.
#define PING_KEYEXPR "test/ping/esp32-" STR(NODE_ID)
#define PONG_KEYEXPR "test/pong/esp32-" STR(NODE_ID)
#define STATS_KEYEXPR "test/stats/esp32-" STR(NODE_ID)

// ---- Virtual domains (KETI IVN 3세부 test metric: virtual domain count,
// target 8; 4 here). Each ECU role publishes synthetic-but-labeled traffic
// under its own domain -- no physical sensor needed for this metric, the
// point is the domain/QoS separation over the same Ethernet, not the data
// source. NODE_ID=1 plays chassis ECU, NODE_ID=2 plays body ECU; both play
// diag. Cabin (WiFi CSI presence/motion) is the next domain, added once
// CSI is confirmed not to disturb the Ethernet/Zenoh session running here.
#define DIAG_KEYEXPR "ivn/diag/esp32-" STR(NODE_ID)
#define CHASSIS_WHEEL_KEYEXPR "ivn/chassis/wheel_speed"
#define CHASSIS_VEHICLE_KEYEXPR "ivn/chassis/vehicle_speed"
#define BODY_KEYEXPR "ivn/body/control"
#define CABIN_CSI_KEYEXPR "ivn/cabin/csi"
#define CABIN_PRESENCE_KEYEXPR "ivn/cabin/presence"

struct RttStats {
  uint32_t count = 0;
  int64_t sum_us = 0;
  int64_t min_us = INT64_MAX;
  int64_t max_us = 0;
  int64_t last_us = 0;
  int64_t jitter_sum_us = 0;  // sum of |rtt[n] - rtt[n-1]|
};
static RttStats s_rtt;

static z_owned_session_t s_session;
static z_owned_publisher_t s_pub;
static z_owned_publisher_t s_ping_pub;
static z_owned_publisher_t s_stats_pub;
static z_owned_publisher_t s_diag_pub;
static z_owned_publisher_t s_chassis_wheel_pub;
static z_owned_publisher_t s_chassis_vehicle_pub;
static z_owned_publisher_t s_body_pub;
static z_owned_publisher_t s_cabin_csi_pub;
static z_owned_publisher_t s_cabin_presence_pub;
static z_owned_subscriber_t s_sub;
static z_owned_subscriber_t s_pong_sub;
static bool s_zenoh_up = false;
static uint32_t s_idx = 0;
static uint32_t s_ping_seq = 0;

// Called from csi_link.h's csiLinkLoop() (NODE_ID==2 only) -- kept out of
// that header so it doesn't need zenoh-pico.h itself.
void publishCabin(int8_t rssi, float amplitudeMean, float amplitudeVar, uint32_t frames, bool present) {
  if (!s_zenoh_up) return;
  char cbuf[80];
  snprintf(cbuf, sizeof(cbuf), "rssi=%d amp_mean=%.1f amp_var=%.1f frames=%lu", (int)rssi, amplitudeMean,
           amplitudeVar, (unsigned long)frames);
  z_owned_bytes_t csi_payload;
  z_bytes_copy_from_str(&csi_payload, cbuf);
  z_publisher_put(z_publisher_loan(&s_cabin_csi_pub), z_bytes_move(&csi_payload), NULL);

  z_owned_bytes_t presence_payload;
  z_bytes_copy_from_str(&presence_payload, present ? "present" : "empty");
  z_publisher_put(z_publisher_loan(&s_cabin_presence_pub), z_bytes_move(&presence_payload), NULL);
}

static void dataHandler(z_loaned_sample_t *sample, void *arg) {
  (void)arg;
  z_view_string_t keystr;
  z_keyexpr_as_view_string(z_sample_keyexpr(sample), &keystr);
  z_owned_string_t value;
  z_bytes_to_string(z_sample_payload(sample), &value);

  Serial.print(" >> [sub] (");
  Serial.write(z_string_data(z_view_string_loan(&keystr)), z_string_len(z_view_string_loan(&keystr)));
  Serial.print(", ");
  Serial.write(z_string_data(z_string_loan(&value)), z_string_len(z_string_loan(&value)));
  Serial.println(")");

  z_string_drop(z_string_move(&value));
}

static void pongHandler(z_loaned_sample_t *sample, void *arg) {
  (void)arg;
  z_owned_string_t value;
  z_bytes_to_string(z_sample_payload(sample), &value);
  char buf[48];
  size_t len = z_string_len(z_string_loan(&value));
  if (len >= sizeof(buf)) len = sizeof(buf) - 1;
  memcpy(buf, z_string_data(z_string_loan(&value)), len);
  buf[len] = '\0';
  z_string_drop(z_string_move(&value));

  uint32_t seq;
  int64_t t_send_us;
  if (sscanf(buf, "%u %lld", &seq, (long long *)&t_send_us) != 2) return;

  int64_t rtt_us = (int64_t)esp_timer_get_time() - t_send_us;
  if (rtt_us < 0) return;  // stale/out-of-order pong, discard rather than corrupt stats

  RttStats &s = s_rtt;
  if (s.count > 0) s.jitter_sum_us += llabs(rtt_us - s.last_us);
  s.count++;
  s.sum_us += rtt_us;
  s.min_us = min(s.min_us, rtt_us);
  s.max_us = max(s.max_us, rtt_us);
  s.last_us = rtt_us;
}

static bool startZenoh() {
  z_owned_config_t config;
  z_config_default(&config);
  zp_config_insert(z_config_loan_mut(&config), Z_CONFIG_MODE_KEY, "peer");
  zp_config_insert(z_config_loan_mut(&config), Z_CONFIG_CONNECT_KEY, PC_LOCATOR);

  Serial.print("[zenoh] opening session...");
  if (z_open(&s_session, z_config_move(&config), NULL) < 0) {
    Serial.println(" FAILED");
    return false;
  }
  Serial.println(" ok");

  z_view_keyexpr_t sub_ke;
  z_view_keyexpr_from_str_unchecked(&sub_ke, SUB_KEYEXPR);
  z_owned_closure_sample_t callback;
  z_closure_sample(&callback, dataHandler, NULL, NULL);
  if (z_declare_subscriber(z_session_loan(&s_session), &s_sub, z_view_keyexpr_loan(&sub_ke),
                            z_closure_sample_move(&callback), NULL) < 0) {
    Serial.println("[zenoh] subscriber declare FAILED");
    return false;
  }

  z_view_keyexpr_t pub_ke;
  z_view_keyexpr_from_str_unchecked(&pub_ke, PUB_KEYEXPR);
  if (z_declare_publisher(z_session_loan(&s_session), &s_pub, z_view_keyexpr_loan(&pub_ke), NULL) < 0) {
    Serial.println("[zenoh] publisher declare FAILED");
    return false;
  }

  z_view_keyexpr_t pong_ke;
  z_view_keyexpr_from_str_unchecked(&pong_ke, PONG_KEYEXPR);
  z_owned_closure_sample_t pong_callback;
  z_closure_sample(&pong_callback, pongHandler, NULL, NULL);
  if (z_declare_subscriber(z_session_loan(&s_session), &s_pong_sub, z_view_keyexpr_loan(&pong_ke),
                            z_closure_sample_move(&pong_callback), NULL) < 0) {
    Serial.println("[zenoh] pong subscriber declare FAILED");
    return false;
  }

  z_view_keyexpr_t ping_ke;
  z_view_keyexpr_from_str_unchecked(&ping_ke, PING_KEYEXPR);
  if (z_declare_publisher(z_session_loan(&s_session), &s_ping_pub, z_view_keyexpr_loan(&ping_ke), NULL) < 0) {
    Serial.println("[zenoh] ping publisher declare FAILED");
    return false;
  }

  z_view_keyexpr_t stats_ke;
  z_view_keyexpr_from_str_unchecked(&stats_ke, STATS_KEYEXPR);
  if (z_declare_publisher(z_session_loan(&s_session), &s_stats_pub, z_view_keyexpr_loan(&stats_ke), NULL) < 0) {
    Serial.println("[zenoh] stats publisher declare FAILED");
    return false;
  }

  z_view_keyexpr_t diag_ke;
  z_view_keyexpr_from_str_unchecked(&diag_ke, DIAG_KEYEXPR);
  if (z_declare_publisher(z_session_loan(&s_session), &s_diag_pub, z_view_keyexpr_loan(&diag_ke), NULL) < 0) {
    Serial.println("[zenoh] diag publisher declare FAILED");
    return false;
  }

  if (NODE_ID == 1) {
    z_view_keyexpr_t wheel_ke;
    z_view_keyexpr_from_str_unchecked(&wheel_ke, CHASSIS_WHEEL_KEYEXPR);
    if (z_declare_publisher(z_session_loan(&s_session), &s_chassis_wheel_pub, z_view_keyexpr_loan(&wheel_ke),
                             NULL) < 0) {
      Serial.println("[zenoh] chassis wheel publisher declare FAILED");
      return false;
    }
    z_view_keyexpr_t vehicle_ke;
    z_view_keyexpr_from_str_unchecked(&vehicle_ke, CHASSIS_VEHICLE_KEYEXPR);
    if (z_declare_publisher(z_session_loan(&s_session), &s_chassis_vehicle_pub, z_view_keyexpr_loan(&vehicle_ke),
                             NULL) < 0) {
      Serial.println("[zenoh] chassis vehicle publisher declare FAILED");
      return false;
    }
  } else if (NODE_ID == 2) {
    z_view_keyexpr_t body_ke;
    z_view_keyexpr_from_str_unchecked(&body_ke, BODY_KEYEXPR);
    if (z_declare_publisher(z_session_loan(&s_session), &s_body_pub, z_view_keyexpr_loan(&body_ke), NULL) < 0) {
      Serial.println("[zenoh] body publisher declare FAILED");
      return false;
    }
    z_view_keyexpr_t csi_ke;
    z_view_keyexpr_from_str_unchecked(&csi_ke, CABIN_CSI_KEYEXPR);
    if (z_declare_publisher(z_session_loan(&s_session), &s_cabin_csi_pub, z_view_keyexpr_loan(&csi_ke), NULL) < 0) {
      Serial.println("[zenoh] cabin csi publisher declare FAILED");
      return false;
    }
    z_view_keyexpr_t presence_ke;
    z_view_keyexpr_from_str_unchecked(&presence_ke, CABIN_PRESENCE_KEYEXPR);
    if (z_declare_publisher(z_session_loan(&s_session), &s_cabin_presence_pub, z_view_keyexpr_loan(&presence_ke),
                             NULL) < 0) {
      Serial.println("[zenoh] cabin presence publisher declare FAILED");
      return false;
    }
  }

  s_rtt = RttStats();
  Serial.println("[zenoh] session up: pub=" PUB_KEYEXPR " sub=" SUB_KEYEXPR " ping=" PING_KEYEXPR
                  " diag=" DIAG_KEYEXPR);
  return true;
}

void zenohBridgeSetup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) {
    delay(10);
  }

  if (!ethStart(kSck, kMiso, kMosi, kCs, /*pollPeriodMs=*/1, kLocalIP, kMask, kGateway)) {
    Serial.println("[eth] ethStart() failed");
  }

  Serial.print("[eth] waiting for link");
  while (!ethLinkUp()) {
    Serial.print(".");
    delay(500);
  }
  Serial.printf("\n[eth] link up, %u Mbit %s-duplex, IP %s, MAC %s\n", ethLinkSpeed(),
                ethFullDuplex() ? "full" : "half", ethLocalIP().toString().c_str(),
                ethMacAddress().c_str());

  // WiFi (cabin/CSI domain) after Ethernet is confirmed up: if the two are
  // going to fight over anything (memory, event loop, IRQs), better to
  // find out with a known-good Ethernet baseline already established.
  csiLinkSetup();
}

void zenohBridgeLoop() {
  csiLinkLoop();

  if (!ethLinkUp()) {
    s_zenoh_up = false;
    delay(500);
    return;
  }

  if (!s_zenoh_up) {
    s_zenoh_up = startZenoh();
    if (!s_zenoh_up) {
      delay(2000);
      return;
    }
  }

  static uint32_t t_hello = 0, t_ping = 0, t_stats = 0, t_domain = 0;
  uint32_t now = millis();

  if (now - t_ping >= 200) {  // 5 Hz RTT probe
    t_ping = now;
    char pbuf[48];
    snprintf(pbuf, sizeof(pbuf), "%u %lld", (unsigned)s_ping_seq++, (long long)esp_timer_get_time());
    z_owned_bytes_t ping_payload;
    z_bytes_copy_from_str(&ping_payload, pbuf);
    z_publisher_put(z_publisher_loan(&s_ping_pub), z_bytes_move(&ping_payload), NULL);
  }

  if (now - t_stats >= 1000 && s_rtt.count > 0) {  // 1 Hz stats report, rolling 1s window
    t_stats = now;
    RttStats &s = s_rtt;
    double avg_ms = (double)s.sum_us / s.count / 1000.0;
    double jitter_ms = s.count > 1 ? (double)s.jitter_sum_us / (s.count - 1) / 1000.0 : 0.0;
    char sbuf[96];
    snprintf(sbuf, sizeof(sbuf), "n=%u avg=%.2fms min=%.2fms max=%.2fms jitter=%.2fms", s.count,
             avg_ms, s.min_us / 1000.0, s.max_us / 1000.0, jitter_ms);
    Serial.printf("[rtt] %s\n", sbuf);
    z_owned_bytes_t stats_payload;
    z_bytes_copy_from_str(&stats_payload, sbuf);
    z_publisher_put(z_publisher_loan(&s_stats_pub), z_bytes_move(&stats_payload), NULL);
    // Reset for the next window rather than accumulating since boot: a
    // months-old average would hide a transient QoS-induced spike behind
    // its own history, and "what does the link look like right now" is
    // the more useful number for both the dashboard and the D10 QoS
    // before/after test (see pc/qos_before_after.py).
    int64_t last = s.last_us;
    s = RttStats();
    s.last_us = last;
  }

  if (now - t_domain >= 500) {  // 2 Hz virtual domain traffic
    t_domain = now;
    char dbuf[80];
    snprintf(dbuf, sizeof(dbuf), "uptime_s=%lu free_heap=%u rtt_avg_ms=%.2f rtt_jitter_ms=%.2f",
             (unsigned long)(now / 1000), (unsigned)ESP.getFreeHeap(),
             s_rtt.count ? (double)s_rtt.sum_us / s_rtt.count / 1000.0 : 0.0,
             s_rtt.count > 1 ? (double)s_rtt.jitter_sum_us / (s_rtt.count - 1) / 1000.0 : 0.0);
    z_owned_bytes_t diag_payload;
    z_bytes_copy_from_str(&diag_payload, dbuf);
    z_publisher_put(z_publisher_loan(&s_diag_pub), z_bytes_move(&diag_payload), NULL);

    if (NODE_ID == 1) {
      // Synthetic but labeled: a smooth 0-120 km/h sweep, wheel speed
      // running slightly ahead of vehicle speed (a plausible, if fake,
      // slip figure) rather than two identical numbers under two names.
      float vehicle_kmh = 60.0f + 60.0f * sinf(now / 4000.0f);
      float wheel_kmh = vehicle_kmh * 1.02f;
      char wbuf[32], vbuf[32];
      snprintf(wbuf, sizeof(wbuf), "%.1f", wheel_kmh);
      snprintf(vbuf, sizeof(vbuf), "%.1f", vehicle_kmh);
      z_owned_bytes_t wheel_payload, vehicle_payload;
      z_bytes_copy_from_str(&wheel_payload, wbuf);
      z_bytes_copy_from_str(&vehicle_payload, vbuf);
      z_publisher_put(z_publisher_loan(&s_chassis_wheel_pub), z_bytes_move(&wheel_payload), NULL);
      z_publisher_put(z_publisher_loan(&s_chassis_vehicle_pub), z_bytes_move(&vehicle_payload), NULL);
    } else if (NODE_ID == 2) {
      // Door lock state, flipping every ~6s -- a body-domain event stream
      // rather than a periodic sensor value.
      const char *state = ((now / 6000) % 2 == 0) ? "locked" : "unlocked";
      z_owned_bytes_t body_payload;
      z_bytes_copy_from_str(&body_payload, state);
      z_publisher_put(z_publisher_loan(&s_body_pub), z_bytes_move(&body_payload), NULL);
    }
  }

  if (now - t_hello >= 2000) {
    t_hello = now;
    char buf[64];
    snprintf(buf, sizeof(buf), "[esp32-%d %4u] hello from W5500", NODE_ID, (unsigned)s_idx++);

    z_owned_bytes_t payload;
    z_bytes_copy_from_str(&payload, buf);
    if (z_publisher_put(z_publisher_loan(&s_pub), z_bytes_move(&payload), NULL) < 0) {
      Serial.println("[zenoh] publish failed");
    } else {
      Serial.printf("[pub] %s\n", buf);
    }
  }
}
