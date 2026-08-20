#!/bin/bash
# longrun — run a long command detached from the calling session.
#
# Sprint-20 S20.5c: the pattern that survived two zcode restarts during
# sprint-19/20 (the r1 suite was killed at 66% by a session restart; the
# setsid-detached r2 finished green), productized. Use for full-suite runs,
# live eval batches, and anything else that outlives a session:
#
#   scripts/longrun.sh s20-suite .venv/bin/python -m pytest
#   scripts/longrun.sh s20-batch .venv/bin/python scripts/live_eval_realworld.py \
#       --provider nova-gemma4 --case ... --out ...
#
# Behavior:
# - runs the command under setsid+nohup (own session; terminal/zcode
#   restarts cannot kill it);
# - stdout+stderr -> /tmp/capybase-live/<name>/<name>.log;
# - START/DONE(+exit code) markers -> /tmp/capybase-live/<name>/progress.log
#   (the durable record a future session reads first);
# - refuses to start when a run of the same name is already active;
# - prints the log path and exits immediately.
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "usage: $0 <name> <command> [args...]" >&2
    exit 2
fi
NAME="$1"; shift
BASE="/tmp/capybase-live/$NAME"
mkdir -p "$BASE"

if [ -e "$BASE/worker.pid" ] && kill -0 "$(cat "$BASE/worker.pid")" 2>/dev/null; then
    echo "longrun: a run named '$NAME' is already active (pid $(cat "$BASE/worker.pid")); refusing." >&2
    exit 3
fi

# The worker script carries the command (argv through setsid is fragile).
printf '%q ' "$@" > "$BASE/cmd"
cat > "$BASE/worker.sh" <<EOF
#!/bin/bash
# Self-record the REAL worker pid (defect review 2026-08-20: setsid may
# fork when the caller is a process-group leader, so the launcher's \$!
# can die immediately while this script runs under a different pid —
# the active-run guard then false-negatives and allows duplicates).
echo \$\$ > "$BASE/worker.pid"
cd "$PWD"
echo "\$(date +%F' '%H:%M:%S) ${NAME}_START" >> "$BASE/progress.log"
eval "\$(cat "$BASE/cmd")" >> "$BASE/${NAME}.log" 2>&1
rc=\$?
echo "\$(date +%F' '%H:%M:%S) ${NAME}_DONE exit=\$rc" >> "$BASE/progress.log"
rm -f "$BASE/worker.pid"
exit \$rc
EOF
chmod +x "$BASE/worker.sh"

setsid nohup "$BASE/worker.sh" >/dev/null 2>&1 < /dev/null &
echo $! > "$BASE/worker.pid"
echo "longrun: '$NAME' detached (worker pid $(cat "$BASE/worker.pid"))"
echo "  log:     $BASE/${NAME}.log"
echo "  markers: $BASE/progress.log"
