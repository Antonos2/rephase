#!/bin/bash
set -e

echo "==> System info"
uname -a
whoami
echo "PATH=$PATH"

echo "==> Installing ffmpeg and sox"
apt-get update -y || sudo apt-get update -y
apt-get install -y ffmpeg sox libsox-fmt-all || sudo apt-get install -y ffmpeg sox libsox-fmt-all

echo "==> Verifying sox installation"
which sox || echo "SOX NOT IN PATH"
ls -la /usr/bin/sox 2>/dev/null || echo "SOX NOT IN /usr/bin"
sox --version 2>&1 || echo "SOX VERSION FAILED"

echo "==> Verifying ffmpeg installation"
which ffmpeg || echo "FFMPEG NOT IN PATH"
ffmpeg -version 2>&1 | head -1 || echo "FFMPEG VERSION FAILED"

echo "==> Installing Python dependencies"
pip install -r requirements.txt

echo "==> Build complete"
