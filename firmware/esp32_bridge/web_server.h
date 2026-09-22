// Minimal embedded HTTP server, common to every board (NODE_ID 1/2/3...).
// Runs over the same W5500/esp_netif interface as everything else --
// Arduino's WebServer class binds to whatever the default TCP/IP stack is,
// it doesn't care whether that's WiFi or esp_eth underneath. This is the
// "나중에는 랜으로 다 되게" (LAN-only control, no PC/zenoh/USB needed) path:
// point a browser at the board's own IP and see its status / flip its LED
// directly, independent of whether zenohd or this session's PC scripts are
// even running.
#pragma once
#include <Arduino.h>
#include <WebServer.h>

#include "led_control.h"

static WebServer gWebServer(80);

inline String webServerStatusPage() {
  String html;
  html.reserve(768);
  html += "<!doctype html><html><head><meta charset=utf-8>";
  html += "<meta name=viewport content='width=device-width,initial-scale=1'>";
  html += "<title>esp32-" + String(NODE_ID) + "</title>";
  html += "<style>body{font-family:-apple-system,system-ui,sans-serif;background:#f5f5f7;"
          "color:#1d1d1f;max-width:420px;margin:40px auto;padding:0 20px}"
          "h1{font-size:1.2rem}.row{display:flex;justify-content:space-between;padding:8px 0;"
          "border-bottom:1px solid #e5e5e7;font-size:0.9rem}"
          "a.btn{display:inline-block;margin:4px 6px 0 0;padding:8px 16px;border-radius:8px;"
          "background:#0071e3;color:white;text-decoration:none;font-size:0.85rem}</style></head><body>";
  html += "<h1>esp32-" + String(NODE_ID) + "</h1>";
  html += "<div class=row><span>IP</span><span>" + ethLocalIP().toString() + "</span></div>";
  html += "<div class=row><span>MAC</span><span>" + ethMacAddress() + "</span></div>";
  html += "<div class=row><span>Eth link</span><span>" + String(ethLinkUp() ? "up" : "down") + "</span></div>";
  html += "<div class=row><span>Uptime</span><span>" + String(millis() / 1000) + "s</span></div>";
  html += "<div class=row><span>LED</span><span>" + String(ledControlGet() ? "on" : "off") + "</span></div>";
  html += "<p><a class=btn href='/led/on'>LED on</a><a class=btn href='/led/off'>LED off</a>"
          "<a class=btn href='/led/toggle'>Toggle</a></p>";
  html += "</body></html>";
  return html;
}

inline void webServerSetup() {
  gWebServer.on("/", HTTP_GET, []() {
    gWebServer.send(200, "text/html", webServerStatusPage());
  });
  gWebServer.on("/led/on", HTTP_GET, []() {
    ledControlHandleCommand("on");
    gWebServer.sendHeader("Location", "/");
    gWebServer.send(303);
  });
  gWebServer.on("/led/off", HTTP_GET, []() {
    ledControlHandleCommand("off");
    gWebServer.sendHeader("Location", "/");
    gWebServer.send(303);
  });
  gWebServer.on("/led/toggle", HTTP_GET, []() {
    ledControlHandleCommand("toggle");
    gWebServer.sendHeader("Location", "/");
    gWebServer.send(303);
  });
  gWebServer.begin();
  Serial.printf("[web] server up: http://%s/\n", ethLocalIP().toString().c_str());
}

inline void webServerLoop() { gWebServer.handleClient(); }
