#!/usr/bin/env bash
# Fetches the two standalone Rust binaries the DDS bridge needs, pinned to
# 1.10.1 to match the `eclipse-zenoh` Python version pc/*.py was built
# against (the zenoh wire protocol isn't guaranteed compatible across
# minor versions). Not vendored in git -- just release zips, re-fetched
# here into tools/ (gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=1.10.1
mkdir -p tools

fetch() {
    local repo=$1 name=$2 dest=$3
    if [ -x "$dest" ]; then
        echo "skip (exists): $dest"
        return
    fi
    local url="https://github.com/eclipse-zenoh/${repo}/releases/download/${VERSION}/${name}-${VERSION}-x86_64-unknown-linux-gnu-standalone.zip"
    local tmp
    tmp=$(mktemp -d)
    curl -sL -o "$tmp/dl.zip" "$url"
    unzip -oq "$tmp/dl.zip" -d "$tmp/out"
    mkdir -p "$(dirname "$dest")"
    cp "$tmp/out/$(basename "$dest")" "$dest"
    chmod +x "$dest"
    rm -rf "$tmp"
    echo "fetched: $dest"
}

fetch zenoh zenoh tools/zenohd/zenohd
fetch zenoh-plugin-dds zenoh-plugin-dds tools/zenoh-bridge-dds/zenoh-bridge-dds

cat << 'EOF'

Run (in order, each in its own terminal or backgrounded):
  tools/zenohd/zenohd -l udp/<pc-ip>:7447
  tools/zenoh-bridge-dds/zenoh-bridge-dds -e udp/<pc-ip>:7447
  python3 pc/dds_adapter.py     # after editing its connect endpoint
  python3 pc/zenoh_peer.py      # after editing its connect endpoint

Then any DDS/ROS2 participant sees the ESP32 boards' ivn/**, bridge/**
and test/stats/** samples as topics /ivn/..., /bridge/..., /test/...
(std_msgs/String) -- e.g.:
  source /opt/ros/jazzy/setup.bash   # or whatever ROS2/Fast DDS setup
  ros2 topic echo /ivn/chassis/wheel_speed std_msgs/msg/String
EOF
