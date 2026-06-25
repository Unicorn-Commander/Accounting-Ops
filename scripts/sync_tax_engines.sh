#!/usr/bin/env bash
# Re-sync the vendored pure tax engines from Tax-Planning-Ops.
# Usage: TPO=/path/to/Tax-Planning-Ops bash scripts/sync_tax_engines.sh
set -euo pipefail
TPO="${TPO:-../Tax-Planning-Ops}"
SRC="$TPO/backend/app/engines"
DST="app/tax/engines"
for f in form1040.py; do
  { echo "# SYNCED FROM Tax-Planning-Ops backend/app/engines/$f — DO NOT EDIT HERE."; \
    echo "# Re-sync via scripts/sync_tax_engines.sh."; cat "$SRC/$f"; } > "$DST/$f"
  echo "synced $f"
done
echo "Done. Review the diff + re-run tests/smoke/test_compute_1040.py before deploy."
