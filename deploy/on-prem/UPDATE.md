# Offline update & rollback — on-prem (air-gapped) runbook

How an operator patches a regulated, air-gapped clinic box and reverts instantly
if a release misbehaves. **Nothing here touches the network.** No PyPI, no
phone-home, no telemetry: a release is prepared off-site, copied in on removable
media, verified locally, and applied atomically.

The design goal: an update is a **single atomic symlink flip** after the new
release has been **verified and smoke-tested**, and a rollback is the **reverse
flip** — so a bad patch is a seconds-long, zero-data-loss event.

---

## On-disk model

Under `storage_root` (from `onprem.yaml`):

```
releases/<version>/venv/     an immutable release, built in place (its venv's
releases/<version>/RELEASE   absolute paths never move — only `current` moves)
releases/HISTORY             append-only ledger of activate/rollback events
current  -> releases/<version>    the ACTIVE release   (atomic symlink)
previous -> releases/<version>    the rollback target  (atomic symlink)
venv     -> current/venv          convenience alias for humans/docs

bundles/ runs/ jobs/ audit/ keys/ deployment.yaml   DATA — NEVER touched by an
                                                     update or a rollback.
```

The systemd unit runs the engine via `storage_root/current/venv/bin`. A flip or
rollback therefore takes effect on the **next job** — no unit edit, no
`daemon-reload`, no service interruption of an in-flight run (it finishes on the
release it started with; `Type=oneshot` picks up the new one next time).

Customer data lives **outside** `releases/` and is never read, moved, or deleted
by update/rollback. Config, keys, the hash-chained `audit.log`, and queued jobs
all survive across any number of updates.

---

## Build a signed release bundle (off-site, on a connected host)

The bundle is a tarball containing a `VERSION` file and an offline pip
wheelhouse, plus a checksum and (recommended) a detached signature:

```bash
# 1. Assemble the payload
mkdir -p release/wheels
echo "1.7.0" > release/VERSION
pip download 'openadapt-flow[privacy]==1.7.0' -d release/wheels   # + deps

# 2. Archive it
tar czf release-1.7.0.tar.gz release

# 3. Integrity: sha256 sidecar (REQUIRED unless you sign)
shasum -a 256 release-1.7.0.tar.gz > release-1.7.0.tar.gz.sha256

# 4. Authenticity: detached signature with your PINNED vendor key (RECOMMENDED)
#    minisign (ed25519):
minisign -Sm release-1.7.0.tar.gz            # -> release-1.7.0.tar.gz.minisig
#    or openssl (RSA/ECDSA PEM key):
openssl dgst -sha256 -sign vendor_priv.pem \
  -out release-1.7.0.tar.gz.sig release-1.7.0.tar.gz
```

Copy the archive + its `.sha256` and/or signature onto removable media. Keep the
**private** signing key off the clinic box forever; only the **public** key is
staged on the box (`updates.vendor_pubkey`).

---

## Apply an update (on the clinic box, offline)

```bash
cd deploy/on-prem

# Option A — point the config at the staged files (updates: block in onprem.yaml)
sudo ./install.sh --update --config onprem.yaml

# Option B — pass paths explicitly
sudo ./install.sh --update --config onprem.yaml \
  --release   /media/usb/release-1.7.0.tar.gz \
  --checksum  /media/usb/release-1.7.0.tar.gz.sha256 \
  --signature /media/usb/release-1.7.0.tar.gz.minisig \
  --pubkey    /srv/openadapt/keys/vendor.pub \
  --sig-tool  minisign
```

What `--update` does, in order — **it never flips `current` until every gate
passes**:

1. **Verify** the bundle: the sha256 checksum must match **and/or** the
   signature must verify against the pinned public key. If either is present and
   **fails**, the update aborts (fail-closed). If neither can be verified, it
   aborts.
2. **Build** the new release in a *new* `releases/<version>/` dir (offline `pip
   --no-index` from the bundled wheelhouse). The running release is untouched.
3. **Smoke-test** the new release: the engine CLI must run and the air-gap
   acceptance gate (`verify-airgap.sh`) must pass.
4. **Atomically flip** `current` -> the new release and record the outgoing one
   as `previous`. A PHI-free `updated` record is appended to `audit.log`.

If step 1, 2, or 3 fails, the half-built release dir is removed and the box stays
on the release it was already running. **There is no window in which a part, or
unverified, release is live.**

---

## Roll back (instant, no rebuild)

```bash
sudo ./install.sh --rollback --config onprem.yaml
```

Flips `current` back to `previous` atomically and swaps the pointers so you can
roll forward again. A `rolledback` record is appended to `audit.log`. Because
both releases already exist on disk, rollback is a symlink swap — effectively
instantaneous, with zero effect on customer data.

> First-update caveat: a box installed before the release layout existed has no
> `previous` until its **second** managed update. The first `--update` builds the
> first managed release; a pre-existing `venv/` (old layout) is left in place and
> can be removed once the new release is confirmed healthy.

---

## Verify state / audit

```bash
readlink /srv/openadapt/current      # which version is live
readlink /srv/openadapt/previous     # the rollback target
cat      /srv/openadapt/releases/HISTORY
OPENADAPT_ONPREM_AUDIT_LOG=/srv/openadapt/audit/audit.log \
  ./bin/verify-airgap.sh --config onprem.yaml --audit    # walk the hash chain
```

`audit.log` records `updated` / `rolledback` events (version only — PHI-free),
hash-chained to the surrounding run records, so the update history is
tamper-evident alongside the run history.

---

## Prune old releases (disk hygiene)

Keep at least `current` and `previous`. Older releases can be removed once you
are confident you won't roll back to them:

```bash
# keep the 2 newest, delete the rest (never delete current/previous targets)
ls -1dt /srv/openadapt/releases/*/ | tail -n +3 | xargs -r rm -rf
```

---

## Test it yourself

`bin/test-update.sh` proves the whole update -> rollback lifecycle (atomic flip,
data preservation, checksum/signature/smoke gates, and audit records) in a
throwaway temp dir, fully offline:

```bash
./bin/test-update.sh        # 27 assertions; exit 0 = all pass
```

---

## Vendor visibility

By design the vendor is **blind** to a pilot's health — nothing phones home (see
`COMPLIANCE.md`). Update outcomes live only in the local `audit.log` and
`releases/HISTORY`. Support is out-of-band: the operator emails a scrubbed
`audit.log` slice. Applying and rolling back releases is entirely the operator's
local action.
