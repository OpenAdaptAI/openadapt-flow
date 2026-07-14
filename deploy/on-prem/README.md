# `deploy/on-prem/` — air-gapped clinic deployment package

The deployable form a regulated clinic installs so that **PHI never leaves their
network and nothing phones home**. A single clinic machine/server runs the
openadapt-flow engine + a local run queue + PHI scrubbing + local encrypted
storage + a local audit log, fully offline.

Start with **[../../docs/ON_PREM.md](../../docs/ON_PREM.md)** (architecture +
install story) and **[COMPLIANCE.md](COMPLIANCE.md)** (posture + data-flow).

## Contents

| File | What | REAL vs STUB |
|---|---|---|
| `onprem.example.yaml` | Install config (storage roots, audit path, egress posture, offline-update pointers). Copy to `onprem.yaml`. | REAL (data file) |
| `install.sh` | Stand up storage layout + venv (offline wheelhouse) + systemd units, then run the air-gap gate. | REAL for layout/venv/systemd/gate; STUB for FDE provisioning + `--update` apply |
| `bin/run-queue.sh` | Local, directory-based run queue. Claims jobs, runs `openadapt-flow run`, records outcomes, files done/failed. Fail-closed on `SCRUB!=on`. | REAL (thin wrapper over the shipped CLI) |
| `bin/audit-log.sh` | Append-only, hash-chained, PHI-free audit writer. | REAL |
| `bin/verify-airgap.sh` | Asserts no outbound path: config/env/deployment URL scan, optional active egress probe, optional audit-chain walk. | REAL (best-effort; firewall is primary) |
| `systemd/openadapt-flow-runner.service` + `.path` | Event-driven runner unit with kernel-level egress denial (`IPAddressDeny=any`). | REAL (Linux/systemd) |
| `docker-compose.yml` + `Dockerfile` | Container alternative on an `internal:true` (egress-blocked) network; optional LAN-only VLM appliance profile. | REAL topology; STUB image build (needs offline `./wheels`) |
| `COMPLIANCE.md` | PHIPA/HIPAA-adjacent posture, data-flow diagram, what's encrypted, boundaries. | REAL (doc; non-legal) |

## 60-second install (systemd host)

```bash
cp onprem.example.yaml onprem.yaml            # edit storage_root etc.
sudo ./install.sh --config onprem.yaml --wheelhouse ./wheels --systemd
sudo systemctl enable --now openadapt-flow-runner.path

# place a compiled bundle + a deployment.yaml under storage_root, then:
cat > /srv/openadapt/jobs/inbox/triage-0001.job <<'EOF'
bundle=/srv/openadapt/bundles/vitals-triage
params=patient_ref=PT-INTERNAL-42;note=Reviewed
EOF
```

The runner picks up the job, runs it through the deterministic engine, appends a
PHI-free record to `audit/audit.log`, and files the job under `jobs/done` (exit
0) or `jobs/failed` (a fail-safe halt). Re-run the gate any time:

```bash
OPENADAPT_FLOW_SCRUB=on ./bin/verify-airgap.sh --config onprem.yaml --probe --audit
```

## Non-negotiables

- **`OPENADAPT_FLOW_SCRUB=on`** — fail-closed PHI scrubbing. `run-queue.sh`
  refuses to start otherwise.
- **`storage_root` on a full-disk-encrypted volume** — the primary at-rest
  control (per-bundle sealing is deferred; see COMPLIANCE.md).
- **No cloud keys, no off-LAN URLs** — `verify-airgap.sh` must PASS before PHI.
- Offline, operator-pulled, signed updates only — never auto-update.
