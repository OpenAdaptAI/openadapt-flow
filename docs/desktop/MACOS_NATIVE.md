# Native macOS backend

`backend.kind: macos` drives one uniquely selected local application window.
It is a separate backend from the Citrix/Parallels remote-display path because
native Mac shortcuts use Command and native text entry can use Unicode events.

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

The selector must resolve to exactly one normal window. The exact window must
also be topmost before input. Missing, ambiguous, occluded, or permission-denied
targets halt before input; the backend never chooses the first partial match.

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
makes no AX structural-resolution claim: target resolution remains on the
existing visual ladder pending permissioned cross-application AX validation.
