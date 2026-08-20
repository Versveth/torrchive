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
import fcntl
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


tr = _Translator()

# ─── i18n ────────────────────────────────────────────────────────────────────

def _compile_po(po_path: Path, mo_path: Path):
    """Pure Python .po → .mo compiler. No external tools required."""
    import struct
    entries: dict[bytes, bytes] = {}
    msgid = msgstr = None

    with open(po_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("msgid "):
                msgid = line[7:-1]
            elif line.startswith("msgstr "):
                msgstr = line[8:-1]
                if msgid is not None:
                    entries[msgid.encode("utf-8")] = msgstr.encode("utf-8")
                msgid = msgstr = None

    # Add metadata entry so gettext knows the charset
    metadata = b"Content-Type: text/plain; charset=UTF-8\nContent-Transfer-Encoding: 8bit\n"
    entries[b""] = metadata

    keys = sorted(entries.keys())
    ids = b""
    strs = b""
    offsets = []
    for k in keys:
        v = entries[k]
        offsets.append((len(ids), len(k), len(strs), len(v)))
        ids += k + b"\x00"
        strs += v + b"\x00"

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


class RunLockError(Exception):
    """Raised when another `torrchive.py run` is already active."""


def acquire_run_lock(log_file: Optional[Path]):
    """
    Exclusive, non-blocking lock so two `run` invocations can never process
    the same library concurrently — two independent processes can otherwise
    both pick the same pending file, compute the same deterministic temp
    filename (md5 of the source path), and clobber each other's output.
    Returns the open lock file handle; caller must keep a reference to it
    for the lifetime of the run (closing/GC'ing it releases the lock).
    """
    lock_dir = log_file.parent if log_file else Path(__file__).parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "torrchive.run.lock"
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown PID"
        fh.close()
        raise RunLockError(
            f"Another torrchive run is already active (lock held by {holder}). "
            f"Refusing to start a second instance against the same library — "
            f"check `ps aux | grep torrchive` before retrying."
        )
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


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
        # qBittorrent <5.0 returns "Ok.", >=5.0 returns HTTP 204 with empty body
        if resp.status_code == 200 and resp.text.strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {resp.text}")
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"qBittorrent login failed (HTTP {resp.status_code}): {resp.text}")
        logging.info(tr("qBittorrent: authenticated"))

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
        logging.info(tr("Deluge: authenticated"))

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
        logging.info(tr("Deluge: {} managed files").format(len(managed)))
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
        logging.info(tr("Transmission: {} managed files").format(len(managed)))
        return managed


def build_torrent_client(cfg: dict) -> TorrentClient:
    client_type = cfg.get("type", "none").lower()

    if client_type == "none":
        logging.info(tr("Torrent client: none — all files eligible"))
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
    hwaccel: bool = True  # use hardware-accelerated decoding on input


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


def _is_thunderbolt_egpu() -> bool:
    """
    Detect if the GPU is connected via Thunderbolt (eGPU).
    On Linux, authorized TB devices appear under /sys/bus/thunderbolt/devices.
    If any authorized TB device is present, assume the GPU may be on TB.
    """
    tb_path = Path("/sys/bus/thunderbolt/devices")
    if not tb_path.exists():
        return False
    try:
        for device in tb_path.iterdir():
            authorized = device / "authorized"
            if authorized.exists() and authorized.read_text().strip() == "1":
                return True
    except Exception:
        pass
    return False


def _resolve_hwaccel(cfg_value: str, backend: str) -> bool:
    """
    Resolve hwaccel config value to a boolean.
      auto  → enabled, but disabled automatically for Thunderbolt eGPUs
              (TB3 bandwidth limitations make hwaccel decode unstable)
      true  → always enabled
      false → always disabled (CPU decoding — safer for eGPU setups)
    """
    val = str(cfg_value).lower()
    if val == "false":
        return False
    if val == "true":
        return True
    # auto: disable for Thunderbolt eGPUs
    if backend in ("nvenc", "vaapi") and _is_thunderbolt_egpu():
        logging.info(tr("Encoder: Thunderbolt eGPU detected — disabling hwaccel decode for stability"))
        return False
    return True


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
                logging.info(tr("Encoder: auto-detected {}").format(backend))
                return backend
        except Exception:
            pass
    logging.info(tr("Encoder: falling back to software (libx265)"))
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

    hwaccel_cfg = str(cfg.get("hwaccel", "auto")).lower()
    hwaccel = _resolve_hwaccel(hwaccel_cfg, backend)
    if not hwaccel:
        logging.info(tr("Encoder: hardware decode disabled — using CPU for decoding"))

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
        hwaccel=hwaccel,
    )


def build_ffmpeg_cmd(src: Path, dst: Path, profile: EncoderProfile,
                     source_height: int) -> list[str]:
    codec_name = CODEC_MAP[profile.backend][profile.codec]
    quality_flags = QUALITY_FLAG[profile.backend] + [str(profile.quality)]
    hwaccel_flags = HWACCEL_FLAGS[profile.backend] if profile.hwaccel else []
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

    cmd = ["ffmpeg", "-y", *hwaccel_flags, "-fflags", "+genpts", "-stats_period", "1", "-i", str(src), "-max_muxing_queue_size", "9999"]

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
                logging.warning(tr("Probe cache: failed to load ({}), starting fresh").format(e))
                self._data = {}

    def save(self):
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f)
        logging.info(tr("Probe cache: saved {} entries to {}").format(len(self._data), self.path))
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
            logging.info(tr("Probe cache: purged {} stale entries").format(len(stale)))
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
            logging.info(tr("Startup cleanup: removed {} leftover temp file(s)").format(len(found)))
    return len(found)


def scan(media_paths: list[Path], managed_files: set[str],
         min_size_mb: float, workers: int = 16,
         cache: Optional[ProbeCache] = None) -> list[VideoFile]:
    all_files: list[Path] = []
    for base in media_paths:
        if not base.exists():
            logging.warning(tr("Media path not found: {}").format(base))
            continue
        logging.info(tr("Scanning {} ...").format(base))
        all_files.extend(
            f for f in sorted(base.rglob("*"))
            if f.suffix.lower() in VIDEO_EXTENSIONS
            and ".torrchive_tmp_" not in f.name
        )

    cached_count = sum(1 for f in all_files if cache and cache.get(f))
    fresh_count = len(all_files) - cached_count
    logging.info(tr("Found {} video files — {} cached, {} to probe — {} workers...").format(len(all_files), cached_count, fresh_count, workers))

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
                logging.info(tr("Probed {}/{} files...").format(done, len(all_files)))

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

    logging.info(tr("Transcoding: {}").format(src.name))
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
            logging.error(tr("Output file not created."))
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
        logging.info(tr("Done in {}s: {} MB → {} MB ({}% reduction)").format(int(elapsed), int(vf.size_mb), int(new_size_mb), int(reduction)))

        record_transcode(ledger_path, str(src), str(dst), vf.size_mb, new_size_mb)
        return True

    except subprocess.TimeoutExpired:
        logging.error(tr("ffmpeg timed out for {}").format(src))
        tmp.unlink(missing_ok=True)
        return False
    except Exception as e:
        logging.error(tr("Unexpected error: {}").format(e))
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
        console.print(tr("Interrupt received — stopping active jobs..."))
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
                        logging.error(tr("Worker error: {}").format(e))
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
    logging.info(tr("Scan complete:"))
    logging.info(tr("Total video files : {}").format(len(all_files)))
    logging.info(tr("To transcode      : {}").format(len(queue)))
    logging.info(tr("Total size        : {} GB").format(f"{total_size / 1024:.1f}"))
    logging.info(tr("Skipped           : {}").format(len(skipped)))
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        logging.info(f"    - {reason}: {count}")
    logging.info("=" * 60)

    logging.info(tr("Queue (first 20):"))
    for vf in queue[:20]:
        logging.info(f"  [{vf.codec.upper():6}] {vf.size_mb:>7.0f} MB | {vf.path.name}")
    if len(queue) > 20:
        logging.info(tr("... and {} more").format(len(queue) - 20))


def run_transcode(cfg: dict, managed_files: set[str], profile: EncoderProfile,
                  dry_run: bool, limit: int, no_schedule: bool, parallel: int = 1,
                  background: bool = False):
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
    logging.info(tr("Queue: {} files, {} GB to process").format(len(queue), f"{total_size / 1024:.1f}"))

    if dry_run:
        logging.info("\n[DRY RUN] Files that would be transcoded:")
        for vf in queue:
            logging.info(f"  [{vf.codec.upper():6}] {vf.size_mb:>7.0f} MB | {vf.path}")
        return

    target = queue[:limit] if limit > 0 else queue
    # 0 = not explicitly set — fall back to config value
    if parallel == 0:
        parallel = cfg_parallel
    parallel = max(1, parallel)
    success = 0
    failed = 0
    saved_mb = 0.0

    # --background flag overrides config display setting
    if background:
        use_progress = False
    else:
        use_progress = cfg.get("display", {}).get("progress_bars", True) and RICH_AVAILABLE

    logging.info(tr("Starting transcode: {} files, {} parallel job(s)").format(len(target), parallel))

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
                        logging.error(tr("Worker error: {}").format(e))
                        failed += 1

    logging.info(tr("Pipeline complete: {} transcoded, {} failed").format(success, failed))

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
                logging.info(tr("Post-transcode: Plex library refresh triggered"))
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
                logging.info(tr("Post-transcode: Jellyfin library refresh triggered"))
            else:
                logging.warning(f"Post-transcode: Jellyfin refresh failed ({resp.status_code})")
        except Exception as e:
            logging.warning(f"Post-transcode: Jellyfin refresh error: {e}")


def run_status(cfg: dict):
    ledger_path = Path(cfg.get("ledger_file", "torrchive_ledger.json"))
    ledger = load_ledger(ledger_path)

    if not ledger:
        logging.info(tr("Ledger is empty — no files transcoded yet."))
        return

    total_original = sum(e["original_mb"] for e in ledger)
    total_transcoded = sum(e["transcoded_mb"] for e in ledger)
    saved = total_original - total_transcoded

    logging.info(tr("Torrchive status — {} files transcoded").format(len(ledger)))
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
    parser.add_argument("mode", nargs="?", default=None,
                        choices=["scan", "run", "status", "setup"],
                        help="Operation mode — omit to launch interactive menu")
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).parent / "config.yaml",
                        help="Path to config file (default: config.yaml next to script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only, don't modify any files")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max files to transcode in this run (0 = unlimited)")
    parser.add_argument("--no-schedule", action="store_true",
                        help="Ignore schedule window, run immediately")
    parser.add_argument("--parallel", type=int, default=0,
                        help="Concurrent transcode jobs (default: 1). "
                             "Tune to your NFS bandwidth and GPU capacity. "
                             "2-3 recommended for NVENC setups.")
    parser.add_argument("--background", action="store_true",
                        help="Disable progress bars for background/nohup runs. "
                             "Equivalent to display.progress_bars: false in config.")
    parser.add_argument("--library", nargs="+", default=None,
                        help="Override config paths — scan specific library "
                             "subfolder(s) by name (e.g. --library Anime Films). "
                             "Must be direct children of a configured media path.")
    parser.add_argument("--version", action="version", version=f"Torrchive {__version__}")
    args = parser.parse_args()

    if args.mode == "setup":
        run_setup(args.config)
        return

    if not args.config.exists():
        print(f"No config.yaml found. Run: python3 torrchive.py setup")
        print(f"Or copy config.example.yaml to config.yaml and edit it.")
        sys.exit(1)

    cfg = load_config(args.config)

    # Set up translations before any output
    tr.set(setup_i18n(cfg.get("language", "fr")))

    # Interactive menu when launched with no arguments
    if args.mode is None and not any([
        args.dry_run, args.limit, args.no_schedule,
        args.parallel > 1, args.library
    ]):
        mode, _, dry_run, no_schedule, parallel, libraries, background = run_guided_menu(cfg, args.config)
        if mode == "setup":
            run_setup(args.config)
            return
        args.mode = mode
        if dry_run:
            args.dry_run = True
        if no_schedule:
            args.no_schedule = True
        if parallel > 1:
            args.parallel = parallel
        if libraries:
            args.library = libraries
        if background:
            args.background = True
    elif args.mode is None:
        args.mode = "scan"

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
    logging.info(tr("Encoder: {} / {} / quality {} / preset {}").format(profile.backend, profile.codec.upper(), profile.quality, profile.preset) + (f" / max {profile.max_resolution}p" if profile.max_resolution else ""))

    # --library CLI override: filter configured paths by their last component name
    if args.library:
        all_paths = [Path(p) for p in cfg["media"]["paths"]]
        resolved = []
        for lib in args.library:
            matches = [p for p in all_paths if p.name == lib]
            if matches:
                resolved.extend(str(p) for p in matches)
            else:
                logging.warning(tr("Library '{}' not found in configured media paths").format(lib))
        if resolved:
            cfg = dict(cfg)
            cfg["media"] = dict(cfg["media"])
            cfg["media"]["paths"] = resolved
            logging.info(tr("Library override: {}").format(resolved))
        else:
            logging.error(tr("No matching libraries found for: {}. Available: {}").format(args.library, [p.name for p in all_paths]))
            sys.exit(1)

    if args.mode == "scan":
        run_scan(cfg, managed_files, profile)
    elif args.mode == "run":
        if not args.dry_run:
            try:
                _run_lock_fh = acquire_run_lock(Path(log_file) if log_file else None)
            except RunLockError as e:
                logging.error(str(e))
                sys.exit(1)
        run_transcode(cfg, managed_files, profile,
                      dry_run=args.dry_run,
                      limit=args.limit,
                      no_schedule=args.no_schedule,
                      parallel=args.parallel,
                      background=args.background)


# ─── Setup wizard ─────────────────────────────────────────────────────────────

# Wizard strings in all supported languages (needed before translations load)
_WIZARD_STRINGS = {
    "fr": {
        "lang_prompt":     "Choisissez votre langue / Select your language",
        "welcome":         "Bienvenue dans Torrchive — Assistant de configuration",
        "welcome_sub":     "Répondez aux questions suivantes pour créer votre config.yaml.",
        "section_media":   "BIBLIOTHÈQUES MÉDIA",
        "how_many_libs":   "Combien de bibliothèques voulez-vous configurer ?",
        "lib_path":        "Chemin de la bibliothèque {}",
        "path_ok":         "Chemin valide : {}",
        "path_missing":    "Chemin introuvable : {}",
        "nfs_detected":    "Ce chemin ressemble à un montage NFS. Est-il déjà monté ?",
        "nfs_fstab":       "Pour monter automatiquement au démarrage, ajoutez à /etc/fstab :",
        "nfs_fstab_entry": "{}:{} {} nfs4 soft,timeo=30,retrans=3,vers=4.1,_netdev 0 0",
        "nfs_mount_now":   "Voulez-vous monter ce chemin maintenant (sudo mount -a) ?",
        "nfs_mount_cmd":   "Lancez : sudo mount -a",
        "add_another":     "Ajouter une autre bibliothèque ?",
        "section_client":  "CLIENT TORRENT",
        "client_type":     "Type de client torrent",
        "client_url":      "URL du client (ex: http://192.168.1.1:8080)",
        "client_user":     "Nom d'utilisateur",
        "client_pass":     "Mot de passe",
        "client_test":     "Test de connexion...",
        "client_ok":       "Connexion réussie ({} fichiers gérés)",
        "client_fail":     "Échec de connexion : {}",
        "section_encoder": "ENCODEUR",
        "detected":        "Détecté automatiquement : {}",
        "confirm_encoder": "Utiliser {} comme backend d'encodage ?",
        "encoder_choice":  "Choisissez un backend",
        "section_codec":   "CODEC ET QUALITÉ",
        "codec_choice":    "Codec cible",
        "codec_hevc_desc": "HEVC (H.265) — meilleure compatibilité, recommandé",
        "codec_av1_desc":  "AV1 — fichiers plus petits, clients récents uniquement",
        "codec_h264_desc": "H.264 — compatibilité maximale, fichiers plus grands",
        "quality_prompt":  "Valeur de qualité CQ/CRF (défaut: 26, plage recommandée: 22-28)",
        "preset_prompt":   "Preset d'encodage (défaut: p6 pour NVENC, medium pour logiciel)",
        "section_perf":    "PERFORMANCES",
        "parallel_prompt": "Nombre de jobs parallèles (défaut: {})",
        "parallel_hint":   "Conseil : commencez par {} et ajustez selon l'utilisation GPU",
        "workers_prompt":  "Workers de scan parallèles (défaut: 16)",
        "section_sched":   "PLANIFICATION",
        "sched_enable":    "Activer une fenêtre horaire (ex: heures solaires) ?",
        "sched_start":     "Heure de début (format HH:MM)",
        "sched_stop":      "Heure de fin (format HH:MM)",
        "section_paths":   "CHEMINS DE SORTIE",
        "log_prompt":      "Fichier de log",
        "ledger_prompt":   "Fichier de bilan (espace économisé)",
        "cache_prompt":    "Fichier de cache de sonde",
        "section_preview": "APERÇU DE LA CONFIGURATION",
        "confirm_write":   "Écrire cette configuration dans {} ?",
        "written":         "Configuration sauvegardée dans {}",
        "run_scan":        "Lancer un scan maintenant pour vérifier ?",
        "done":            "Configuration terminée. Lancez : python3 torrchive.py scan",
        "press_enter_retry": "Montez le chemin puis appuyez sur Entrée pour réessayer...",
        "mounts_available":  "Chemins montés disponibles :",
        "mount_manual":      "Entrer un chemin manuellement",
        "mount_select":      "Sélectionnez une bibliothèque",
        "no_mounts":         "Aucun montage détecté. Entrez le chemin manuellement.",
        "existing_config":   "Un fichier config.yaml existe déjà. Écraser ?",
        "menu_title":        "Que souhaitez-vous faire ?",
        "menu_scan":         "Scanner la bibliothèque (aperçu sans modification)",
        "menu_run":          "Lancer le transcodage",
        "menu_run_lib":      "Lancer le transcodage sur une bibliothèque spécifique",
        "menu_status":       "Afficher l'espace économisé",
        "menu_setup":        "Reconfigurer Torrchive",
        "menu_quit":         "Quitter",
        "menu_choice":       "Votre choix",
        "menu_lib_choice":   "Bibliothèque",
        "menu_parallel":     "Nombre de jobs parallèles",
        "menu_schedule":     "Respecter la fenêtre horaire configurée",
        "menu_dry_run":      "Simulation uniquement (aucun fichier modifié)",
        "menu_bg_title":     "Mode d'exécution",
        "menu_fg":           "Interactif — barres de progression (fermer le terminal arrête le processus)",
        "menu_bg":           "Arrière-plan — logs seuls (résiste à la fermeture du terminal)",
        "menu_bg_choice":    "Mode d'exécution",
        "menu_bg_warn":      "Attention : fermer cette fenêtre ou perdre la connexion SSH arrêtera le transcodage.",
        "menu_bg_cmd":       "Pour lancer en arrière-plan sans risque, utilisez :",
        "hwaccel_auto":      "Décodage matériel : auto (désactivé si eGPU Thunderbolt détecté)",
        "hwaccel_prompt":    "Décodage matériel sur l'entrée (auto/true/false)",
        "tb_detected":       "eGPU Thunderbolt détecté — décodage matériel désactivé pour la stabilité",
        "hwaccel_disabled":  "Décodage matériel désactivé — décodage CPU utilisé",
        "skip":            "Ignorer / conserver la valeur par défaut",
        "yes": "oui", "no": "non",
    },
    "en": {
        "lang_prompt":     "Select your language / Choisissez votre langue",
        "welcome":         "Welcome to Torrchive — Setup Wizard",
        "welcome_sub":     "Answer the following questions to create your config.yaml.",
        "section_media":   "MEDIA LIBRARIES",
        "how_many_libs":   "How many libraries do you want to configure?",
        "lib_path":        "Path for library {}",
        "path_ok":         "Valid path: {}",
        "path_missing":    "Path not found: {}",
        "nfs_detected":    "This path looks like an NFS mount. Is it already mounted?",
        "nfs_fstab":       "To mount automatically at boot, add to /etc/fstab:",
        "nfs_fstab_entry": "{}:{} {} nfs4 soft,timeo=30,retrans=3,vers=4.1,_netdev 0 0",
        "nfs_mount_now":   "Mount this path now (sudo mount -a)?",
        "nfs_mount_cmd":   "Run: sudo mount -a",
        "add_another":     "Add another library?",
        "section_client":  "TORRENT CLIENT",
        "client_type":     "Torrent client type",
        "client_url":      "Client URL (e.g. http://192.168.1.1:8080)",
        "client_user":     "Username",
        "client_pass":     "Password",
        "client_test":     "Testing connection...",
        "client_ok":       "Connection successful ({} managed files)",
        "client_fail":     "Connection failed: {}",
        "section_encoder": "ENCODER",
        "detected":        "Auto-detected: {}",
        "confirm_encoder": "Use {} as encoding backend?",
        "encoder_choice":  "Choose a backend",
        "section_codec":   "CODEC AND QUALITY",
        "codec_choice":    "Target codec",
        "codec_hevc_desc": "HEVC (H.265) — best compatibility, recommended",
        "codec_av1_desc":  "AV1 — smallest files, modern clients only",
        "codec_h264_desc": "H.264 — maximum compatibility, larger files",
        "quality_prompt":  "Quality value CQ/CRF (default: 26, recommended range: 22-28)",
        "preset_prompt":   "Encoding preset (default: p6 for NVENC, medium for software)",
        "section_perf":    "PERFORMANCE",
        "parallel_prompt": "Number of parallel jobs (default: {})",
        "parallel_hint":   "Tip: start with {} and adjust based on GPU usage",
        "workers_prompt":  "Parallel scan workers (default: 16)",
        "section_sched":   "SCHEDULE",
        "sched_enable":    "Enable a time window (e.g. solar hours)?",
        "sched_start":     "Start time (HH:MM format)",
        "sched_stop":      "Stop time (HH:MM format)",
        "section_paths":   "OUTPUT PATHS",
        "log_prompt":      "Log file",
        "ledger_prompt":   "Ledger file (space savings)",
        "cache_prompt":    "Probe cache file",
        "section_preview": "CONFIGURATION PREVIEW",
        "confirm_write":   "Write this configuration to {}?",
        "written":         "Configuration saved to {}",
        "run_scan":        "Run a scan now to verify?",
        "done":            "Setup complete. Run: python3 torrchive.py scan",
        "press_enter_retry": "Mount the path then press Enter to retry...",
        "mounts_available":  "Available mounted paths:",
        "mount_manual":      "Enter path manually",
        "mount_select":      "Select a library",
        "no_mounts":         "No mounts detected. Enter path manually.",
        "existing_config":   "A config.yaml already exists. Overwrite?",
        "menu_title":        "What would you like to do?",
        "menu_scan":         "Scan library (preview, no changes)",
        "menu_run":          "Run transcoding",
        "menu_run_lib":      "Run transcoding on a specific library",
        "menu_status":       "Show space savings",
        "menu_setup":        "Reconfigure Torrchive",
        "menu_quit":         "Quit",
        "menu_choice":       "Your choice",
        "menu_lib_choice":   "Library",
        "menu_parallel":     "Number of parallel jobs",
        "menu_schedule":     "Respect configured schedule window",
        "menu_dry_run":      "Dry run only (no files modified)",
        "menu_bg_title":     "Execution mode",
        "menu_fg":           "Interactive — progress bars (closing terminal stops the process)",
        "menu_bg":           "Background — log only (survives terminal close)",
        "menu_bg_choice":    "Execution mode",
        "menu_bg_warn":      "Warning: closing this window or losing the SSH session will stop transcoding.",
        "menu_bg_cmd":       "To run safely in the background, use:",
        "hwaccel_auto":      "Hardware decode: auto (disabled if Thunderbolt eGPU detected)",
        "hwaccel_prompt":    "Hardware decode on input (auto/true/false)",
        "tb_detected":       "Thunderbolt eGPU detected — disabling hwaccel decode for stability",
        "hwaccel_disabled":  "Hardware decode disabled — using CPU for decoding",
        "skip":            "Skip / keep default",
        "yes": "yes", "no": "no",
    },
}


def _w(lang: str, key: str, *args) -> str:
    """Get wizard string for given language, optionally formatted."""
    s = _WIZARD_STRINGS.get(lang, _WIZARD_STRINGS["fr"]).get(key, key)
    return s.format(*args) if args else s


def _section(console, lang: str, key: str):
    console.print(f"\n[bold cyan]── {_w(lang, key)} ──[/]")


def _prompt(console, lang: str, key: str, default: str = "", *args) -> str:
    label = _w(lang, key, *args)
    if default:
        label += f" [{default}]"
    console.print(f"[yellow]{label}:[/] ", end="")
    val = input().strip()
    return val if val else default


def _confirm(console, lang: str, key: str, default: bool = True) -> bool:
    yes = _w(lang, "yes")[0].lower()
    no = _w(lang, "no")[0].lower()
    hint_yes = yes.upper() if default else yes
    hint_no = no if default else no.upper()
    console.print(f"[yellow]{_w(lang, key)}[/yellow] [{hint_yes}/{hint_no}]: ", end="")
    val = input().strip().lower()
    if not val:
        return default
    return val.startswith(yes)


def _get_mounted_paths() -> list[str]:
    """
    Return list of mounted paths that look like media directories.
    Excludes system mounts (/, /boot, /sys, /proc, /dev, /run, /tmp etc.)
    """
    skip_prefixes = ("/sys", "/proc", "/dev", "/run", "/tmp", "/boot",
                     "/snap", "/var/lib", "/usr", "/home")
    found = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mount_point = parts[1]
                if mount_point == "/":
                    continue
                if any(mount_point.startswith(p) for p in skip_prefixes):
                    continue
                if mount_point not in found:
                    found.append(mount_point)
    except Exception:
        pass
    return sorted(found)


def _detect_nfs_server(path: str) -> tuple[str, str] | None:
    """
    Check /proc/mounts to see if path is already an NFS mount.
    Returns (server, export) or None.
    """
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == path and "nfs" in parts[2]:
                    server_export = parts[0]
                    if ":" in server_export:
                        server, export = server_export.split(":", 1)
                        return server, export
    except Exception:
        pass
    return None


def _suggest_parallel() -> int:
    """Suggest parallel job count based on detected GPU."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return 3  # NVENC sweet spot
    except Exception:
        pass
    try:
        import multiprocessing
        cores = multiprocessing.cpu_count()
        return max(1, cores // 2)
    except Exception:
        return 1


def run_guided_menu(cfg: dict, config_path: Path):
    """
    Interactive menu shown when torrchive.py is launched with no arguments.
    Lets the user choose what to do without needing to know CLI flags.
    """
    if not RICH_AVAILABLE:
        print("1. scan  2. run  3. status  4. setup")
        choice = input("Choice: ").strip()
        mode_map = {"1": "scan", "2": "run", "3": "status", "4": "setup"}
        return mode_map.get(choice, "scan"), {}, False, False, 0, [], False

    from rich.console import Console
    from rich.panel import Panel
    console = Console()

    lang = cfg.get("language", "fr")
    tr.set(setup_i18n(lang))

    console.print(Panel(
        f"[bold cyan]Torrchive v{__version__}[/]",
        border_style="cyan", expand=False
    ))

    console.print(f"\n[bold]{_w(lang, 'menu_title')}[/]\n")
    options = [
        ("scan",   _w(lang, "menu_scan")),
        ("run",    _w(lang, "menu_run")),
        ("runlib", _w(lang, "menu_run_lib")),
        ("status", _w(lang, "menu_status")),
        ("setup",  _w(lang, "menu_setup")),
        ("quit",   _w(lang, "menu_quit")),
    ]
    for i, (_, label) in enumerate(options, 1):
        console.print(f"  [bold cyan]{i}.[/] {label}")

    console.print(f"\n[yellow]{_w(lang, 'menu_choice')} (1-{len(options)}):[/] ", end="")
    choice = input().strip()

    if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
        return "scan", {}, False, False, 0, []

    mode_key = options[int(choice) - 1][0]

    if mode_key == "quit":
        sys.exit(0)

    if mode_key == "setup":
        return "setup", {}, False, False, 0, [], False

    if mode_key in ("scan", "status"):
        return mode_key, {}, False, False, 0, [], False

    # run or runlib — ask additional options
    kwargs: dict = {}
    libraries: list = []

    if mode_key == "runlib":
        configured_libs = [Path(p).name for p in cfg.get("media", {}).get("paths", [])]
        if configured_libs:
            console.print(f"\n[cyan]{_w(lang, 'menu_lib_choice')}:[/]")
            for i, lib in enumerate(configured_libs, 1):
                console.print(f"  [bold]{i}.[/] {lib}")
            console.print(f"[yellow]{_w(lang, 'menu_choice')}:[/] ", end="")
            lib_choice = input().strip()
            if lib_choice.isdigit() and 1 <= int(lib_choice) <= len(configured_libs):
                libraries = [configured_libs[int(lib_choice) - 1]]

    # Parallel jobs
    cfg_parallel = int(cfg.get("performance", {}).get("parallel", 1))
    console.print(f"[yellow]{_w(lang, 'menu_parallel')} [{cfg_parallel}]:[/] ", end="")
    p_input = input().strip()
    parallel = int(p_input) if p_input.isdigit() else cfg_parallel

    # Schedule
    no_schedule = not _confirm(console, lang, "menu_schedule",
                               cfg.get("schedule", {}).get("enabled", False))

    # Dry run
    dry_run = _confirm(console, lang, "menu_dry_run", False)

    # Execution mode — foreground (progress bars) or background
    console.print(f"\n[cyan]{_w(lang, 'menu_bg_title')}:[/]")
    console.print(f"  [bold]1.[/] {_w(lang, 'menu_fg')}")
    console.print(f"  [bold]2.[/] {_w(lang, 'menu_bg')}")
    console.print(f"[yellow]{_w(lang, 'menu_bg_choice')} [1/2]: [/yellow]", end="")
    bg_choice = input().strip()
    background = bg_choice == "2"

    if not background:
        console.print(f"\n[yellow]{_w(lang, 'menu_bg_warn')}[/]")

    return "run", {"parallel": parallel, "dry_run": dry_run,
                   "no_schedule": no_schedule, "background": background}, \
           dry_run, no_schedule, parallel, libraries, background


def run_setup(config_path: Path):
    """Interactive first-run setup wizard."""
    if not RICH_AVAILABLE:
        print("ERROR: rich is required for the setup wizard.")
        print("Run: pip install rich")
        sys.exit(1)

    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    console = Console()

    # ── Existing config check ─────────────────────────────────────────────────
    if config_path.exists():
        from rich.console import Console as _C
        _c = _C()
        _c.print("\n[yellow]1. Français  2. English[/]")
        _c.print("config.yaml existe déjà / config.yaml already exists. Écraser / Overwrite? [o/y/N]: ", end="")
        ans = input().strip().lower()
        if ans not in ("o", "y"):
            _c.print("[dim]Configuration conservée.[/]")
            return

    # ── Language selection (shown in all languages) ───────────────────────────
    console.print("\n[bold]1. Français  2. English[/]")
    console.print(f"[yellow]{_WIZARD_STRINGS['fr']['lang_prompt']} (1/2):[/] ", end="")
    lang_choice = input().strip()
    lang = "en" if lang_choice == "2" else "fr"

    console.print(Panel(
        f"[bold]{_w(lang, 'welcome')}[/]\n{_w(lang, 'welcome_sub')}",
        border_style="cyan"
    ))

    cfg: dict = {}

    # ── Media paths ───────────────────────────────────────────────────────────
    _section(console, lang, "section_media")
    n_libs = int(_prompt(console, lang, "how_many_libs", "1") or "1")
    paths = []
    mounted = _get_mounted_paths()

    for i in range(n_libs):
        while True:
            path_str = ""

            if mounted:
                console.print(f"\n[cyan]{_w(lang, 'mounts_available')}[/]")
                for idx, mp in enumerate(mounted, 1):
                    console.print(f"  [bold]{idx}.[/] {mp}")
                console.print(f"  [bold]{len(mounted) + 1}.[/] {_w(lang, 'mount_manual')}")
                console.print(f"[yellow]{_w(lang, 'mount_select')} (1-{len(mounted) + 1}):[/] ", end="")
                choice = input().strip()
                if choice.isdigit():
                    c = int(choice)
                    if 1 <= c <= len(mounted):
                        path_str = mounted[c - 1]
                    elif c == len(mounted) + 1:
                        path_str = ""  # fall through to manual
                if not path_str:
                    console.print(f"[yellow]{_w(lang, 'lib_path', i + 1)}:[/] ", end="")
                    path_str = input().strip()
            else:
                console.print(f"[dim]{_w(lang, 'no_mounts')}[/]")
                console.print(f"[yellow]{_w(lang, 'lib_path', i + 1)}:[/] ", end="")
                path_str = input().strip()

            if not path_str:
                continue

            p = Path(path_str)
            if p.exists():
                console.print(f"[green]{_w(lang, 'path_ok', path_str)}[/]")
                paths.append(path_str)
                if path_str in mounted:
                    mounted.remove(path_str)
                break

            # Path not found — show fstab hint and let user retry
            console.print(f"[yellow]{_w(lang, 'path_missing', path_str)}[/]")
            if path_str.startswith("/mnt/"):
                console.print(f"[dim]{_w(lang, 'nfs_fstab')}[/]")
                console.print(f"[dim]  192.168.x.x:/volume1/your/export {path_str} nfs4 soft,timeo=30,retrans=3,vers=4.1,_netdev 0 0[/]")
                console.print(f"[dim]{_w(lang, 'nfs_mount_cmd')}[/]")

            console.print(f"[dim]{_w(lang, 'press_enter_retry')}[/]", end="")
            input()

            if p.exists():
                console.print(f"[green]{_w(lang, 'path_ok', path_str)}[/]")
                paths.append(path_str)
                if path_str in mounted:
                    mounted.remove(path_str)
                break

            if _confirm(console, lang, "nfs_detected", False):
                paths.append(path_str)
                break

    cfg["media"] = {"paths": paths, "min_size_mb": 100}

    # ── Torrent client ────────────────────────────────────────────────────────
    _section(console, lang, "section_client")
    client_options = ["qbittorrent", "deluge", "transmission", "none"]
    for i, c in enumerate(client_options, 1):
        console.print(f"  {i}. {c}")
    console.print(f"[yellow]{_w(lang, 'client_type')} (1-4, défaut: 1):[/] ", end="")
    c_choice = input().strip()
    c_idx = (int(c_choice) - 1) if c_choice.isdigit() and 1 <= int(c_choice) <= 4 else 0
    client_type = client_options[c_idx]

    client_cfg: dict = {"type": client_type}
    if client_type != "none":
        client_cfg["url"] = _prompt(console, lang, "client_url", "http://192.168.1.1:8080")
        client_cfg["username"] = _prompt(console, lang, "client_user", "admin")
        console.print(f"[yellow]{_w(lang, 'client_pass')}:[/] ", end="")
        import getpass
        client_cfg["password"] = getpass.getpass("")

        console.print(f"[dim]{_w(lang, 'client_test')}[/]")
        try:
            test_client = build_torrent_client(client_cfg)
            files = test_client.get_managed_files()
            console.print(f"[green]{_w(lang, 'client_ok', len(files))}[/]")
        except Exception as e:
            console.print(f"[red]{_w(lang, 'client_fail', e)}[/]")

    cfg["torrent_client"] = client_cfg

    # ── Encoder ───────────────────────────────────────────────────────────────
    _section(console, lang, "section_encoder")
    detected = detect_backend()
    console.print(f"[green]{_w(lang, 'detected', detected)}[/]")
    if not _confirm(console, lang, "confirm_encoder", True):
        backends = ["auto", "nvenc", "vaapi", "videotoolbox", "software"]
        for i, b in enumerate(backends, 1):
            console.print(f"  {i}. {b}")
        console.print(f"[yellow]{_w(lang, 'encoder_choice')} (1-5):[/] ", end="")
        b_choice = input().strip()
        detected = backends[(int(b_choice) - 1)] if b_choice.isdigit() and 1 <= int(b_choice) <= 5 else "auto"

    # ── Codec & quality ───────────────────────────────────────────────────────
    _section(console, lang, "section_codec")
    console.print(f"  1. {_w(lang, 'codec_hevc_desc')}")
    console.print(f"  2. {_w(lang, 'codec_av1_desc')}")
    console.print(f"  3. {_w(lang, 'codec_h264_desc')}")
    console.print(f"[yellow]{_w(lang, 'codec_choice')} (1-3, défaut: 1):[/] ", end="")
    codec_choice = input().strip()
    codec = ["hevc", "av1", "h264"][(int(codec_choice) - 1) if codec_choice.isdigit() and 1 <= int(codec_choice) <= 3 else 0]

    default_preset = "p6" if detected == "nvenc" else "medium"
    quality = _prompt(console, lang, "quality_prompt", "26")
    preset = _prompt(console, lang, "preset_prompt", default_preset)

    cfg["encoder"] = {
        "backend": detected,
        "codec": codec,
        "quality": int(quality) if quality.isdigit() else 26,
        "preset": preset or default_preset,
        "max_resolution": None,
        "audio": "copy",
        "skip_if_already_optimal": True,
        "skip_source_codecs": ["av1", "vp9"] if codec == "hevc" else [],
        "normalize_filename": True,
    }

    # ── Performance ───────────────────────────────────────────────────────────
    _section(console, lang, "section_perf")
    suggested = _suggest_parallel()
    console.print(f"[dim]{_w(lang, 'parallel_hint', suggested)}[/]")
    parallel = _prompt(console, lang, "parallel_prompt", str(suggested))
    workers = _prompt(console, lang, "workers_prompt", "16")

    cfg["performance"] = {
        "scan_workers": int(workers) if workers.isdigit() else 16,
        "parallel": int(parallel) if parallel.isdigit() else suggested,
    }

    # ── Schedule ──────────────────────────────────────────────────────────────
    _section(console, lang, "section_sched")
    sched_enabled = _confirm(console, lang, "sched_enable", False)
    sched_start = "09:00"
    sched_stop = "20:00"
    if sched_enabled:
        sched_start = _prompt(console, lang, "sched_start", "09:00")
        sched_stop = _prompt(console, lang, "sched_stop", "20:00")

    cfg["schedule"] = {"enabled": sched_enabled, "start": sched_start, "stop": sched_stop}

    # ── Output paths ──────────────────────────────────────────────────────────
    _section(console, lang, "section_paths")
    default_dir = str(Path.home() / "torrchive")
    log = _prompt(console, lang, "log_prompt", f"{default_dir}/torrchive.log")
    ledger = _prompt(console, lang, "ledger_prompt", f"{default_dir}/torrchive_ledger.json")
    cache = _prompt(console, lang, "cache_prompt", f"{default_dir}/torrchive_probe_cache.json")

    cfg["language"] = lang
    cfg["display"] = {"progress_bars": True}
    cfg["post_transcode"] = {"enabled": False}
    cfg["log_file"] = log
    cfg["ledger_file"] = ledger
    cfg["probe_cache_file"] = cache

    # ── Preview + write ───────────────────────────────────────────────────────
    _section(console, lang, "section_preview")
    yaml_preview = yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False)
    console.print(Syntax(yaml_preview, "yaml", theme="monokai"))

    if _confirm(console, lang, "confirm_write", True):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        console.print(f"[green]{_w(lang, 'written', config_path)}[/]")

        if _confirm(console, lang, "run_scan", True):
            tr.set(setup_i18n(lang))
            setup_logging(Path(log) if log else None)
            client = build_torrent_client(cfg.get("torrent_client", {"type": "none"}))
            managed = client.get_managed_files()
            profile = build_encoder_profile(cfg.get("encoder", {}))
            run_scan(cfg, managed, profile)
    else:
        console.print(f"[dim]{_w(lang, 'done')}[/]")


if __name__ == "__main__":
    main()
