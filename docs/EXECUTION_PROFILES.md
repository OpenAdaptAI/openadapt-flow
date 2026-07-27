# Execution profiles

`openadapt-flow run` applies one named posture over the existing policy,
identity, effect, authorization, durability, and evidence machinery:

| Profile | Contract | Successful report |
| --- | --- | --- |
| `demo` | Permits uncertified tutorials and screen evidence. Integrity checks and runtime refusals still apply. | `COMPLETED_UNVERIFIED`; never production-eligible |
| `standard` | Requires certification, a sealed manifest, durable and settled-state execution, identity coverage for consequential actions, and effect evidence at the configured minimum tier for every consequential effect. Application-level encryption is optional when the qualified deployment supplies an encrypted storage boundary; an encrypted bundle always produces encrypted checkpoints. | `VERIFIED` only when the complete runtime contract passes |
| `regulated` | Standard plus encrypted bundle contents, strictly sealed evidence assets, and encrypted durable checkpoints in the customer-controlled environment. Model egress remains off unless explicitly authorized and PHI allowlisted. | `VERIFIED` only when the complete runtime contract passes |

Select the profile in deployment configuration:

```yaml
runtime:
  profile: regulated
```

or for one invocation:

```bash
openadapt-flow run bundle --config deployment.yaml --profile standard
```

Raw `replay` is the Demo path. For compatibility, an existing `run` invocation
that selects no profile retains the pre-profile low-level flag behavior and
legacy report fields. New production deployments should select `standard` or
`regulated` explicitly.

Named profiles do not replace policy certification. A policy describes what the
bundle must contain; the profile determines which admission and runtime
properties are mandatory for this execution.

Low-level flags can strengthen a profile. They cannot weaken a selected
Standard or Regulated contract. In particular:

- Standard and Regulated require effect evidence at the configured minimum
  tier; an operator approval cannot turn an immediate-screen-only or
  unverified write into `VERIFIED`.
- Regulated refuses `--allow-unencrypted` and requires
  `OPENADAPT_BUNDLE_KEY`; the same key seals its durable checkpoints. Standard
  accepts a qualified external encrypted-storage boundary, but if its bundle is
  application-sealed the runtime requires and reuses that key for checkpoints.
- Standard and Regulated enable durable execution automatically.
- Standard and Regulated require settled-state detection.
- A successful Demo remains `COMPLETED_UNVERIFIED`, even when every tutorial
  step completed.

Reports retain the legacy `success` field for compatibility and add
`execution_profile`, `execution_outcome`, and `production_eligible`. Production
callers must use `execution_outcome`; Standard and Regulated treat
`COMPLETED_UNVERIFIED` as a non-success exit.

## Transaction outcomes (Section 3)

The coarse `execution_outcome` (`VERIFIED` / `COMPLETED_UNVERIFIED` / `HALTED` /
`FAILED` / `ROLLED_BACK`) is refined into a first-class **terminal transaction
outcome** that states what is known about the BUSINESS EFFECT. It is additive:
`execution_outcome`, `success`, `production_eligible`, and `outcome_envelope`
are unchanged, and the new `transaction_outcome` is derived from the same typed
evidence (see `openadapt_flow.transaction`).

| `transaction_outcome` | Meaning | Billable | Production success |
| --- | --- | --- | --- |
| `VERIFIED` | Every declared effect (and collateral-effect check) passed at/above the required tier. | yes | yes |
| `HALTED_BEFORE_EFFECT` | The run stopped AND absence was positively established for **every** consequential step: each was either observed `absent` by the effect verifier, or recorded as having stopped before delivery was attempted. A consequential step that reached actuation and was never verified does **not** qualify. | no | no |
| `RECONCILIATION_REQUIRED` | Delivery/persistence is uncertain, conflicting, or temporarily unverifiable. The runtime does NOT blind-retry; resuming must reconcile current state first. | no | no |
| `FAILED_PLATFORM` | An OpenAdapt/platform failure before any possible effect. | no (`transaction_platform_fault=true`) | no |
| `CANCELED` | Canceled before any business effect. | no | no |
| `REJECTED_POLICY` | Authorization / identity / qualification / environment refused execution before any effect. | no | no |
| `COMPLETED_UNVERIFIED` | Demo-only completion with no production-grade effect evidence. | never | never |
| `ROLLED_BACK` | A detected duplicate / collateral write was compensated and re-verified (legacy compensation path). | no | no |

Mapping from the coarse outcome: `VERIFIED` and `ROLLED_BACK` map through
1:1; a coarse `HALTED` splits into `REJECTED_POLICY` (a governed pre-execution
refusal or identity refusal), `HALTED_BEFORE_EFFECT` (verifier-established
absence), or `RECONCILIATION_REQUIRED` (any uncertain/conflicting delivery or
persistence, which always dominates); a coarse `FAILED` maps to `CANCELED` (when
the run was canceled) or `FAILED_PLATFORM`.

**Absence requires positive evidence.** `HALTED_BEFORE_EFFECT`,
`REJECTED_POLICY`, `CANCELED`, and `FAILED_PLATFORM` all assert that no business
effect occurred, and a customer who receives one reconciles nothing. So none of
them may be returned while a consequential step's effect is unaccounted for. For
each consequential step the runtime requires one of:

- the effect verifier read the system of record and observed **every** declared
  effect `absent`; or
- the step is recorded as `not_actuated` — it was skipped, it is a
  pre-execution gate pseudo-step, or it stopped at a typed pre-delivery refusal
  (policy/authorization gate, unverified pre-click identity check, or a
  `safety_halt` / `governed_refusal` such as an OCR or structural resolution
  refusal).

An empty `effect_evidence` list is **not** evidence of absence — it means
verification never ran. A consequential step that reached actuation with no
verification performed is `delivery_uncertain`, and the run is
`RECONCILIATION_REQUIRED`. This holds even when the backend committed the write
and then hung past the client deadline: the client sees an error, the store
holds the row, and the run must report that the write needs reconciling rather
than claim a clean bill of health.

Each run also persists an **effect journal** (`effect_journal`): one PHI-free
entry per consequential step recording the intended effect (by contract hash),
the actuation attempt state, the verifier's observed effect, verifier freshness,
and any collateral reconciliation. Callers that meter usage should read
`transaction_billable` / `transaction_platform_fault`: a `FAILED_PLATFORM` is
never a billable success and a `COMPLETED_UNVERIFIED` is never a production
success.

**Idempotency.** A caller may pass a run-level `idempotency_key`; when the
`Replayer` is built with an `idempotency_ledger`, a repeat under the same key is
suppressed before any actuation (`idempotent_replay=true`, no consequential
action re-performed) rather than blind-retried.

Scoped out of the runtime and tracked as follow-ups: full saga compensation
steps, the human-reconciliation-task UI, and Cloud/runner propagation of the
transaction taxonomy.
