# Product maturity

This is the authoritative map of what a code checkout can do versus what an
operator should rely on. Code presence is not a production-readiness claim.
The machine-readable claim-to-evidence registry is [`claims.yaml`](../claims.yaml),
and its generated view is [`VERIFICATION.md`](VERIFICATION.md).

## Status vocabulary

| Status | Meaning here |
| --- | --- |
| **Beta** | Runs end to end on the default CI or clean-machine lifecycle and is available for evaluation. No production SLA or general enterprise certification is implied. |
| **Experimental** | Real implementation exists, but evidence is opt-in, infrastructure-gated, mocked at an external boundary, or limited to design-partner validation. |
| **Research** | Protocol spike, design, or synthetic proof. Not a supported deployment surface. |
| **Deprecated** | Superseded path retained only for migration. Do not start new work on it. |
| **Archived** | Historical code with no active product role. |
| **Internal** | Maintainer tooling, not an OpenAdapt product surface. |

## Integrated matrix

| Surface | Status | What is proven | Boundary that remains |
| --- | --- | --- | --- |
| Demonstration compiler and bundle | **Beta** | Browser recording compiles into a parameterized, inspectable bundle in CI. | One demonstration can under-specify intent; production policies and effect bindings still require operator work. |
| Browser / Playwright recording and replay | **Beta** | Record, compile, replay, deterministic drift repair, reports, and refusal all run end to end against MockMed; a bounded OpenEMR result is published separately. | The reference path is not evidence for arbitrary sites, long-term drift, or production reliability. |
| Healthy zero-model replay | **Beta** | Repeated CI runs use the deterministic ladder with zero model calls. | Optional model grounding is a separate opt-in fallback; a changed app can still halt. |
| Deterministic re-resolution | **Beta** | Theme, moved-control, and renamed-control fixtures resolve through non-model rungs and emit reviewable patches. | It covers bounded evidence-preserving drift, not arbitrary workflow or business-logic change. |
| AI-assisted repair | **Experimental** | Local/remote VLM contracts, egress gates, refusal behavior, and retention boundaries are tested. | Off by default; model accuracy is not a safety guarantee and real deployment quality is unmeasured. |
| Human teaching (`teach`) | **Experimental** | Halt-to-correction-to-guarded-promotion and regression refusal run in default CI. | Evidence is controlled/synthetic; broad authoring UX and field recovery time are not established. |
| Windows UIA replay | **Experimental** | Backend protocol is mock-tested in CI and an opt-in real Windows-on-ARM path has exercised UIA resolution. | Not default CI, not broadly validated on third-party Windows apps, and not a supported enterprise surface. |
| Desktop recording (`windows` / `rdp`) | **Experimental** | `openadapt-capture` conversion, compile, and replay-shaped orchestration are tested. | Offline capture does not yet carry structural UIA locators; desktop secret-field redaction is deferred. |
| Native macOS desktop actuation | **Research** | The browser backend runs on macOS CI. | There is no production-candidate native macOS accessibility backend in this repository. |
| RDP | **Research** | The adapter and record/compile/replay contract are mock/offline tested. | Live session diversity, DPI mapping, latency, lock screens, and synthetic-input acceptance remain unvalidated. |
| Citrix / pixel-only remote display | **Research** | A pixel-only analog spike proves the backend seam and safe-halt shape. | This is not a validated Citrix integration; ICA/HDX and real clinical environments remain untested. |
| Identity verification | **Experimental, armed steps only** | Wrong-entity refusal and adversarial corpora run in CI. | Unarmed clicks have no identity check. Real compiled bundles currently arm only a subset of clicks. |
| System-of-record effect verification | **Experimental** | REST, FHIR, and document-hash verifier contracts catch fault classes that screen-only verification misses. | Effects are not generally inferred; both authored effects and a configured verifier are required. |
| Lint and certification policies | **Beta** | The CLI reports coverage gaps and refuses bundles that violate a selected policy. | Certification is opt-in; `replay` remains the permissive tutorial path. Use fail-closed `run` for a deployment. |
| Durable pause, approval, and resume | **Experimental** | Checkpoint, bundle-version binding, approval, stale-pause, and resume semantics are tested. | Operator identity is recorded, not integrated with an enterprise IdP; field operation is unmeasured. |
| On-prem / air-gapped scaffold | **Experimental** | Local queue, fail-closed run gate, egress attestation, audit-chain verification, and deployment files ship. | Site firewall, storage, keys, OS hardening, and effect integrations are operator responsibilities; offline update apply is still a documented stub. |
| Desktop GUI and tray | **Experimental, separate repositories** | Component-level engines and rewiring branches exist. | No integrated, supported installer proves record through governed production operation today. |
| Hosted dashboard / control plane | **Beta launch path, separate repository** | Account and organization onboarding, configured Stripe checkout, exact-hash artifact ingest, attested browser workflow versions, structural reports, replacement activation, scheduling, entitlements, and metering are implemented and contract-tested. | Production Supabase migration, Stripe webhook/checkout, runner, scheduler, and signed-ingest probes still require deployment credentials and launch evidence. No SLA or regulated certification is implied. |
| Hosted execution | **Beta browser launch path** | Production mode requires configured live dependencies, admits only exact attested browser bundles, dispatches a real runner, authenticates callbacks, and refuses mock fallback; development mock mode remains visibly synthetic. | The complete paid-account-to-runner lifecycle has not been executed against the production providers in this checkout. Windows, RDP, Citrix, and PHI-bearing shared-cloud execution are outside the browser launch claim. |
| Offline update and rollback | **Research** | The intended signed, operator-pulled blue/green procedure is documented. | `deploy/on-prem/install.sh --update` does not yet perform the apply/swap; it is explicitly a stub. |

## Repair modes

"Self-healing" is shorthand for four materially different outcomes:

1. **Automatic deterministic re-resolution:** a lower non-model rung finds the
   same target from retained evidence and emits a patch. This is the path the
   bundled theme-drift demo exercises.
2. **AI-assisted repair:** an explicitly enabled grounding model proposes a
   target or state interpretation. Identity, risk, postcondition, and policy
   checks still apply; a model answer is not authorization.
3. **Human teaching:** an operator demonstrates a correction after a halt. The
   correction is induced as a guarded branch and promoted only if its regression
   gate passes.
4. **Unsupported drift:** evidence is insufficient, identity is ambiguous, a
   postcondition fails, or policy refuses the action. The correct outcome is a
   halt and report, not a repair.

## Evidence policy

- CI-backed capability claims are registered in [`claims.yaml`](../claims.yaml).
- Opt-in and field evidence cannot be promoted to a stronger tier merely because
  code exists.
- Benchmarks describe their task, environment, run count, success oracle,
  latency, model calls, cost assumptions, and caveats. They are bounded evidence,
  not general market proof.
- The nightly
  [`quickstart-lifecycle.yml`](../.github/workflows/quickstart-lifecycle.yml)
  installs the built wheel in a clean environment on Linux, macOS, and Windows;
  records, compiles, lints, certifies, replays, induces drift, inspects repair
  and report artifacts, uninstalls, and verifies the import is gone.
