#!/usr/bin/env python3
"""
parse_and_schedule.py — Parse un rapport Markdown Torrchive, extrait les
commandes ffmpeg, les trie par gain estimé décroissant, et génère un script
shell Bash avec progression et logging.

Usage:
    python3 parse_and_schedule.py <rapport.md> [--output batch.sh]
"""

import argparse
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Parse un rapport Torrchive Markdown et génère un batch shell HEVC."
    )
    p.add_argument("markdown_path", help="Chemin vers le fichier Markdown d'entrée")
    p.add_argument(
        "--output", "-o", default=None,
        help="Chemin du script shell de sortie (défaut: même dossier, nom auto)"
    )
    return p.parse_args()


def extract_table_entries(content: str) -> List[Dict]:
    """
    Parse le tableau Markdown pour extraire numéro, nom tronqué, taille et gain.

    Format attendu :
      | N | `nom_fichier…` | 1080p | 41.5 Go | 18.7 Go | 22.8 Go |
    """
    # Pattern : | N | `...` | résol | taille Go | hevc Go | gain Go |
    pattern = re.compile(
        r'^\|\s*(\d+)\s*\|'          # col 1 : numéro
        r'\s*`([^`]+)`[^|]*\|'       # col 2 : nom (backticks)
        r'[^|]+\|'                   # col 3 : résolution
        r'\s*([\d.]+)\s*Go\s*\|'    # col 4 : taille actuelle
        r'\s*([\d.]+)\s*Go\s*\|'    # col 5 : estimé HEVC
        r'\s*([\d.]+)\s*Go\s*\|',   # col 6 : gain
        re.MULTILINE,
    )
    entries = []
    for m in pattern.finditer(content):
        entries.append({
            "n":        int(m.group(1)),
            "name":     m.group(2).strip(),
            "size_gb":  float(m.group(3)),
            "hevc_gb":  float(m.group(4)),
            "gain_gb":  float(m.group(5)),
        })
    return entries


def extract_ffmpeg_commands(content: str) -> Dict[int, str]:
    """
    Extrait les commandes ffmpeg depuis les blocs ```bash sous ### N. `...`.
    Reconstruit chaque commande multi-ligne (backslash) en une seule ligne.
    """
    # Capture : ### N. `titre` puis bloc ```bash ... ```
    block_pattern = re.compile(
        r'###\s+(\d+)\.\s+`[^`]+`\s*\n+```bash\n(.*?)```',
        re.DOTALL,
    )
    commands: Dict[int, str] = {}
    for m in block_pattern.finditer(content):
        n = int(m.group(1))
        raw = m.group(2)

        # Joindre les lignes continuation backslash, normaliser les espaces
        lines = raw.splitlines()
        joined = []
        for line in lines:
            line = line.rstrip()
            if line.endswith("\\"):
                joined.append(line[:-1].strip())   # retire le \ final
            else:
                stripped = line.strip()
                if stripped:
                    joined.append(stripped)

        full_cmd = " ".join(joined).strip()
        if full_cmd.startswith("ffmpeg"):
            commands[n] = full_cmd

    return commands


def extract_output_path(cmd: str) -> Optional[str]:
    """
    Extrait le chemin de sortie d'une commande ffmpeg (dernier argument entre guillemets).
    """
    # Le dernier "..." dans la commande est le fichier de sortie
    matches = re.findall(r'"([^"]+)"', cmd)
    return matches[-1] if len(matches) >= 2 else None


# ── Génération du script shell ────────────────────────────────────────────────

BASH_HEADER = '''\
#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Torrchive — Batch HEVC recompression
# Généré le {date} par parse_and_schedule.py
# Trié par gain estimé décroissant | CPU decode + hevc_nvenc
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

LOG_FILE="{log_file}"
TOTAL={total}
TOTAL_GAIN_GB={total_gain:.1f}

log() {{
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}}

log "=== Batch démarré | $TOTAL fichiers | Gain estimé : ${{TOTAL_GAIN_GB}} Go ==="
echo ""
'''

BASH_ENTRY = '''\
# ── {idx}/{total} ────────────────────────────────────────────────────────────
log "[{idx}/{total}] Démarrage : {name}"
log "  Taille actuelle : {size_gb:.1f} Go | Gain estimé : {gain_gb:.1f} Go"
START_TIME=$(date +%s)

{cmd} 2>&1 | tee -a "$LOG_FILE"

ELAPSED=$(( $(date +%s) - START_TIME ))
log "[{idx}/{total}] ✅ Terminé en ${{ELAPSED}}s"
OUTPUT_FILE="{output_path}"
if [ -f "$OUTPUT_FILE" ]; then
    REAL_SIZE=$(du -sh "$OUTPUT_FILE" | cut -f1)
    log "[{idx}/{total}] Taille réelle sortie : $REAL_SIZE → $OUTPUT_FILE"
else
    log "[{idx}/{total}] ⚠️  Fichier de sortie introuvable : $OUTPUT_FILE"
fi
echo ""
'''

BASH_FOOTER = '''\
log "=== ✅ Batch terminé | $TOTAL fichiers traités | Gain estimé total : ${{TOTAL_GAIN_GB}} Go ==="
'''


def generate_shell_script(
    sorted_entries: List[Dict],
    commands: Dict[int, str],
    output_path: str,
    log_file: str,
) -> None:
    total = len(sorted_entries)
    total_gain = sum(e["gain_gb"] for e in sorted_entries)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [BASH_HEADER.format(
        date=now_str,
        log_file=log_file,
        total=total,
        total_gain=total_gain,
    )]

    for idx, entry in enumerate(sorted_entries, 1):
        n = entry["n"]
        cmd = commands.get(n)
        if not cmd:
            print(f"⚠️  Commande manquante pour l'entrée #{n}, ignorée.", file=sys.stderr)
            continue

        out_path = extract_output_path(cmd) or "UNKNOWN"

        lines.append(BASH_ENTRY.format(
            idx=idx,
            total=total,
            name=entry["name"][:70],
            size_gb=entry["size_gb"],
            gain_gb=entry["gain_gb"],
            cmd=cmd,
            output_path=out_path,
        ))

    lines.append(BASH_FOOTER)

    script = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(output_path, 0o755)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    if not os.path.isfile(args.markdown_path):
        print(f"❌ Fichier introuvable : {args.markdown_path}", file=sys.stderr)
        sys.exit(1)

    with open(args.markdown_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extraction
    entries = extract_table_entries(content)
    if not entries:
        print("❌ Aucune entrée trouvée dans le tableau Markdown.", file=sys.stderr)
        sys.exit(1)

    commands = extract_ffmpeg_commands(content)
    if not commands:
        print("❌ Aucune commande ffmpeg trouvée.", file=sys.stderr)
        sys.exit(1)

    # Tri par gain décroissant
    sorted_entries = sorted(entries, key=lambda e: e["gain_gb"], reverse=True)

    # Récap console
    print(f"\n{'='*72}")
    print(f"  Torrchive — Ordre de traitement (gain décroissant)")
    print(f"{'='*72}")
    print(f"  {'Ordre':>5}  {'#orig':>5}  {'Gain':>8}  {'Taille':>8}  Fichier")
    print(f"  {'-'*5}  {'-'*5}  {'-'*8}  {'-'*8}  {'-'*45}")
    for i, e in enumerate(sorted_entries, 1):
        cmd_ok = "✅" if e["n"] in commands else "❌"
        print(f"  {i:>5}  #{e['n']:>4}  {e['gain_gb']:>6.1f} Go  {e['size_gb']:>6.1f} Go  {cmd_ok} {e['name'][:50]}")
    total_gain = sum(e["gain_gb"] for e in sorted_entries)
    print(f"{'='*72}")
    print(f"  Total gain estimé : {total_gain:.1f} Go sur {len(sorted_entries)} fichiers")
    print(f"{'='*72}\n")

    # Chemin de sortie
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_dir = os.path.dirname(os.path.abspath(args.markdown_path))

    if args.output:
        output_path = args.output
    else:
        output_path = os.path.join(md_dir, f"hevc_batch_{ts}.sh")

    log_file = os.path.join(md_dir, f"hevc_batch_{ts}.log")

    # Génération
    generate_shell_script(sorted_entries, commands, output_path, log_file)

    print(f"✅ Script shell généré : {output_path}")
    print(f"   Log prévu dans      : {log_file}")
    print(f"\n   Pour lancer : bash {output_path}\n")


if __name__ == "__main__":
    main()
