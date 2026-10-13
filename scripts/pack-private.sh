#!/usr/bin/env bash
# Packs the files that are NOT in the public repo into one zip, to share privately
# (Google Drive / internal chat, never a public link) with a teammate.
#   knowledge/*.md         the docs the bot answers from
#   internal_terms.txt     internal names the bot must never reveal
# Usage: scripts/pack-private.sh   ->  private-files.zip in the repo root
set -euo pipefail
cd "$(dirname "$0")/.."

files=()
for f in knowledge/*.md; do
  [[ "$(basename "$f")" == "README.md" ]] && continue
  files+=("$f")
done
[[ -f internal_terms.txt ]] && files+=("internal_terms.txt") || echo "warning: internal_terms.txt not found"

rm -f private-files.zip
zip -q private-files.zip "${files[@]}"
echo "Created private-files.zip with:"
printf '  %s\n' "${files[@]}"
echo "Share it privately. Your teammate unzips it in the repo root:  unzip private-files.zip"
