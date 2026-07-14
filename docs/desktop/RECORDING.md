# Desktop recording — `record --backend windows|rdp`

`openadapt-flow record` records a workflow **once** so it can be compiled into a
deterministic, vision-anchored script and replayed. This page documents the
**desktop** side of that verb — capturing a workflow the operator performs on a
native Windows desktop (`--backend windows`) or a remote display / Citrix
(`--backend rdp`) — so `record → compile → replay` closes through the product
CLI on the desktop substrate, not just the browser.

```
openadapt-flow record --backend windows --out rec/ --task "triage note"
openadapt-flow compile rec/ --out bundle/ --name triage
openadapt-flow replay bundle/ --backend windows --agent-url http://localhost:5001
```

## What it does (and what it reuses)

Desktop capture is **not reinvented**. The `record --backend windows|rdp` path
is a thin orchestration over two components that already exist and are tested:

1. **[openadapt-capture](https://pypi.org/project/openadapt-capture/)** — the
   cross-platform GUI capture component. It records the operator's
   mouse/keyboard input stream time-aligned with an action-gated screen video
   into a *capture session* directory. This is the extensively-tested capture
   stack; the recorder wires it in via `openadapt_capture.Recorder` (a context
   manager: records on enter, stops + flushes on exit).

2. **The capture adapter**
   (`openadapt_flow.adapters.capture.convert_capture`) — converts that capture
   session into the **exact** recording format the compiler consumes
   (`meta.json` + `events.jsonl` + `frames/{i:04d}_before.png` / `_after.png`),
   running openadapt-capture's own event-processing pipeline (raw streams →
   merged clicks / typed text). This adapter is unit-tested end to end against a
   real capture session in `tests/test_capture_adapter.py`.

The genuinely new piece (`openadapt_flow/desktop_record.py`,
`record_desktop_capture`) is the **live orchestration**: start a capture
session, let the operator perform the workflow, stop on `Ctrl-C`, then convert
to a compile-ready recording. It is dependency-injectable (the recorder and the
converter are parameters), so the orchestration is unit-tested without a live
display; the capture and conversion are tested in their own suites.

openadapt-capture is the optional `capture` extra:

```
pip install 'openadapt-flow[capture]'
```

## REAL vs deferred

| Capability | Status |
|---|---|
| Capture the operator's real desktop demonstration (mouse/keyboard + screen) | **REAL** (openadapt-capture) |
| Convert to the compile-ready recording format | **REAL** (`convert_capture`, CI-tested) |
| The recording **compiles** into a bundle | **REAL** (CI-tested) |
| The bundle **replays** through the desktop backends | **REAL** (`replay --backend windows|rdp`; visual-ladder replay proven in CI, live desktop opt-in via Parallels) |
| Parameters (typed value → replay-time override, `--param NAME=VALUE`) | **REAL** |
| **Structural (UIA `AutomationId`) locators** on click steps | **DEFERRED** — see below |
| **Secret-field redaction** at capture time | **DEFERRED** — see below |
| **`rdp` cross-machine coordinate remap** (host-screen → remote framebuffer) | **DEFERRED** — see below |

### Deferred: structural (UIA) locators — visual ladder only

Offline capture records mouse/keyboard/video **only** — there is no live
accessibility tree at conversion time to read an element identity from. So every
`anchor.structural` is `None` and replay resolves on the **visual ladder**
(template → OCR → geometry). The bundle is fully valid and replays; it simply
lacks the deterministic *structural* top rung that a DOM-armed web bundle
(`dom_arm`) or a live UIA-armed desktop recording carries.

The deterministic structural rung is armed only by the **live-over-`WindowsBackend`**
path (`openadapt_flow.adapters.desktop_recorder.record_desktop_demo`), which
queries `WindowsBackend.structural_locator_at` at each click — but that path
needs a **scripted driver**, not a human-in-the-wild demonstration. Re-arming a
capture-converted recording against a live UIA tree (a `uia_arm` pass, the
native-desktop analog of `benchmark/dom_arm.py`) is the tracked follow-up.

### Deferred: secret-field redaction

The browser recorder blacks out a secret field's pixels using the field's DOM
rectangle. A pixel/desktop capture has **no field geometry**, so it cannot
redact the typed value from the captured frames. Rather than persist an
unredacted secret frame (a silent PHI leak), `record --backend windows|rdp`
**refuses** `--secret`. Use a masked/password field (its on-screen glyphs are
already dots) until the geometry-free redaction design lands.

### Deferred: `rdp` coordinate space

openadapt-capture records the machine it runs on. For `rdp` / Citrix where the
remote desktop is painted into a **client window** on the operator's host, a
host-screen capture is in host-screen pixel space, while the `rdp` backend
replays in the remote **framebuffer** space — these differ. Until a
window→framebuffer coordinate remap lands, record `rdp` workflows in the **same
pixel space the backend replays in**: record inside the remote session itself
(openadapt-capture runs on Windows), or full-screen the client at the target
resolution. `--backend rdp` records identically to `--backend windows`; the flag
selects intent and the replay wiring.

## Parameters

Desktop has no field identity (no DOM `name`/`id`), so a parameter is keyed by
its **demonstrated value**: `--param NAME=VALUE`. A typed value equal to `VALUE`
is marked as parameter `NAME`; its demonstrated value becomes the default,
overridable at replay with `--param NAME=<new value>`. (This mirrors
`convert_capture`'s `params` contract and the replay `--param` contract.)

## Where recording happens

`record --backend windows` captures the desktop **the recorder process runs
on**. In the product deployment the operator runs `openadapt-flow record` on the
target Windows desktop (or inside the remote session) and performs the workflow;
replay then drives the same substrate via the in-guest agent
(`replay --backend windows --agent-url …`). Record on the box, replay through the
agent.
