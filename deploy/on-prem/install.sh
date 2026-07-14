#!/usr/bin/env bash
#
# install.sh — stand up the openadapt-flow on-prem (air-gapped) clinic install.
#
# REAL for the parts marked [REAL]; a documented STUB for the parts marked
# [STUB] (they print exactly what an operator must do, rather than pretending to
# do something that needs site-specific input like a disk key or a signed
# release). Nothing here reaches the internet.
#
# What it does:
#   [REAL] create the storage layout (bundles/runs/jobs/audit/keys) with tight
#          perms under storage_root;
#   [REAL] create a Python venv and install the engine + privacy extra FROM A
#          LOCAL WHEELHOUSE (no PyPI/internet) when --wheelhouse is given;
#   [REAL] install the systemd unit + .path watcher (Linux, when --systemd);
#   [REAL] run verify-airgap.sh as an acceptance gate at the end;
#   [STUB] full-disk encryption (operator provisions LUKS/BitLocker/FileVault);
#   [STUB] --update offline signed-release apply (prints the verify+swap steps).
#
# Usage:
#   sudo ./install.sh --config onprem.yaml [--wheelhouse ./wheels] [--systemd]
#   ./install.sh --update --config onprem.yaml     # apply a staged offline release
#
# This script intentionally does NOT call pip against PyPI. Air-gapped installs
# use a wheelhouse copied in on removable media (`pip download` on a connected
# build host, then `--wheelhouse`).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG=""
WHEELHOUSE=""
DO_SYSTEMD=0
DO_UPDATE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)     CONFIG="${2:-}"; shift 2 ;;
    --wheelhouse) WHEELHOUSE="${2:-}"; shift 2 ;;
    --systemd)    DO_SYSTEMD=1; shift ;;
    --update)     DO_UPDATE=1; shift ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

[[ -z "$CONFIG" ]] && { echo "install.sh: --config onprem.yaml is required" >&2; exit 2; }
[[ -f "$CONFIG" ]] || { echo "install.sh: config $CONFIG not found" >&2; exit 2; }

# Minimal YAML scalar reader (top-level or one-level-nested "key: value"). The
# on-prem config is a flat, operator-edited file; we avoid a YAML dependency in
# the bootstrap path so install.sh runs before the venv exists.
cfg() {
  local key="$1"
  grep -E "^[[:space:]]*${key}[[:space:]]*:" "$CONFIG" | head -n1 \
    | sed -E "s/^[[:space:]]*${key}[[:space:]]*:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^\"//; s/\"$//" \
    | tr -d "'"
}

STORAGE_ROOT="$(cfg storage_root)"; STORAGE_ROOT="${STORAGE_ROOT:-/srv/openadapt}"
SERVICE_USER="$(cfg service_user)"; SERVICE_USER="${SERVICE_USER:-openadapt}"
SCRUB="$(cfg scrub)"; SCRUB="${SCRUB:-on}"

echo "openadapt-flow on-prem install"
echo "  storage_root : $STORAGE_ROOT"
echo "  service_user : $SERVICE_USER"
echo "  scrub        : $SCRUB"
echo

if [[ "$DO_UPDATE" -eq 1 ]]; then
  echo "== [STUB] offline signed-release update =="
  cat <<'EOF'
An air-gapped update is operator-pulled and signature-verified — never phoned.
Staged fields come from onprem.yaml:updates. The apply procedure is:

  1. Operator copies the signed release archive + detached signature onto the
     host via removable media (release_archive / release_signature).
  2. Verify the signature against the PINNED vendor public key, e.g.:
       minisign -Vm <release_archive> -p <vendor_pubkey>
     (or `age`/`gpg --verify`). ABORT if verification fails.
  3. Install the verified wheels into a NEW venv (blue/green), run the test
     smoke + `verify-airgap.sh`, then flip the systemd unit to the new venv.
  4. Record the applied version in the audit log.

This step is a documented STUB: it needs the site's staged artifacts and vendor
key, which are not present in this scaffold. Wire steps 2-4 to your key
custodian's procedure.
EOF
  exit 0
fi

echo "== [REAL] storage layout under $STORAGE_ROOT =="
for d in bundles runs jobs jobs/inbox jobs/processing jobs/done jobs/failed audit keys; do
  mkdir -p "$STORAGE_ROOT/$d"
done
# Tight perms: only the service user (and root) should read PHI-at-rest.
chmod 0700 "$STORAGE_ROOT" "$STORAGE_ROOT"/{bundles,runs,jobs,audit,keys} 2>/dev/null || true
if id "$SERVICE_USER" >/dev/null 2>&1; then
  chown -R "$SERVICE_USER" "$STORAGE_ROOT" 2>/dev/null || \
    echo "  (could not chown to $SERVICE_USER — run as root to set ownership)"
else
  echo "  NOTE: service user '$SERVICE_USER' does not exist; create it, then re-run to chown."
fi
echo "  created bundles/ runs/ jobs/{inbox,processing,done,failed} audit/ keys/"

echo
echo "== [STUB] full-disk encryption check =="
cat <<EOF
  PHI-at-rest is protected by FULL-DISK ENCRYPTION on the volume holding
  $STORAGE_ROOT — the operator provisions this (LUKS on Linux, BitLocker on
  Windows, FileVault on macOS). openadapt-flow never holds the disk key.
  This installer cannot verify or provision it for you; confirm the volume is
  encrypted before storing any real bundle. See COMPLIANCE.md.
EOF

echo
if [[ -n "$WHEELHOUSE" ]]; then
  echo "== [REAL] engine venv from local wheelhouse ($WHEELHOUSE) =="
  if [[ ! -d "$WHEELHOUSE" ]]; then
    echo "  wheelhouse $WHEELHOUSE not found" >&2; exit 2
  fi
  python3 -m venv "$STORAGE_ROOT/venv"
  # --no-index => NEVER touch PyPI/internet; install only from the local wheels.
  "$STORAGE_ROOT/venv/bin/pip" install --no-index --find-links "$WHEELHOUSE" \
    'openadapt-flow[privacy]'
  echo "  installed openadapt-flow[privacy] into $STORAGE_ROOT/venv (offline)"
  echo "  NOTE: also install the spaCy NER model offline:"
  echo "    $STORAGE_ROOT/venv/bin/python -m spacy download en_core_web_trf   # needs the model staged locally"
else
  echo "== [STUB] engine install skipped (no --wheelhouse) =="
  echo "  Provide --wheelhouse ./wheels (built off-site with 'pip download"
  echo "  openadapt-flow[privacy]') to install fully offline. Air-gapped hosts"
  echo "  must NOT install from PyPI."
fi

if [[ "$DO_SYSTEMD" -eq 1 ]]; then
  echo
  echo "== [REAL] systemd units =="
  if command -v systemctl >/dev/null 2>&1; then
    sed "s#@STORAGE_ROOT@#$STORAGE_ROOT#g; s#@SERVICE_USER@#$SERVICE_USER#g; s#@HERE@#$HERE#g; s#@CONFIG@#$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")#g" \
      "$HERE/systemd/openadapt-flow-runner.service" > /etc/systemd/system/openadapt-flow-runner.service
    cp "$HERE/systemd/openadapt-flow-runner.path" /etc/systemd/system/openadapt-flow-runner.path
    systemctl daemon-reload
    echo "  installed openadapt-flow-runner.service + .path"
    echo "  enable with: systemctl enable --now openadapt-flow-runner.path"
  else
    echo "  systemctl not found — this host is not systemd. Use docker-compose.yml"
    echo "  or a Windows Scheduled Task calling bin/run-queue.sh watch."
  fi
fi

echo
echo "== [REAL] air-gap acceptance gate =="
OPENADAPT_FLOW_SCRUB="$SCRUB" bash "$HERE/bin/verify-airgap.sh" --config "$CONFIG" || {
  echo
  echo "install.sh: air-gap verification FAILED — resolve before processing PHI." >&2
  exit 1
}

echo
echo "Install complete. Next:"
echo "  1. Confirm $STORAGE_ROOT is on a full-disk-encrypted volume."
echo "  2. Place a compiled bundle under $STORAGE_ROOT/bundles and a"
echo "     deployment.yaml at the path named in $CONFIG (deployment_config)."
echo "  3. Drop a .job file into $STORAGE_ROOT/jobs/inbox and start the runner:"
echo "       OPENADAPT_FLOW_SCRUB=$SCRUB bin/run-queue.sh watch"
