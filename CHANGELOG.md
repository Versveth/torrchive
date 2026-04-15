# Changelog

All notable changes to Torrchive will be documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## [0.1.2] - 2026-04-15

### Added
- **Launcher scripts** — `start.sh` (Linux/macOS) and `start.bat` (Windows)
  - Auto-detects Python 3.10+, installs missing dependencies, checks ffmpeg
  - Launches setup wizard automatically on first run
  - No terminal or Python knowledge required for Windows users
- **Interactive setup wizard** (`python3 torrchive.py setup`)
  - Language selection first — all subsequent prompts in chosen language
  - Media path validation with NFS mount detection and fstab guidance
  - Live torrent client connection test during setup
  - GPU auto-detection with parallel job suggestion
  - Full config preview before writing
  - Optional scan immediately after setup

---

## [0.1.1] - 2026-04-15

### Added
- **Rich progress bars** — interactive per-job progress with ETA and live space saved counter
- **`display.progress_bars`** config option — set `false` for background/nohup runs, `true` for interactive use
- **Auto-cleanup on startup** — leftover `.torrchive_tmp_*` files from interrupted runs are removed automatically
- **Cleanup prompt on exit** — when exiting interactively, prompts to delete any incomplete temp files
- **`--library` error handling** — aborts with a helpful message listing available libraries when no match is found

### Fixed
- Single Ctrl+C now stops active jobs and exits cleanly without requiring a second interrupt
- Probe cache no longer purges entries from unscanned libraries when using `--library` to target a specific one
- Tmp files excluded from library scan (previously caused probe errors on restart)
- UTF-8 decode errors on ffmpeg stderr from files with non-Latin metadata
- Filename-too-long errors on titles exceeding 255 bytes (hash-based tmp filenames)
- MP4 sources with embedded thumbnails failing due to MJPEG stream picked up by `-map 0:v`
- `--library` falling back to full scan silently when no match found

### Changed
- `audio` default changed to `copy` — preserves original audio tracks including surround sound
- `skip_source_codecs` now configurable (replaces hardcoded AV1/VP9 skip)
- `skip_if_already_optimal` replaces hardcoded HEVC skip logic
- `parallel` moved to `performance` config section (CLI `--parallel` still overrides)
- Post-transcode Plex/Jellyfin hooks implemented (disabled by default)

---

## [0.1.0] - 2026-04-14

### Added
- Initial release
- Torrent client abstraction: qBittorrent, Deluge, Transmission, none
- Encoder backend abstraction: NVENC (NVIDIA), VAAPI (Intel/AMD), VideoToolbox (Apple), software fallback, auto-detect
- Codec targets: HEVC (H.265), AV1, H.264
- Resolution downscaling support (e.g. 4K → 1080p)
- Parallel transcoding with configurable job count
- Parallel library scan with configurable worker count
- Persistent probe cache — instant rescans after first run
- Schedule window — restrict runs to solar/off-peak hours
- Space savings ledger
- Filename normalisation — rewrites stale codec/resolution tokens post-transcode
- `--library` flag to target specific libraries without editing config
- YAML config with environment variable interpolation (`${VAR}` syntax)
- Bilingual README (French / English)
