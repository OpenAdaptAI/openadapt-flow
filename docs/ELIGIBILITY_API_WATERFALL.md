# API-first eligibility — the 270/271 waterfall

**Status: contract-proven.** The Stedi client
(`openadapt_flow/eligibility/client.py`) is built against Stedi's
documented request/response contract (endpoint, `Authorization: Key`
header, request body, `benefitsInformation`/AAA response shapes — all
fetched from Stedi's public docs on 2026-07-18, including their published
**dental mock catalog**: Ameritas, Anthem, Cigna, MetLife, UnitedHealthcare
Dental, service type code `35`). CI proves the client, the route resolver,
and the artifact/verification roundtrip against **faithful local fakes of
those documented shapes** (`tests/test_eligibility_api.py`), not against
Stedi's live service. The env-gated smoke test
(`tests/test_eligibility_live_stedi.py`) runs Stedi's own dental mock
end-to-end the moment a TEST-mode `STEDI_API_KEY` is present — mock checks
are free and touch no real member data. Until someone runs it with a real
key, do **not** describe this path as live-proven.

## Why API-first

Where a payer exposes a sanctioned real-time 270/271 route, an eligibility
check should hit that route instead of driving the payer portal's GUI:

- **Deletes drift classes upstream.** No page to drift, no CAPTCHA, no
  session/MFA cascade for API-covered payers — the halts never happen
  instead of being resolved downstream.
- **Cheap and fast.** Stedi is self-serve, no minimums, roughly
  **$0.30/check tapering to $0.08 at volume** (verified against Stedi's
  public pricing mid-2026); test-mode mock checks are free.
- **Not a pivot.** The no-API / no-DOM tail (Citrix-world portals, payers
  with API-missing fields) is exactly where compiled replay + effect
  verification remain the honest automation. The API tier is the cheap
  floor under the vertical offer; portal replay is the fallback, not the
  competitor.

Known limitation (ADA's 2021 eligibility whitepaper): top dental payers
return on average **under half** of the NDEDIC-recommended data elements
over 271. Active/inactive, deductibles/maximums, and category coinsurance
are reliable; frequency limits come through for many payers; waiting
periods, shared-frequency rules, and full history are the weak tail. The
waterfall exists precisely because of this: the API answers the reliable
head, the portal replay fills payer-specific gaps, and the per-payer
field-coverage map grows out of real 271s during the pilot.

## The waterfall

```
resolve_route(payer)              # committed per-payer capability map
  ├─ api      → StediEligibilityClient.check(270)
  │              ├─ active/inactive  → write_and_verify(...)  [done]
  │              └─ no answer        → portal tier (a 270 is a READ:
  │                                    falling through is safe)
  │                                    …unless portal_banned → queue
  ├─ portal   → compiled portal replay (the wedge, unchanged)
  └─ excluded → practice queue (no automated tier may run)
```

The registry is a reviewed YAML file,
`openadapt_flow/eligibility/payer_routes.yaml` — route decisions are data,
not code. Current contents: the six confirmed-covered dental payers
(Delta Dental plans, MetLife, Cigna Dental, Guardian, United Concordia,
DentaQuest) route **api-first**; **Availity** is `excluded` with
`portal_banned: true` (its terms ban scraping outright) and a note that
Availity **sells a sanctioned API route** via trading-partner agreement —
completing that enrollment flips the entry to `api`, which is how a
categorical exclusion converts to in-scope. Unknown payers default to
`portal` (the honest default: no confirmed API route).

Payer IDs: only doc-verified IDs are committed (`cigna_dental: "62308"`,
from Stedi's own mock catalog). Every other `stedi_payer_id` is null with
`verified: false` — resolve the exact `tradingPartnerServiceId` in Stedi's
payer directory during practice enrollment (several plans, notably Delta
Dental, are per-state entities with per-state IDs).

### Read vs write: why fallback is allowed here

The write-path `ApiActuator` (`openadapt_flow/runtime/actuators/api.py`)
must HALT on any sent-but-unacknowledged request — retrying a write through
the GUI risks a double write. A 270 inquiry is an **idempotent read**:
nothing is written, so an unavailable payer, an unfindable member, or an
unparseable response may safely fall through to the portal tier. What is
*never* allowed is guessing: a malformed 271 is `indeterminate` and is
never recorded as a benefits answer (`parse_271` fails closed).

## Effect verification — source-agnostic, on purpose

The API result lands in the **same practice-local results artifact set** as
a portal replay's output: one appended row in `eligibility_results.csv`
plus the raw 271 response written **byte-exact** as `271_<digest16>.json`
(`openadapt_flow/eligibility/artifact.py`). The kit's `document-hash`
substrate (`docs/EFFECT_KIT.md`) then certifies the write with the standard
pair of contracts — exactly one raw-271 document (`record_written`) whose
bytes hash to the digest computed on the wire (`field_equals` on
`sha256`). The CSV row carries that digest, so every benefits answer is
traceable to the exact 271 that produced it. A truncated write, duplicate
document, or missing store is REFUTED/INDETERMINATE → the check halts into
the practice queue instead of a wrong row silently becoming the answer.

This is the competitive story: the halt-instead-of-guess wedge governs the
artifact **regardless of whether a portal, an API, or a human produced
it**.

## Deployment model: practice-held account, practice-held BAA

The engine calls Stedi **from the practice's machine** under the
**practice's own Stedi account and click-through BAA** (Stedi's self-serve
terms explicitly contemplate business-associate customers). We set it up at
onboarding; the API key stays on the practice's box, referenced only by
environment variable (`STEDI_API_KEY` — the client refuses to construct
without it, and the secret never enters configs, results, or logs, per the
kit's secret-isolation convention). 271s land in the practice's local
results folder. We stay out of the PHI chain for the data path.

Honest caveat: HHS guidance suggests any realistic product with cloud
dashboards/support tends toward business-associate status eventually — plan
to sign BAAs gracefully rather than architect around never needing one.

### What a practice needs to activate the API route

1. **Create a Stedi account** (self-serve) and accept the click-through
   BAA under the practice's name.
2. **Generate API keys**: a TEST-mode key first (free mock catalog — the
   smoke test runs against it), then a production key. Export as
   `STEDI_API_KEY` on the machine that runs checks.
3. **Resolve payer IDs** for the practice's payer mix in Stedi's payer
   directory and fill them into `payer_routes.yaml`
   (`stedi_payer_id`, flip `verified: true` with the date).
4. **Transaction enrollment**: most dental payers require none for
   eligibility (verified mid-2026); the few that do are driven through
   Stedi's enrollment API/dashboard under the practice's NPI.
5. **Provider identity**: the practice's NPI and organization name go in
   each request; nothing else is needed for 270/271.

Per-check cost at current Stedi pricing: **$0.30 tapering to $0.08**, no
minimums, billed to the practice's own account. Test-mode mock checks: $0.

## Backup clearinghouse (documented, not built)

- **pVerify** — dental-specialized fallback. REST API (`api.pverify.com`,
  OAuth2 token + `EligibilitySummary` endpoint), notable for reaching
  **non-EDI dental payers** an EDI clearinghouse can't; ~$0.25/check but
  with monthly minimums and setup fees, and API access is contract-gated
  (no self-serve sandbox). If a pilot payer mix leans on non-EDI payers,
  pVerify is the second integration; the `EligibilityResult` normalization
  here is deliberately vendor-neutral so a second client slots in beside
  the Stedi one.
- **Availity Essentials API** — the sanctioned route for Availity-locked
  payers, via trading-partner agreement (see the registry entry).

## Module map

- `openadapt_flow/eligibility/client.py` — Stedi 270/271 client,
  fail-closed `parse_271`, normalized `EligibilityResult` (raw 271 + wire
  digest retained).
- `openadapt_flow/eligibility/waterfall.py` — capability map loading,
  `resolve_route`, `run_waterfall` (the fulfillment seam).
- `openadapt_flow/eligibility/payer_routes.yaml` — the committed registry.
- `openadapt_flow/eligibility/artifact.py` — results CSV + raw-271
  document + document-hash verification.
- Tests: `tests/test_eligibility_api.py`,
  `tests/test_eligibility_waterfall.py`,
  `tests/test_eligibility_artifact.py`, and the env-gated
  `tests/test_eligibility_live_stedi.py`.

None of this is on the replay hot path: the package is import-light and
nothing in the recorder/compiler/replayer imports it.
