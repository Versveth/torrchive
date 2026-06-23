# Changelog

All notable changes to Torrchive will be documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Added
- **Interactive setup wizard** (`torrchive.py setup`) — bilingual (FR/EN) first-run wizard that configures media paths, torrent client, encoder, codec/quality, schedule, and output paths
- **Guided menu** — launched when running `torrchive.py` with no arguments; presents numbered options for scan / run / run library / status / setup / quit
- **`--background` flag** — disables Rich progress bars for nohup/systemd runs without a config file edit
- **Thunderbolt eGPU auto-detect** — `hwaccel: auto` in config disables hardware decode automatically when an authorized TB device is found under `/sys/bus/thunderbolt/devices`; `hwaccel: true` always enables it (correct for native PCIe)
- **`probe_stats.py`** — standalone tool to analyse the probe cache and generate a Markdown report by codec
- **`parse_and_schedule.py`** — converts a probe Markdown report into a prioritised ffmpeg batch script

### Fixed
- **qBittorrent 5.0+ login** — now accepts HTTP 204 (empty body) as a valid login response in addition to HTTP 200 "Ok." (legacy)
- **`--parallel` CLI default** — changed from 1 to 0 (unset); 0 now correctly falls back to `performance.parallel` in config instead of silently overriding it

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
