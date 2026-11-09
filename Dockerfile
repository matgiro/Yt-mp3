FROM python:3.12-slim
LABEL org.opencontainers.image.source=https://github.com/matgiro/Yt-mp3

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp a besoin d'un moteur JavaScript (Deno) pour bien gérer YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.sh ./

ENV MUSIC_DIR=/music DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "/app/entrypoint.sh"]
