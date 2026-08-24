#!/bin/bash
# Sprint-22 reround: the post-fix measurement harvest (README row 2).
#
# Uniform commit, all four shards sequential, fresh result files (r2-*).
# Mechanisms under test: R1 propagation+guard, C1 symbol injection, R2
# use-dedup, C4 repair rotation, P5 resolved-file provenance, E1 probe.
# src/ is FROZEN while this runs — any edit contaminates the round.
set -euo pipefail
cd "$(dirname "$0")/.."
for lang in python c rust cpp; do
  echo "=== reround shard: $lang ($(date +%H:%M:%S)) ==="
  env CAPYBASE_SKIP_SIZE_GUARD=1 .venv/bin/python scripts/live_eval_realworld.py \
    --provider nova-gemma4 --lang "$lang" --repeat-nonpass 3 --skip-existing \
    --out "/var/tmp/capybase-live/s22/r2-$lang.json" \
    --preserve-flights "/var/tmp/capybase-live/s22/flights-r2-$lang"
done
echo "=== reround complete ($(date +%H:%M:%S)) ==="
