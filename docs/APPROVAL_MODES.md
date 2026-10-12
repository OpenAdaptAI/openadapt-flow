# Approval modes

An approval mode decides whether a person approves an item before the
workflow saves it. Flow has three modes:

| Mode | What happens before each consequential save |
| --- | --- |
| `each_item` | The item stops before the save and waits for a person. This is the mode for a missing or unknown value. |
| `problems_only` | "Only ask a person about problems." The save proceeds without a person only when every check below passes. Any other result goes to a person. |
| `not_gated` | No per-item pause, as before approval modes existed. The run report records `not_gated` so the absence of a pause is visible. |

This page describes the engine part in `openadapt_flow/approval_mode/`. Cloud
switch records, the Desktop list queue, spot checks and the approve-and-save
path aren't part of Flow yet, so a customer can't turn `problems_only` on
today. The requirements report (below) says so for every workflow.

## How a caller uses it

Pass an `ApprovalGate` to the `Replayer`. One gate instance serves one item
(one run):

```python
from openadapt_flow.approval_mode.gate import ApprovalGate

gate = ApprovalGate(
    "problems_only",
    record=switch_record,          # runner copy of the clinic's switch record
    log=switch_log_path,           # the runner's local switch log
    pinned_status_keys=cloud_keys, # pinned at runner registration
    fetch_status=fetch_from_cloud, # returns Cloud's signed status
    environment_digest=observer,   # reads the live application version
    runner_id=runner_id,
    signed_in_user=signed_in_check,
    dedicated_user_key=hmac_key,
    item=item_context,             # Desktop's list parsing and history
)
report = Replayer(backend, approval_gate=gate, ...).run(workflow, ...)
report.approval  # the item's decision and checks, as closed codes
```

The item context is single-use. To reuse a gate for the next item, call
`gate.next_item(context)` first; otherwise the next item gets a default
context that fails every item check. Without a gate, replay and the run
report are unchanged.

## Before each save

The gate runs at the last point before the save's input is delivered, after
the target, the right-record check and the record check's pre-state
resolved. A save is a step the run gate's classifier calls consequential.
The runtime also asks before an identity-checked navigation click, such as
opening the patient's chart; that click isn't a save, so the gate lets it
through, and the engine's own identity check still applies. In
`problems_only` the gate checks, in order:

1. **Item checks.** The item came from a Desktop list with a header row,
   its values were parsed strictly, it isn't a retry, repeat or resumed run,
   its record key wasn't entered recently, it uses the deployment's own
   configuration under the Regulated profile with model egress off, and the
   runner holds the switch's lease. A failure sends the item to per-item
   approval and doesn't count as a problem.
2. **P1 `switch_on`.** The local switch log is intact and shows the switch
   on for this record, no earlier item was interrupted, the bundle,
   environment and runner match the record, and a Cloud status signed by a
   pinned key answers this runner's nonce and latest log event within the
   last 15 minutes on the monotonic clock. A stale status pauses the item
   and clears by itself on the next fresh status.
3. **P2 `signed_in_user`.** The application shows the dedicated OpenAdapt
   user.
4. **P3 `right_record`.** Every identity check so far, and the save's own,
   affirmatively verified.
5. **P4 `same_screens`.** No repair, model call, drift rescue, `grounder`
   rung, rung below the step's floor from passing cases, or undeclared
   dialog dismissal so far.
6. **P5 `record_check_ready`.** The verifier is reachable, reads at
   `INDEPENDENT_SYSTEM`, has an identity the switch record names, and its
   effects read back every input the item typed or selected since the
   previous save, and every input an API binding puts in its request. The
   last save also refuses when anything is typed or selected after it. Every
   executable path declares an `exact_new_set` and an idempotency key, as E2
   requires: `field_equals` and `record_written` don't compare with the
   record's state before the save, so they would confirm an update whose
   value was already there, or a record that already existed.

Any failure stops before the save. P2, P3 and P5 failures read "stopped
before saving"; the others wait for a person to approve the item. In
`each_item` every save stops this way. In a gated mode, a run in which any
input would come from the recording is refused before any step.

## After the run

- **R1 `verified`.** The transaction outcome is `VERIFIED` and agrees with
  the execution outcome and the outcome envelope. Every save is one the gate
  passed, and was confirmed on the first read, with no reconciliation, by a
  bound system-of-record verifier at `INDEPENDENT_SYSTEM`.
- **R2 `whole_run_clean`.** The whole run used no repair, model call,
  rescue, `grounder` rung, lower rung or undeclared dialog.

The item counts as saved without a person only when both pass. Otherwise it
goes to a person. The outcome words never change: an item that fails R2
after a confirmed save still reads "Done and checked", and still goes to a
person.

## When the mode turns itself off

The gate writes a `switched_off` event to the runner's hash-chained switch
log, which applies before the next save, offline too. Cloud can't undo it,
and a missing or broken log also reads as off.

| Reason | Trigger in Flow |
| --- | --- |
| `uncertain_result` | The item ended "Check the record", "Finished, not checked", rolled back, had an unknown or inconsistent outcome, or an earlier item was interrupted. Also when the gate let a save through and its input was delivered, but the outcome says nothing was written: the read-back showed only that the expected record is absent. |
| `too_many_problems` | 3 counted problems in a row, or 5 in the last 20 counted items. Items sent to approval by the item checks or by a stale status don't count. |
| `screen_changed` | P4 or R2 failed. |
| `record_check_lost` | P5 failed, or the engine's own record-check preflight refused the save. |
| `version_changed` / `environment_changed` | The bundle, the environment, the runner or the dedicated user changed. |
| Any reason in a Cloud "off" status | Cloud turned the switch off. |

These thresholds are the public defaults. A deployment can only make them
stricter.

## Requirements

`evaluate_requirements` in `openadapt_flow/approval_mode/requirements.py`
returns E1 to E8 in order with a closed reason for each unmet one. It reads
decisions people record, such as the readiness sign-off and the runner
setup attestations, and never makes them. Two requirements can't be met by
any workflow yet: E2 needs a field-delta check for updates, and E4 needs
three drills that don't exist yet.

## What the record check doesn't see

The checks above confirm the values the record check names. They don't see:

- a field on the new record that no input reaches, such as a recorded choice
  that landed on the wrong option;
- a change to another, existing record;
- a second field that the same input was typed into.

The field-delta check and the whole-item snapshot in the design are meant to
close these. Until then, `tests/test_approval_mode_adversarial.py` keeps
strict expected-failure tests for them.

## Not built yet

- The approve-and-save path for an item that waits for approval.
- Binding the mode into `GovernedRunAuthorization` and the deployment file.
- The field-delta effect check, the three new drills and the `customer_stop`
  case kind.
- A Cloud status client, switch records, spot checks, incidents and the
  Desktop list queue.
