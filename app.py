"""
YT-MP3 — récupère des MP3 depuis YouTube à partir d'une liste de chansons
ou d'une playlist Spotify / Deezer / YouTube.
Interface web simple, file d'attente persistante, tags ID3 propres et
playlists .m3u importées automatiquement par Navidrome.
"""
import json
import os
import re
import threading
import queue
import shutil
import time
from glob import escape as glob_escape
import urllib.request
import uuid
from pathlib import Path

import yt_dlp
from flask import Flask, jsonify, request
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# Dossiers de l'hôte visibles dans le conteneur (montés au même chemin) et explorables dans les réglages
BROWSE_ROOTS = [Path(p.strip()) for p in os.environ.get("BROWSE_ROOTS", "/DATA,/media").split(",") if p.strip()]
_env_music = os.environ.get("MUSIC_DIR", "/DATA/Media/Music")
_env_sub = os.environ.get("SUBFOLDER", "").strip()
DEFAULT_SETTINGS = {
    "music_dir": str(Path(_env_music) / _env_sub) if _env_sub else _env_music,
    "layout": os.environ.get("LAYOUT", "artist"),          # flat | artist | artist_album
    "playlist_dir": os.environ.get("PLAYLIST_DIR", "Playlists").strip() or "Playlists",
}
LAYOUTS = {
    "flat": "Tout dans le même dossier",
    "artist": "Artiste / Artiste - Titre.mp3",
    "artist_album": "Artiste / Album / Artiste - Titre.mp3",
}
SETTINGS_FILE = DATA_DIR / "settings.json"
TMP_DIR = DATA_DIR / "tmp"
QUALITY = os.environ.get("MP3_QUALITY", "192")
DEFAULT_ALBUM = os.environ.get("DEFAULT_ALBUM", "YouTube")
MAX_DURATION = int(os.environ.get("MAX_DURATION", "900"))  # secondes, 0 = pas de limite
SEARCH_SUFFIX = os.environ.get("SEARCH_SUFFIX", "audio")
SEARCH_RESULTS = int(os.environ.get("SEARCH_RESULTS", "6"))
STATE_FILE = DATA_DIR / "jobs.json"

app = Flask(__name__)
lock = threading.Lock()
jobs: list[dict] = []
imports: list[dict] = []
work_queue: "queue.Queue[str]" = queue.Queue()
ACTIVE = ("pending", "searching", "downloading")


# ---------------------------------------------------------------- persistance
def save_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with lock:
        payload = json.dumps({"jobs": jobs, "imports": imports}, ensure_ascii=False, indent=1)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_state():
    global jobs, imports
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):  # ancien format
                data = {"jobs": data, "imports": []}
            jobs, imports = data.get("jobs", []), data.get("imports", [])
        except Exception:
            jobs, imports = [], []
    for j in jobs:  # reprise après redémarrage
        if j["status"] in ACTIVE:
            j["status"] = "pending"
            j["progress"] = 0
            work_queue.put(j["id"])
    for imp in imports:  # une analyse interrompue ne reprend pas toute seule
        if imp["status"] == "resolving":
            imp.update(status="error", message="Interrompu par un redémarrage — recolle le lien")


# ---------------------------------------------------------------- réglages
settings: dict = dict(DEFAULT_SETTINGS)


def load_settings():
    global settings
    try:
        settings = {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except Exception:
        settings = dict(DEFAULT_SETTINGS)


def music_dir() -> Path:
    return Path(settings["music_dir"])


def playlist_folder() -> Path:
    return music_dir() / settings["playlist_dir"]


def inside_roots(p: Path) -> bool:
    rp = p.resolve()
    return any(rp == r.resolve() or r.resolve() in rp.parents for r in BROWSE_ROOTS)


def check_writable(p: Path):
    if not inside_roots(p):
        raise ValueError(f"Le dossier doit être dans : {', '.join(map(str, BROWSE_ROOTS))}")
    p.mkdir(parents=True, exist_ok=True)
    test = p / ".yt-mp3-test"
    try:
        test.write_text("ok")
        test.unlink()
    except OSError as e:
        raise ValueError(f"Impossible d'écrire dans {p} : {e.strerror}") from e


def get_job(job_id):
    with lock:
        return next((j for j in jobs if j["id"] == job_id), None)


def update(obj, **kw):
    with lock:
        obj.update(kw)


# ---------------------------------------------------------------- utilitaires
URL_RE = re.compile(r"^https?://", re.I)
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def http_get(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def http_json(url: str):
    data = json.loads(http_get(url)[1])
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(f"Deezer : {err.get('message', err) if isinstance(err, dict) else err}")
    return data


def safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().rstrip(".")
    return name[:150] or "Inconnu"


def first_artist(s: str) -> str:
    return re.split(r"\s*(?:,|;|&| feat\.? | ft\.? | x )\s*", s.strip(), maxsplit=1, flags=re.I)[0].strip()


def parse_line(line: str):
    """'Artiste - Titre [- Album]'  ou  'Titre'  ou une URL YouTube."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if URL_RE.match(line):
        return {"url": line, "artist": "", "title": "", "album": "", "label": line}
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", line, maxsplit=2)]
    if len(parts) == 1:
        return {"url": "", "artist": "", "title": parts[0], "album": ""}
    return {
        "url": "",
        "artist": parts[0],
        "title": parts[1],
        "album": parts[2] if len(parts) > 2 else "",
    }


NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|officiel|video|vidéo|clip|audio|lyrics?|paroles|visuali[sz]er|"
    r"\bhd\b|\bhq\b|4k|remaster)[^\)\]]*[\)\]]",
    re.I,
)


def clean_video_title(t: str) -> str:
    return re.sub(r"\s{2,}", " ", NOISE_RE.sub("", t or "")).strip(" -|")


def search_title(t: str) -> str:
    """'Song - Remastered 2011' -> 'Song' (pour la recherche uniquement)."""
    return re.sub(r"\s+-\s+.*remaster.*$", "", t, flags=re.I).strip()


def tag_mp3(path: Path, artist: str, title: str, album: str, year: str = ""):
    try:
        tags = EasyID3(str(path))
    except ID3NoHeaderError:
        from mutagen.mp3 import MP3
        audio = MP3(str(path))
        audio.add_tags()
        audio.save()
        tags = EasyID3(str(path))
    if artist:
        tags["artist"] = artist
        tags["albumartist"] = artist
    if title:
        tags["title"] = title
    tags["album"] = album or DEFAULT_ALBUM
    if year:
        tags["date"] = str(year)
    tags.save()


# ---------------------------------------------------------------- playlists
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(playlist|album|track)/([A-Za-z0-9]+)")
DEEZER_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?(playlist|album|track)/(\d+)")
SHORT_RE = re.compile(r"(spotify\.link|spotify\.app\.link|deezer\.page\.link|link\.deezer\.com)/", re.I)
YT_LIST_RE = re.compile(r"(youtube\.com/playlist\?|music\.youtube\.com/(playlist\?|browse/))", re.I)
SOURCE_LABEL = {"spotify": "Spotify", "deezer": "Deezer", "youtube": "YouTube"}


def classify(line: str):
    """Renvoie la source si la ligne est un lien de playlist/album/titre à importer."""
    line = line.strip()
    if not URL_RE.match(line):
        return None
    if SPOTIFY_RE.search(line) or "spotify.link" in line or "spotify.app.link" in line:
        return "spotify"
    if DEEZER_RE.search(line) or "deezer.page.link" in line or "link.deezer.com" in line:
        return "deezer"
    if YT_LIST_RE.search(line):
        return "youtube"
    return None


def expand_short(url: str, regex: re.Pattern):
    m = regex.search(url)
    if m or not SHORT_RE.search(url):
        return m
    final, body = http_get(url)  # suit les redirections
    return regex.search(final) or regex.search(body)


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def spotify_tracks(url: str):
    m = expand_short(url, SPOTIFY_RE)
    if not m:
        raise RuntimeError("Lien Spotify non reconnu")
    kind, sid = m.groups()
    _, page = http_get(f"https://open.spotify.com/embed/{kind}/{sid}")
    nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not nd:
        raise RuntimeError("Page Spotify illisible (playlist privée ou format changé)")
    data = json.loads(nd.group(1))

    if kind == "track":
        ent = next((d for d in walk(data) if d.get("type") == "track" and (d.get("name") or d.get("title"))), None)
        if not ent:
            raise RuntimeError("Titre Spotify introuvable")
        artists = ", ".join(a.get("name", "") for a in ent.get("artists") or [])
        title = ent.get("name") or ent.get("title")
        return title, [{"artist": first_artist(artists), "title": title, "album": "",
                        "duration": (ent.get("duration") or 0) // 1000}]

    ent = next((d for d in walk(data) if isinstance(d.get("trackList"), list)), None)
    if not ent:
        raise RuntimeError("Liste de titres Spotify introuvable (playlist privée ?)")
    name = ent.get("name") or ent.get("title") or f"Spotify {sid}"
    tracks = []
    for t in ent["trackList"]:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        tracks.append({
            "artist": first_artist(t.get("subtitle") or ""),
            "title": title,
            "album": name if kind == "album" else "",
            "duration": (t.get("duration") or 0) // 1000,
        })
    return name, tracks


def deezer_tracks(url: str):
    m = expand_short(url, DEEZER_RE)
    if not m:
        raise RuntimeError("Lien Deezer non reconnu")
    kind, did = m.groups()

    def conv(t, album=""):
        return {
            "artist": (t.get("artist") or {}).get("name", ""),
            "title": t.get("title") or t.get("title_short") or "",
            "album": (t.get("album") or {}).get("title") or album,
            "duration": t.get("duration") or 0,
        }

    if kind == "track":
        t = http_json(f"https://api.deezer.com/track/{did}")
        return t.get("title", "Deezer"), [conv(t)]

    meta = http_json(f"https://api.deezer.com/{kind}/{did}")
    name = meta.get("title") or f"Deezer {did}"
    tracks, nxt = [], f"https://api.deezer.com/{kind}/{did}/tracks?limit=100"
    while nxt:
        page = http_json(nxt)
        tracks += [conv(t, name if kind == "album" else "") for t in page.get("data", [])]
        nxt = page.get("next")
    return name, tracks


def youtube_tracks(url: str):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    name = re.sub(r"^(Album|Playlist)\s*-\s*", "", info.get("title") or "Playlist YouTube")
    tracks = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        tracks.append({
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "artist": "", "title": "", "album": "",
            "duration": e.get("duration") or 0,
            "label": e.get("title") or e["id"],
        })
    return name, tracks


RESOLVERS = {"spotify": spotify_tracks, "deezer": deezer_tracks, "youtube": youtube_tracks}


def resolve_import(imp):
    try:
        name, tracks = RESOLVERS[imp["source"]](imp["url"])
        if not tracks:
            raise RuntimeError("Aucun titre trouvé")
        name = imp.get("forced_name") or name
        playlist = name if imp.get("m3u") else ""
        for t in tracks:
            new_job(t, playlist=playlist, import_id=imp["id"])
        update(imp, status="ok", name=name, count=len(tracks), message="")
    except Exception as e:  # noqa: BLE001
        update(imp, status="error", message=str(e).replace("ERROR: ", "")[:300])
    save_state()


def write_m3u(name: str):
    if not name:
        return
    with lock:
        files = [j["file"] for j in jobs
                 if j.get("playlist") == name and j.get("file") and j["status"] in ("done", "exists")]
    folder = playlist_folder()
    folder.mkdir(parents=True, exist_ok=True)
    seen, lines = set(), ["#EXTM3U"]
    for f in files:
        if f not in seen:
            seen.add(f)
            lines.append(os.path.relpath(f, folder))
    (folder / f"{safe(name)}.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- recherche
BAD_WORDS = ["live", "cover", "karaoke", "instrumental", "remix", "8d", "sped up", "slowed",
             "nightcore", "reaction", "tutorial", "piano version", "acoustic"]


def pick(entries, job):
    """Choisit le meilleur résultat : bonne durée, pas de live/cover, chaîne officielle."""
    want = f"{job.get('title', '')} {job.get('album', '')}".lower()
    target = job.get("duration") or 0
    artist = (job.get("artist") or "").lower()
    best = None
    for rank, e in enumerate(entries):
        d = e.get("duration") or 0
        if MAX_DURATION and d > MAX_DURATION:
            continue
        t = (e.get("title") or "").lower()
        ch = (e.get("channel") or e.get("uploader") or "").lower()
        score = rank * 2
        for w in BAD_WORDS:
            if re.search(rf"\b{re.escape(w)}\b", t) and w not in want:
                score += 25
        if target and d:
            diff = abs(d - target)
            score += diff if diff <= 60 else 100 + diff
        if ch.endswith(" - topic"):
            score -= 6
        if artist and (artist in t or artist in ch):
            score -= 3
        if best is None or score < best[0]:
            best = (score, e)
    return best[1] if best else None


def guess_from_info(info, job):
    artist, title = job["artist"], job["title"]
    if not artist:
        artists = info.get("artists")
        artist = artists[0] if artists else first_artist(info.get("artist") or "")
    if not title:
        title = info.get("track") or ""
    if not artist or not title:
        vt = clean_video_title(info.get("title") or "")
        parts = re.split(r"\s+[-–—]\s+", vt, maxsplit=1)
        if len(parts) == 2:
            artist = artist or parts[0]
            title = title or parts[1]
        else:
            title = title or vt
    artist = artist or (info.get("uploader") or info.get("channel") or "Inconnu")
    return artist.removesuffix(" - Topic").strip(), (title or "Sans titre").strip()


# ---------------------------------------------------------------- traitement
def process(job):
    base_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    update(job, status="searching", message="Recherche…")

    if job["url"]:
        with yt_dlp.YoutubeDL({**base_opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(job["url"], download=False)
        if info and info.get("entries") is not None:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise RuntimeError("Vidéo introuvable")
            info = entries[0]
        duration = info.get("duration") or 0
        if MAX_DURATION and duration > MAX_DURATION:
            raise RuntimeError(f"Vidéo trop longue ({duration // 60} min) : « {info.get('title')} »")
        video_url = info.get("webpage_url") or job["url"]
        found = info.get("title", "")
        artist, title = guess_from_info(info, job)
    else:
        q = f"{job['artist']} {search_title(job['title'])} {SEARCH_SUFFIX}".strip()
        opts = {**base_opts, "skip_download": True, "extract_flat": "in_playlist"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{q}", download=False)
        entries = [e for e in (res or {}).get("entries") or [] if e and e.get("id")]
        if not entries:
            raise RuntimeError("Aucun résultat sur YouTube")
        best = pick(entries, job)
        if not best:
            raise RuntimeError("Seulement des vidéos trop longues trouvées")
        video_url = f"https://www.youtube.com/watch?v={best['id']}"
        found = best.get("title", "")
        if job["artist"]:
            artist, title = job["artist"], job["title"]
        else:
            artist, title = guess_from_info(best, job)

    update(job, found=found, video_url=video_url, artist=artist, title=title)

    # déjà présent ? (avec l'organisation de dossiers actuelle)
    root, layout = music_dir(), settings["layout"]
    base = safe(f"{artist} - {title}")
    if layout == "flat":
        existing = root / f"{base}.mp3"
        existing = existing if existing.exists() else None
    else:
        artist_dir = root / safe(artist)
        existing = next(artist_dir.glob(f"**/{glob_escape(base)}.mp3"), None) if artist_dir.is_dir() else None
    if existing:
        update(job, status="exists", progress=100, message="Déjà présent", file=str(existing))
        return

    # on télécharge dans un dossier temporaire, puis on range le fichier fini
    work = TMP_DIR / job["id"]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    tmp_mp3 = work / f"{base}.mp3"

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                update(job, progress=int(d.get("downloaded_bytes", 0) * 90 / total))
        elif d["status"] == "finished":
            update(job, progress=92, message="Conversion MP3…")

    update(job, status="downloading", message="Téléchargement…", progress=0)
    opts = {
        **base_opts,
        "format": "bestaudio/best",
        "outtmpl": str(work / f"{base}.%(ext)s"),
        "writethumbnail": True,
        "progress_hooks": [hook],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": QUALITY},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            full = ydl.extract_info(video_url, download=True) or {}
        if not tmp_mp3.exists():
            raise RuntimeError("Fichier MP3 introuvable après conversion")
        album = job.get("album") or full.get("album") or DEFAULT_ALBUM
        tag_mp3(tmp_mp3, artist, title, album, full.get("release_year") or "")

        if layout == "flat":
            folder = root
        elif layout == "artist_album":
            folder = root / safe(artist) / safe(album)
        else:
            folder = root / safe(artist)
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / f"{base}.mp3"
        shutil.move(str(tmp_mp3), str(final))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    update(job, status="done", progress=100, message="OK", file=str(final), album=album)


def worker():
    while True:
        job_id = work_queue.get()
        job = get_job(job_id)
        if not job or job["status"] != "pending":
            continue
        try:
            process(job)
        except Exception as e:  # noqa: BLE001
            msg = str(e).replace("ERROR: ", "")
            update(job, status="error", message=msg[:300])
        finally:
            update(job, updated=time.time())
            if job.get("playlist"):
                try:
                    write_m3u(job["playlist"])
                except Exception as e:  # noqa: BLE001
                    print(f"m3u : {e}", flush=True)
            save_state()
            time.sleep(2)  # on reste discret vis-à-vis de YouTube


def new_job(t: dict, playlist: str = "", import_id: str = ""):
    label = t.get("label") or (f"{t.get('artist')} - {t.get('title')}" if t.get("artist")
                               else t.get("title") or t.get("url", ""))
    job = {
        "id": uuid.uuid4().hex[:10],
        "line": label,
        "url": t.get("url", ""),
        "artist": (t.get("artist") or "").strip(),
        "title": (t.get("title") or "").strip(),
        "album": (t.get("album") or "").strip(),
        "duration": int(t.get("duration") or 0),
        "playlist": playlist,
        "import_id": import_id,
        "status": "pending",
        "progress": 0,
        "message": "En attente",
        "found": "",
        "file": "",
        "created": time.time(),
    }
    with lock:
        jobs.append(job)
    work_queue.put(job["id"])
    return job


# ---------------------------------------------------------------- API
@app.get("/api/jobs")
def api_jobs():
    with lock:
        return jsonify({"jobs": list(reversed(jobs)), "imports": list(reversed(imports))})


@app.post("/api/jobs")
def api_add():
    body = request.get_json(silent=True) or {}
    playlist_name = (body.get("playlist") or "").strip()
    m3u = bool(body.get("m3u", True))
    added = started = 0

    # titres structurés (import CSV)
    for t in body.get("tracks") or []:
        if t.get("title"):
            new_job(t, playlist=playlist_name if m3u else "")
            added += 1

    plain_playlist = playlist_name if m3u else ""
    for line in (body.get("text") or "").splitlines():
        line = line.strip()
        src = classify(line)
        if src:
            imp = {
                "id": uuid.uuid4().hex[:10], "source": src, "url": line,
                "name": SOURCE_LABEL[src], "forced_name": playlist_name, "m3u": m3u,
                "status": "resolving", "count": 0, "message": "Analyse…", "created": time.time(),
            }
            with lock:
                imports.append(imp)
            threading.Thread(target=resolve_import, args=(imp,), daemon=True).start()
            started += 1
            continue
        p = parse_line(line)
        if p:
            new_job(p, playlist=plain_playlist)
            added += 1
    save_state()
    return jsonify({"added": added, "imports": started})


@app.post("/api/retry")
def api_retry():
    n = 0
    with lock:
        for j in jobs:
            if j["status"] == "error":
                j.update(status="pending", progress=0, message="En attente")
                work_queue.put(j["id"])
                n += 1
    save_state()
    return jsonify({"retried": n})


@app.post("/api/clear")
def api_clear():
    global jobs, imports
    with lock:
        jobs = [j for j in jobs if j["status"] in ACTIVE + ("error",)]
        live = {j.get("import_id") for j in jobs}
        imports = [i for i in imports if i["status"] == "resolving" or i["id"] in live]
    save_state()
    return jsonify({"ok": True})


@app.get("/api/settings")
def api_settings():
    return jsonify({**settings, "layouts": LAYOUTS, "roots": [str(r) for r in BROWSE_ROOTS if r.is_dir()]})


@app.post("/api/settings")
def api_settings_save():
    global settings
    body = request.get_json(silent=True) or {}
    new = dict(settings)
    if "music_dir" in body:
        p = Path(str(body["music_dir"]).strip() or "/")
        if not p.is_absolute():
            return jsonify({"error": "Le chemin doit commencer par /"}), 400
        try:
            check_writable(p)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        new["music_dir"] = str(p.resolve())
    if body.get("layout") in LAYOUTS:
        new["layout"] = body["layout"]
    if "playlist_dir" in body:
        pd = str(body["playlist_dir"]).strip().strip("/")
        if not pd or ".." in Path(pd).parts:
            return jsonify({"error": "Nom de dossier de playlists invalide"}), 400
        new["playlist_dir"] = pd
    settings = new
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
    return api_settings()


@app.get("/api/browse")
def api_browse():
    roots = [r for r in BROWSE_ROOTS if r.is_dir()]
    raw = request.args.get("path", "").strip()
    if not raw:
        return jsonify({"path": "", "parent": None, "dirs": [str(r) for r in roots], "writable": False})
    p = Path(raw)
    if not p.is_absolute() or not inside_roots(p) or not p.is_dir():
        return jsonify({"error": "Dossier inaccessible"}), 400
    p = p.resolve()
    try:
        dirs = sorted((d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    except OSError as e:
        return jsonify({"error": e.strerror}), 400
    parent = "" if any(p == r.resolve() for r in roots) else str(p.parent)
    return jsonify({"path": str(p), "parent": parent, "dirs": [str(p / d) for d in dirs],
                    "writable": os.access(p, os.W_OK)})


@app.post("/api/mkdir")
def api_mkdir():
    body = request.get_json(silent=True) or {}
    name = safe(str(body.get("name", "")))
    parent = Path(str(body.get("path", "")))
    if not parent.is_absolute() or not inside_roots(parent) or not parent.is_dir():
        return jsonify({"error": "Dossier parent inaccessible"}), 400
    try:
        (parent / name).mkdir(exist_ok=True)
    except OSError as e:
        return jsonify({"error": e.strerror}), 400
    return jsonify({"path": str((parent / name).resolve())})


@app.get("/")
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT MP3</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--txt:#e6e8ec;--mut:#8b93a3;--acc:#ff4d4d;--ok:#3ecf8e;--warn:#f5b041;--sp:#1ed760;--dz:#a238ff;--yt:#ff3d3d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h1 span{color:var(--acc)}.sub{color:var(--mut);margin:0 0 20px;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:18px}
textarea,input[type=text]{width:100%;background:#0c0e12;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:10px;font:14px/1.5 ui-monospace,Consolas,monospace}
textarea{min-height:170px;resize:vertical}
input[type=text]{font-family:inherit;padding:8px 10px;max-width:320px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px}
button,label.btn{background:#262b36;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;font:inherit}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button:hover,label.btn:hover{filter:brightness(1.15)}
label.chk{display:flex;gap:6px;align-items:center;color:var(--mut);font-size:13px;cursor:pointer}
.stats{color:var(--mut);font-size:13px;margin-left:auto}
.toast{color:var(--ok);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:8px 6px;border-top:1px solid var(--line);vertical-align:top}
.l{font-weight:600}.f{color:var(--mut);font-size:12px;word-break:break-all}.f a{color:var(--mut)}
.bar{height:5px;background:#0c0e12;border-radius:3px;overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;background:var(--acc)}
.st{white-space:nowrap;font-size:12px;padding:2px 8px;border-radius:99px;background:#262b36}
.pl{font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:99px;padding:0 7px;margin-left:6px;font-weight:400}
.src{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;color:#0f1115}
.src.spotify{background:var(--sp)}.src.deezer{background:var(--dz);color:#fff}.src.youtube{background:var(--yt);color:#fff}
.done,.exists,.ok{color:var(--ok)}.error{color:var(--acc)}.downloading,.searching,.resolving{color:var(--warn)}
.empty{color:var(--mut);text-align:center;padding:20px}
code{background:#0c0e12;padding:1px 5px;border-radius:4px}
#impcard,#setcard,#browser{display:none}
button.link{background:none;border:none;color:var(--txt);text-decoration:underline;padding:0 4px;font-size:13px}
.field{margin-top:12px}.field label{display:block;font-size:13px;color:var(--mut);margin-bottom:4px}
.hint{font-size:12px;color:var(--mut);margin-top:4px}
select{background:#0c0e12;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;width:100%}
#browser{margin-top:10px;border:1px solid var(--line);border-radius:8px;padding:10px;background:#12151b}
#b_list{max-height:260px;overflow:auto;margin-top:8px}
#b_list div{padding:6px 8px;border-radius:6px;cursor:pointer}#b_list div:hover{background:#262b36}
.toast.err{color:var(--acc)}
</style></head><body><main>
<h1>YT <span>MP3</span></h1>
<p class="sub">Une ligne par chanson : <code>Artiste - Titre</code> (option : <code>Artiste - Titre - Album</code>), une URL YouTube,
ou un lien de <b>playlist / album Spotify, Deezer ou YouTube</b>. Les lignes commençant par # sont ignorées.<br>
Musique : <code id="curdir">…</code> · Playlists Navidrome : <code id="curpl">…</code>
<button class="link" onclick="openSettings()">⚙ Réglages</button></p>

<div class="card" id="setcard">
<strong>Réglages de stockage</strong>
<div class="field"><label>Dossier de la musique</label>
 <div class="row" style="margin:0"><input type="text" id="s_dir" style="max-width:none;flex:1"><button onclick="browse($('s_dir').value)">Parcourir…</button></div>
 <div class="hint">Il doit être dans la bibliothèque de Navidrome (ou être le dossier de la bibliothèque lui-même).</div>
</div>
<div id="browser">
 <div class="crumb"><code id="b_path"></code></div>
 <div id="b_list"></div>
 <div class="row">
  <button onclick="browse(B.parent??'')" id="b_up">⬆ Dossier parent</button>
  <input type="text" id="b_new" placeholder="Nouveau dossier" style="max-width:180px"><button onclick="mkdir()">Créer</button>
  <button class="primary" style="margin-left:auto" onclick="pick()" id="b_pick">Choisir ce dossier</button>
 </div>
</div>
<div class="row">
 <div class="field" style="flex:1;min-width:220px"><label>Organisation des fichiers</label><select id="s_layout"></select></div>
 <div class="field" style="flex:1;min-width:220px"><label>Sous-dossier des playlists .m3u</label><input type="text" id="s_pl" style="max-width:none"></div>
</div>
<div class="row"><button class="primary" onclick="saveSettings()">Enregistrer</button><button onclick="$('setcard').style.display='none'">Fermer</button>
 <span id="s_msg" class="toast"></span></div>
</div>

<div class="card">
<textarea id="txt" placeholder="Daft Punk - Get Lucky&#10;Stromae - Alors on danse&#10;https://open.spotify.com/playlist/...&#10;https://www.deezer.com/fr/playlist/...&#10;https://www.youtube.com/playlist?list=..."></textarea>
<div class="row">
 <input type="text" id="plname" placeholder="Nom de playlist (optionnel)">
 <label class="chk"><input type="checkbox" id="m3u" checked> Créer la playlist dans Navidrome</label>
</div>
<div class="row">
 <button class="primary" onclick="add()">Télécharger</button>
 <label class="btn">Importer un fichier (.txt, .csv, .m3u)<input type="file" accept=".txt,.csv,.m3u,.m3u8" hidden onchange="loadFile(this)"></label>
 <span class="toast" id="toast"></span>
 <span class="stats" id="stats"></span>
</div></div>

<div class="card" id="impcard">
<strong>Imports de playlists</strong>
<table><tbody id="imps"></tbody></table>
</div>

<div class="card">
<div class="row" style="margin:0 0 8px">
 <strong>File d'attente</strong>
 <span style="margin-left:auto"></span>
 <button onclick="post('/api/retry')">Réessayer les erreurs</button>
 <button onclick="post('/api/clear')">Vider les terminés</button>
</div>
<table><tbody id="list"></tbody></table>
</div>
</main>
<script>
const LBL={pending:"En attente",searching:"Recherche",downloading:"Téléchargement",done:"Terminé",exists:"Déjà là",error:"Erreur",resolving:"Analyse…",ok:"Ajoutée"};
const SRC={spotify:"Spotify",deezer:"Deezer",youtube:"YouTube"};
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(m){$('toast').textContent=m;clearTimeout(toast.t);toast.t=setTimeout(()=>$('toast').textContent='',6000);}
async function send(payload){
 const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({...payload,playlist:$('plname').value.trim(),m3u:$('m3u').checked})});
 if(!r.ok){toast('Erreur serveur');return false;}
 const d=await r.json();const p=[];
 if(d.added)p.push(d.added+' titre(s) ajouté(s)');if(d.imports)p.push(d.imports+' playlist(s) en analyse');
 toast(p.join(' · ')||'Rien à ajouter');refresh();return true;}
async function add(){const t=$('txt');if(!t.value.trim())return;if(await send({text:t.value})){t.value='';$('plname').value='';}}
async function post(u){await fetch(u,{method:'POST'});refresh();}

function parseCSV(text){const rows=[];let row=[],f='',q=false;
 const sep=(text.split('\n')[0].split(';').length>text.split('\n')[0].split(',').length)?';':',';
 for(let i=0;i<text.length;i++){const c=text[i];
  if(q){if(c=='"'){if(text[i+1]=='"'){f+='"';i++;}else q=false;}else f+=c;}
  else if(c=='"')q=true;else if(c==sep){row.push(f);f='';}
  else if(c=='\n'||c=='\r'){if(c=='\r'&&text[i+1]=='\n')i++;row.push(f);rows.push(row);row=[];f='';}
  else f+=c;}
 if(f||row.length){row.push(f);rows.push(row);}return rows.filter(r=>r.some(x=>x.trim()));}
function csvTracks(text){const rows=parseCSV(text);if(!rows.length)return[];
 const h=rows[0].map(x=>x.trim().toLowerCase());
 const col=(...names)=>h.findIndex(x=>names.includes(x));
 let it=col('track name','title','titre','name'),ia=col('artist name(s)','artist','artiste','artist name'),
     ib=col('album name','album'),id=col('duration (ms)','duration_ms');
 let data=rows.slice(1);
 if(it<0||ia<0){it=1;ia=0;ib=2;id=-1;data=rows;}
 return data.map(r=>({artist:(r[ia]||'').split(/[,;]/)[0].trim(),title:(r[it]||'').trim(),
   album:ib>=0?(r[ib]||'').trim():'',duration:id>=0?Math.round((+r[id]||0)/1000):0})).filter(t=>t.title);}
function loadFile(inp){const f=inp.files[0];if(!f)return;const rd=new FileReader();
 rd.onload=async()=>{const name=f.name.replace(/\.[^.]+$/,'');
  if(!$('plname').value.trim())$('plname').value=name;
  if(/\.csv$/i.test(f.name)){const tr=csvTracks(rd.result);
   if(!tr.length){toast('CSV vide ou illisible');return;}
   await send({tracks:tr});$('plname').value='';return;}
  let lines=rd.result.split(/\r?\n/);
  if(/\.m3u8?$/i.test(f.name)){lines=lines.filter(l=>l.startsWith('#EXTINF')).map(l=>l.replace(/^#EXTINF:[^,]*,/,''));}
  const t=$('txt');t.value=(t.value?t.value+'\n':'')+lines.join('\n');};
 rd.readAsText(f);inp.value='';}

async function refresh(){try{const d=await (await fetch('/api/jobs')).json();const js=d.jobs,im=d.imports;
 const c={};js.forEach(j=>c[j.status]=(c[j.status]||0)+1);
 $('stats').textContent=`${(c.done||0)+(c.exists||0)} ok · ${(c.pending||0)+(c.searching||0)+(c.downloading||0)} en cours · ${c.error||0} erreur(s)`;
 $('impcard').style.display=im.length?'block':'none';
 $('imps').innerHTML=im.map(i=>{const mine=js.filter(j=>j.import_id===i.id);
   const ok=mine.filter(j=>j.status==='done'||j.status==='exists').length,er=mine.filter(j=>j.status==='error').length;
   return `<tr><td style="width:1%"><span class="src ${i.source}">${SRC[i.source]}</span></td>
   <td><div class="l">${esc(i.name)}</div><div class="f">${esc(i.url)}${i.status==='error'?'<br>'+esc(i.message):''}</div></td>
   <td style="text-align:right;white-space:nowrap">${i.status==='ok'?`<span class="f">${ok}/${i.count}${er?' · '+er+' err.':''}</span> `:''}
   <span class="st ${i.status}">${LBL[i.status]||i.status}</span></td></tr>`;}).join('');
 $('list').innerHTML=js.length?js.map(j=>`<tr>
  <td><div class="l">${esc(j.artist?j.artist+' - '+j.title:(j.title||j.line))}${j.playlist?`<span class="pl">${esc(j.playlist)}</span>`:''}</div>
   <div class="f">${j.found?'▶ '+(j.video_url?`<a href="${esc(j.video_url)}" target="_blank">${esc(j.found)}</a>`:esc(j.found)):''}${j.status==='error'?'<br>'+esc(j.message):''}</div>
   ${['downloading','searching'].includes(j.status)?`<div class="bar"><i style="width:${j.progress||0}%"></i></div>`:''}</td>
  <td style="text-align:right"><span class="st ${j.status}">${LBL[j.status]||j.status}</span></td></tr>`).join('')
  :'<tr><td class="empty">Rien pour l\'instant — colle une liste ou un lien de playlist ci-dessus.</td></tr>';}catch(e){}}
let S={},B={};
function smsg(m,err){const e=$('s_msg');e.textContent=m;e.className='toast'+(err?' err':'');}
function showSettings(){$('curdir').textContent=S.music_dir;$('curpl').textContent=S.music_dir+'/'+S.playlist_dir;}
async function loadSettings(){S=await (await fetch('/api/settings')).json();showSettings();}
function openSettings(){$('s_dir').value=S.music_dir;$('s_pl').value=S.playlist_dir;
 $('s_layout').innerHTML=Object.entries(S.layouts).map(([k,v])=>`<option value="${k}"${k===S.layout?' selected':''}>${esc(v)}</option>`).join('');
 $('browser').style.display='none';smsg('');$('setcard').style.display='block';}
async function browse(p){const r=await fetch('/api/browse?path='+encodeURIComponent(p||''));let d=await r.json();
 if(!r.ok){if(p){return browse('');}smsg(d.error,true);return;}
 B=d;$('browser').style.display='block';
 $('b_path').textContent=d.path||'Emplacements disponibles';
 $('b_up').disabled=d.parent===null;$('b_pick').disabled=!d.path;
 $('b_list').innerHTML=d.dirs.length?d.dirs.map(x=>`<div data-p="${esc(x)}">📁 ${esc(d.path?x.split('/').pop():x)}</div>`).join(''):'<div class="hint">Aucun sous-dossier</div>';
 $('b_list').querySelectorAll('[data-p]').forEach(el=>el.onclick=()=>browse(el.dataset.p));}
async function mkdir(){const n=$('b_new').value.trim();if(!n||!B.path)return;
 const r=await fetch('/api/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:B.path,name:n})});
 const d=await r.json();if(!r.ok){smsg(d.error,true);return;}$('b_new').value='';browse(d.path);}
function pick(){if(!B.path)return;$('s_dir').value=B.path;$('browser').style.display='none';smsg('Dossier sélectionné — pense à enregistrer');}
async function saveSettings(){const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({music_dir:$('s_dir').value,layout:$('s_layout').value,playlist_dir:$('s_pl').value})});
 const d=await r.json();if(!r.ok){smsg(d.error,true);return;}S=d;showSettings();smsg('Enregistré ✔ — les prochains téléchargements iront ici');}
loadSettings();refresh();setInterval(refresh,2000);
</script></body></html>"""


if __name__ == "__main__":
    load_settings()
    try:
        music_dir().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Attention : dossier musique {music_dir()} inaccessible ({e.strerror})", flush=True)
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    load_state()
    threading.Thread(target=worker, daemon=True).start()
    from waitress import serve
    print(f"YT-MP3 prêt sur :8080 — musique dans {music_dir()}", flush=True)
    serve(app, host="0.0.0.0", port=8080, threads=4)
