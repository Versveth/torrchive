#!/usr/bin/env python3
"""
probe_stats.py — Analyse le cache probe de Torrchive et génère un rapport Markdown.

Usage:
    python3 probe_stats.py [chemin/vers/torrchive_probe_cache.json]
"""

import argparse
import json
import os
from datetime import datetime


MODERN_CODECS = {"hevc", "av1", "vp9"}
LEGACY_CODECS = {"h264", "mpeg2video", "xvid", "divx", "mpeg4", "wmv3", "msmpeg4v3"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Génère des stats de compression par codec depuis le cache probe Torrchive."
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        default="torrchive_probe_cache.json",
        help="Chemin vers le fichier JSON (défaut: torrchive_probe_cache.json)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=".",
        help="Dossier de sortie pour le rapport Markdown (défaut: répertoire courant)",
    )
    return parser.parse_args()


def load_json(file_path: str) -> dict:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Fichier introuvable : {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON invalide : {e}")


def classify_resolution(height: int) -> str:
    """Classe la hauteur en bucket de résolution standard."""
    if height >= 2160:
        return "4K (2160p+)"
    elif height >= 1080:
        return "1080p"
    elif height >= 720:
        return "720p"
    elif height >= 480:
        return "480p/SD"
    else:
        return f"Autre ({height}p)"


def calculate_stats(probe_data: dict) -> dict:
    """
    Calcule les statistiques par codec depuis le cache probe.

    Retourne un dict avec :
      - codec_stats : dict[codec] -> {files, size_bytes, resolutions}
      - total_files : int
      - total_size_bytes : int
      - skipped : int
    """
    codec_stats = {}
    total_files = 0
    total_size_bytes = 0
    skipped = 0

    for key, value in probe_data.items():
        # Clé format : "chemin_absolu:taille_en_octets"
        # Le chemin peut contenir des ':' (ex: Windows paths), on split depuis la droite
        parts = key.rsplit(":", 1)
        if len(parts) != 2:
            skipped += 1
            continue

        _, size_str = parts
        try:
            size_bytes = int(size_str)
        except ValueError:
            skipped += 1
            continue

        codec = value.get("codec", "unknown").lower().strip()
        height = value.get("height")

        if not codec:
            codec = "unknown"

        if codec not in codec_stats:
            codec_stats[codec] = {
                "files": 0,
                "size_bytes": 0,
                "resolutions": {},
            }

        codec_stats[codec]["files"] += 1
        codec_stats[codec]["size_bytes"] += size_bytes

        if isinstance(height, (int, float)) and height > 0:
            bucket = classify_resolution(int(height))
        else:
            bucket = "Inconnu"

        codec_stats[codec]["resolutions"][bucket] = (
            codec_stats[codec]["resolutions"].get(bucket, 0) + 1
        )

        total_files += 1
        total_size_bytes += size_bytes

    return {
        "codec_stats": codec_stats,
        "total_files": total_files,
        "total_size_bytes": total_size_bytes,
        "skipped": skipped,
    }


def bytes_to_gb(b: int) -> float:
    return b / (1024 ** 3)


def generate_report(stats: dict, source_file: str) -> str:
    codec_stats = stats["codec_stats"]
    total_files = stats["total_files"]
    total_size_bytes = stats["total_size_bytes"]
    skipped = stats["skipped"]

    now = datetime.now()
    lines = []

    # ── Header ──────────────────────────────────────────────────────────────
    lines.append(f"# Rapport Torrchive — Analyse Probe Cache")
    lines.append(f"")
    lines.append(f"**Généré le :** {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Source :** `{source_file}`")
    lines.append(f"**Total fichiers analysés :** {total_files:,}")
    lines.append(f"**Total size :** {bytes_to_gb(total_size_bytes):.1f} Go")
    if skipped:
        lines.append(f"**Entrées ignorées (format invalide) :** {skipped}")
    lines.append("")

    # ── Stats globales efficacité ────────────────────────────────────────────
    modern_files = sum(
        v["files"] for k, v in codec_stats.items() if k in MODERN_CODECS
    )
    modern_size = sum(
        v["size_bytes"] for k, v in codec_stats.items() if k in MODERN_CODECS
    )
    legacy_files = sum(
        v["files"] for k, v in codec_stats.items() if k in LEGACY_CODECS
    )
    legacy_size = sum(
        v["size_bytes"] for k, v in codec_stats.items() if k in LEGACY_CODECS
    )

    pct_modern_files = (modern_files / total_files * 100) if total_files else 0
    pct_modern_size = (modern_size / total_size_bytes * 100) if total_size_bytes else 0

    lines.append("## 📊 Résumé Efficacité")
    lines.append("")
    lines.append(f"| Catégorie | Fichiers | % fichiers | Taille (Go) | % taille |")
    lines.append(f"|-----------|--------:|----------:|------------:|---------:|")
    lines.append(
        f"| ✅ Modernes ({', '.join(sorted(MODERN_CODECS))}) "
        f"| {modern_files:,} | {pct_modern_files:.1f}% "
        f"| {bytes_to_gb(modern_size):.1f} | {pct_modern_size:.1f}% |"
    )
    lines.append(
        f"| ⚠️ Legacy ({', '.join(sorted(LEGACY_CODECS))}) "
        f"| {legacy_files:,} | {legacy_files/total_files*100:.1f}% "
        f"| {bytes_to_gb(legacy_size):.1f} | {legacy_size/total_size_bytes*100:.1f}% |"
    )
    lines.append("")

    # ── Tableau par codec ────────────────────────────────────────────────────
    lines.append("## 🎬 Stats par Codec")
    lines.append("")
    lines.append(
        "| Codec | Type | Fichiers | % | Taille (Go) | % | Moy. (Go) |"
    )
    lines.append(
        "|-------|------|--------:|--:|------------:|--:|---------:|"
    )

    sorted_codecs = sorted(
        codec_stats.items(), key=lambda x: x[1]["size_bytes"], reverse=True
    )

    for codec, data in sorted_codecs:
        if codec in MODERN_CODECS:
            codec_type = "✅ Moderne"
        elif codec in LEGACY_CODECS:
            codec_type = "⚠️ Legacy"
        else:
            codec_type = "❓ Autre"

        pct_files = data["files"] / total_files * 100
        pct_size = data["size_bytes"] / total_size_bytes * 100
        avg_gb = bytes_to_gb(data["size_bytes"]) / data["files"] if data["files"] else 0

        lines.append(
            f"| `{codec}` | {codec_type} | {data['files']:,} | {pct_files:.1f}% "
            f"| {bytes_to_gb(data['size_bytes']):.1f} | {pct_size:.1f}% | {avg_gb:.2f} |"
        )

    lines.append("")

    # ── Distribution résolutions globale ─────────────────────────────────────
    res_totals: dict[str, int] = {}
    for data in codec_stats.values():
        for res, count in data["resolutions"].items():
            res_totals[res] = res_totals.get(res, 0) + count

    lines.append("## 📐 Distribution des Résolutions")
    lines.append("")
    lines.append("| Résolution | Fichiers | % |")
    lines.append("|------------|--------:|--:|")

    for res, count in sorted(res_totals.items(), key=lambda x: -x[1]):
        pct = count / total_files * 100
        lines.append(f"| {res} | {count:,} | {pct:.1f}% |")

    lines.append("")

    # ── Détail résolutions par codec (top codecs seulement) ──────────────────
    lines.append("## 🔍 Résolutions par Codec (top 6)")
    lines.append("")

    top_codecs = sorted_codecs[:6]
    for codec, data in top_codecs:
        lines.append(f"### `{codec}`")
        lines.append("")
        lines.append("| Résolution | Fichiers | % codec |")
        lines.append("|------------|--------:|--------:|")
        for res, count in sorted(data["resolutions"].items(), key=lambda x: -x[1]):
            pct = count / data["files"] * 100
            lines.append(f"| {res} | {count:,} | {pct:.1f}% |")
        lines.append("")

    return "\n".join(lines)


def main():
    args = parse_args()

    print(f"→ Chargement de {args.json_file}…")
    data = load_json(args.json_file)
    print(f"→ {len(data):,} entrées chargées. Calcul des stats…")

    stats = calculate_stats(data)
    print(
        f"→ {stats['total_files']:,} fichiers analysés, "
        f"{stats['skipped']} ignorés."
    )

    report = generate_report(stats, args.json_file)

    # Affichage stdout
    print()
    print(report)

    # Sauvegarde fichier
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(args.output_dir, f"probe_report_{timestamp}.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n→ Rapport sauvegardé : {output_path}")


if __name__ == "__main__":
    main()
