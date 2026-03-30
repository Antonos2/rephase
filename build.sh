#!/usr/bin/env bash
set -e

echo "==> Installing system dependencies: ffmpeg, sox"
apt-get update -qq && apt-get install -y --no-install-recommends ffmpeg sox

echo "==> Installing Python dependencies"
pip install -r requirements.txt

echo "==> Build complete"
echo "    ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
echo "    sox:    $(sox --version 2>&1)"
