#!/bin/sh
# YouTube change souvent : on met yt-dlp à jour à chaque démarrage du conteneur.
pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "Mise à jour yt-dlp impossible, on continue avec la version installée"
exec python /app/app.py
