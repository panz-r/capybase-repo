#!/usr/bin/env bash
#
# run-live-test.sh — set up a fixture conflict and run capybase against it,
# logging everything to timestamped files under logs/.
#
# The model endpoint is NOT hardcoded anywhere in this repo. Resolve it from
# a provider config (canonical; JSON under ~/.config/capybase/providers/,
# listed by `capybase provider list`):
#
#   CB_PROVIDER=nova-gemma4 ./scripts/run-live-test.sh
#
# or give an explicit endpoint on the command line:
#
#   CB_BASE_URL=http://host:8085/v1 CB_MODEL=my-model CB_PROFILE=e2b \
#     ./scripts/run-live-test.sh
#
# A calibration profile is REQUIRED (provider configs reference one); live
# runs never proceed on uncalibrated defaults.
#
# Usage:
#   ./scripts/run-live-test.sh                 # default fixture: python-uu
#   ./scripts/run-live-test.sh text-uu-simple  # pick a fixture
#   ./scripts/run-live-test.sh python-uu inspect  # run 'inspect' instead of 'run'
#
# Logs:
#   logs/live-test-<timestamp>/run.log         full capybase stdout+stderr
#   logs/live-test-<timestamp>/summary.txt     journal flow + candidate states
#   logs/live-test-<timestamp>/cfgdir/capybase.toml  the effective config
#
set -euo pipefail

# --------------------------------------------------------------------------
# Config (env-overridable). No endpoint or model default lives here — a
# provider config (CB_PROVIDER / CAPYBASE_PROVIDER) or explicit CB_BASE_URL +
# CB_MODEL + CB_PROFILE must supply them.
# --------------------------------------------------------------------------
CB_PROVIDER="${CB_PROVIDER:-${CAPYBASE_PROVIDER:-}}"
CB_BASE_URL="${CB_BASE_URL:-${CAPYBASE_BASE_URL:-}}"
CB_API_KEY="${CB_API_KEY:-${CAPYBASE_API_KEY:-}}"
CB_MODEL="${CB_MODEL:-${CAPYBASE_MODEL:-}}"
CB_PROFILE="${CB_PROFILE:-${CAPYBASE_PROFILE:-}}"
CB_PROFILE_PATH="${CB_PROFILE_PATH:-${CAPYBASE_PROFILE_PATH:-}}"
CB_MAX_TOKENS="${CB_MAX_TOKENS:-8192}"
CB_REQUEST_TIMEOUT="${CB_REQUEST_TIMEOUT:-600}"
CB_GENERATION_TIMEOUT="${CB_GENERATION_TIMEOUT:-180}"
CB_MAX_RETRIES="${CB_MAX_RETRIES:-3}"
CB_CONTEXT_LINES="${CB_CONTEXT_LINES:-20}"
# Structural context + AST preservation. The grammar-free abstract parser is
# built in (no tree-sitter, no extra install). Set CB_STRUCTURAL_ENABLED=true
# to test the AST layer live.
CB_STRUCTURAL_ENABLED="${CB_STRUCTURAL_ENABLED:-false}"
# Multi-request pipeline (Steps 2-5). These default to off so the script works
# with the simple single-sample path; set them to test the full pipeline.
CB_SAMPLES="${CB_SAMPLES:-1}"
CB_SAMPLING_TEMP="${CB_SAMPLING_TEMP:-0.7}"
CB_TWO_PASS="${CB_TWO_PASS:-false}"
CB_PARALLEL_SAMPLES="${CB_PARALLEL_SAMPLES:-true}"
CB_ENABLE_SELF_CONSISTENCY="${CB_ENABLE_SELF_CONSISTENCY:-false}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
PYTHON="${PYTHON:-$VENV/bin/python}"
CAPYBASE="${CAPYBASE:-$VENV/bin/capybase}"

# Fixture selection: arg1 = fixture spec id (fixtures/specs/<id>.json), arg2 = mode.
FIXTURE="${1:-python-uu}"
MODE="${2:-run}"
# P2 default flip: `capybase rebase` now runs candidate mode (source never
# touched). This script's contract for MODE=rebase is "advance the fixture
# branch" (it inspects the resolved tree afterward) — pin the legacy
# in-place mode explicitly. `run` (the default) is unaffected.
if [ "$MODE" = "rebase" ]; then
  MODE="rebase --in-place"
fi

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
cd "$REPO_ROOT"

if [ ! -x "$CAPYBASE" ]; then
  echo "ERROR: capybase not found at $CAPYBASE" >&2
  echo "       run: pip install -e .  (in the venv)" >&2
  exit 2
fi

# --------------------------------------------------------------------------
# Provider resolution: a named provider config fills any endpoint field the
# caller did not set explicitly (CB_* / CAPYBASE_* exports from `capybase
# provider show --shell`). Explicit variables always win. A calibration
# profile is required — refuse to run uncalibrated.
# --------------------------------------------------------------------------
if [ -n "$CB_PROVIDER" ]; then
  eval "$("$CAPYBASE" provider show "$CB_PROVIDER" --shell)"
fi
if [ -z "$CB_BASE_URL" ] || [ -z "$CB_MODEL" ]; then
  echo "ERROR: no model endpoint configured." >&2
  echo "  set CB_PROVIDER=<name>   (see: capybase provider list)" >&2
  echo "  or CB_BASE_URL=... CB_MODEL=... CB_PROFILE=<calibration profile>" >&2
  exit 2
fi
if [ -z "$CB_PROFILE" ]; then
  echo "ERROR: no calibration profile configured — live runs never proceed" >&2
  echo "  uncalibrated. Reference one in the provider config or set CB_PROFILE." >&2
  exit 2
fi
CB_API_KEY="${CB_API_KEY:-sk-local}"
# Resolve a bare profile name (e.g. "e2b") to its file path so the generated
# config can reference it explicitly.
if [ -z "$CB_PROFILE_PATH" ]; then
  CB_PROFILE_PATH="$("$PYTHON" -c \
    'import sys; from capybase.provider_config import profile_path_for; print(profile_path_for(sys.argv[1] if len(sys.argv) > 1 else ""))' \
    "$CB_PROFILE")"
fi

if [ ! -f "$REPO_ROOT/fixtures/specs/$FIXTURE.json" ]; then
  echo "ERROR: no fixture spec at fixtures/specs/$FIXTURE.json" >&2
  echo "       available: $(ls "$REPO_ROOT/fixtures/specs/" 2>/dev/null | sed 's/\.json$//' | tr '\n' ' ')" >&2
  exit 2
fi
FIXTURES="$REPO_ROOT/fixtures/.built/$FIXTURE"

TS="$(date +%Y%m%d-%H%M%S)"
LOGDIR="$REPO_ROOT/logs/live-test-$TS"
mkdir -p "$LOGDIR"
RUN_LOG="$LOGDIR/run.log"
SUMMARY="$LOGDIR/summary.txt"
# --config takes a DIRECTORY containing capybase.toml (Config.load refuses a
# file path — s27-extend-41). The heredoc below writes capybase.toml inside it.
CFG_DIR="$LOGDIR/cfgdir"
mkdir -p "$CFG_DIR"
CFG_FILE="$CFG_DIR/capybase.toml"

echo "==> live test: fixture=$FIXTURE mode=$MODE"
echo "==> model: $CB_MODEL @ $CB_BASE_URL"
echo "==> logs:   $LOGDIR"

# --------------------------------------------------------------------------
# Write the runtime config to the log dir and pass it explicitly via --config.
# We do NOT rely on CWD-based discovery (capybase.toml / capybase.local.toml
# in the current directory) because `capybase --repo fixtures` runs from the
# repo root and would pick up the placeholder capybase.toml instead.
#
# TOML-escape string values (handles model names with backslashes, e.g.
# "..\VibeThinker-3B.Q5_K_M.gguf").
# --------------------------------------------------------------------------
toml_str() { printf '%s' "$1" | sed 's/\\/\\\\/g'; }
M_ESC="$(toml_str "$CB_MODEL")"
U_ESC="$(toml_str "$CB_BASE_URL")"
K_ESC="$(toml_str "$CB_API_KEY")"
P_ESC="$(toml_str "$CB_PROFILE_PATH")"

cat > "$CFG_FILE" <<EOF
[model]
base_url = "$U_ESC"
api_key = "$K_ESC"
model = "$M_ESC"
temperature = 0.2
max_tokens = $CB_MAX_TOKENS
request_timeout_seconds = $CB_REQUEST_TIMEOUT
generation_timeout_seconds = $CB_GENERATION_TIMEOUT
samples = $CB_SAMPLES
sampling_temperature = $CB_SAMPLING_TEMP
two_pass = $CB_TWO_PASS
parallel_samples = $CB_PARALLEL_SAMPLES

[calibration]
model_profile_path = "$P_ESC"

[policy]
max_retries_per_unit = $CB_MAX_RETRIES
context_lines = $CB_CONTEXT_LINES

[structural]
enabled = $CB_STRUCTURAL_ENABLED
languages = ["python", "rust"]

[future]
enable_self_consistency = $CB_ENABLE_SELF_CONSISTENCY

[tests]
pre_continue = "true"
final = "true"
required = true
EOF
echo "==> config written to $CFG_FILE"

# --------------------------------------------------------------------------
# Reachability check (informational; does not abort — capybase retries).
# --------------------------------------------------------------------------
echo "==> checking endpoint reachability..."
if "$PYTHON" - "$CB_BASE_URL" "$CB_API_KEY" <<'PY' >/dev/null 2>&1
import sys, urllib.request
url, key = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url + "/models", headers={"Authorization": f"Bearer {key}"})
urllib.request.urlopen(req, timeout=8)
PY
then
  echo "    endpoint reachable"
else
  echo "    WARNING: endpoint not reachable right now — capybase will still try (and retry)" | tee -a "$RUN_LOG"
fi

# --------------------------------------------------------------------------
# Set up the fixture: build the repo from its spec (deterministic OIDs), then
# drive a conflict. A successful capybase run ADVANCES the fixture branch
# (the resolved rebase commits), so we rebuild with --force on every run —
# the same spec always produces the same commits, so this is idempotent.
# --------------------------------------------------------------------------
echo "==> building fixture '$FIXTURE' from its spec..."
"$PYTHON" "$REPO_ROOT/fixtures/build.py" --spec "$FIXTURE" --force

echo "==> setting up fixture '$FIXTURE' (rebase replayed onto current)..."
(
  cd "$FIXTURES"
  # Abort any in-progress rebase and detach HEAD so the conflict-driving
  # checkout below always starts clean.
  git rebase --abort 2>/dev/null || true
  git checkout -q --detach 2>/dev/null || true
  git checkout -q replayed
  if git rebase current >/dev/null 2>&1; then
    echo "    NOTE: rebase did NOT conflict for '$FIXTURE' — fixture may be stale" | tee -a "$RUN_LOG"
  else
    echo "    conflict established"
  fi
)

# --------------------------------------------------------------------------
# Run capybase. All output to both the terminal and run.log.
# --------------------------------------------------------------------------
echo "==> running: capybase --config <logdir>/cfgdir --repo fixtures $MODE"
set +e
"$CAPYBASE" --config "$CFG_DIR" --repo "$FIXTURES" "$MODE" 2>&1 | tee "$RUN_LOG"
RC=${PIPESTATUS[0]}
set -e
echo "==> capybase exit code: $RC" | tee -a "$RUN_LOG"

# --------------------------------------------------------------------------
# Capture diagnostics: journal flow + candidate/validation states.
# --------------------------------------------------------------------------
echo "==> writing summary..."
SID="$(cd "$FIXTURES" && ls -t .rebase-agent/sessions/ 2>/dev/null | head -1 || true)"
{
  echo "# live-test summary"
  echo "# timestamp:      $TS"
  echo "# fixture:        $FIXTURE (replayed rebased onto current)"
  echo "# mode:           $MODE"
  echo "# model:          $CB_MODEL @ $CB_BASE_URL"
  echo "# session:        ${SID:-<none>}"
  echo "# capybase exit:  $RC"
  echo
  if [ -n "$SID" ]; then
    SB="$FIXTURES/.rebase-agent/sessions/$SID"
    echo "## journal flow"
    "$PYTHON" - "$SB/journal.jsonl" <<'PY'
import json, sys
path = sys.argv[1]
try:
    for line in open(path):
        e = json.loads(line); p = e.get("payload", {})
        keys = {k: p[k] for k in ("passed", "action", "needs_human", "hard_failures") if k in p}
        print(e["event_type"], keys)
except Exception as exc:
    print("(could not read journal:", exc, ")")
PY
    echo
    echo "## candidates"
    for f in "$SB"/candidates/*.json; do
      [ -e "$f" ] || continue
      "$PYTHON" - "$f" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  {d['candidate_id'][-8:]} failure_kind={d.get('failure_kind','')!r} "
      f"needs_human={d['needs_human']} resolved={d['resolved_text']!r}")
print(f"    warns: {d.get('parse_warnings', [])[:2]}")
PY
    done
    echo
    echo "## validations"
    for f in "$SB"/validations/*.json; do
      [ -e "$f" ] || continue
      "$PYTHON" - "$f" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  {d['candidate_id'][-8:]} passed={d['passed']}")
for hf in d.get("hard_failures", []):
    print(f"    HARD [{hf['validator']}]: {hf['message'][:100]}")
PY
    done
    echo
    echo "## files in fixture"
    # Show whichever fixture content files actually exist on disk.
    for cand in app.py story.txt settings.py src/config.rs; do
      if [ -f "$FIXTURES/$cand" ]; then
        echo "--- $cand ---"; cat "$FIXTURES/$cand"
      fi
    done
  else
    echo "(no session directory found)"
  fi
} > "$SUMMARY" 2>&1

echo
echo "==> DONE. exit=$RC"
echo "    run log:    $RUN_LOG"
echo "    summary:    $SUMMARY"
echo "    config:     $CFG_FILE"

# Reset the fixture back to base so the script is re-runnable.
(
  cd "$FIXTURES"
  git rebase --abort 2>/dev/null || true
  git checkout -q base 2>/dev/null || true
)

exit "$RC"
