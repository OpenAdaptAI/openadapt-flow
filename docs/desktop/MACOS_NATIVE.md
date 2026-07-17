# Native macOS backend

`backend.kind: macos` drives one uniquely selected local application window.
It is a separate backend from the Citrix/Parallels remote-display path because
native Mac shortcuts use Command and native text delivery must be bound to the
exact focused application window.

```bash
python -m pip install "openadapt-flow[macos]"
```

```yaml
backend:
  kind: macos
  macos_app: TextEdit
  macos_window_title: oa-macos-workflow
```

Or select it directly:

```bash
openadapt-flow replay bundle/ \
  --backend macos \
  --macos-app TextEdit \
  --macos-window-title oa-macos-workflow
```

The selector must resolve to exactly one normal window. Coordinate clicks,
global keys, and any physical-input fallback require both the system-wide AX
focused-application PID to match the owner and the exact CoreGraphics window to
be topmost. The exact AX window must also be focused/main. Missing, errored,
ambiguous, occluded, or permission-denied proofs halt before input; the backend
never chooses the first partial match.

Native text uses Accessibility selected-text replacement only after the focused
element is proven to belong to that unique focused/main AX window and its exact
CoreGraphics window id is topmost. This exact-element delivery may proceed when
NSWorkspace's frontmost PID is stale because no global keyboard event is routed;
all physical/global input retains the active-PID gate. This avoids two unsafe
fallbacks: framework-discarded CoreGraphics Unicode payloads and temporarily
placing workflow data on the system clipboard. An application that does not
expose writable selected text halts instead of changing whole-value semantics
or reporting delivery.

## Permissions and qualification

Window capture requires Screen & System Audio Recording and input requires
Accessibility. Request both in one operator step:

```bash
python scripts/qualify_macos_textedit.py --request-permissions
```

Approve both macOS prompts for the application that launches the command,
restart that application, then run the evidence harness:

```bash
python scripts/qualify_macos_textedit.py --trials 3 \
  --output /tmp/openadapt-macos-textedit-evidence.json
```

The harness creates isolated `/tmp` documents in new TextEdit processes,
performs three replace-and-save trials, verifies exact file bytes independently,
requires an ambiguous two-window selector to halt without changing either file,
then restores the previously frontmost application and removes its artifacts.

Until those live trials pass, this implementation is not Beta evidence. It also
makes no AX structural-resolution claim: AX is used for exact window focus and
focused-text delivery, while target resolution remains on the existing visual
ladder pending permissioned cross-application AX validation.
