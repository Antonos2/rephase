#!/bin/bash
set -e
apt-get update -y
apt-get install -y ffmpeg sox
pip install -r requirements.txt
