#!/bin/sh
# 1) YouTube change souvent : on met yt-dlp à jour à chaque démarrage.
pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "Mise à jour yt-dlp impossible, on continue avec la version installée"

# 2) Mise à jour automatique de l'appli : on récupère la dernière version de app.py sur GitHub.
#    Un simple redémarrage de l'appli dans CasaOS suffit donc pour avoir la dernière version.
if [ "${AUTO_UPDATE:-1}" = "1" ]; then
python - <<'PY'
import py_compile, shutil, urllib.request
url = "https://raw.githubusercontent.com/matgiro/Yt-mp3/main/app.py"
try:
    data = urllib.request.urlopen(url, timeout=20).read()
    open("/tmp/app_new.py", "wb").write(data)
    py_compile.compile("/tmp/app_new.py", doraise=True)
    shutil.copy("/tmp/app_new.py", "/app/app.py")
    print("app.py mis à jour depuis GitHub", flush=True)
except Exception as e:
    print(f"Mise à jour de app.py impossible ({e}), on garde la version de l'image", flush=True)
PY
fi
exec python /app/app.py
