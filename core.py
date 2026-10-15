"""Platform-neutral logic: read playlists, match songs on YouTube, download MP3s.

This is the desktop script's matching/parsing code, with two changes for Android:
  * yt-dlp is used as a Python library (there is no `python -m yt_dlp` on a phone).
  * Finished files are handed to a `Target` (see storage.py) instead of written
    straight to a folder, because Android only lets apps write to a folder the
    user picked through the system folder picker.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import ssl
import threading
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse

APPLE_HOSTS = {"music.apple.com", "embed.music.apple.com"}
SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com"}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class PlaylistError(Exception):
    """A user-facing playlist, search, dependency, or download problem."""


SongError = PlaylistError


class Cancelled(Exception):
    pass


CANCEL = threading.Event()


class Ctx(threading.local):
    n = t = 0


ctx = Ctx()
_PL = threading.Lock()


def _console(msg, err=False):
    with _PL:
        print(msg, flush=True)


SINK = _console
STATUS = lambda n, text, kind="": None
PROGRESS = lambda res, total: None


def emit(msg="", err=False):
    SINK((f"[{ctx.n}/{ctx.t}] " if ctx.n else "") + msg, err)


def note(text, kind=""):
    if ctx.n:
        STATUS(ctx.n, text, kind)


def stage(n, text):
    emit(f"{n}/4 {text}")
    note(f"Searching {n}/4")


def stop():
    CANCEL.set()


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


class Limiter:
    def __init__(self, n):
        self.n, self.busy, self.cv = n, 0, threading.Condition()

    def set(self, n):
        with self.cv:
            self.n = n
            self.cv.notify_all()

    def acquire(self):
        with self.cv:
            while self.busy >= self.n:
                if CANCEL.is_set():
                    return False
                self.cv.wait(0.3)
            self.busy += 1
            return True

    def release(self):
        with self.cv:
            self.busy -= 1
            self.cv.notify_all()


# --------------------------------------------------------------------------
# Reading the playlist
# --------------------------------------------------------------------------

def check_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in APPLE_HOSTS:
        raise PlaylistError("That does not look like an Apple Music link (expected music.apple.com).")
    if "/playlist/" not in parsed.path:
        raise PlaylistError("That link is not an Apple Music playlist link.")
    return parsed.geturl()


def fetch_page(url: str) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    # Android's Python has no system CA bundle, so certifi goes first.
    contexts = []
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass
    contexts.append(ssl.create_default_context())

    last_error = None
    for context in contexts:
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise PlaylistError(f"Music service returned HTTP {exc.code}. Is the link public?") from exc
        except urllib.error.URLError as exc:
            last_error = getattr(exc, "reason", exc)
            if "CERTIFICATE_VERIFY_FAILED" not in str(last_error) and \
                    not isinstance(last_error, ssl.SSLCertVerificationError):
                raise PlaylistError(f"Could not reach music service: {last_error}") from exc
        except OSError as exc:
            last_error = exc
    raise PlaylistError(f"Could not reach music service: {last_error}") from last_error


def walk(node):
    """Yield every dict inside nested JSON."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def script_blocks(page: str, pattern: str) -> list:
    found = []
    for raw in re.findall(pattern, page, flags=re.S | re.I):
        try:
            found.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return found


def tracks_from_server_data(page: str) -> list[tuple[str, str]]:
    blocks = script_blocks(
        page, r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>')
    tracks, seen = [], set()
    for block in blocks:
        for node in walk(block):
            title, artist = node.get("title"), node.get("artistName")
            if not isinstance(title, str) or not isinstance(artist, str):
                continue
            ident = str(node.get("id") or "")
            descriptor = node.get("contentDescriptor")
            kind = descriptor.get("kind") if isinstance(descriptor, dict) else None
            if not (ident.startswith("track-lockup") or kind == "song"):
                continue
            key = ident or (title.casefold(), artist.casefold())
            if key in seen:
                continue
            seen.add(key)
            tracks.append((title.strip(), artist.strip()))
    return tracks


def artist_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return artist_text(value.get("name"))
    if isinstance(value, list):
        return ", ".join(a for a in (artist_text(v) for v in value) if a)
    return ""


def tracks_from_json_ld(page: str) -> list[tuple[str, str]]:
    blocks = script_blocks(
        page, r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>')
    tracks = []
    for block in blocks:
        for node in walk(block):
            listing = node.get("track")
            if not isinstance(listing, list):
                continue
            playlist_artist = artist_text(node.get("byArtist"))
            for item in listing:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    artist = artist_text(item.get("byArtist")) or playlist_artist
                    tracks.append((item["name"].strip(), artist))
    return tracks


def playlist_name(page: str) -> str:
    patterns = (r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"',
                r'<meta[^>]+content="([^"]*)"[^>]+property="og:title"',
                r"<title>(.*?)</title>")
    for pattern in patterns:
        match = re.search(pattern, page, flags=re.S | re.I)
        if match:
            name = html.unescape(match.group(1)).strip()
            name = re.sub(r"\s*(?:on|-)\s*Apple\s*Music\s*$", "", name, flags=re.I).strip()
            if name:
                return name
    return "Music playlist"


def read_playlist(url: str) -> tuple[str, list[tuple[str, str]]]:
    page = fetch_page(check_url(url))
    tracks = tracks_from_server_data(page) or tracks_from_json_ld(page)
    if not tracks:
        raise PlaylistError(
            "Found the page but could not read any songs from it. Apple may have changed "
            "its page, or the playlist may be private.")
    return playlist_name(page), tracks


class _SpotifyScripts(HTMLParser):
    """Read script payloads regardless of quote style or attribute order."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.attrs = None
        self.parts = []
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.attrs = dict(attrs)
            self.parts = []

    def handle_data(self, data):
        if self.attrs is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.attrs is not None:
            attrs = self.attrs
            if attrs.get("id") == "__NEXT_DATA__" or attrs.get("type") == "application/ld+json":
                try:
                    self.blocks.append(json.loads("".join(self.parts)))
                except json.JSONDecodeError:
                    pass
            self.attrs = None


def check_spotify_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in SPOTIFY_HOSTS:
        raise PlaylistError("Use a Spotify link beginning https://open.spotify.com/.")
    match = re.fullmatch(
        r"/(?:intl-[A-Za-z-]+/)?(?:embed/)?(playlist|album|track)/([A-Za-z0-9]{22})/?",
        parsed.path)
    if not match:
        raise PlaylistError("Spotify URL must identify a playlist, album, or track.")
    return "https://open.spotify.com/" + "/".join(match.groups())


def spotify_entity(page: str, expected_uri: str = "") -> dict | None:
    parser = _SpotifyScripts()
    parser.feed(page)
    for block in parser.blocks:
        for node in walk(block):
            if expected_uri:
                if node.get("uri") != expected_uri:
                    continue
            elif node.get("type") not in {"track", "album", "playlist"}:
                continue
            if node.get("title") or node.get("name"):
                return node
    return None


def _spotify_rows(entity: dict) -> list[tuple[str, str]]:
    rows = entity.get("trackList")
    if not isinstance(rows, list):
        if entity.get("type") == "track" or str(entity.get("uri", "")).startswith("spotify:track:"):
            rows = [entity]
        else:
            return []
    tracks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uri = str(row.get("uri") or "")
        kind = row.get("entityType") or row.get("type")
        if (uri and not uri.startswith("spotify:track:")) or kind in {"episode", "show", "audiobook"}:
            continue
        title = row.get("title") or row.get("name")
        artist = artist_text(row.get("artists")) or artist_text(row.get("subtitle"))
        if not isinstance(title, str) or not title.strip() or not artist:
            raise PlaylistError("Spotify returned a track without a title or artist; cannot match it reliably.")
        tracks.append((title.strip(), artist))
    return tracks


def tracks_from_spotify_page(page: str, expected_uri: str = "") -> list[tuple[str, str]]:
    entity = spotify_entity(page, expected_uri)
    if entity is not None:
        return _spotify_rows(entity)
    parser = _SpotifyScripts()
    parser.feed(page)
    tracks = []
    for block in parser.blocks:
        for node in walk(block):
            if node.get("@type") != "MusicRecording":
                continue
            title, artist = node.get("name"), artist_text(node.get("byArtist"))
            if isinstance(title, str) and title.strip() and artist:
                tracks.append((title.strip(), artist))
    return tracks


def read_spotify(url: str) -> tuple[str, list[tuple[str, str]]]:
    canonical = check_spotify_url(url)
    kind, ident = urlparse(canonical).path.strip("/").split("/")
    uri = f"spotify:{kind}:{ident}"
    page = fetch_page(f"https://open.spotify.com/embed/{kind}/{ident}?utm_source=generator")
    entity = spotify_entity(page, uri)
    tracks = tracks_from_spotify_page(page, uri)
    if not tracks:
        raise PlaylistError(
            "Spotify's embed player returned no readable songs for this link. "
            "The playlist may be private, empty, unavailable, or its page format may have changed.")
    if kind == "track" and len(tracks) != 1:
        raise PlaylistError("Spotify returned ambiguous data for this single-track link.")
    if entity:
        for field in ("trackCount", "totalTracks"):
            total = entity.get(field)
            if isinstance(total, int) and total > len(tracks):
                raise PlaylistError(f"Spotify exposed only {len(tracks)} of {total} tracks; "
                                    "stopping to avoid an incomplete download.")
    if kind != "track" and len(tracks) >= 100:
        emit(f"Note: Spotify exposed {len(tracks)} tracks. Large embed lists can be limited.")
    name = (entity.get("name") or entity.get("title")) if entity else playlist_name(page)
    return str(name or f"Spotify {kind}"), tracks


def read_source(item: str):
    """Return (name, tracks) for a playlist/album/track link, else None."""
    p = urlparse(item)
    host = p.netloc.lower()
    if host in APPLE_HOSTS:
        if "/playlist/" not in p.path:
            raise PlaylistError("Apple Music URL must be a playlist link.")
        return read_playlist(item)
    if host in SPOTIFY_HOSTS:
        if not re.search(r"/(playlist|album|track)/", p.path):
            raise PlaylistError("Spotify URL must be a playlist, album, or track link.")
        return read_spotify(item)
    return None


# --------------------------------------------------------------------------
# Cleaning names so YouTube matching works
# --------------------------------------------------------------------------

FEAT_RE = re.compile(r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring)\s[^)\]]*[)\]]", re.I)
REMASTER_RE = re.compile(
    r"\s*(?:[(\[]\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*[)\]]"
    r"|\s-\s(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*$)", re.I)


def clean_title(title: str) -> str:
    cleaned = REMASTER_RE.sub("", FEAT_RE.sub("", title)).strip()
    return cleaned or title


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = " ".join(name.split()).strip(" .")[:80].strip(" .")
    return name or "Playlist"


def track_key(title: str, artist: str) -> str:
    return (artist + " | " + title).strip().casefold()


PREFERRED_CHANNELS = (
    "https://www.youtube.com/@7cloudsRock",
    "https://www.youtube.com/channel/UCq46lKycfqrBtUPTxUk_NRQ",  # Dan Music
    "https://www.youtube.com/@7clouds",
    "https://www.youtube.com/@7cloudsIndieOfficial",
    "https://www.youtube.com/channel/UCbuK8xxu2P_sqoMnDsoBrrg",  # WaveMusic
    "https://www.youtube.com/@7cloudsRap",
)


def normal(text: object) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("'", "").replace("’", "")
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def has_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


def remove_phrase(text: str, phrase: str) -> str:
    if not phrase:
        return text
    return " ".join(re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ", text).split())


def channel_name(info: dict) -> str:
    return str(info.get("channel") or info.get("uploader") or "")


def performer(info: dict) -> str:
    artists = info.get("artists")
    if isinstance(artists, list) and artists:
        return ", ".join(str(a) for a in artists)
    if info.get("artist"):
        return str(info["artist"])
    channel = channel_name(info)
    if normal(channel).endswith(" topic"):
        return re.sub(r"\s*[-–—]?\s*Topic\s*$", "", channel, flags=re.I)
    return ""


def video_url(info: dict) -> str:
    video_id = str(info.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise SongError("The search result did not contain a valid YouTube video ID.")
    return "https://www.youtube.com/watch?v=" + video_id


UNWANTED = (
    "live", "cover", "remix", "slowed",
    "sped up", "speed up", "nightcore", "karaoke", "instrumental",
    "reaction", "tutorial", "acoustic", "extended", "full album", "loop",
    "reverb", "8d", "bass boosted", "concert", "teaser", "trailer",
)


def score(info: dict, song: str, artist: str, full: bool = False, allow_video: bool = False):
    """Return (ranking points, evidence) or None. Points are not probabilities."""
    title, wanted, who = normal(info.get("title")), normal(song), normal(artist)
    track = normal(info.get("track"))
    channel = normal(channel_name(info))
    known_artist = normal(performer(info))
    if not has_phrase(title, wanted) and not has_phrase(track, wanted):
        return None
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live", "was_live"}:
        return None

    artist_fields = [title, known_artist, channel, channel.removesuffix("vevo").strip()]
    if who and not any(has_phrase(field, who) for field in artist_fields):
        return None

    leftovers = []
    for field in [title, track]:
        remaining = remove_phrase(field, wanted)
        remaining = remove_phrase(remaining, who or known_artist)
        leftovers.append(remaining)
    if any(has_phrase(field, term) for field in leftovers for term in UNWANTED):
        return None
    music_video = any(has_phrase(field, term) for field in leftovers
                      for term in ("music video", "official video"))
    if music_video and not allow_video:
        return None

    points = 40 + (20 if who else 0)
    reasons = ["song title matches"]
    if who:
        reasons.append("artist matches")
    if track:
        track_base = re.sub(r"\b(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?\b", "", track).strip()
        if track_base != wanted:
            track_base = re.split(r"\b(?:feat|ft|featuring)\b", track_base, maxsplit=1)[0].strip()
        if track_base != wanted:
            return None
        points += 20
        reasons.append("track metadata matches")

    description = normal(info.get("description"))
    supplied = has_phrase(description, "provided to youtube by") and has_phrase(description, "auto generated by youtube")
    topic = channel.endswith(" topic")
    official_audio = has_phrase(title, "official audio")
    audio = any(has_phrase(title, term) for term in ("audio", "lyrics", "lyric video", "lyric", "album version"))
    if full and not (supplied or topic or audio or (allow_video and music_video)):
        return None
    if supplied:
        points += 45
        reasons.append("auto-generated recording")
    elif topic:
        points += 38
        reasons.append("Topic channel")
    elif official_audio:
        points += 30
        reasons.append("labeled official audio")
    elif audio:
        points += 12
        reasons.append("labeled audio/lyrics")
    elif music_video:
        reasons.append("MUSIC VIDEO FALLBACK: may contain extra dialogue or effects")
        if info.get("channel_is_verified"):
            points += 8
    if official_audio and (supplied or topic):
        points += 5
    if normal(info.get("title")) == wanted:
        points += 5
    return points, reasons


# --------------------------------------------------------------------------
# YouTube search + download (yt-dlp as a library)
# --------------------------------------------------------------------------

class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def dependency_check():
    try:
        import yt_dlp  # noqa: F401
    except ImportError as exc:
        raise SongError("yt-dlp is not installed in this build.") from exc


def ffmpeg_path():
    """ffmpeg for MP3 conversion. On Android it must ship as libffmpeg.so (see README)."""
    env = os.environ.get("FFMPEG_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    libdir = os.environ.get("ANDROID_NATIVE_LIB_DIR")
    if libdir:
        candidate = os.path.join(libdir, "libffmpeg.so")
        if os.path.isfile(candidate):
            return candidate
    return None


def _base_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 20, "retries": 2, "extractor_retries": 1,
            "logger": _Quiet()}
    # YouTube extraction can need an external JavaScript runtime. A phone usually
    # has none, so this only applies if one happens to be on PATH.
    for runtime in ("deno", "node"):
        if shutil.which(runtime):
            opts["js_runtimes"] = {runtime: {}}
            break
    return opts


def extract(target: str, flat: bool = False, extra: dict | None = None) -> dict:
    import yt_dlp
    check_cancel()
    opts = _base_opts()
    if flat:
        opts.update(extract_flat="in_playlist", playlistend=12)
    if extra:
        opts.update(extra)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=bool(extra and extra.get("_download")))
            info = ydl.sanitize_info(info) if info else {}
    except Cancelled:
        raise
    except Exception as exc:
        if CANCEL.is_set():
            raise Cancelled() from exc
        raise SongError(" ".join(str(exc).split())[-600:] or "YouTube request failed.") from exc
    check_cancel()
    if not isinstance(info, dict):
        raise SongError("yt-dlp returned unexpected data.")
    return info


def channel_search_url(channel: str, query: str) -> str:
    if channel.startswith("@"):
        channel = "https://www.youtube.com/" + channel
    parsed = urlparse(channel)
    if parsed.scheme not in {"https", "http"} or parsed.netloc not in {"youtube.com", "www.youtube.com"}:
        raise SongError("Channel must be a YouTube @handle or channel URL.")
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"/(?:@[^/?#]+|channel/UC[A-Za-z0-9_-]{22}|(?:c|user)/[^/?#]+)", path):
        raise SongError("Use the channel's main URL, without /videos or /search.")
    return "https://www.youtube.com" + path + "/search?query=" + quote(query, safe="")


def search(song: str, artist: str, channels=None, strict=False, workers: int = 4):
    query = " ".join(part for part in (artist, song) if part)
    errors = []
    cache = {}

    def entries(target):
        try:
            data = extract(target, flat=True)
        except SongError as exc:
            errors.append(str(exc))
            return []
        results = []
        for entry in data.get("entries") or []:
            if isinstance(entry, dict) and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(entry.get("id") or "")):
                entry = dict(entry)
                if "/search?query=" in target:
                    for field in ("channel", "channel_id", "uploader"):
                        if not entry.get(field) and data.get(field):
                            entry[field] = data[field]
                    entry["_expected_channel"] = data.get("channel_id")
                results.append(entry)
        return results

    def inspect(candidates, allow_video=False):
        unique = {entry["id"]: entry for entry in candidates}
        preliminary = []
        for entry in unique.values():
            ranked = score(entry, song, artist, allow_video=allow_video)
            if ranked is not None:
                preliminary.append((ranked[0], entry))
        preliminary.sort(key=lambda row: row[0], reverse=True)
        shortlist = [entry for _, entry in preliminary[:6]]

        pending = [entry for entry in shortlist if entry["id"] not in cache]
        for entry in pending:
            emit("Checking: " + str(entry.get("title", "Untitled")))

        def load(entry):
            try:
                return extract(video_url(entry))
            except SongError as exc:
                errors.append(str(exc))
                return None

        if pending:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
                for entry, loaded in zip(pending, pool.map(load, pending)):
                    if loaded is not None:
                        cache[entry["id"]] = loaded

        checked = []
        for entry in shortlist:
            full = cache.get(entry["id"])
            if full is None:
                continue
            expected = entry.get("_expected_channel")
            if expected and full.get("channel_id") != expected:
                continue
            ranked = score(full, song, artist, full=True, allow_video=allow_video)
            if ranked is not None:
                checked.append((ranked[0], ranked[1], full))
        return sorted(checked, key=lambda row: row[0], reverse=True)

    stage(1, "Looking for matching Topic recordings...")
    topic_results = entries(f"ytsearch12:{query} Topic")
    topics = [entry for entry in topic_results if normal(channel_name(entry)).endswith(" topic")]
    checked = inspect(topics)
    if checked:
        return checked, False

    stage(2, "Searching preferred audio/lyric channels and all of YouTube...")
    urls = [channel_search_url(channel, query) for channel in (channels or PREFERRED_CHANNELS)]
    targets = urls + [f"ytsearch12:{query} {suffix}" for suffix in ("official audio", "lyrics")]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as pool:
        batches = list(pool.map(entries, targets))
    channel_results = [entry for batch in batches[:len(urls)] for entry in batch]
    checked = inspect(channel_results)
    if checked:
        return checked, False

    stage(3, "Checking all-YouTube results for song-only uploads...")
    general = list(topic_results)
    for batch in batches[len(urls):]:
        general.extend(batch)
    checked = inspect(general)
    if checked:
        return checked, False

    if errors:
        raise SongError("Some audio searches or candidate checks failed. Music-video fallback was skipped "
                        "because the audio search could not be completed.\nLast error: " + errors[-1])
    if strict:
        raise SongError("No suitable song-only match found; strict audio prevents music-video fallback.")
    stage(4, "No song-only match found. Trying a music-video fallback...")
    fallback = entries(f"ytsearch12:{query} official music video")
    checked = inspect(fallback)
    if checked:
        return checked, False
    checked = inspect(fallback + general, allow_video=True)
    if checked:
        return checked, True
    raise SongError("No suitable match found. Try a more exact song title or add the artist."
                    + ("\nLast error: " + errors[-1] if errors else ""))


def download(info: dict, workdir: Path, fallback=False) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    vid = str(info.get("id") or "")

    def hook(_d):
        check_cancel()

    opts = {"outtmpl": ("FALLBACK - " if fallback else "") + "%(title).150B [%(id)s].%(ext)s",
            "paths": {"home": str(workdir)}, "windowsfilenames": True,
            "overwrites": False, "progress_hooks": [hook], "_download": True}
    ffmpeg = ffmpeg_path()
    if ffmpeg:
        opts.update(format="bestaudio/best", ffmpeg_location=ffmpeg,
                    postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                     "preferredquality": "0"},
                                    {"key": "FFmpegMetadata"}])
    else:
        # No ffmpeg: save the audio stream as-is (usually M4A) instead of failing.
        opts["format"] = "bestaudio[ext=m4a]/bestaudio"
        emit("ffmpeg not found: saving the original audio (M4A) instead of converting to MP3.")

    result = extract(video_url(info), extra=opts)
    for item in result.get("requested_downloads") or []:
        path = Path(str(item.get("filepath") or ""))
        if path.is_file() and path.stat().st_size > 0:
            return path.resolve()
    hits = [p for p in workdir.iterdir() if f"[{vid}]" in p.name and p.is_file() and p.stat().st_size > 0]
    if hits:
        return max(hits, key=lambda p: p.stat().st_mtime).resolve()
    raise SongError("The download finished but no audio file appeared.")


def read_lines(path: Path) -> set[str]:
    try:
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def fetch_and_deliver(title: str, artist: str, target, workdir: Path, strict: bool,
                      workers: int = 4, channels=None) -> str:
    """Find the song, download it to workdir, hand it to the chosen folder."""
    if not normal(title):
        raise SongError("Enter a song title.")
    dependency_check()
    matches, fallback = search(clean_title(title), artist, channels, strict, workers)
    _, reasons, chosen = matches[0]
    emit(f"Selected: {chosen.get('title')} | {channel_name(chosen)}")
    emit("Evidence: " + "; ".join(reasons))
    if fallback:
        emit("MUSIC VIDEO FALLBACK: may contain extra dialogue/effects.")
    note("Downloading")
    emit("Downloading audio...")
    path = download(chosen, workdir, fallback)
    note("Saving to folder")
    name = target.deliver(path)
    try:
        path.unlink()
    except OSError:
        pass
    emit("Saved: " + name)
    return name


def download_single(title: str, artist: str, target, workdir: Path, strict: bool) -> int:
    ctx.n = ctx.t = 1
    emit((artist + " - " if artist else "") + title)
    try:
        fetch_and_deliver(title, artist, target, workdir, strict)
        STATUS(1, "Done", "done")
        return 0
    except Cancelled:
        STATUS(1, "Cancelled", "")
        return 130
    except (SongError, OSError) as exc:
        emit("FAILED: " + str(exc), True)
        STATUS(1, "Failed: " + " ".join(str(exc).split())[-48:], "failed")
        return 1


def download_all(tracks, target, workdir: Path, strict: bool, redo: bool, jobs, channels=None) -> int:
    dependency_check()
    done_file = target.state_file()
    done = set() if redo else read_lines(done_file)
    lim = jobs if isinstance(jobs, Limiter) else Limiter(max(1, min(jobs, 8)))
    lock, total = threading.Lock(), len(tracks)
    res = {"saved": 0, "skipped": 0, "failed": 0}

    def finish(n, kind, text, tag):
        with lock:
            res[kind] += 1
            snap = dict(res)
        STATUS(n, text, tag)
        PROGRESS(snap, total)

    def work(n, title, artist, label, key):
        ctx.n, ctx.t = n, total
        try:
            emit(label)
            fetch_and_deliver(title, artist, target, workdir, strict,
                              max(1, min(8, 8 // lim.n)), channels)
            with lock:
                append_line(done_file, key)
                done.add(key)
            finish(n, "saved", "Done", "done")
        except Cancelled:
            STATUS(n, "Cancelled", "")
        except (SongError, OSError) as exc:
            msg = " ".join(str(exc).split())
            emit("FAILED: " + str(exc), True)
            finish(n, "failed", "Failed: " + msg[-48:], "failed")
        except Exception as exc:
            emit(f"FAILED (unexpected {type(exc).__name__}): {exc}", True)
            finish(n, "failed", f"Failed: {type(exc).__name__}", "failed")
        finally:
            lim.release()

    PROGRESS(res, total)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for n, (title, artist) in enumerate(tracks, 1):
            if CANCEL.is_set():
                break
            label = f"{artist} - {title}" if artist else title
            key = track_key(title, artist)
            if key in done:
                emit(f"[{n}/{total}] Already downloaded: {label}")
                finish(n, "skipped", "Already downloaded", "skipped")
                continue
            if not lim.acquire():
                break
            pool.submit(work, n, title, artist, label, key)
    if CANCEL.is_set():
        emit("Cancelled.")
    emit(f"Finished. Saved {res['saved']}, skipped {res['skipped']}, failed {res['failed']}.")
    return 130 if CANCEL.is_set() else (1 if res["failed"] else 0)
