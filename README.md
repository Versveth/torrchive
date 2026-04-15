<div align="center">

# Torrchive

<a href="https://ko-fi.com/versveth">
  <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Support me on Ko-fi" />
</a>

</div>

[![Ko-fi](https://img.shields.io/badge/Soutenir-Ko--fi-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/versveth)

**Torrchive** transcode les fichiers média que vous ne seedez plus vers des formats optimisés, en les conservant dans votre bibliothèque Plex ou Jellyfin à une fraction de leur taille d'origine.

> *« Je l'ai téléchargé, seedé, c'est terminé — Torrchive le convertit silencieusement en arrière-plan. »*

---

## Fonctionnalités

- **Conscience du client torrent** — les fichiers encore gérés par votre client (quel que soit leur état) ne sont jamais touchés
- **Encodage matériel** — NVENC (NVIDIA), VAAPI (Intel/AMD), VideoToolbox (Apple), ou encodage logiciel en fallback
- **Flexibilité des codecs** — HEVC, AV1 ou H.264 comme cible
- **Réduction de résolution** — optionnelle, ex. 4K → 1080p
- **Normalisation des noms de fichiers** — réécrit les tokens de codec/résolution obsolètes après le transcodage
- **Fenêtre horaire** — limite les exécutions aux heures solaires ou creuses
- **Scan parallèle** — analyse de bibliothèque rapide avec un nombre de workers configurable
- **Journal d'espace** — suit précisément l'espace économisé au fil du temps
- **Cache de sonde** — mémorise les résultats ffprobe entre les exécutions pour des scans instantanés

---

## Prérequis

- Python 3.10+
- `ffmpeg` et `ffprobe` dans votre PATH
- `pip install pyyaml requests rich`

---

## Démarrage rapide

```bash
# 1. Cloner ou télécharger
git clone https://github.com/Versveth/torrchive.git
cd torrchive

# 2. Installer les dépendances
pip install pyyaml requests

# 3. Créer votre configuration
cp config.example.yaml config.yaml
# Éditez config.yaml selon votre installation

# 4. Scanner d'abord — voir ce qui serait transcodé
python3 torrchive.py scan

# 5. Simulation — aucun fichier modifié
python3 torrchive.py run --dry-run

# 6. Lancer
python3 torrchive.py run
```

---

## Modes

| Commande | Description |
|---|---|
| `torrchive.py scan` | Analyse la bibliothèque, affiche la file |
| `torrchive.py run` | Transcode les fichiers éligibles |
| `torrchive.py run --dry-run` | Affiche ce qui serait fait, sans modification |
| `torrchive.py run --limit 10` | Transcode 10 fichiers puis s'arrête |
| `torrchive.py run --library Anime` | Traite uniquement la bibliothèque spécifiée |
| `torrchive.py run --parallel 4` | Nombre de jobs simultanés (remplace la config) |
| `torrchive.py run --no-schedule` | Ignore la fenêtre horaire |
| `torrchive.py status` | Affiche l'espace économisé |

---

## Clients torrent supportés

| Client | Valeur | Notes |
|---|---|---|
| qBittorrent | `qbittorrent` | API Web UI v2. Activer le Web UI dans les paramètres. |
| Deluge | `deluge` | JSON-RPC. Activer le plugin Web. |
| Transmission | `transmission` | API RPC. Fonctionne sans configuration. |
| Aucun | `none` | Pas de protection — tous les fichiers sont éligibles. |

Les fichiers connus de votre client dans **n'importe quel état** (seed, pause, en attente) sont ignorés. Torrchive ne transcode que ce que vous avez définitivement abandonné.

---

## Backends d'encodage

| Backend | Valeur | Prérequis |
|---|---|---|
| GPU NVIDIA | `nvenc` | Pilote NVIDIA + ffmpeg avec support CUDA |
| GPU Intel / AMD | `vaapi` | Linux, `/dev/dri/renderD128` accessible |
| Apple Silicon / Mac | `videotoolbox` | macOS, ffmpeg avec VideoToolbox |
| CPU (toute plateforme) | `software` | Pas de GPU requis, plus lent |
| Détection automatique | `auto` | Essaie le matériel disponible, repli sur software |

---

## Guide des codecs

| Codec | Valeur | Notes |
|---|---|---|
| H.265 / HEVC | `hevc` | **Recommandé.** Meilleure compatibilité avec Plex, Jellyfin et tous les clients. |
| AV1 | `av1` | Fichiers les plus petits (~30% de moins que HEVC). Clients récents uniquement. Non disponible sur VideoToolbox. |
| H.264 | `h264` | Compatibilité maximale. Fichiers plus volumineux que HEVC. |

---

## Variables d'environnement

Évitez de stocker les mots de passe en clair dans `config.yaml` — utilisez des variables d'environnement :

```yaml
torrent_client:
  password: ${QBIT_PASSWORD}
```

```bash
export QBIT_PASSWORD=votremotdepasse
python3 torrchive.py run
```

Ou placez-les dans un fichier `.env` et faites `source .env` avant d'exécuter, ou utilisez un `EnvironmentFile` systemd.

---

## Planification (solaire / heures creuses)

```yaml
schedule:
  enabled: true
  start: "09:00"
  stop: "20:00"
```

Le processus dort en dehors de la fenêtre et reprend automatiquement. Utilisez `--no-schedule` pour une exécution manuelle complète.

---

## Exemple de service systemd

```ini
[Unit]
Description=Torrchive transcode pipeline
After=network.target

[Service]
Type=simple
User=votre_utilisateur
WorkingDirectory=/home/votre_utilisateur/torrchive
EnvironmentFile=/home/votre_utilisateur/torrchive/.env
ExecStart=python3 /home/votre_utilisateur/torrchive/torrchive.py run
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Normalisation des noms de fichiers

Avec `normalize_filename: true`, Torrchive réécrit les tokens de codec et résolution obsolètes après le transcodage :

| Avant | Après |
|---|---|
| `Show.S01E01.1080p.WEB.x264.mkv` | `Show.S01E01.1080p.WEB.x265.mkv` |
| `Movie.2160p.BluRay.AVC.mkv` | `Movie.2160p.BluRay.x265.mkv` |
| `Film.2160p.x264.mkv` (avec max_resolution: 1080) | `Film.1080p.x265.mkv` |

Définissez `normalize_filename: false` pour laisser les noms de fichiers intacts.

---

## Journal d'espace

Torrchive écrit un fichier `torrchive_ledger.json` à chaque transcodage. Consultez vos économies à tout moment :

```bash
python3 torrchive.py status
```

```
Torrchive v0.1.0 — mode: status
Torrchive status — 842 fichiers transcodés
  Taille originale  : 4821.3 Go
  Taille actuelle   : 1644.2 Go
  Espace économisé  : 3177.1 Go (66%)
```

---

## Contribuer

Les PRs sont les bienvenues. Ajouts prévus :
- Support rTorrent / ruTorrent
- Notifications Discord / Gotify / Apprise
- Interface web / tableau de bord de progression

---

## Licence

```
MIT License

Copyright (c) 2026 Versveth

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

---

# Torrchive

[![Ko-fi](https://img.shields.io/badge/Support-Ko--fi-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/versveth)

**Torrchive** transcodes media files you're no longer seeding into space-efficient formats, keeping them alive in your Plex or Jellyfin library at a fraction of their original size.

> *"I downloaded it, seeded it, I'm done — Torrchive quietly converts it in the background."*

---

## Features

- **Torrent-client aware** — files still managed by your client (any state) are never touched
- **Hardware encoder support** — NVENC (NVIDIA), VAAPI (Intel/AMD), VideoToolbox (Apple), or software fallback
- **Codec flexibility** — HEVC, AV1, or H.264 as target
- **Resolution downscaling** — optional, e.g. 4K → 1080p
- **Filename normalisation** — rewrites stale codec/resolution tokens after transcode
- **Schedule window** — restrict runs to solar/off-peak hours
- **Parallel scanning** — fast library analysis with configurable worker count
- **Space ledger** — tracks exactly how much space has been saved over time
- **Probe cache** — remembers ffprobe results between runs for instant rescans

---

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` in your PATH
- `pip install pyyaml requests rich`

---

## Quick start

```bash
# 1. Clone or download
git clone https://github.com/Versveth/torrchive.git
cd torrchive

# 2. Install dependencies
pip install pyyaml requests

# 3. Create your config
cp config.example.yaml config.yaml
# Edit config.yaml to match your setup

# 4. Scan first — see what would be transcoded
python3 torrchive.py scan

# 5. Dry run — no files modified
python3 torrchive.py run --dry-run

# 6. Run
python3 torrchive.py run
```

---

## Modes

| Command | Description |
|---|---|
| `torrchive.py scan` | Analyse library, show queue |
| `torrchive.py run` | Transcode eligible files |
| `torrchive.py run --dry-run` | Show what would run, no changes |
| `torrchive.py run --limit 10` | Transcode 10 files then stop |
| `torrchive.py run --library Anime` | Process a specific library only |
| `torrchive.py run --parallel 4` | Concurrent jobs (overrides config) |
| `torrchive.py run --no-schedule` | Ignore time window |
| `torrchive.py status` | Show space saved so far |

---

## Torrent client support

| Client | Type value | Notes |
|---|---|---|
| qBittorrent | `qbittorrent` | Web UI API v2. Enable Web UI in settings. |
| Deluge | `deluge` | JSON-RPC. Enable Web plugin. |
| Transmission | `transmission` | RPC API. Works out of the box. |
| None | `none` | No protection — all files eligible. |

Files known to your client in **any state** (seeding, paused, stalled, queued) are skipped. Torrchive only transcodes what you've fully let go of.

---

## Encoder backends

| Backend | Value | Requirements |
|---|---|---|
| NVIDIA GPU | `nvenc` | NVIDIA driver + CUDA-capable ffmpeg |
| Intel / AMD GPU | `vaapi` | Linux, `/dev/dri/renderD128` accessible |
| Apple Silicon / Mac | `videotoolbox` | macOS, ffmpeg with VideoToolbox |
| CPU (any platform) | `software` | No GPU needed, slower |
| Auto-detect | `auto` | Tries hardware in order, falls back to software |

---

## Codec guidance

| Codec | Value | Notes |
|---|---|---|
| H.265 / HEVC | `hevc` | **Recommended baseline.** Best compatibility with Plex, Jellyfin, all clients. |
| AV1 | `av1` | Smallest files (~30% smaller than HEVC). Requires newer clients. Not available on VideoToolbox. |
| H.264 | `h264` | Maximum compatibility. Larger files than HEVC. |

---

## Environment variables

Avoid storing passwords in `config.yaml` — use environment variables instead:

```yaml
torrent_client:
  password: ${QBIT_PASSWORD}
```

```bash
export QBIT_PASSWORD=yourpassword
python3 torrchive.py run
```

Or put it in a `.env` file and `source .env` before running, or use a systemd `EnvironmentFile`.

---

## Scheduling (solar / off-peak)

```yaml
schedule:
  enabled: true
  start: "09:00"
  stop: "20:00"
```

The process sleeps outside the window and resumes automatically. Override with `--no-schedule` for a manual full run.

---

## Systemd service example

```ini
[Unit]
Description=Torrchive transcode pipeline
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/home/your_user/torrchive
EnvironmentFile=/home/your_user/torrchive/.env
ExecStart=python3 /home/your_user/torrchive/torrchive.py run
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Filename normalisation

With `normalize_filename: true`, Torrchive rewrites stale codec and resolution tokens in filenames after transcoding:

| Before | After |
|---|---|
| `Show.S01E01.1080p.WEB.x264.mkv` | `Show.S01E01.1080p.WEB.x265.mkv` |
| `Movie.2160p.BluRay.AVC.mkv` | `Movie.2160p.BluRay.x265.mkv` |
| `Film.2160p.x264.mkv` (with max_resolution: 1080) | `Film.1080p.x265.mkv` |

Set `normalize_filename: false` to keep filenames untouched.

---

## Space ledger

Torrchive writes a `torrchive_ledger.json` with every transcode. Check your savings at any time:

```bash
python3 torrchive.py status
```

```
Torrchive v0.1.0 — mode: status
Torrchive status — 842 files transcoded
  Original size  : 4821.3 GB
  Current size   : 1644.2 GB
  Space saved    : 3177.1 GB (66%)
```

---

## Contributing

PRs welcome. Planned additions:
- rTorrent / ruTorrent client support
- Discord / Gotify / Apprise notifications
- Web UI / progress dashboard

---

## License

```
MIT License

Copyright (c) 2026 Versveth

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
