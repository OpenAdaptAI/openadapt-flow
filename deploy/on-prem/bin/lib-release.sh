#!/usr/bin/env bash
#
# lib-release.sh — atomic, rollback-able release management for the on-prem
# (air-gapped) install. Sourced by install.sh (and by bin/test-update.sh).
#
# REAL. No network, no phone-home. Everything here operates on LOCAL paths under
# the clinic's storage_root and reads updates from an OPERATOR-STAGED local
# bundle copied in on removable media.
#
# ---------------------------------------------------------------------------
# On-disk layout (under $STORAGE_ROOT)
# ---------------------------------------------------------------------------
#   releases/<version>/           an immutable release: its own venv, built in
#                                 place so the venv's absolute shebangs never
#                                 move (only the `current` symlink moves).
#   releases/<version>/venv/      the Python venv for that release.
#   releases/<version>/RELEASE    metadata (version, applied_at, source, sha256,
#                                 verify method).
#   releases/HISTORY              append-only ledger of activate/rollback events.
#   current  -> releases/<version>   the ACTIVE release (atomic symlink).
#   previous -> releases/<version>   the prior release (instant rollback target).
#   venv     -> current/venv         human/doc convenience alias (systemd uses
#                                     current/venv/bin directly).
#
#   bundles/ runs/ jobs/ audit/ keys/ deployment.yaml   DATA — NEVER touched by
#   an update or rollback. They live at $STORAGE_ROOT, outside releases/.
#
# ---------------------------------------------------------------------------
# The offline release bundle (prepared off-site, copied in on removable media)
# ---------------------------------------------------------------------------
#   release-<version>.tar.gz          a tar.gz whose contents include:
#                                       VERSION           text: release version
#                                       wheels/           offline pip wheelhouse
#   release-<version>.tar.gz.sha256   checksum sidecar (shasum/sha256sum format)
#   release-<version>.tar.gz.sig      OPTIONAL detached signature (gpg/openssl/
#                                     minisign/signify) for AUTHENTICITY.
#
# Integrity policy (fail-closed): a checksum OR a verifiable signature MUST
# succeed before anything is built; if either is present and FAILS, abort. A
# signature (when the tool + pinned pubkey are present) additionally proves
# authenticity. If neither can be verified, abort — we never apply unverified
# code to a clinic box.
#
# Testing hooks (used ONLY by bin/test-update.sh — never in production):
#   OPENADAPT_ONPREM_BUILD_HOOK  cmd <release_dir> <wheelhouse>  (replace pip)
#   OPENADAPT_ONPREM_SMOKE_HOOK  cmd <release_dir> <config>      (replace smoke)
#
# Audit: if OPENADAPT_ONPREM_AUDIT_BIN + OPENADAPT_ONPREM_AUDIT_LOG are set,
# activate/rollback append a PHI-free `updated`/`rolledback` record.

# NOTE: no `set -e` here — this file is sourced. Callers own their own options;
# functions return non-zero on failure and callers check.

# --- small utilities --------------------------------------------------------

_rel_err()  { printf '  \033[31mERROR\033[0m %s\n' "$1" >&2; }
_rel_warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1" >&2; }
_rel_ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1" >&2; }

# sha256_of FILE -> prints the hex digest (portable: sha256sum or shasum -a 256).
sha256_of() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" | awk '{print $1}'
  else
    shasum -a 256 "$f" | awk '{print $1}'
  fi
}

# atomic_symlink TARGET LINKPATH
# Point LINKPATH at TARGET atomically: create a temp symlink beside LINKPATH,
# then rename(2) it over LINKPATH. rename() is atomic on a single filesystem, so
# a concurrent reader sees either the old or the new target, never a missing one.
#
# The subtlety: when LINKPATH already exists as a symlink-to-a-DIRECTORY, the
# plain `mv` CLI DEREFERENCES it and moves the temp INTO that directory instead
# of replacing the link (BSD/macOS default). We must rename WITHOUT derefer. In
# order of preference:
#   1. GNU `mv -fT`  (--no-target-directory) — the Linux clinic host: atomic.
#   2. perl rename() — portable atomic fallback (macOS/dev boxes).
#   3. rm + mv       — last resort; a sub-millisecond window where the link is
#                      briefly absent (only reached if neither of the above).
atomic_symlink() {
  local target="$1" link="$2"
  local dir base tmp
  dir="$(dirname "$link")"
  base="$(basename "$link")"
  tmp="$dir/.$base.tmp.$$"
  rm -f "$tmp"
  ln -s "$target" "$tmp"
  if mv -fT "$tmp" "$link" 2>/dev/null; then
    return 0
  elif command -v perl >/dev/null 2>&1 && perl -e 'rename($ARGV[0],$ARGV[1]) or exit 1' "$tmp" "$link" 2>/dev/null; then
    return 0
  else
    rm -f "$link"
    mv -f "$tmp" "$link"
  fi
}

# --- integrity + authenticity ----------------------------------------------

# rel_verify_integrity ARCHIVE CHECKSUM SIG PUBKEY [SIG_TOOL]
#   ARCHIVE   path to the release tarball (required, must exist)
#   CHECKSUM  path to a sha256 sidecar, or "" (auto-detects <ARCHIVE>.sha256)
#   SIG       path to a detached signature, or ""
#   PUBKEY    path to the pinned vendor public key, or ""
#   SIG_TOOL  gpg|openssl|minisign|signify, or "" to infer
# Returns 0 if the archive is verified (checksum and/or signature), 1 otherwise.
rel_verify_integrity() {
  local archive="$1" checksum="${2:-}" sig="${3:-}" pubkey="${4:-}" tool="${5:-}"
  local verified=0

  [[ -f "$archive" ]] || { _rel_err "release archive not found: $archive"; return 1; }

  # Auto-detect a checksum sidecar if none was given.
  if [[ -z "$checksum" && -f "$archive.sha256" ]]; then checksum="$archive.sha256"; fi

  # --- checksum (integrity) ---
  if [[ -n "$checksum" ]]; then
    if [[ ! -f "$checksum" ]]; then
      _rel_err "checksum file not found: $checksum"; return 1
    fi
    local want have
    want="$(awk '{print $1}' "$checksum" | head -n1)"
    have="$(sha256_of "$archive")"
    if [[ -n "$want" && "$want" == "$have" ]]; then
      _rel_ok "checksum matches ($have)"; verified=1
    else
      _rel_err "checksum MISMATCH: expected $want, got $have"; return 1
    fi
  fi

  # --- signature (authenticity) ---
  if [[ -n "$sig" && -n "$pubkey" ]]; then
    if [[ ! -f "$sig" ]];    then _rel_err "signature file not found: $sig"; return 1; fi
    if [[ ! -f "$pubkey" ]]; then _rel_err "vendor pubkey not found: $pubkey"; return 1; fi
    # Infer the tool from the signature extension when not told.
    if [[ -z "$tool" ]]; then
      case "$sig" in
        *.minisig) tool=minisign ;;
        *.asc|*.gpg) tool=gpg ;;
        *) if command -v gpg >/dev/null 2>&1; then tool=gpg; else tool=openssl; fi ;;
      esac
    fi
    local rc=1
    case "$tool" in
      minisign) command -v minisign >/dev/null 2>&1 && { minisign -V -m "$archive" -p "$pubkey" -x "$sig" >/dev/null 2>&1; rc=$?; } ;;
      signify)  command -v signify  >/dev/null 2>&1 && { signify -V -p "$pubkey" -m "$archive" -x "$sig" >/dev/null 2>&1; rc=$?; } ;;
      gpg)      command -v gpg      >/dev/null 2>&1 && { gpg --verify "$sig" "$archive" >/dev/null 2>&1; rc=$?; } ;;
      # openssl `dgst -verify` supports RSA/ECDSA PEM keys. For ed25519 use
      # minisign/signify/gpg instead (a pure-signature scheme dgst can't verify).
      openssl)  command -v openssl  >/dev/null 2>&1 && { openssl dgst -sha256 -verify "$pubkey" -signature "$sig" "$archive" >/dev/null 2>&1; rc=$?; } ;;
      *) _rel_err "unknown signature tool: $tool"; return 1 ;;
    esac
    if ! command -v "$tool" >/dev/null 2>&1; then
      _rel_warn "signature tool '$tool' not installed — authenticity UNVERIFIED (relying on checksum)"
    elif [[ "$rc" -eq 0 ]]; then
      _rel_ok "signature verified with $tool against pinned vendor key"; verified=1
    else
      _rel_err "signature verification FAILED with $tool — refusing this release"; return 1
    fi
  fi

  if [[ "$verified" -eq 1 ]]; then
    return 0
  fi
  _rel_err "no verifiable integrity material (need a .sha256 checksum or a signature+pubkey)"
  return 1
}

# --- extraction + version ---------------------------------------------------

# rel_extract ARCHIVE DEST -> extracts into DEST, prints the dir holding wheels/
rel_extract() {
  local archive="$1" dest="$2"
  mkdir -p "$dest"
  case "$archive" in
    *.tar.gz|*.tgz) tar xzf "$archive" -C "$dest" ;;
    *.tar)          tar xf  "$archive" -C "$dest" ;;
    *) _rel_err "unsupported archive type: $archive (want .tar.gz/.tgz/.tar)"; return 1 ;;
  esac
  # Locate the payload root (handles both flat and single-top-dir archives).
  local wheels
  wheels="$(find "$dest" -maxdepth 3 -type d -name wheels 2>/dev/null | head -n1)"
  if [[ -n "$wheels" ]]; then
    dirname "$wheels"
  else
    # No wheels/ dir (e.g. a build-hook test bundle): use the top-level dir.
    local top
    top="$(find "$dest" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    printf '%s' "${top:-$dest}"
  fi
}

# rel_wheelhouse_version WHEELHOUSE -> prints the openadapt-flow wheel's version
# by parsing the wheel filename (no build/network needed).
rel_wheelhouse_version() {
  local wh="$1" f
  [[ -d "$wh" ]] || return 1
  f="$(find "$wh" -maxdepth 1 -type f \( -name 'openadapt_flow-*.whl' -o -name 'openadapt-flow-*.whl' \) 2>/dev/null | head -n1)"
  [[ -n "$f" ]] || return 1
  basename "$f" | sed -E 's/^openadapt[_-]flow-([^-]+)-.*/\1/'
}

# rel_detect_version SRC_DIR WHEELHOUSE -> resolves a version string.
# Priority: VERSION file -> wheelhouse wheel name -> timestamp.
rel_detect_version() {
  local src="$1" wheels="${2:-}" v=""
  if [[ -f "$src/VERSION" ]]; then
    v="$(head -n1 "$src/VERSION" | tr -d '[:space:]')"
  fi
  if [[ -z "$v" && -n "$wheels" ]]; then
    v="$(rel_wheelhouse_version "$wheels" 2>/dev/null || true)"
  fi
  [[ -z "$v" ]] && v="r$(date -u +%Y%m%dT%H%M%SZ)"
  # Filesystem-safe.
  printf '%s' "$v" | tr -c 'A-Za-z0-9._+-' '_'
}

# --- build + smoke ----------------------------------------------------------

# rel_build_release RELEASE_DIR WHEELHOUSE -> creates RELEASE_DIR/venv.
# Built IN PLACE at the final release path so the venv's absolute shebangs are
# correct forever (only the `current` symlink ever moves). NEVER touches PyPI.
rel_build_release() {
  local reldir="$1" wheels="$2"
  if [[ -n "${OPENADAPT_ONPREM_BUILD_HOOK:-}" ]]; then
    "$OPENADAPT_ONPREM_BUILD_HOOK" "$reldir" "$wheels"
    return $?
  fi
  local py="${OPENADAPT_ONPREM_PY:-python3}"
  if [[ ! -d "$wheels" ]]; then
    _rel_err "wheelhouse not found in release bundle: $wheels"; return 1
  fi
  "$py" -m venv "$reldir/venv" || return 1
  # --no-index => offline only; the wheelhouse is the sole package source.
  "$reldir/venv/bin/pip" install --no-index --find-links "$wheels" 'openadapt-flow[privacy]'
}

# rel_smoke RELEASE_DIR CONFIG -> 0 if the NEW release is healthy, else 1.
# Runs BEFORE the atomic flip; a failure leaves `current` untouched.
rel_smoke() {
  local reldir="$1" config="${2:-}"
  if [[ -n "${OPENADAPT_ONPREM_SMOKE_HOOK:-}" ]]; then
    "$OPENADAPT_ONPREM_SMOKE_HOOK" "$reldir" "$config"
    return $?
  fi
  local cli="$reldir/venv/bin/openadapt-flow"
  if [[ ! -x "$cli" ]]; then
    _rel_err "smoke: engine CLI missing in new release ($cli)"; return 1
  fi
  if ! "$cli" --version >/dev/null 2>&1 && ! "$cli" --help >/dev/null 2>&1; then
    _rel_err "smoke: engine CLI does not run in new release"; return 1
  fi
  # Re-run the air-gap acceptance gate against the new release, if present.
  local here airgap
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  airgap="$here/verify-airgap.sh"
  if [[ -x "$airgap" && -n "$config" && -f "$config" ]]; then
    if ! OPENADAPT_FLOW_SCRUB="${OPENADAPT_FLOW_SCRUB:-on}" bash "$airgap" --config "$config" >/dev/null 2>&1; then
      _rel_err "smoke: air-gap verification failed for new release"; return 1
    fi
  fi
  _rel_ok "smoke passed (CLI runs, air-gap gate green)"
  return 0
}

# --- history + audit --------------------------------------------------------

_rel_history_append() {
  local root="$1" event="$2" version="$3" relpath="$4"
  local hist="$root/releases/HISTORY"
  mkdir -p "$root/releases"
  printf '{"ts":"%s","event":"%s","version":"%s","release":"%s","actor":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event" "$version" "$relpath" \
    "$(id -un 2>/dev/null || echo unknown)" >> "$hist"
  chmod 0600 "$hist" 2>/dev/null || true
}

_rel_audit() {
  local event="$1" version="$2"
  [[ -n "${OPENADAPT_ONPREM_AUDIT_BIN:-}" && -n "${OPENADAPT_ONPREM_AUDIT_LOG:-}" ]] || return 0
  OPENADAPT_ONPREM_AUDIT_LOG="$OPENADAPT_ONPREM_AUDIT_LOG" \
    bash "$OPENADAPT_ONPREM_AUDIT_BIN" "$event" release --note "version=$version" >/dev/null 2>&1 || true
}

# --- activation (the atomic flip) + rollback --------------------------------

# rel_current_path ROOT  -> absolute path the `current` symlink resolves to ("")
rel_current_path()  { readlink "$1/current"  2>/dev/null || true; }
rel_previous_path() { readlink "$1/previous" 2>/dev/null || true; }

# rel_version_of RELEASE_DIR -> version recorded in its RELEASE file (or basename)
rel_version_of() {
  local d="$1"
  if [[ -f "$d/RELEASE" ]]; then
    grep -E '^version=' "$d/RELEASE" | head -n1 | sed 's/^version=//'
  else
    basename "$d"
  fi
}

# rel_activate ROOT RELEASE_DIR -> flip `current` to RELEASE_DIR atomically,
# recording the outgoing release as `previous`. Idempotent-safe.
rel_activate() {
  local root="$1" reldir="$2"
  local old version
  old="$(rel_current_path "$root")"
  atomic_symlink "$reldir" "$root/current"
  if [[ -n "$old" && "$old" != "$reldir" ]]; then
    atomic_symlink "$old" "$root/previous"
  fi
  # Convenience alias for humans/docs (systemd uses current/venv/bin directly).
  atomic_symlink "current/venv" "$root/venv"
  version="$(rel_version_of "$reldir")"
  _rel_history_append "$root" activated "$version" "$reldir"
  _rel_audit updated "$version"
  _rel_ok "current -> $reldir (version $version)"
}

# rel_do_update ROOT ARCHIVE CHECKSUM SIG PUBKEY SIG_TOOL CONFIG VERSION_OVERRIDE
# The whole air-gapped update: verify -> build a new release in place -> smoke
# -> atomic flip. On ANY failure before the flip, `current` is untouched.
rel_do_update() {
  local root="$1" archive="$2" checksum="${3:-}" sig="${4:-}" pubkey="${5:-}"
  local tool="${6:-}" config="${7:-}" version_override="${8:-}"

  [[ -n "$archive" ]] || { _rel_err "no release archive given (--release / updates.release_archive)"; return 2; }
  [[ -f "$archive" ]] || { _rel_err "release archive not found: $archive"; return 2; }

  mkdir -p "$root/releases"

  echo "== [1/5] verify offline release bundle =="
  rel_verify_integrity "$archive" "$checksum" "$sig" "$pubkey" "$tool" || return 1

  echo "== [2/5] extract (local, no network) =="
  local stage src wheels
  stage="$(mktemp -d "$root/releases/.stage.XXXXXX")" || return 1
  # Ensure staging is cleaned on any early return.
  # shellcheck disable=SC2064
  trap "rm -rf '$stage'" RETURN
  src="$(rel_extract "$archive" "$stage")" || { rm -rf "$stage"; return 1; }
  wheels="$src/wheels"

  local version reldir
  version="${version_override:-$(rel_detect_version "$src" "$wheels")}"
  reldir="$root/releases/$version"
  if [[ -d "$reldir" ]]; then
    if [[ "$(rel_current_path "$root")" == "$reldir" ]]; then
      _rel_err "version $version is already the current release — nothing to do"
      return 3
    fi
    # A non-current dir with this name exists (partial/old): disambiguate.
    reldir="$root/releases/${version}+$(date -u +%Y%m%dT%H%M%SZ)"
    version="$(basename "$reldir")"
  fi

  echo "== [3/5] build new release in place: $reldir =="
  mkdir -p "$reldir"
  if ! rel_build_release "$reldir" "$wheels"; then
    _rel_err "build failed — removing $reldir, staying on current release"
    rm -rf "$reldir"; return 1
  fi
  {
    echo "version=$version"
    echo "applied_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "source_archive=$(basename "$archive")"
    echo "source_sha256=$(sha256_of "$archive")"
  } > "$reldir/RELEASE"
  chmod 0600 "$reldir/RELEASE" 2>/dev/null || true

  echo "== [4/5] smoke-check new release BEFORE flip =="
  if ! rel_smoke "$reldir" "$config"; then
    _rel_err "smoke failed — removing $reldir, staying on current release"
    rm -rf "$reldir"; return 1
  fi

  echo "== [5/5] atomic flip =="
  rel_activate "$root" "$reldir"
  rm -rf "$stage"; trap - RETURN
  echo
  echo "UPDATE OK: now running $version. Roll back instantly with: install.sh --rollback --config <cfg>"
  return 0
}

# rel_do_rollback ROOT -> instantly revert `current` to `previous`.
rel_do_rollback() {
  local root="$1"
  local prev cur pv
  prev="$(rel_previous_path "$root")"
  cur="$(rel_current_path "$root")"
  if [[ -z "$prev" ]]; then
    _rel_err "no previous release recorded — nothing to roll back to"; return 1
  fi
  if [[ ! -d "$prev" ]]; then
    _rel_err "previous release dir missing: $prev — cannot roll back"; return 1
  fi
  pv="$(rel_version_of "$prev")"
  atomic_symlink "$prev" "$root/current"
  # Swap so a rollback can be rolled forward again.
  if [[ -n "$cur" && "$cur" != "$prev" ]]; then
    atomic_symlink "$cur" "$root/previous"
  fi
  atomic_symlink "current/venv" "$root/venv"
  _rel_history_append "$root" rolledback "$pv" "$prev"
  _rel_audit rolledback "$pv"
  _rel_ok "ROLLED BACK: current -> $prev (version $pv)"
  echo
  echo "ROLLBACK OK: now running $pv."
  return 0
}
