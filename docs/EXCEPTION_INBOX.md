# Local Needs Attention Queue

The staff-attended queue is the local review surface for workflows that halt
instead of guessing:

```bash
pip install 'openadapt-flow[console]'
openadapt-flow console --runs ./runs --bundles ./bundles --attend
```

`--attend` opens the Needs Attention view first and is always read-only.
`--allow-actions` is ignored for this mode. The attended browser surface cannot
approve, resume, retry, teach, promote, roll back, certify, or run a workflow.

## Security boundary

- The server binds only to `127.0.0.1`.
- Every API and protected screenshot requires the launch URL's bearer
  capability.
- Browser mutations also require a valid Host, same-origin request,
  `application/json`, and the session CSRF token.
- Queue records use opaque IDs and category-derived copy. Workflow names,
  parameters, raw halt reasons, observed text, local paths, and raw reports do
  not enter the DTO.
- Artifact lookup accepts only report-referenced PNG IDs and never follows a
  symlink.
- The notification endpoint returns only a count and `#/attention`; it is the
  PHI-free seam for a future desktop/tray OS notification. Flow does not pop a
  system notification or inject input.

## Human-required interruptions

CAPTCHA, MFA, one-time codes, and expired authentication can appear as
`human_required` halts. OpenAdapt never answers, solves, clicks, retries, or
sends those challenges to a model. The person at the workstation may complete
the challenge in the live application, but the queue only displays redacted
local evidence. It does not record approval or continue the run.

## Deliberately deferred

The first secure slice does not approve or resume runs, collect free-text staff
notes, automate teaching, upload local evidence, send cloud notifications, or
implement CAPTCHA/MFA solvers. Approval and resume require an immutable
pause/action-delivery binding and single-flight execution semantics before they
can be added to an attended browser surface.
