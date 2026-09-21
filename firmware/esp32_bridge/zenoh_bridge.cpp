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
#include <esp_timer.h>
#include <zenoh-pico.h>

#include "eth_w5500.h"

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
static z_owned_subscriber_t s_sub;
static z_owned_subscriber_t s_pong_sub;
static bool s_zenoh_up = false;
static uint32_t s_idx = 0;
static uint32_t s_ping_seq = 0;

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

  s_rtt = RttStats();
  Serial.println("[zenoh] session up: pub=" PUB_KEYEXPR " sub=" SUB_KEYEXPR " ping=" PING_KEYEXPR);
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
}

void zenohBridgeLoop() {
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

  static uint32_t t_hello = 0, t_ping = 0, t_stats = 0;
  uint32_t now = millis();

  if (now - t_ping >= 200) {  // 5 Hz RTT probe
    t_ping = now;
    char pbuf[48];
    snprintf(pbuf, sizeof(pbuf), "%u %lld", (unsigned)s_ping_seq++, (long long)esp_timer_get_time());
    z_owned_bytes_t ping_payload;
    z_bytes_copy_from_str(&ping_payload, pbuf);
    z_publisher_put(z_publisher_loan(&s_ping_pub), z_bytes_move(&ping_payload), NULL);
  }

  if (now - t_stats >= 1000 && s_rtt.count > 0) {  // 1 Hz stats report
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
