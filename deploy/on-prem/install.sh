#!/usr/bin/env bash
#
# install.sh — stand up / update the openadapt-flow on-prem (air-gapped) install.
#
# REAL for the parts marked [REAL]; a documented STUB only for the one part that
# needs site-specific hardware input (full-disk encryption). Nothing here reaches
# the internet: no PyPI, no phone-home, no telemetry.
#
# What it does:
#   [REAL] create the storage layout (bundles/runs/jobs/audit/keys) with tight
#          perms under storage_root;
#   [REAL] create the FIRST versioned release (releases/<version>/venv) from a
#          LOCAL WHEELHOUSE (no PyPI/internet) and point `current` at it;
#   [REAL] install the systemd unit + .path watcher (Linux, when --systemd);
#   [REAL] --update: verify a staged offline release, build it in a NEW release
#          dir, smoke-check it, then ATOMICALLY flip `current` (rollback-able);
#   [REAL] --rollback: instantly revert `current` to the previous release;
#   [REAL] run verify-airgap.sh as an acceptance gate at the end;
#   [STUB] full-disk encryption (operator provisions LUKS/BitLocker/FileVault).
#
# Usage:
#   # first install
#   sudo ./install.sh --config onprem.yaml [--wheelhouse ./wheels] [--systemd]
#
#   # apply a staged, signed offline release (atomic; verifies before flipping)
#   sudo ./install.sh --update --config onprem.yaml \
#        [--release ./release-1.7.0.tar.gz] [--checksum ...] \
#        [--signature ...] [--pubkey ...] [--sig-tool gpg|openssl|minisign]
#
#   # revert to the previous release instantly (no rebuild)
#   sudo ./install.sh --rollback --config onprem.yaml
#
# Air-gapped installs use a wheelhouse copied in on removable media (`pip
# download` on a connected build host, then `--wheelhouse`). See UPDATE.md for
# the full update/rollback runbook and how to build a signed release bundle.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bin/lib-release.sh
source "$HERE/bin/lib-release.sh"

CONFIG=""
WHEELHOUSE=""
DO_SYSTEMD=0
DO_UPDATE=0
DO_ROLLBACK=0
REL_ARCHIVE=""
REL_CHECKSUM=""
REL_SIG=""
REL_PUBKEY=""
REL_SIGTOOL=""
REL_VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)     CONFIG="${2:-}"; shift 2 ;;
    --wheelhouse) WHEELHOUSE="${2:-}"; shift 2 ;;
    --systemd)    DO_SYSTEMD=1; shift ;;
    --update)     DO_UPDATE=1; shift ;;
    --rollback)   DO_ROLLBACK=1; shift ;;
    --release)    REL_ARCHIVE="${2:-}"; shift 2 ;;
    --checksum)   REL_CHECKSUM="${2:-}"; shift 2 ;;
    --signature)  REL_SIG="${2:-}"; shift 2 ;;
    --pubkey)     REL_PUBKEY="${2:-}"; shift 2 ;;
    --sig-tool)   REL_SIGTOOL="${2:-}"; shift 2 ;;
    --release-version) REL_VERSION="${2:-}"; shift 2 ;;
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

# Wire the audit log so update/rollback append a PHI-free record.
export OPENADAPT_ONPREM_AUDIT_BIN="$HERE/bin/audit-log.sh"
export OPENADAPT_ONPREM_AUDIT_LOG="${OPENADAPT_ONPREM_AUDIT_LOG:-$STORAGE_ROOT/audit/audit.log}"

echo "openadapt-flow on-prem install"
echo "  storage_root : $STORAGE_ROOT"
echo "  service_user : $SERVICE_USER"
echo "  scrub        : $SCRUB"
echo

# ---------------------------------------------------------------------------
# --rollback : instant revert to the previous release (no rebuild, no network)
# ---------------------------------------------------------------------------
if [[ "$DO_ROLLBACK" -eq 1 ]]; then
  echo "== [REAL] rollback to previous release =="
  rel_do_rollback "$STORAGE_ROOT"
  exit $?
fi

# ---------------------------------------------------------------------------
# --update : verify + build + smoke + atomic flip of a staged offline release
# ---------------------------------------------------------------------------
if [[ "$DO_UPDATE" -eq 1 ]]; then
  echo "== [REAL] offline signed-release update =="
  # Inputs come from flags first, then onprem.yaml:updates. Nothing is fetched.
  REL_ARCHIVE="${REL_ARCHIVE:-$(cfg release_archive)}"
  REL_SIG="${REL_SIG:-$(cfg release_signature)}"
  REL_PUBKEY="${REL_PUBKEY:-$(cfg vendor_pubkey)}"
  REL_CHECKSUM="${REL_CHECKSUM:-$(cfg release_checksum)}"
  REL_SIGTOOL="${REL_SIGTOOL:-$(cfg signature_tool)}"
  if [[ -z "$REL_ARCHIVE" ]]; then
    echo "install.sh: no release archive. Pass --release <archive> or set" >&2
    echo "  updates.release_archive in $CONFIG (staged from removable media)." >&2
    exit 2
  fi
  rel_do_update "$STORAGE_ROOT" "$REL_ARCHIVE" "$REL_CHECKSUM" "$REL_SIG" \
    "$REL_PUBKEY" "$REL_SIGTOOL" "$CONFIG" "$REL_VERSION"
  exit $?
fi

# ---------------------------------------------------------------------------
# first install
# ---------------------------------------------------------------------------
echo "== [REAL] storage layout under $STORAGE_ROOT =="
for d in bundles runs jobs jobs/inbox jobs/processing jobs/done jobs/failed audit keys releases; do
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
echo "  created bundles/ runs/ jobs/{inbox,processing,done,failed} audit/ keys/ releases/"

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
  if [[ ! -d "$WHEELHOUSE" ]]; then
    echo "  wheelhouse $WHEELHOUSE not found" >&2; exit 2
  fi
  # Build the FIRST versioned release in place, then flip `current` to it. The
  # version is read from the wheelhouse wheel name (no build/network needed).
  VER="${REL_VERSION:-$(rel_detect_version "$WHEELHOUSE" "$WHEELHOUSE")}"
  RELDIR="$STORAGE_ROOT/releases/$VER"
  echo "== [REAL] first release from local wheelhouse ($WHEELHOUSE) -> $RELDIR =="
  if [[ -d "$RELDIR" ]]; then
    echo "  release $VER already exists at $RELDIR (reusing)"
  else
    mkdir -p "$RELDIR"
    if ! rel_build_release "$RELDIR" "$WHEELHOUSE"; then
      echo "install.sh: first release build failed" >&2; rm -rf "$RELDIR"; exit 1
    fi
    {
      echo "version=$VER"
      echo "applied_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "source_archive=wheelhouse:$(basename "$WHEELHOUSE")"
      echo "source_sha256="
    } > "$RELDIR/RELEASE"
  fi
  rel_activate "$STORAGE_ROOT" "$RELDIR"
  echo "  installed openadapt-flow[privacy] into $RELDIR/venv (offline)"
  echo "  active engine path: $STORAGE_ROOT/current/venv/bin"
  echo "  NOTE: also install the spaCy NER model offline:"
  echo "    $STORAGE_ROOT/current/venv/bin/python -m spacy download en_core_web_trf   # model staged locally"
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
    echo "  the unit runs the engine via $STORAGE_ROOT/current/venv/bin — an"
    echo "  --update flip / --rollback takes effect on the NEXT job (no unit edit)."
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
echo "  4. To patch later: stage a signed release and run"
echo "       sudo ./install.sh --update --config $CONFIG      (atomic, rollback-able)"
echo "     Revert instantly with:  sudo ./install.sh --rollback --config $CONFIG"
echo "     See deploy/on-prem/UPDATE.md for the full runbook."
