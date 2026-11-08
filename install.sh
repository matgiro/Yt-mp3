#!/bin/sh
# Installation en une commande :  sudo sh install.sh
set -e
cd "$(dirname "$0")"
mkdir -p /DATA/Media/Music /DATA/AppData/yt-mp3 /media
docker compose up -d --build
echo ""
echo "✅ YT MP3 tourne : http://$(hostname -I | awk '{print $1}'):8095"
