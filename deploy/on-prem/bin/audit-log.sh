#!/usr/bin/env bash
#
# audit-log.sh — append a structured, PHI-free record to the LOCAL audit log.
#
# REAL. This is a thin, dependency-free append-only writer. The audit log is a
# newline-delimited JSON file on local disk (never network). It records WHAT ran
# and the OUTCOME — never patient data. The run artifacts (report.json,
# REPORT.md, checkpoints/) hold the per-step detail and live beside the run
# under the encrypted storage root; this log is the tamper-evident index over
# them.
#
# Fields (all non-PHI):
#   ts          UTC ISO-8601 timestamp
#   event       queued | started | verified | halted | failed | resumed |
#               updated | rolledback   (release lifecycle; see lib-release.sh)
#   job         opaque job id (operator-chosen; MUST NOT be a patient id)
#   bundle      bundle directory basename (not its contents)
#   run_dir     run output directory (path only)
#   exit        process exit code (for terminal events)
#   actor       OS user that invoked the runner
#   note        short free-text status (operator-supplied; MUST be PHI-free)
#   prev_sha    sha256 of the previous log line (hash chain — tamper-evidence)
#
# The prev_sha field chains each record to the previous one: altering or
# deleting a past line breaks every subsequent hash, so silent edits are
# detectable with `verify-airgap.sh --audit` (or any sha256 walk). This is
# tamper-EVIDENCE, not tamper-PROOF — a determined local root can recompute the
# chain. Pair it with append-only filesystem controls (chattr +a / immutable
# WORM export) for real assurance; see COMPLIANCE.md.
#
# Usage:
#   audit-log.sh <event> <job> [--bundle B] [--run-dir D] [--exit N] [--note "..."]
#
# Env:
#   OPENADAPT_ONPREM_AUDIT_LOG   audit log path (default: ./audit/audit.log)

set -euo pipefail

AUDIT_LOG="${OPENADAPT_ONPREM_AUDIT_LOG:-./audit/audit.log}"

event="${1:-}"
job="${2:-}"
if [[ -z "$event" || -z "$job" ]]; then
  echo "usage: audit-log.sh <event> <job> [--bundle B] [--run-dir D] [--exit N] [--note ...]" >&2
  exit 2
fi
shift 2

bundle=""
run_dir=""
exit_code=""
note=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)  bundle="${2:-}"; shift 2 ;;
    --run-dir) run_dir="${2:-}"; shift 2 ;;
    --exit)    exit_code="${2:-}"; shift 2 ;;
    --note)    note="${2:-}"; shift 2 ;;
    *) echo "audit-log.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$(dirname "$AUDIT_LOG")"
chmod 0700 "$(dirname "$AUDIT_LOG")" 2>/dev/null || true

# Hash-chain: sha256 of the last line (empty string for the first record).
prev_sha=""
if [[ -s "$AUDIT_LOG" ]]; then
  prev_sha="$(tail -n 1 "$AUDIT_LOG" | shasum -a 256 | awk '{print $1}')"
fi

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
actor="$(id -un 2>/dev/null || echo unknown)"

# JSON-escape a value (backslash, quote, control chars). PHI must never reach
# here, but escape defensively so a stray char cannot corrupt the log.
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/ }"
  s="${s//$'\t'/ }"
  printf '%s' "$s"
}

line=$(printf '{"ts":"%s","event":"%s","job":"%s","bundle":"%s","run_dir":"%s","exit":"%s","actor":"%s","note":"%s","prev_sha":"%s"}' \
  "$ts" \
  "$(json_escape "$event")" \
  "$(json_escape "$job")" \
  "$(json_escape "$(basename "${bundle:-}")")" \
  "$(json_escape "$run_dir")" \
  "$(json_escape "$exit_code")" \
  "$(json_escape "$actor")" \
  "$(json_escape "$note")" \
  "$prev_sha")

# Append atomically. `>>` on a local file is O_APPEND — concurrent runners do
# not interleave a single write() of one line.
printf '%s\n' "$line" >> "$AUDIT_LOG"
chmod 0600 "$AUDIT_LOG" 2>/dev/null || true
