#!/bin/bash
set -e
echo "=== WHOAMI ===" && whoami
echo "=== APT INSTALL ==="
apt-get update -y
apt-get install -y ffmpeg sox
echo "=== WHICH SOX ===" && which sox || echo "SOX NOT IN PATH"
echo "=== LS SOX ===" && ls -la /usr/bin/sox || echo "SOX NOT IN /usr/bin"
echo "=== SOX VERSION ===" && sox --version || echo "SOX VERSION FAILED"
pip install -r requirements.txt
