#!/usr/bin/env bash
# End-to-end pipeline: legacy C file + git history -> evidence-backed system map + viewer.
set -euo pipefail
export PYTHONUTF8=1   # Windows: read files and git output as UTF-8, not the local codepage
REPO=${REPO:-../work/linux}
FILE=${FILE:-drivers/pci/quirks.c}

if [ ! -d "$REPO/.git" ]; then
  git clone --filter=blob:none --no-checkout https://github.com/torvalds/linux "$REPO"
fi
git -C "$REPO" config gc.auto 0
git -C "$REPO" show "HEAD:$FILE" > data/quirks.c

echo "[1/5] static analysis ..."
python3 extract.py data/quirks.c > data/static.json                         # 1. static layer
echo "[2/5] git history (a few minutes) ..."
python3 history.py --repo "$REPO" --path "$FILE" --static data/static.json \
    --out data/history.json --deep "$(cat data/selected.txt)"               # 2. history layer
echo "[3/5] building map + cross-file caller lookup ..."
python3 build_map.py --static data/static.json --history data/history.json \
    --source data/quirks.c --out data/system_map.json --repo "$REPO"        #    evidence pools
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  python3 reason.py --map data/system_map.json --functions data/selected.txt # 3. reasoning layer
fi
echo "[4/5] verifying quoted evidence ..."
python3 validate.py --history data/history.json --source data/quirks.c \
    --analyses data/analyses --repo "$REPO" --fix                           # 4. verify every quote
python3 build_map.py --static data/static.json --history data/history.json \
    --source data/quirks.c --out data/system_map.json --repo "$REPO"
echo "[5/5] writing viewer ..."
python3 export_viewer.py                                                    # 5. viewer
echo "Done. Open viewer/index.html"
