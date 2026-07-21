# Governed API-first eligibility

OpenAdapt uses a sanctioned 270/271 API when an exact practice/payer route is
available, then uses compiled portal replay only as a reviewed fallback. Every
answer is qualified by service, date, network, coverage level, and time period;
ambiguous responses enter the attended queue.

## Execution contract

```text
exact practice payer binding
  ├─ API
  │   ├─ exact, unambiguous 271 → atomic local evidence → verify → complete
  │   ├─ transient failure    → bounded retry → reviewed portal or queue
  │   └─ identity/config/data ambiguity → attended queue
  ├─ reviewed portal route    → compiled replay + independent effect check
  ├─ queue                    → practice staff resolves with evidence
  └─ excluded                 → no automated action
```

Unknown payer names, unmatched payer IDs, unreviewed service types, and account
or test/production mode mismatches never select an API or portal implicitly.

The public `payer_routes.yaml` is one synthetic Stedi TEST-mode example.
Production payer maps are practice-scoped deployment data. Each API binding
records:

- the exact request payer ID, including leading zeroes;
- Stedi's stable five-character payer ID;
- a digest of the reviewed payer-directory record;
- supported service-type codes;
- the practice account and application mode;
- the verification date and portal policy.

This keeps the route mechanism open while keeping productionized payer recipes
inside the deployment boundary.

## Parsing without “first match” shortcuts

`parse_271` retains every qualified patient-responsibility entry and populates
practice-facing convenience values only when one value matches the request's:

- service type or procedure;
- in-network, out-of-network, or not-applicable qualifier;
- individual/family coverage level;
- benefit date and requested date of service;
- time qualifier, including separate total (`23`, calendar year) and remaining
  (`29`) deductible/out-of-pocket amounts.

Conflicting active/inactive signals, a response for another service, a payer or
application-mode mismatch, an out-of-range service date, or two different
values with the same qualifiers is not an answer.

## Error and fallback rules

Only explicitly transient outcomes retry automatically:

- HTTP `429`;
- HTTP `5xx` and network timeouts/failures;
- payer connectivity AAA `42`, `80`, or Stedi's documented `42` + `79`
  combination.

Retries are bounded. After they are exhausted, the result may use a reviewed
portal fallback unless that portal is barred. Authentication, invalid payer or
request, provider configuration, member identity, and ambiguous response
outcomes go to practice staff without automatic retry or portal substitution.

A 270 is a read, so retrying does not duplicate a business write. The local
`operation_id` binds attempts and artifact promotion to one logical check.

## Practice-held account and PHI boundary

The practice owns the Stedi account and credential. Production mode requires an
explicit practice-held-account and BAA confirmation. The API key is resolved
from an environment reference and never enters a result, artifact, or reason
string. Requests and response bodies are never logged.

PHI-bearing evidence is written only through `PracticeArtifactPolicy`, which
requires:

- an explicit local PHI boundary;
- either attested encrypted-volume storage or application AES-256-GCM;
- an owner-only artifact directory and files;
- a retention period and `egress: none`;
- symlink-safe, exclusive writes and a bounded single-writer lock.

The raw response and normalized practice record are staged, hashed, fsynced,
and promoted together as one transaction directory. The CSV is a derived index,
not the source of truth. Repeating the same `operation_id` and content is
idempotent; reusing it with different content fails. Spreadsheet formula
prefixes are neutralized. Hash effects independently re-read both stored files.

## Qualification

The deterministic contract suite covers routing, qualifiers, conflicting
benefits, the HTTP/AAA taxonomy, retries, PHI-safe diagnostics, atomicity,
idempotency, encryption, tampering, symlink substitution, and package import
behavior.

The live test runs Stedi's published synthetic Cigna dental request three times:

```bash
export STEDI_API_KEY='<sandbox test key>'
pytest -q tests/test_eligibility_live_stedi.py -rs
```

All three trials must return an exact TEST-mode answer and independently verify
the encrypted raw and normalized artifacts. If the key is absent, the tests
skip and explicitly state that no live evidence was collected.

## Primary references

- [Real-Time Eligibility Check JSON](https://www.stedi.com/docs/healthcare/api-reference/post-healthcare-eligibility)
- [Submit eligibility checks](https://www.stedi.com/docs/healthcare/send-eligibility-checks)
- [Eligibility troubleshooting and retries](https://www.stedi.com/docs/healthcare/eligibility-troubleshooting)
- [Patient responsibility qualifiers](https://www.stedi.com/docs/healthcare/eligibility-patient-responsibility-benefits)
- [Payer retrieval](https://www.stedi.com/docs/healthcare/api-reference/get-payer)
- [Integrated practice accounts](https://www.stedi.com/docs/healthcare/integrated-account-overview)
- [Account, test keys, production, and BAA setup](https://www.stedi.com/docs/healthcare/account-settings)

The client is import-light and is not imported by the recorder, compiler, or
replayer unless the eligibility waterfall is used.
