# Limits and guarantees

OpenAdapt compiles demonstrated GUI workflows into deterministic, locally
executable programs. Healthy runs make no model calls. When an interface
changes, OpenAdapt can re-resolve a target from retained evidence, ask an
explicitly enabled model for a proposal, or halt for an operator.

This page defines the boundary of those claims. It is written for people
deciding whether a workflow is suitable for evaluation or deployment. It is
not a compliance certification, a safety case for a specific workflow, an SLA,
or evidence that OpenAdapt is appropriate for unsupervised clinical use.

For the current evidence behind each maturity claim, see
[`VERIFICATION.md`](VERIFICATION.md). For the package-wide maturity map, see
[`PRODUCT_STATUS.md`](PRODUCT_STATUS.md).

## Product boundary

| Capability | Maturity | What the claim means | What it does not mean |
| --- | --- | --- | --- |
| Browser record, compile, and replay | **Beta** | The reference browser path runs end to end in automated and clean-environment tests. | It is not evidence for every site, browser extension, authentication flow, or long-running production workload. |
| Healthy replay with zero model calls | **Beta** | A run that resolves from retained evidence can execute without a language or vision model; the run report counts model calls. | Zero model calls does not mean zero network traffic. The target application, hosted control plane, remote backend, or effect verifier may still use the network. |
| Deterministic re-resolution | **Beta** | Bounded visual or structural drift can be resolved through non-model evidence and recorded as a reviewable change. | It is not general adaptation to a redesigned workflow, changed business rules, or missing evidence. |
| AI-assisted repair | **Experimental** | An explicitly enabled model can propose a target or interpret a changed screen. Existing runtime checks still apply. | A model proposal is not authorization, proof of identity, or proof that a business transaction succeeded. |
| Human teaching and resume | **Experimental** | A halt can produce evidence for an operator correction, guarded promotion, and durable resume. | Field recovery time, broad authoring UX, enterprise identity integration, and non-repudiation are not established. |
| Windows UI Automation | **Experimental** | The backend contract is tested and a gated Windows environment has exercised structural resolution. | Windows is not the default supported substrate and is not broadly validated across third-party desktop applications. |
| Native macOS automation | **Research** | The browser path runs on macOS. | There is no production-candidate native macOS accessibility backend in this repository. |
| RDP and Citrix-style pixel-only automation | **Research** | Adapter and pixel-only analog tests exercise the backend seam and refusal behavior. | Real RDP session diversity, ICA/HDX, DPI changes, lock screens, latency, credentials, and clinical applications are not validated. This is not a validated Citrix integration. |
| Managed browser execution | **Beta** | The hosted lane admits attested browser bundles; production mode requires a configured real runner and refuses silent mock fallback. | It does not extend the supported claim to Windows, RDP, Citrix, PHI-bearing shared-cloud execution, an SLA, or a regulated certification. |
| On-premises / customer-managed deployment | **Experimental** | Deployment configuration, local run gates, egress checks, and audit primitives are supplied. | OpenAdapt does not configure the customer's firewall, KMS, storage, identity provider, backups, retention, incident response, or legal compliance program. |

These labels describe product evidence, not risk acceptance. A Beta engine can
still produce an unsafe workflow if the demonstration, inferred checks, policy,
or deployment configuration is inadequate.

## What "deterministic" means

The compiled program fixes the action sequence, parameters, target evidence,
postconditions, and selected policy. The runtime applies a defined resolution
ladder to the live observations. It does not ask a general-purpose agent to
re-plan the task on every healthy run.

Deterministic does **not** mean:

- the application, OCR, network, or remote desktop will return identical
  observations;
- the same screen pixels prove the same database state;
- every run will take the same time or choose the same fallback rung;
- an action is idempotent or reversible; or
- a successful report proves the user's business intent was satisfied.

A "healthy" run is one that stays within the evidence and checks encoded in
the bundle. It is not a claim that the surrounding application is healthy.

## Repair has four distinct outcomes

"Self-healing" is shorthand and should not be read as unrestricted autonomy.
OpenAdapt distinguishes:

1. **Automatic deterministic re-resolution.** A non-model rung finds a target
   from retained structural, template, OCR, landmark, or geometric evidence.
2. **AI-assisted proposal.** An explicitly enabled model proposes a target or
   state interpretation. Identity, risk, postcondition, effect, and policy
   checks are not intentionally bypassed, but those checks have the limits
   described below.
3. **Human teaching.** An operator demonstrates or approves a correction after
   a halt. Promotion can be refused when the correction is underdetermined or
   weakens a guarded property.
4. **Unsupported drift.** Evidence is missing, ambiguous, or contradicted. The
   expected outcome is a halt and report, not a guessed continuation.

Target repair does not infer changed business intent. Adding a required step,
removing a business state, changing a transaction's meaning, or introducing a
new authorization rule requires a new or reviewed program.

## The main incorrect-success risks

OpenAdapt is designed to halt on many ambiguous conditions. The following
classes can still produce a green run unless the workflow and deployment add
the named control.

### 1. Identity protection applies only where it is armed

The compiler records whether each applicable action has usable identity
evidence. Reports expose identity-armed coverage and list unarmed steps with a
reason. An **unarmed** click has no pre-action identity check. Reporting the gap
does not close it.

For an armed step:

- structured browser text can distinguish characters that OCR may collapse;
- a definitive mismatch halts before the action;
- OCR can be unable to distinguish identifiers such as `O` from `0` or `l`
  from `1` on a pixel-only display;
- the pixel comparison tier can reject some differences but is not enabled to
  authorize a match; and
- when all usable identity evidence is unreadable or ambiguous, irreversible
  actions halt, while a reversible action can proceed with the condition
  recorded in its report.

That last distinction makes correct risk classification important. Risk is
inferred from action text and metadata and can be overridden, but inference is
heuristic. An icon-only save, a generic "OK", or an Enter key that submits a
form can be under-classified. A benign label containing a write-shaped word can
be over-classified and halt unnecessarily.

For consequential or entity-sensitive workflows, require a policy that refuses
unarmed actions, review the inferred risk and tags, and test identity against
real near-match and same-name records. Pure-pixel substrates should expect more
safe halts because OCR ambiguity cannot always be resolved. The adversarial
identity evidence and measured over-halt tradeoff are documented in
[`IDENTITY_ROC.md`](validation/IDENTITY_ROC.md) and the generated claim registry
in [`VERIFICATION.md`](VERIFICATION.md).

### 2. A postcondition verifies only what it observes

The compiler mines visual and, where available, structural changes from one
demonstration. A step can have no meaningful observable change, or the mined
assertion can describe incidental screen state rather than the intended
outcome. In those cases replay can accept a no-op or halt on harmless change.

Common examples include:

- an action whose before and after screens are effectively identical;
- a native control whose popup exists outside the captured browser surface;
- a stable-looking tenant-specific row, menu, counter, or banner frozen into
  the demonstration;
- a typed value that the application reformats or masks; and
- a target that moved after an earlier action but before a later check.

Typed-input read-back and structural postconditions reduce this risk but remain
same-surface observations. A visible value does not prove it was durably stored.
Masked input can prove that the field changed, not what secret value it now
contains.

`lint` reports vacuous postconditions and other coverage gaps. `certify`
enforces the selected policy. Neither command understands the application's
business semantics, and the permissive `replay` command does not require
certification. Runnable is not the same as certified for a particular policy.

### 3. The screen is not the system of record

A success banner can coexist with a rejected, duplicated, partial, stale, or
later-rolled-back write. Visual verification and a screen-reading model cannot
detect a backend fault when the interface itself displays success.

OpenAdapt can verify a declared effect against a configured REST, FHIR, or
document-store verifier. A declared effect without a verifier halts in the
replayer. The deployment `run` gate requires consequential writes to declare
effects and requires either a configured verifier or explicit operator
approval.

The remaining boundary is material:

- the compiler does not generally infer system-of-record effects from a
  demonstration;
- the operator must author and bind the effect to the correct record and run
  parameters;
- the verifier's permissions, query, freshness window, and independence must
  be validated in the real deployment;
- explicit approval permits execution without independent confirmation; and
- a screen read-back is useful but is not independent effect verification.

A bounded fault-model study demonstrates why this matters: the screen-only
oracle silently accepted several transactional fault classes that the configured
effect path refused. See
[`FAULT_MODEL.md`](../benchmark/fault_model/FAULT_MODEL.md) and
[`EFFECT_VERIFIER.md`](design/EFFECT_VERIFIER.md). This is evidence for the
mechanism, not proof that a customer-specific effect binding is correct.

### 4. Model assistance can convert a halt into a false pass

Model use is off by default. When configured, a grounding model can propose a
target and a state verifier can re-evaluate some failed visual postconditions.
Only an affirmative answer is eligible to rescue the supported state checks;
uncertainty or service failure keeps the halt.

The model still reads the screen, can be wrong, and cannot prove a hidden
transactional effect. A model-assisted run is counted in the report and no
longer qualifies as a zero-model run. Dense lists, small identifiers, unusual
rendering, and in-progress states are known hard cases. Treat the model as an
availability aid, not a safety authority, and keep consequential actions behind
identity, effect, and policy controls that do not depend on the model's answer.

### 5. A halt is not a rollback

A run may halt after earlier steps have already changed the application or
system of record. OpenAdapt does not provide a general transaction spanning an
arbitrary GUI workflow. Compensation exists only where a deployment has
explicitly implemented and verified it.

Before resuming or teaching a halted consequential workflow, reconcile prior
effects, confirm the current entity and application state, and ensure the bundle
and approval still match the paused run. A report that accurately names the
halt does not prove earlier actions were harmless.

## Interaction and environment limits

The reference path is strongest when the target remains inside one captured
browser surface and the demonstration exposes observable outcomes.

| Condition | Current boundary |
| --- | --- |
| Zoom, DPI, font, layout, or viewport changes | Structural evidence can survive some reflow and visual rungs can survive some movement, but support is workflow-specific. Large rescale or reflow can halt. |
| Native select menus, file choosers, permission prompts, and secure desktops | OS or browser chrome may not appear in the captured surface and may be unrecordable or undrivable. Prefer an application-level or keyboard/API path. |
| New tabs and windows | Opening a tab can be observed structurally where the backend supports it; interaction inside additional windows and multi-window coordination are not a general supported path. |
| Drag and drop or gesture-heavy controls | Not a general supported primitive. Use a structured/API alternative or validate a purpose-built workflow. |
| Dates, locale-sensitive fields, and auto-formatting | The same keystrokes can produce different values across platforms or locales. Require an explicit read-back or system effect and test the deployment locale. |
| Slow or asynchronous applications | A state that outlasts configured waits can halt. Increasing a timeout does not establish correctness. |
| Transparent overlays or intercepted input | The action can be swallowed and detected only by a later check. If that check is vacuous, the no-op can be silent. |
| Parameterized entity selection | Replacing typed text does not automatically make recorded target evidence generalize to every entity. The resolved row still needs identity evidence for the runtime value. |
| Tenant, version, and dataset transfer | A demonstration can freeze tenant-specific state. Re-recording, recompilation, or reviewed program changes may be required. |

## Privacy, PHI, and sanitized artifacts

Compilation does **not** make a recording or bundle PHI-free. Recordings can
contain full screenshots, typed values, DOM or accessibility text, and
parameters. Bundles can retain target crops, labels, postconditions, identity
evidence, and example values. Machine-readable run reports and live frames can
contain data observed during execution.

OpenAdapt's outbound artifact path works on a copy:

1. transform supported text and still images under a named scrubber policy;
2. preserve and validate the recording or bundle's operational structure;
3. account for every file and refuse unsupported or unknown content;
4. rescan the derivative and expose remaining findings;
5. review it locally, including rendered images and text, before approval; and
6. freeze the exact approved archive and bind approval to its hash.

Changing the derivative invalidates approval. A changed executable bundle must
also pass separate runtime-semantics validation; privacy approval is not
executability proof. The safer authoring path is to review and approve a
sanitized recording derivative, then compile and validate the resulting bundle.

Scrubbing means **verified coverage under the configured detector, policy, and
review**, not a mathematical guarantee that no PHI remains. OCR and named-entity
recognition can miss unusual text, handwriting, images, or domain-specific
identifiers. Human review materially improves confidence but is still fallible;
regulated deployments must define reviewer identity, separation of duties,
acceptable residual risk, and incident handling.

A clean design-time derivative does not sanitize live execution. A runtime can
reintroduce PHI through the target application, parameters, screenshots,
postconditions, model requests, effect queries, logs, and reports. Keep those
observations inside the declared execution boundary and apply storage,
retention, access, and egress controls there.

See [`SANITIZED_ARTIFACTS.md`](SANITIZED_ARTIFACTS.md) for the review and
exact-hash workflow, [`PRIVACY.md`](PRIVACY.md) for the field-level data map,
and [`ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md) for component
boundaries.

## Deployment responsibilities

### Managed browser lane

Only upload the exact artifact admitted by the destination's policy. An
approved sanitized derivative does not authorize PHI-bearing runtime data in a
shared managed runner. Do not use the managed lane for PHI unless the written
service scope, deployment architecture, retention controls, and applicable
agreements explicitly cover the complete runtime data flow.

### Customer-managed and on-premises lanes

Running in a customer boundary changes where data and execution live; it does
not improve target resolution or verification by itself. The operator remains
responsible for network allowlists, firewall enforcement, secrets and KMS,
host hardening, storage encryption, backups, retention, access control,
monitoring, effect-verifier integration, update validation, and disaster
recovery.

The repository supplies integrity manifests, optional bundle encryption,
destination policy, local egress checks, and audit primitives. A content digest
proves byte identity, not publisher identity. A local hash chain is tamper
evident against ordinary changes, not tamper proof against an administrator who
can rewrite it. Local approvals record provenance but are not enterprise-IdP
signatures or non-repudiation.

OpenAdapt does not claim that this repository alone satisfies HIPAA, PHIPA,
PIPEDA, or another legal regime. It does not itself provide a BAA or independent
security certification. See [`ON_PREM.md`](ON_PREM.md) and
[`COMPLIANCE.md`](../deploy/on-prem/COMPLIANCE.md).

## Reading the benchmark evidence

The reproducible MockMed comparison shows that, on one simple browser task,
compiled replay completed the tested runs with lower latency and no model calls
on healthy runs than the tested computer-use agent. Both arms passed the same
external success check in that sample. The result supports the cost-and-latency
mechanism; it does not establish a production reliability advantage, long-term
maintenance cost, or safety on a real clinical system. Full task, sample,
oracle, model, pricing assumptions, and artifacts are in
[`benchmark/BENCHMARK.md`](../benchmark/BENCHMARK.md).

The OpenEMR public-demo comparison is a bounded field check against fake data
on a shared instance that resets. It is not exactly reproducible and is not
evidence of clinical production safety, real-patient operation, Citrix support,
or superior reliability. See
[`benchmark/openemr/BENCHMARK.md`](../benchmark/openemr/BENCHMARK.md).

When evaluating either result, include silent incorrect success and over-halt,
not only task success. Also account for demonstration and review time,
maintenance interventions, recovery time, effect-verifier integration, and the
cost of safe halts.

## Minimum deployment review

Before unattended execution of a consequential workflow, require evidence that:

- the exact bundle passes strict lint and the intended certification policy;
- every entity-sensitive and consequential action is identity-armed and tested
  against realistic near matches;
- risk classifications and tags have been reviewed by someone who understands
  the target application;
- every write declares the intended effect and a correctly scoped independent
  verifier is configured, or the lack of verification is explicitly accepted;
- every step has a meaningful postcondition and known same-surface limitations
  are accepted;
- the fail-closed `run --dry-run` gate admits the exact sealed bundle and
  compiler version intended for deployment;
- secrets, storage, screenshots, reports, checkpoints, logs, and retention are
  controlled inside the execution boundary;
- model egress is disabled or explicitly allowlisted and tested; and
- halt, reconciliation, approval, resume, rollback, and incident procedures
  have been exercised in the real environment.

Passing this review narrows known risks. It does not turn an application-level
automation into an independently certified transaction system.
