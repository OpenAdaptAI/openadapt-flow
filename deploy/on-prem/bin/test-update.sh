#!/usr/bin/env bash
#
# test-update.sh — hermetic proof that the on-prem update/rollback path is
# atomic, integrity-gated, data-preserving, and rollback-able.
#
# REAL. Runs fully offline in a throwaway temp storage_root. It exercises the
# SAME code paths install.sh uses (lib-release.sh), stubbing only the two
# expensive/host-specific steps via the documented testing hooks:
#   OPENADAPT_ONPREM_BUILD_HOOK  — replaces `python -m venv + pip` with a fake
#                                  engine CLI (so the test needs no wheels).
#   (smoke uses the REAL rel_smoke, which runs the fake CLI's --version.)
#
# Cases:
#   1. update v1 -> v2            current flips atomically to v2
#   2. data preserved             bundles/ jobs/ audit/ untouched (byte-identical)
#   3. previous recorded          previous -> v1
#   4. rollback                   current reverts to v1 instantly, previous -> v2
#   5. bad checksum aborts        tampered archive -> update fails, stays on current
#   6. no integrity material      no checksum + no sig -> update aborts
#   7. smoke failure aborts       broken engine CLI -> update fails, stays on current
#   8. audit + history records    updated/rolledback appended, PHI-free
#
# Exit 0 => all cases pass.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bin/lib-release.sh
source "$HERE/lib-release.sh"

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
check(){ if eval "$2"; then ok "$1"; else bad "$1 (assertion: $2)"; fi; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/onprem-update-test.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

ROOT="$WORK/storage"          # the fake clinic storage_root
STAGE="$WORK/media"           # the "removable media" with staged releases
mkdir -p "$ROOT/releases" "$ROOT/bundles" "$ROOT/jobs/inbox" "$ROOT/audit" "$STAGE"

# --- audit wiring (proves update/rollback write PHI-free records) -----------
export OPENADAPT_ONPREM_AUDIT_BIN="$HERE/audit-log.sh"
export OPENADAPT_ONPREM_AUDIT_LOG="$ROOT/audit/audit.log"

# --- customer DATA that must survive every update/rollback ------------------
echo "identity evidence PLACEHOLDER" > "$ROOT/bundles/keepme.txt"
printf 'bundle=/x\nparams=k=v\n' > "$ROOT/jobs/inbox/queued-0001.job"
bash "$OPENADAPT_ONPREM_AUDIT_BIN" queued "job-0001" --note "seeded" >/dev/null 2>&1
DATA_BUNDLE_SHA="$(sha256_of "$ROOT/bundles/keepme.txt")"
DATA_JOB_SHA="$(sha256_of "$ROOT/jobs/inbox/queued-0001.job")"

# --- testing build hook: fake, offline "engine" (no wheels/pip) -------------
# Writes a fake engine CLI into <release_dir>/venv/bin/openadapt-flow. If
# OPENADAPT_TEST_BROKEN_CLI=1 the CLI exits non-zero (to exercise smoke-fail).
HOOK="$WORK/build-hook.sh"
cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
set -euo pipefail
reldir="$1"
mkdir -p "$reldir/venv/bin"
if [[ "${OPENADAPT_TEST_BROKEN_CLI:-0}" == "1" ]]; then
  cat > "$reldir/venv/bin/openadapt-flow" <<'CLI'
#!/usr/bin/env bash
exit 1
CLI
else
  cat > "$reldir/venv/bin/openadapt-flow" <<'CLI'
#!/usr/bin/env bash
echo "openadapt-flow (fake) ${OPENADAPT_TEST_VERSION:-0.0.0}"
exit 0
CLI
fi
chmod +x "$reldir/venv/bin/openadapt-flow"
HOOK_EOF
chmod +x "$HOOK"
export OPENADAPT_ONPREM_BUILD_HOOK="$HOOK"

# make_release VERSION [--no-checksum]  -> stages an archive + sha256 on media,
# prints the archive path. Archive layout: <top>/VERSION + <top>/wheels/.
make_release() {
  local ver="$1" nocsum="${2:-}"
  local d="$WORK/build-$ver"
  mkdir -p "$d/release/wheels"
  echo "$ver" > "$d/release/VERSION"
  echo "placeholder wheel" > "$d/release/wheels/README.txt"
  local arc="$STAGE/release-$ver.tar.gz"
  tar czf "$arc" -C "$d" release
  [[ "$nocsum" == "--no-checksum" ]] || sha256_of "$arc" > "$arc.sha256"
  printf '%s' "$arc"
}

echo "== on-prem update/rollback test (offline, hermetic) =="
echo "storage_root: $ROOT"
echo

# --- seed the FIRST release (as `install.sh --wheelhouse` would) ------------
V1="1.0.0"
R1="$ROOT/releases/$V1"; mkdir -p "$R1"
"$HOOK" "$R1" "unused" >/dev/null 2>&1
echo "version=$V1" > "$R1/RELEASE"
rel_activate "$ROOT" "$R1" >/dev/null 2>&1
check "seed: current -> v1" "[[ \"\$(readlink '$ROOT/current')\" == '$R1' ]]"
check "seed: venv alias resolves" "[[ -x '$ROOT/venv/bin/openadapt-flow' ]]"

# --- case 1-3: update v1 -> v2 ----------------------------------------------
echo
echo "-- update $V1 -> 2.0.0 --"
ARC2="$(make_release 2.0.0)"
rel_do_update "$ROOT" "$ARC2" "" "" "" "" "" >/dev/null 2>&1
rc=$?
check "update: exit 0" "[[ $rc -eq 0 ]]"
check "update: current -> v2" "[[ \"\$(readlink '$ROOT/current')\" == '$ROOT/releases/2.0.0' ]]"
check "update: previous -> v1" "[[ \"\$(readlink '$ROOT/previous')\" == '$R1' ]]"
check "update: v2 engine present" "[[ -x '$ROOT/releases/2.0.0/venv/bin/openadapt-flow' ]]"
check "update: RELEASE metadata written" "grep -q '^version=2.0.0' '$ROOT/releases/2.0.0/RELEASE'"

# --- case 2: data preserved -------------------------------------------------
check "data: bundle untouched" "[[ \"\$(sha256_of '$ROOT/bundles/keepme.txt')\" == '$DATA_BUNDLE_SHA' ]]"
check "data: queued job untouched" "[[ \"\$(sha256_of '$ROOT/jobs/inbox/queued-0001.job')\" == '$DATA_JOB_SHA' ]]"

# --- case 8a: audit + history recorded --------------------------------------
check "audit: 'updated' record" "grep -q '\"event\":\"updated\".*version=2.0.0' '$OPENADAPT_ONPREM_AUDIT_LOG'"
check "history: 'activated' record" "grep -q '\"event\":\"activated\",\"version\":\"2.0.0\"' '$ROOT/releases/HISTORY'"
check "audit: PHI-free (no seeded bundle text)" "! grep -q 'identity evidence' '$OPENADAPT_ONPREM_AUDIT_LOG'"

# --- case 4: rollback -------------------------------------------------------
echo
echo "-- rollback --"
rel_do_rollback "$ROOT" >/dev/null 2>&1
rc=$?
check "rollback: exit 0" "[[ $rc -eq 0 ]]"
check "rollback: current -> v1" "[[ \"\$(readlink '$ROOT/current')\" == '$R1' ]]"
check "rollback: previous -> v2 (roll-forward ready)" "[[ \"\$(readlink '$ROOT/previous')\" == '$ROOT/releases/2.0.0' ]]"
check "rollback: v1 engine live via current" "[[ -x '$ROOT/current/venv/bin/openadapt-flow' ]]"
check "rollback: data still intact" "[[ \"\$(sha256_of '$ROOT/bundles/keepme.txt')\" == '$DATA_BUNDLE_SHA' ]]"
check "audit: 'rolledback' record" "grep -q '\"event\":\"rolledback\".*version=1.0.0' '$OPENADAPT_ONPREM_AUDIT_LOG'"

# --- case 5: tampered archive (bad checksum) aborts, stays on current -------
echo
echo "-- integrity gate: tampered archive --"
CUR_BEFORE="$(readlink "$ROOT/current")"
ARC3="$(make_release 3.0.0)"
echo "corruption" >> "$ARC3"     # break the checksum without touching sidecar
rel_do_update "$ROOT" "$ARC3" "" "" "" "" "" >/dev/null 2>&1
rc=$?
check "tamper: update fails (nonzero)" "[[ $rc -ne 0 ]]"
check "tamper: current UNCHANGED" "[[ \"\$(readlink '$ROOT/current')\" == '$CUR_BEFORE' ]]"
check "tamper: no v3 release created" "[[ ! -d '$ROOT/releases/3.0.0' ]]"

# --- case 6: no integrity material aborts -----------------------------------
echo
echo "-- integrity gate: no checksum + no signature --"
ARC4="$(make_release 4.0.0 --no-checksum)"
rel_do_update "$ROOT" "$ARC4" "" "" "" "" "" >/dev/null 2>&1
rc=$?
check "no-integrity: update aborts" "[[ $rc -ne 0 ]]"
check "no-integrity: current UNCHANGED" "[[ \"\$(readlink '$ROOT/current')\" == '$CUR_BEFORE' ]]"

# --- case 7: smoke failure aborts, stays on current -------------------------
echo
echo "-- smoke gate: broken engine CLI --"
ARC5="$(make_release 5.0.0)"
OPENADAPT_TEST_BROKEN_CLI=1 rel_do_update "$ROOT" "$ARC5" "" "" "" "" "" >/dev/null 2>&1
rc=$?
check "smoke-fail: update aborts" "[[ $rc -ne 0 ]]"
check "smoke-fail: current UNCHANGED" "[[ \"\$(readlink '$ROOT/current')\" == '$CUR_BEFORE' ]]"
check "smoke-fail: broken release removed" "[[ ! -d '$ROOT/releases/5.0.0' ]]"

# --- case 8b: audit chain still intact after all the update/rollback churn ---
echo
echo "-- audit chain integrity --"
# shellcheck disable=SC2034  # used inside the quoted `check` assertion (eval)
airgap_out="$(OPENADAPT_ONPREM_AUDIT_LOG="$OPENADAPT_ONPREM_AUDIT_LOG" \
  bash "$HERE/verify-airgap.sh" --config "$WORK/nonexistent.yaml" --audit 2>&1 || true)"
check "audit: hash chain intact end-to-end" "grep -q 'audit chain intact' <<< \"\$airgap_out\""

echo
echo "== result: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
