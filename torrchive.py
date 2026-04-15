#!/usr/bin/env python3
"""
Torrchive — Community HEVC/AV1 archive transcoder
https://github.com/Versveth/torrchive

Transcodes media files no longer being seeded into space-efficient formats,
keeping them alive in Plex/Jellyfin at a fraction of the original size.

Modes:
  scan       - analyse library, show what would be transcoded
  run        - transcode eligible files
  status     - show progress and space saved

Usage:
  torrchive.py scan
  torrchive.py run
  torrchive.py run --dry-run
  torrchive.py run --limit 10
  torrchive.py status
"""

import os
import re
import sys
import json
import time
import logging
import argparse
import gettext as gettext_module
import subprocess
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Run: pip install pyyaml")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests is required. Run: pip install requests")
    sys.exit(1)

try:
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn, TextColumn,
        TimeElapsedColumn, TimeRemainingColumn, TaskID,
        MofNCompleteColumn,
    )
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich import print as rprint
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# ─── Version ─────────────────────────────────────────────────────────────────

__version__ = "0.1.0"


class _Translator:
    """Deferred translator — updates in place so all modules and threads see the change."""
    def __init__(self):
        self._fn = lambda s: s

    def __call__(self, s: str) -> str:
        return self._fn(s)

    def set(self, fn):
        self._fn = fn


_ = _Translator()

# ─── i18n ────────────────────────────────────────────────────────────────────

def _compile_po(po_path: Path, mo_path: Path):
    """Pure Python .po → .mo compiler. No external tools required."""
    import struct
    entries: dict[str, str] = {}
    msgid = msgstr = None
    with open(po_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("msgid "):
                msgid = line[7:-1]
            elif line.startswith("msgstr "):
                msgstr = line[8:-1]
                if msgid is not None:
                    entries[msgid] = msgstr
                msgid = msgstr = None

    keys = sorted(entries.keys())
    ids = b""
    strs = b""
    offsets = []
    for k in keys:
        v = entries[k]
        offsets.append((len(ids), len(k.encode()), len(strs), len(v.encode("utf-8"))))
        ids += k.encode() + b"\x00"
        strs += v.encode("utf-8") + b"\x00"

    n = len(keys)
    o_table = 28
    t_table = o_table + n * 8
    o_data = t_table + n * 8
    t_data = o_data + len(ids)

    mo_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mo_path, "wb") as f:
        f.write(struct.pack("<IIIIIII", 0x950412DE, 0, n, o_table, t_table, 0, 0))
        for oi, li, _, _ in offsets:
            f.write(struct.pack("<II", li, o_data + oi))
        for _, _, ot, lt in offsets:
            f.write(struct.pack("<II", lt, t_data + ot))
        f.write(ids)
        f.write(strs)


def setup_i18n(language: str = "fr") -> callable:
    """
    Load translations for the given language code.
    Falls back to French if the requested language is unavailable.
    Auto-compiles .po to .mo if .mo is missing or outdated.
    """
    locale_dir = Path(__file__).parent / "locales"

    po = locale_dir / language / "LC_MESSAGES" / "torrchive.po"
    mo = locale_dir / language / "LC_MESSAGES" / "torrchive.mo"
    if po.exists() and (not mo.exists() or mo.stat().st_mtime < po.stat().st_mtime):
        try:
            _compile_po(po, mo)
        except Exception as e:
            print(f"Warning: could not compile {po}: {e}")

    try:
        t = gettext_module.translation(
            "torrchive", localedir=str(locale_dir), languages=[language]
        )
        return t.gettext
    except FileNotFoundError:
        try:
            t = gettext_module.translation(
                "torrchive", localedir=str(locale_dir), languages=["fr"]
            )
            return t.gettext
        except FileNotFoundError:
            return lambda s: s




# ─── Config loader ───────────────────────────────────────────────────────────

def _interpolate_env(value: str) -> str:
    """Replace ${VAR} or $VAR patterns with environment variable values."""
    return re.sub(
        r"\$\{([^}]+)\}|\$([A-Z_][A-Z0-9_]*)",
        lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)),
        str(value),
    )


def _walk_interpolate(obj):
    if isinstance(obj, dict):
        return {k: _walk_interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_interpolate(i) for i in obj]
    if isinstance(obj, str):
        return _interpolate_env(obj)
    return obj


def load_config(path: Path) -> dict:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return _walk_interpolate(raw)


# ─── Logging ─────────────────────────────────────────────────────────────────

def setup_logging(log_file: Optional[Path]):
    """
    Always log to stdout for interactive use.
    Additionally log to file when configured.
    When running via nohup with stdout redirected to the log file,
    use --log-file-only flag or omit stdout redirect to avoid duplicates.
    """
    handlers: list = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# ─── Torrent client abstraction ──────────────────────────────────────────────

class TorrentClient(ABC):
    """Base class — returns the set of absolute file paths managed by the client."""

    @abstractmethod
    def get_managed_files(self) -> set[str]:
        """Return absolute paths of ALL files known to this client (any state)."""
        ...

    def is_managed(self, path: str) -> bool:
        return path in self.get_managed_files()


class NullClient(TorrentClient):
    """No torrent client — all files are eligible for transcoding."""

    def get_managed_files(self) -> set[str]:
        return set()


class QBittorrentClient(TorrentClient):
    """
    qBittorrent Web API v2.
    Docs: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
    """

    def __init__(self, url: str, username: str, password: str):
        self.url = url.rstrip("/")
        self._session = requests.Session()
        self._login(username, password)
        self._managed: Optional[set[str]] = None

    def _login(self, username: str, password: str):
        resp = self._session.post(
            f"{self.url}/api/v2/auth/login",
            data={"username": username, "password": password},
            timeout=10,
        )
        if resp.text.strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {resp.text}")
        logging.info(_("qBittorrent: authenticated"))

    def get_managed_files(self) -> set[str]:
        if self._managed is not None:
            return self._managed

        torrents = self._session.get(
            f"{self.url}/api/v2/torrents/info", timeout=30
        ).json()

        managed = set()
        for torrent in torrents:
            save_path = torrent.get("save_path", "")
            files = self._session.get(
                f"{self.url}/api/v2/torrents/files",
                params={"hash": torrent["hash"]},
                timeout=10,
            ).json()
            for f in files:
                managed.add(os.path.normpath(os.path.join(save_path, f["name"])))

        self._managed = managed
        logging.info(
            f"qBittorrent: {len(managed)} files across {len(torrents)} torrents (all states)"
        )
        return managed


class DelugeClient(TorrentClient):
    """
    Deluge JSON-RPC API.
    Docs: https://deluge.readthedocs.io/en/latest/reference/api.html

    Config example:
      torrent_client:
        type: deluge
        url: http://localhost:8112
        password: deluge
    """

    def __init__(self, url: str, password: str):
        self.url = url.rstrip("/") + "/json"
        self._session = requests.Session()
        self._id = 0
        self._login(password)
        self._managed: Optional[set[str]] = None

    def _rpc(self, method: str, params: list) -> dict:
        self._id += 1
        resp = self._session.post(
            self.url,
            json={"method": method, "params": params, "id": self._id},
            timeout=30,
        )
        return resp.json()

    def _login(self, password: str):
        result = self._rpc("auth.login", [password])
        if not result.get("result"):
            raise RuntimeError("Deluge authentication failed")
        logging.info(_("Deluge: authenticated"))

    def get_managed_files(self) -> set[str]:
        if self._managed is not None:
            return self._managed

        result = self._rpc("core.get_torrents_status", [{}, ["save_path", "files"]])
        managed = set()
        for torrent in result.get("result", {}).values():
            save_path = torrent.get("save_path", "")
            for f in torrent.get("files", []):
                managed.add(os.path.normpath(os.path.join(save_path, f["path"])))

        self._managed = managed
        logging.info(_("Deluge: {} managed files").format(len(managed)))
        return managed


class TransmissionClient(TorrentClient):
    """
    Transmission RPC API.
    Docs: https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md

    Config example:
      torrent_client:
        type: transmission
        url: http://localhost:9091
        username: transmission
        password: transmission
    """

    def __init__(self, url: str, username: str = "", password: str = ""):
        self.url = url.rstrip("/") + "/transmission/rpc"
        self._session = requests.Session()
        if username:
            self._session.auth = (username, password)
        self._csrf = self._get_csrf()
        self._managed: Optional[set[str]] = None

    def _get_csrf(self) -> str:
        resp = self._session.get(self.url, timeout=10)
        return resp.headers.get("X-Transmission-Session-Id", "")

    def _rpc(self, method: str, arguments: dict) -> dict:
        resp = self._session.post(
            self.url,
            json={"method": method, "arguments": arguments},
            headers={"X-Transmission-Session-Id": self._csrf},
            timeout=30,
        )
        if resp.status_code == 409:
            self._csrf = resp.headers.get("X-Transmission-Session-Id", "")
            return self._rpc(method, arguments)
        return resp.json()

    def get_managed_files(self) -> set[str]:
        if self._managed is not None:
            return self._managed

        result = self._rpc("torrent-get", {"fields": ["downloadDir", "files"]})
        managed = set()
        for torrent in result.get("arguments", {}).get("torrents", []):
            dl_dir = torrent.get("downloadDir", "")
            for f in torrent.get("files", []):
                managed.add(os.path.normpath(os.path.join(dl_dir, f["name"])))

        self._managed = managed
        logging.info(_("Transmission: {} managed files").format(len(managed)))
        return managed


def build_torrent_client(cfg: dict) -> TorrentClient:
    client_type = cfg.get("type", "none").lower()

    if client_type == "none":
        logging.info(_("Torrent client: none — all files eligible"))
        return NullClient()

    if client_type == "qbittorrent":
        return QBittorrentClient(
            url=cfg["url"],
            username=cfg.get("username", ""),
            password=cfg.get("password", ""),
        )

    if client_type == "deluge":
        return DelugeClient(
            url=cfg["url"],
            password=cfg.get("password", ""),
        )

    if client_type == "transmission":
        return TransmissionClient(
            url=cfg["url"],
            username=cfg.get("username", ""),
            password=cfg.get("password", ""),
        )

    raise ValueError(
        f"Unknown torrent client type: '{client_type}'. "
        f"Supported: none, qbittorrent, deluge, transmission"
    )


# ─── Encoder abstraction ─────────────────────────────────────────────────────

@dataclass
class EncoderProfile:
    backend: str       # nvenc | vaapi | videotoolbox | software
    codec: str         # hevc | av1 | h264
    quality: int       # CQ/CRF value (lower = better quality, larger file)
    preset: str        # encoder preset
    max_resolution: Optional[int]  # None | 720 | 1080 | 1440 | 2160
    audio: str         # copy | aac | opus
    audio_bitrate: str
    audio_channels: int
    normalize_filename: bool


# Codec → encoder name per backend
CODEC_MAP = {
    "nvenc":        {"hevc": "hevc_nvenc",  "av1": "av1_nvenc",   "h264": "h264_nvenc"},
    "vaapi":        {"hevc": "hevc_vaapi",  "av1": "av1_vaapi",   "h264": "h264_vaapi"},
    "videotoolbox": {"hevc": "hevc_videotoolbox", "av1": None,    "h264": "h264_videotoolbox"},
    "software":     {"hevc": "libx265",     "av1": "libaom-av1",  "h264": "libx264"},
}

# Quality flag per backend (CQ for NVENC/VAAPI, CRF for software)
QUALITY_FLAG = {
    "nvenc": ["-rc", "vbr", "-cq"],
    "vaapi": ["-rc_mode", "CQP", "-global_quality"],
    "videotoolbox": ["-q:v"],
    "software": ["-crf"],
}

# Hardware acceleration input flags
HWACCEL_FLAGS = {
    "nvenc": ["-hwaccel", "cuda"],
    "vaapi": ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
              "-vaapi_device", "/dev/dri/renderD128"],
    "videotoolbox": ["-hwaccel", "videotoolbox"],
    "software": [],
}

# Preset flag per backend
PRESET_FLAG = {
    "nvenc": "-preset",
    "vaapi": "-compression_level",
    "videotoolbox": "-profile:v",
    "software": "-preset",
}


def detect_backend() -> str:
    """Auto-detect best available hardware encoder."""
    checks = [
        ("nvenc",        ["ffmpeg", "-hide_banner", "-encoders"],  "hevc_nvenc"),
        ("vaapi",        ["ffmpeg", "-hide_banner", "-encoders"],  "hevc_vaapi"),
        ("videotoolbox", ["ffmpeg", "-hide_banner", "-encoders"],  "hevc_videotoolbox"),
    ]
    for backend, cmd, search in checks:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
            if search in out:
                logging.info(_("Encoder: auto-detected {}").format(backend))
                return backend
        except Exception:
            pass
    logging.info(_("Encoder: falling back to software (libx265)"))
    return "software"


def build_encoder_profile(cfg: dict) -> EncoderProfile:
    backend = cfg.get("backend", "auto")
    if backend == "auto":
        backend = detect_backend()

    codec = cfg.get("codec", "hevc").lower()

    if codec not in ("hevc", "av1", "h264"):
        raise ValueError(f"Unsupported codec: '{codec}'. Supported: hevc, av1, h264")

    encoder_name = CODEC_MAP.get(backend, {}).get(codec)
    if encoder_name is None:
        raise ValueError(
            f"Codec '{codec}' is not supported on backend '{backend}'. "
            f"Note: AV1 is not available on VideoToolbox (Apple Silicon uses software libaom-av1)."
        )

    # Verify encoder is available in this ffmpeg build
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        if encoder_name not in out:
            raise RuntimeError(
                f"Encoder '{encoder_name}' not found in your ffmpeg build.\n"
                f"  Backend: {backend}, Codec: {codec}\n"
                f"  Run 'ffmpeg -encoders' to see what's available.\n"
                f"  Consider setting encoder.backend: software as a fallback."
            )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg.")

    return EncoderProfile(
        backend=backend,
        codec=codec,
        quality=int(cfg.get("quality", 26)),
        preset=str(cfg.get("preset", "p6" if backend == "nvenc" else "medium")),
        max_resolution=cfg.get("max_resolution"),
        audio=cfg.get("audio", "copy"),
        audio_bitrate=cfg.get("audio_bitrate", "192k"),
        audio_channels=int(cfg.get("audio_channels", 2)),
        normalize_filename=cfg.get("normalize_filename", True),
    )


def build_ffmpeg_cmd(src: Path, dst: Path, profile: EncoderProfile,
                     source_height: int) -> list[str]:
    codec_name = CODEC_MAP[profile.backend][profile.codec]
    quality_flags = QUALITY_FLAG[profile.backend] + [str(profile.quality)]
    hwaccel = HWACCEL_FLAGS[profile.backend]
    preset_flag = PRESET_FLAG[profile.backend]

    # Resolution filter
    vf_filters = []
    if profile.max_resolution and source_height > profile.max_resolution:
        if profile.backend == "vaapi":
            vf_filters.append(
                f"scale_vaapi=-2:{profile.max_resolution}"
            )
        else:
            vf_filters.append(
                f"scale=-2:{profile.max_resolution}:flags=lanczos"
            )

    cmd = ["ffmpeg", "-y", *hwaccel, "-fflags", "+genpts", "-stats_period", "1", "-i", str(src), "-max_muxing_queue_size", "9999"]

    if vf_filters:
        cmd += ["-vf", ",".join(vf_filters)]

    cmd += [
        "-c:v", codec_name,
        preset_flag, profile.preset,
        *quality_flags,
        "-b:v", "0",
    ]

    if profile.backend == "nvenc":
        cmd += ["-profile:v", "main"]
        if profile.codec == "hevc":
            cmd += ["-pix_fmt", "yuv420p"]

    if profile.audio == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += [
            "-c:a", profile.audio,
            "-b:a", profile.audio_bitrate,
            "-ac", str(profile.audio_channels),
        ]

    cmd += ["-c:s", "copy", "-map", "0:V", "-map", "0:a", "-map", "0:s?", str(dst)]
    return cmd


# ─── Filename normalisation ──────────────────────────────────────────────────

# Tokens that indicate a source codec — will be replaced with target codec tag
CODEC_TOKENS = re.compile(
    r"\b(x264|x\.264|H\.?264|AVC|XviD|DivX|x265|x\.265|H\.?265|HEVC|AV1|VP9|VP8)\b",
    re.IGNORECASE,
)

RESOLUTION_TOKENS = re.compile(
    r"\b(4320p|2160p|1440p|1080p|720p|480p|360p)\b",
    re.IGNORECASE,
)

TARGET_CODEC_TAG = {
    "hevc": "x265",
    "av1":  "AV1",
    "h264": "x264",
}

TARGET_RESOLUTION_TAG = {
    2160: "2160p",
    1440: "1440p",
    1080: "1080p",
    720:  "720p",
    480:  "480p",
}


def normalize_filename(stem: str, profile: EncoderProfile,
                       source_height: int) -> str:
    """
    Replace stale codec and resolution tokens in filename stem.
    e.g. Show.S01E01.1080p.WEB.x264 → Show.S01E01.1080p.WEB.x265
    """
    target_codec = TARGET_CODEC_TAG.get(profile.codec, "x265")
    result = CODEC_TOKENS.sub(target_codec, stem)

    if profile.max_resolution and source_height > profile.max_resolution:
        target_res = TARGET_RESOLUTION_TAG.get(profile.max_resolution, f"{profile.max_resolution}p")
        result = RESOLUTION_TOKENS.sub(target_res, result)

    return result


# ─── Video analysis ──────────────────────────────────────────────────────────

@dataclass
class VideoFile:
    path: Path
    size_mb: float
    codec: str = ""
    height: int = 0
    managed_by_client: bool = False
    skip_reason: Optional[str] = None


def get_video_duration(path: Path) -> float:
    """Return duration in seconds, 0 on failure. Checks both format and stream level."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format=duration:stream=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        # Try format level first, then fall back to first stream
        d = data.get("format", {}).get("duration")
        if not d:
            streams = data.get("streams", [])
            for s in streams:
                d = s.get("duration")
                if d:
                    break
        return float(d) if d else 0.0
    except Exception:
        return 0.0


def probe_file(path: Path) -> tuple[str, int]:
    """Returns (codec_name, height). Both empty/0 on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,height",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            return (
                streams[0].get("codec_name", "unknown"),
                int(streams[0].get("height", 0)),
            )
    except Exception as e:
        logging.warning(f"ffprobe failed for {path}: {e}")
    return "unknown", 0



# ─── Probe cache ─────────────────────────────────────────────────────────────

class ProbeCache:
    """
    ffprobe result cache keyed by path + mtime.
    Re-probes only if the file has changed since last scan.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
                logging.info(
                    f"Probe cache: loaded {len(self._data)} entries from {self.path}"
                )
            except Exception as e:
                logging.warning(_("Probe cache: failed to load ({}), starting fresh").format(e))
                self._data = {}

    def save(self):
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f)
        logging.info(_("Probe cache: saved {} entries to {}").format(len(self._data), self.path))
        self._dirty = False

    def _key(self, path: Path) -> str:
        mtime = int(path.stat().st_mtime)
        return f"{path}:{mtime}"

    def get(self, path: Path) -> Optional[tuple]:
        key = self._key(path)
        entry = self._data.get(key)
        if entry:
            return entry["codec"], entry["height"]
        return None

    def set(self, path: Path, codec: str, height: int):
        key = self._key(path)
        self._data[key] = {"codec": codec, "height": height}
        self._dirty = True

    def purge_stale(self, known_paths: set, scanned_dirs: list):
        """
        Remove cache entries for files under scanned directories that no
        longer exist. Entries from other libraries are left untouched.
        """
        scanned_strs = [str(p) for p in scanned_dirs]
        known_strs = {str(p) for p in known_paths}
        stale = [
            k for k in self._data
            if any(k.split(":")[0].startswith(d) for d in scanned_strs)
            and k.split(":")[0] not in known_strs
        ]
        for k in stale:
            del self._data[k]
        if stale:
            logging.info(_("Probe cache: purged {} stale entries").format(len(stale)))
            self._dirty = True


def _analyse_file(path: Path, managed_files: set[str],
                  min_size_mb: float,
                  cache: Optional[ProbeCache] = None) -> VideoFile:
    size_mb = path.stat().st_size / (1024 * 1024)
    vf = VideoFile(path=path, size_mb=size_mb)

    if size_mb < min_size_mb:
        vf.skip_reason = f"too small ({size_mb:.0f} MB < {min_size_mb:.0f} MB)"
        return vf

    if str(path) in managed_files:
        vf.managed_by_client = True
        vf.skip_reason = "managed by torrent client"
        return vf

    if cache:
        cached = cache.get(path)
        if cached:
            vf.codec, vf.height = cached
            return vf

    vf.codec, vf.height = probe_file(path)

    if cache:
        cache.set(path, vf.codec, vf.height)

    return vf


# ─── Schedule ────────────────────────────────────────────────────────────────

def in_schedule(start: dtime, stop: dtime) -> bool:
    now = datetime.now().time()
    return start <= now <= stop


def wait_for_schedule(start: dtime, stop: dtime):
    while not in_schedule(start, stop):
        now = datetime.now().time()
        logging.info(
            f"Outside schedule window ({start.strftime('%H:%M')}–"
            f"{stop.strftime('%H:%M')}), now {now.strftime('%H:%M')}. "
            f"Sleeping 5 min..."
        )
        time.sleep(300)


# ─── Ledger ──────────────────────────────────────────────────────────────────

def load_ledger(path: Path) -> list[dict]:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return []


def save_ledger(ledger: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2)


def record_transcode(ledger_path: Path, src: str, dst: str,
                     original_mb: float, transcoded_mb: float):
    ledger = load_ledger(ledger_path)
    for entry in ledger:
        if entry["source"] == src:
            entry.update({
                "destination": dst,
                "original_mb": round(original_mb, 1),
                "transcoded_mb": round(transcoded_mb, 1),
                "transcoded_at": datetime.now().isoformat(),
            })
            save_ledger(ledger, ledger_path)
            return
    ledger.append({
        "source": src,
        "destination": dst,
        "original_mb": round(original_mb, 1),
        "transcoded_mb": round(transcoded_mb, 1),
        "transcoded_at": datetime.now().isoformat(),
    })
    save_ledger(ledger, ledger_path)


# ─── Scanner ─────────────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".flv", ".mov"}


def cleanup_tmp_files(media_paths: list[Path], silent: bool = False) -> int:
    """Remove any leftover .torrchive_tmp_* files from previous interrupted runs."""
    found = []
    for base in media_paths:
        if base.exists():
            found.extend(base.rglob(".torrchive_tmp_*.mkv"))
    if found:
        for f in found:
            f.unlink(missing_ok=True)
        if not silent:
            logging.info(_("Startup cleanup: removed {} leftover temp file(s)").format(len(found)))
    return len(found)


def scan(media_paths: list[Path], managed_files: set[str],
         min_size_mb: float, workers: int = 16,
         cache: Optional[ProbeCache] = None) -> list[VideoFile]:
    all_files: list[Path] = []
    for base in media_paths:
        if not base.exists():
            logging.warning(_("Media path not found: {}").format(base))
            continue
        logging.info(_("Scanning {} ...").format(base))
        all_files.extend(
            f for f in sorted(base.rglob("*"))
            if f.suffix.lower() in VIDEO_EXTENSIONS
            and ".torrchive_tmp_" not in f.name
        )

    cached_count = sum(1 for f in all_files if cache and cache.get(f))
    fresh_count = len(all_files) - cached_count
    logging.info(_("Found {} video files — {} cached, {} to probe — {} workers...").format(len(all_files), cached_count, fresh_count, workers))

    results: dict[Path, VideoFile] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_analyse_file, f, managed_files, min_size_mb, cache): f
            for f in all_files
        }
        for future in as_completed(futures):
            vf = future.result()
            results[vf.path] = vf
            done += 1
            if done % 200 == 0:
                logging.info(_("Probed {}/{} files...").format(done, len(all_files)))

    if cache:
        cache.purge_stale(set(all_files), media_paths)
        cache.save()

    return [results[f] for f in sorted(results)]


# Codec efficiency order — higher index = more efficient
# A source codec more efficient than the target is never worth re-encoding
CODEC_EFFICIENCY = ["h264", "mpeg4", "xvid", "divx", "mpeg2video", "hevc", "av1", "vp9"]


def _is_more_efficient(source_codec: str, target_codec: str) -> bool:
    """Return True if source codec is more efficient than the target."""
    src = source_codec.lower()
    tgt = target_codec.lower()
    src_rank = CODEC_EFFICIENCY.index(src) if src in CODEC_EFFICIENCY else -1
    tgt_rank = CODEC_EFFICIENCY.index(tgt) if tgt in CODEC_EFFICIENCY else -1
    return src_rank > tgt_rank


def filter_queue(files: list[VideoFile], profile: EncoderProfile,
                 encoder_cfg: dict) -> list[VideoFile]:
    """Return files that need transcoding based on encoder profile and config."""
    skip_source_codecs = {c.lower() for c in encoder_cfg.get("skip_source_codecs", [])}
    skip_if_already_optimal = encoder_cfg.get("skip_if_already_optimal", True)

    queue = []
    for vf in files:
        if vf.skip_reason:
            continue

        # Skip unreadable files (ffprobe failed — likely corrupt)
        if vf.codec == "unknown":
            vf.skip_reason = "skipped (unreadable — ffprobe failed, file may be corrupt)"
            continue

        # Skip explicitly excluded source codecs
        if vf.codec in skip_source_codecs:
            vf.skip_reason = f"skipped ({vf.codec.upper()} — excluded by skip_source_codecs)"
            continue

        # Skip if source codec is already more efficient than the target
        if encoder_cfg.get("skip_if_smaller_codec", True):
            if _is_more_efficient(vf.codec, profile.codec):
                vf.skip_reason = (
                    f"skipped ({vf.codec.upper()} is more efficient "
                    f"than target {profile.codec.upper()})"
                )
                continue

        # Skip if already optimal (same codec, no resolution change needed)
        if skip_if_already_optimal and vf.codec == profile.codec:
            if profile.max_resolution and vf.height > profile.max_resolution:
                pass  # still needs downscale
            else:
                vf.skip_reason = f"already {profile.codec.upper()} (optimal)"
                continue

        queue.append(vf)
    return queue


# ─── Transcode ───────────────────────────────────────────────────────────────

def transcode_file(vf: VideoFile, profile: EncoderProfile,
                   ledger_path: Path,
                   progress_callback=None,
                   proc_registry: Optional[list] = None) -> bool:
    src = vf.path

    # Always output MKV — avoids MP4 container restrictions with HEVC
    # Hash-based tmp name avoids 255-byte filename limit on long titles
    import hashlib
    path_hash = hashlib.md5(str(src).encode()).hexdigest()[:12]
    tmp = src.parent / f".torrchive_tmp_{path_hash}.mkv"

    new_stem = src.stem
    if profile.normalize_filename:
        new_stem = normalize_filename(src.stem, profile, vf.height)

    dst = src.with_name(new_stem + ".mkv")

    logging.info(_("Transcoding: {}").format(src.name))
    logging.info(f"  Codec: {vf.codec} → {profile.codec.upper()} | "
                 f"Size: {vf.size_mb:.0f} MB | "
                 f"Resolution: {vf.height}p"
                 + (f" → {profile.max_resolution}p"
                    if profile.max_resolution and vf.height > profile.max_resolution
                    else ""))
    if new_stem != src.stem:
        logging.info(f"  Filename: {src.name} → {dst.name}")

    cmd = build_ffmpeg_cmd(src, tmp, profile, vf.height)
    duration = get_video_duration(src) if progress_callback else 0.0

    try:
        start = time.time()

        if progress_callback and duration > 0:
            # Stream stderr to parse progress and update callback
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc_registry is not None:
                proc_registry.append(proc)
            stderr_lines = []
            frame_re = re.compile(r"frame=\s*(\d+)")
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
                    m = frame_re.search(line)
                    if m:
                        # Estimate progress from fps * duration
                        pass
                    if "time=" in line:
                        t_match = re.search(r"time=(-?)(\d+):(\d+):([\d.]+)", line)
                        if t_match:
                            neg, h, m_, s = t_match.groups()
                            if not neg:
                                elapsed_enc = int(h)*3600 + int(m_)*60 + float(s)
                                pct = min(elapsed_enc / duration, 1.0) if duration else 0
                                progress_callback(pct)
            finally:
                proc.wait()
                if proc_registry is not None and proc in proc_registry:
                    proc_registry.remove(proc)
            stderr_out = "".join(stderr_lines[-20:])
            returncode = proc.returncode
        else:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=7200,
            )
            stderr_out = result.stderr
            returncode = result.returncode

        elapsed = time.time() - start

        if returncode != 0:
            logging.error(f"ffmpeg failed: {stderr_out[-500:]}")
            tmp.unlink(missing_ok=True)
            return False

        if not tmp.exists():
            logging.error(_("Output file not created."))
            tmp.unlink(missing_ok=True)
            return False

        new_size_mb = tmp.stat().st_size / (1024 * 1024)
        ratio = new_size_mb / vf.size_mb

        if ratio < 0.05:
            logging.error(
                f"Output suspiciously small ({new_size_mb:.0f} MB, "
                f"{ratio:.1%} of source). Aborting."
            )
            tmp.unlink(missing_ok=True)
            return False

        out_codec, _ = probe_file(tmp)
        if out_codec != profile.codec and not (
            profile.codec == "hevc" and out_codec == "hevc"
        ):
            logging.error(f"Output codec is {out_codec}, expected {profile.codec}. Aborting.")
            tmp.unlink(missing_ok=True)
            return False

        # Replace source with transcoded output
        if dst != src:
            src.unlink()
        os.replace(tmp, dst)

        reduction = (1 - ratio) * 100
        logging.info(_("Done in {}s: {} MB → {} MB ({}% reduction)").format(int(elapsed), int(vf.size_mb), int(new_size_mb), int(reduction)))

        record_transcode(ledger_path, str(src), str(dst), vf.size_mb, new_size_mb)
        return True

    except subprocess.TimeoutExpired:
        logging.error(_("ffmpeg timed out for {}").format(src))
        tmp.unlink(missing_ok=True)
        return False
    except Exception as e:
        logging.error(_("Unexpected error: {}").format(e))
        tmp.unlink(missing_ok=True)
        return False


# ─── Modes ───────────────────────────────────────────────────────────────────


def _run_with_progress(target: list, profile, ledger_path: Path,
                       parallel: int, schedule_enabled: bool,
                       start_t, stop_t):
    """Rich progress UI with per-job bars and overall ETA."""
    import threading
    import signal

    console = Console()
    lock = threading.Lock()
    success = [0]
    failed = [0]
    saved_mb = [0.0]

    # Track tmp files for cleanup prompt
    tmp_files: list[Path] = []

    # Redirect logging through rich console to avoid corrupting live display
    from rich.logging import RichHandler
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.root.addHandler(RichHandler(console=console, show_path=False, show_time=True))

    overall_progress = Progress(
        TextColumn("[bold blue]Overall"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        TextColumn("• Saved: [green]{task.fields[saved]}"),
        console=console,
    )
    job_progress = Progress(
        TextColumn("  [cyan]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    )

    overall_task = overall_progress.add_task(
        "Transcoding", total=len(target), saved="0 GB"
    )

    job_tasks: dict[int, TaskID] = {}

    display_sem = __import__("threading").Semaphore(parallel)

    def _worker(idx: int, vf) -> bool:
        if schedule_enabled:
            wait_for_schedule(start_t, stop_t)

        display_sem.acquire()
        short_name = vf.path.name[:55] + "…" if len(vf.path.name) > 55 else vf.path.name
        job_id = job_progress.add_task(short_name, total=100)
        with lock:
            job_tasks[idx] = job_id

        import hashlib
        path_hash = hashlib.md5(str(vf.path).encode()).hexdigest()[:12]
        tmp = vf.path.parent / f".torrchive_tmp_{path_hash}.mkv"
        tmp_files.append(tmp)

        def _progress_cb(pct: float):
            job_progress.update(job_id, completed=int(pct * 100))

        ok = transcode_file(vf, profile, ledger_path, progress_callback=_progress_cb, proc_registry=active_procs)

        job_progress.update(job_id, completed=100, visible=False)
        job_progress.stop_task(job_id)
        job_progress.remove_task(job_id)
        display_sem.release()

        with lock:
            if ok:
                success[0] += 1
                saved_mb[0] += vf.size_mb * 0.65  # rough estimate
            else:
                failed[0] += 1
            overall_progress.update(
                overall_task,
                advance=1,
                saved=f"{saved_mb[0]/1024:.1f} GB"
            )

        if tmp in tmp_files:
            tmp_files.remove(tmp)

        return ok

    table = Table.grid()
    table.add_row(overall_progress)
    table.add_row(job_progress)

    interrupted = [False]
    active_procs: list = []

    executor_ref = [None]

    def _handle_interrupt(sig, frame):
        if interrupted[0]:
            return  # ignore repeated signals
        interrupted[0] = True
        console.print(_("Interrupt received — stopping active jobs..."))
        for proc in active_procs:
            try:
                proc.terminate()
            except Exception:
                pass

    old_handler = signal.signal(signal.SIGINT, _handle_interrupt)

    try:
        with Live(table, console=console, refresh_per_second=4):
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {pool.submit(_worker, i, vf): vf
                           for i, vf in enumerate(target)}
                for future in as_completed(futures):
                    if interrupted[0]:
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        future.result()
                    except Exception as e:
                        logging.error(_("Worker error: {}").format(e))
                        failed[0] += 1
    finally:
        signal.signal(signal.SIGINT, old_handler)
        # Restore standard logging
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        setup_logging(None)

    console.print(f"\n[bold green]Done:[/] {success[0]} transcoded, "
                  f"[red]{failed[0]} failed[/]")

    # Cleanup prompt
    existing_tmp = [f for f in tmp_files if f.exists()]
    if existing_tmp:
        console.print(f"\n[yellow]Found {len(existing_tmp)} incomplete temp file(s).[/]")
        try:
            answer = input("Delete them? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer == "y":
            for f in existing_tmp:
                f.unlink(missing_ok=True)
            console.print("[green]Temp files deleted.[/]")
        else:
            console.print("[dim]Temp files kept.[/]")


def run_scan(cfg: dict, managed_files: set[str], profile: EncoderProfile):
    media_paths = [Path(p) for p in cfg["media"]["paths"]]
    min_size = float(cfg["media"].get("min_size_mb", 100))
    workers = int(cfg.get("performance", {}).get("scan_workers", 16))

    cache_path = Path(cfg.get("probe_cache_file", "torrchive_probe_cache.json"))
    cache = ProbeCache(cache_path)
    all_files = scan(media_paths, managed_files, min_size, workers, cache)
    queue = filter_queue(all_files, profile, cfg.get("encoder", {}))

    skipped = [v for v in all_files if v.skip_reason]
    skip_reasons: dict[str, int] = {}
    for v in skipped:
        skip_reasons[v.skip_reason] = skip_reasons.get(v.skip_reason, 0) + 1

    total_size = sum(v.size_mb for v in queue)

    logging.info("\n" + "=" * 60)
    logging.info(_("Scan complete:"))
    logging.info(_("Total video files : {}").format(len(all_files)))
    logging.info(_("To transcode      : {}").format(len(queue)))
    logging.info(_("Total size        : {} GB").format(f"{total_size / 1024:.1f}"))
    logging.info(_("Skipped           : {}").format(len(skipped)))
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        logging.info(f"    - {reason}: {count}")
    logging.info("=" * 60)

    logging.info(_("Queue (first 20):"))
    for vf in queue[:20]:
        logging.info(f"  [{vf.codec.upper():6}] {vf.size_mb:>7.0f} MB | {vf.path.name}")
    if len(queue) > 20:
        logging.info(_("... and {} more").format(len(queue) - 20))


def run_transcode(cfg: dict, managed_files: set[str], profile: EncoderProfile,
                  dry_run: bool, limit: int, no_schedule: bool, parallel: int = 1):
    media_paths = [Path(p) for p in cfg["media"]["paths"]]
    min_size = float(cfg["media"].get("min_size_mb", 100))
    workers = int(cfg.get("performance", {}).get("scan_workers", 16))
    cfg_parallel = int(cfg.get("performance", {}).get("parallel", 1))
    ledger_path = Path(cfg.get("ledger_file", "torrchive_ledger.json"))

    schedule_cfg = cfg.get("schedule", {})
    schedule_enabled = schedule_cfg.get("enabled", False) and not no_schedule
    if schedule_enabled:
        start_t = dtime(*map(int, schedule_cfg.get("start", "09:00").split(":")))
        stop_t = dtime(*map(int, schedule_cfg.get("stop", "20:00").split(":")))

    cleanup_tmp_files(media_paths)

    cache_path = Path(cfg.get("probe_cache_file", "torrchive_probe_cache.json"))
    cache = ProbeCache(cache_path)
    all_files = scan(media_paths, managed_files, min_size, workers, cache)
    queue = filter_queue(all_files, profile, cfg.get("encoder", {}))

    total_size = sum(v.size_mb for v in queue)
    logging.info(_("Queue: {} files, {} GB to process").format(len(queue), f"{total_size / 1024:.1f}"))

    if dry_run:
        logging.info("\n[DRY RUN] Files that would be transcoded:")
        for vf in queue:
            logging.info(f"  [{vf.codec.upper():6}] {vf.size_mb:>7.0f} MB | {vf.path}")
        return

    target = queue[:limit] if limit > 0 else queue
    # CLI --parallel overrides config value
    if parallel == 1:
        parallel = cfg_parallel
    parallel = max(1, parallel)
    success = 0
    failed = 0
    saved_mb = 0.0

    use_progress = cfg.get("display", {}).get("progress_bars", True) and RICH_AVAILABLE

    logging.info(_("Starting transcode: {} files, {} parallel job(s)").format(len(target), parallel))

    if use_progress:
        _run_with_progress(target, profile, ledger_path, parallel,
                           schedule_enabled,
                           start_t if schedule_enabled else None,
                           stop_t if schedule_enabled else None)
        # Tally from ledger
        ledger = load_ledger(ledger_path)
        success = len([e for e in ledger])
        failed = 0
    else:
        import threading
        lock = threading.Lock()
        counter = [0]

        def _worker(vf: VideoFile) -> bool:
            if schedule_enabled:
                wait_for_schedule(start_t, stop_t)
            with lock:
                counter[0] += 1
                idx = counter[0]
            logging.info(f"\n[{idx}/{len(target)}] Processing...")
            return transcode_file(vf, profile, ledger_path)

        if parallel == 1:
            for i, vf in enumerate(target):
                if schedule_enabled:
                    wait_for_schedule(start_t, stop_t)
                logging.info(f"\n[{i + 1}/{len(target)}] Processing...")
                if transcode_file(vf, profile, ledger_path):
                    success += 1
                else:
                    failed += 1
        else:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {pool.submit(_worker, vf): vf for vf in target}
                for future in as_completed(futures):
                    try:
                        if future.result():
                            success += 1
                        else:
                            failed += 1
                    except Exception as e:
                        logging.error(_("Worker error: {}").format(e))
                        failed += 1

    logging.info(_("Pipeline complete: {} transcoded, {} failed").format(success, failed))

    if success > 0:
        _run_post_transcode_hooks(cfg)


def _run_post_transcode_hooks(cfg: dict):
    """Trigger optional Plex/Jellyfin library refresh after transcoding."""
    hook_cfg = cfg.get("post_transcode", {})
    if not hook_cfg.get("enabled", False):
        return

    plex_url = hook_cfg.get("plex_url", "")
    plex_token = hook_cfg.get("plex_token", "")
    if plex_url and plex_token:
        try:
            resp = requests.post(
                f"{plex_url.rstrip('/')}/library/sections/all/refresh",
                headers={"X-Plex-Token": plex_token},
                timeout=10,
            )
            if resp.ok:
                logging.info(_("Post-transcode: Plex library refresh triggered"))
            else:
                logging.warning(f"Post-transcode: Plex refresh failed ({resp.status_code})")
        except Exception as e:
            logging.warning(f"Post-transcode: Plex refresh error: {e}")

    jellyfin_url = hook_cfg.get("jellyfin_url", "")
    jellyfin_token = hook_cfg.get("jellyfin_token", "")
    if jellyfin_url and jellyfin_token:
        try:
            resp = requests.post(
                f"{jellyfin_url.rstrip('/')}/Library/Refresh",
                headers={"X-Emby-Token": jellyfin_token},
                timeout=10,
            )
            if resp.ok:
                logging.info(_("Post-transcode: Jellyfin library refresh triggered"))
            else:
                logging.warning(f"Post-transcode: Jellyfin refresh failed ({resp.status_code})")
        except Exception as e:
            logging.warning(f"Post-transcode: Jellyfin refresh error: {e}")


def run_status(cfg: dict):
    ledger_path = Path(cfg.get("ledger_file", "torrchive_ledger.json"))
    ledger = load_ledger(ledger_path)

    if not ledger:
        logging.info(_("Ledger is empty — no files transcoded yet."))
        return

    total_original = sum(e["original_mb"] for e in ledger)
    total_transcoded = sum(e["transcoded_mb"] for e in ledger)
    saved = total_original - total_transcoded

    logging.info(_("Torrchive status — {} files transcoded").format(len(ledger)))
    logging.info(f"  Original size  : {total_original / 1024:.1f} GB")
    logging.info(f"  Current size   : {total_transcoded / 1024:.1f} GB")
    logging.info(f"  Space saved    : {saved / 1024:.1f} GB ({saved / total_original:.0%})")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=f"Torrchive v{__version__} — Archive transcoder for media libraries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  torrchive.py scan                        # show what would be transcoded
  torrchive.py run --dry-run               # same but formatted as run output
  torrchive.py run                         # transcode all eligible files
  torrchive.py run --limit 10              # transcode 10 files then stop
  torrchive.py run --no-schedule           # ignore time window
  torrchive.py status                      # show space saved so far
  torrchive.py --config /path/config.yaml  # use alternate config file
""",
    )
    parser.add_argument("mode", nargs="?", default="scan",
                        choices=["scan", "run", "status"],
                        help="Operation mode (default: scan)")
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).parent / "config.yaml",
                        help="Path to config file (default: config.yaml next to script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only, don't modify any files")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max files to transcode in this run (0 = unlimited)")
    parser.add_argument("--no-schedule", action="store_true",
                        help="Ignore schedule window, run immediately")
    parser.add_argument("--parallel", type=int, default=1,
                        help="Concurrent transcode jobs (default: 1). "
                             "Tune to your NFS bandwidth and GPU capacity. "
                             "2-3 recommended for NVENC setups.")
    parser.add_argument("--library", nargs="+", default=None,
                        help="Override config paths — scan specific library "
                             "subfolder(s) by name (e.g. --library Anime Films). "
                             "Must be direct children of a configured media path.")
    parser.add_argument("--version", action="version", version=f"Torrchive {__version__}")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"ERROR: Config file not found: {args.config}")
        print(f"Copy config.example.yaml to config.yaml and edit it.")
        sys.exit(1)

    cfg = load_config(args.config)

    # Set up translations before any output
    _.set(setup_i18n(cfg.get("language", "fr")))

    log_file = cfg.get("log_file")
    setup_logging(Path(log_file) if log_file else None)

    logging.info("=" * 60)
    logging.info(f"Torrchive v{__version__} — mode: {args.mode}")

    if args.mode == "status":
        run_status(cfg)
        return

    client = build_torrent_client(cfg.get("torrent_client", {"type": "none"}))
    managed_files = client.get_managed_files()

    profile = build_encoder_profile(cfg.get("encoder", {}))
    logging.info(_("Encoder: {} / {} / quality {} / preset {}").format(profile.backend, profile.codec.upper(), profile.quality, profile.preset) + (f" / max {profile.max_resolution}p" if profile.max_resolution else ""))

    # --library CLI override: filter configured paths by their last component name
    if args.library:
        all_paths = [Path(p) for p in cfg["media"]["paths"]]
        resolved = []
        for lib in args.library:
            matches = [p for p in all_paths if p.name == lib]
            if matches:
                resolved.extend(str(p) for p in matches)
            else:
                logging.warning(_("Library '{}' not found in configured media paths").format(lib))
        if resolved:
            cfg = dict(cfg)
            cfg["media"] = dict(cfg["media"])
            cfg["media"]["paths"] = resolved
            logging.info(_("Library override: {}").format(resolved))
        else:
            logging.error(_("No matching libraries found for: {}. Available: {}").format(args.library, [p.name for p in all_paths]))
            sys.exit(1)

    if args.mode == "scan":
        run_scan(cfg, managed_files, profile)
    elif args.mode == "run":
        run_transcode(cfg, managed_files, profile,
                      dry_run=args.dry_run,
                      limit=args.limit,
                      no_schedule=args.no_schedule,
                      parallel=args.parallel)


if __name__ == "__main__":
    main()
