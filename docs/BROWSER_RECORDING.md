# Browser recording

OpenAdapt has one supported browser recording contract. `openadapt-flow`
uses a Playwright page to retain ordered input events, DOM identity, field
geometry, exact before/after frames, and source-time secret redaction. The
result is the same compile-ready recording for both browser entry modes:

- **Launch mode:** Flow starts a new Chromium browser and opens `--url`.
- **Attach mode:** Flow connects to an existing local Chromium browser and
  binds one open tab. This mode keeps a browser session that already completed
  sign-in, SSO, or 2FA.

Both modes are part of the Browser / Playwright Beta surface. Attach mode uses
Chromium DevTools Protocol only as the local connection transport. The
recorder, schema, compiler, secret handling, and governed replay path do not
change.

## Launch a new recording browser

```bash
openadapt-flow record --backend web \
  --url https://your.app \
  --out recordings/browser-session
```

Flow opens the URL. Perform the workflow. Then press Ctrl-C in the terminal or
close the recording window.

## Attach an existing signed-in browser

Start Chromium with a dedicated debugging profile. Reuse this profile for
later recordings if it must retain its signed-in session. Do not enable remote
debugging on a sensitive general-purpose browser profile.

macOS with Google Chrome:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/OpenAdapt/ChromeRecorderProfile"
```

Linux with Google Chrome or Chromium:

```bash
google-chrome \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="${XDG_DATA_HOME:-$HOME/.local/share}/openadapt/chrome-recorder-profile"
```

Windows PowerShell with Google Chrome:

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-address=127.0.0.1 `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\OpenAdapt\ChromeRecorderProfile"
```

Open and sign in to the application in that browser. Then attach the recorder:

```bash
openadapt-flow record --backend web \
  --url https://your.app \
  --browser-cdp-endpoint http://127.0.0.1:9222 \
  --out recordings/browser-session
```

Keep the selected tab open during recording. Press `Ctrl-C` in the terminal to
finish. Flow retains the final evidence and then confirms the output path. It
refuses a closed tab and does not publish incomplete metadata.

Flow selects the sole open HTTP or HTTPS tab on the `--url` origin. It does not
navigate the tab. It also does not close the tab or browser when recording
finishes.

If two or more open tabs have that origin, Flow refuses to guess. Supply the
exact current URL:

```bash
openadapt-flow record --backend web \
  --url https://your.app \
  --browser-cdp-endpoint http://127.0.0.1:9222 \
  --browser-page-url 'https://your.app/work/items?view=open' \
  --out recordings/browser-session-selected
```

Diagnostic messages omit URL query and fragment values. The exact selector is
used only to bind the requested tab and is not stored as separate attachment
metadata. The CDP endpoint is not stored. The normal recording evidence does
retain the declared app URL and each observed page URL before and after an
action. Those URLs can contain query or fragment values. Treat them as
sensitive recording data.

## Safety and privacy contract

- The CDP endpoint must use `localhost` or a loopback IP address and must have
  an explicit port. Flow refuses a remote endpoint, URL credentials, query, or
  fragment.
- The selected tab must have the same origin as `--url`. Zero matches and
  ambiguous matches are refusals.
- The selected tab must stay on that origin for the full recording. A
  cross-origin navigation stops the recording and does not produce complete
  metadata.
- Do not open a popup or a new tab in the selected tab's browser context while
  recording. Flow currently binds one page. A context-level listener records
  every new-page signal, including a tab that acts and closes between recorder
  polls. Flow keeps this refusal active through the final Playwright detach so
  a late tab cannot produce complete metadata. A refusal leaves the external
  browser and its tabs open.
- Attach mode does not combine with `--headless`. The external browser owns
  its display mode.
- The `--out` path must not exist. Flow writes to a new temporary sibling and
  publishes that directory only after it writes the final metadata. A refusal
  removes the temporary output and does not change an existing recording.
- Input event payloads carry a unique recording-session binding and have a
  1 MB limit. Flow removes the current document listeners when it detaches.
- Flow retains one exact frame boundary per logical action. It coalesces only
  consecutive input changes from the same bound field session or consecutive
  scroll deltas from one observed gesture. If two distinct actions arrive in
  one recorder poll, Flow refuses the recording before it captures a shared,
  incorrect after-frame.
- Attached screenshots use CSS pixels and the actual live viewport. Thus, DOM
  coordinates and retained frame coordinates stay aligned on high-density
  displays.
- You can resize the tab or move its window between monitors while no action is
  in progress. Flow observes viewport and device-scale changes, waits for a
  stable CSS-pixel frame, and then starts a new per-event coordinate baseline.
  `meta.json` retains the viewport history. Each frame-backed event retains its
  exact `viewport_before` and `viewport_after`.
- Flow refuses only an action that overlaps a resize or monitor-scale change.
  In that case, no exact pre-action frame exists in the new coordinate space,
  so the recording stops without complete metadata. Stop interacting for a
  moment after a resize. Recording then continues automatically.
- `input[type=password]` and fields declared with `--secret FIELD` never send
  their values to Python. Flow binds a private input-session identity and a
  temporary screenshot-mask marker when a declared secret field receives
  focus. The identity remains secret if application code changes the field name
  or ID, or replaces the active input element during the same input session.
  Flow removes the marker when it detaches. Other typed values and visible page
  content are recording evidence and can contain sensitive data. Keep raw
  recordings inside the approved local boundary.
- Secret masks cover every frame in the selected page. Before each masked
  screenshot, Flow snapshots the frame inventory and its lifecycle generation.
  It accepts the in-memory image only when the inventory stays unchanged
  through capture. It retries a bounded number of times and refuses persistent
  frame churn. A discarded unstable image never reaches disk or metadata.
- The close, new-page, origin, and frame lifecycle guards stay active until
  Flow detaches from the external browser. Flow promotes the temporary output
  only after detach succeeds and every guard remains clear.

## Why the Capture Chrome extension is not this path

The `openadapt-capture` repository contains a custom Chrome extension
prototype. It proved useful DOM event capture, but its current direct
WebSocket and replay design does not implement the supported contract above.
It does not yet bind messages to an authenticated recording session, one tab,
one document, and an ordered acknowledged event stream. It also does not
provide the compiler's exact before/after frame binding and source-time secret
redaction. Its direct DOM replay can dispatch actions without the governed
runtime's identity, policy, fresh-frame, and effect checks.

The prototype should remain available for development. It can become a
supported acquisition transport after it does all of the following:

1. Use the shared Flow event and evidence schema. Do not create a second
   compiler or replay format.
2. Redact secret values before they cross the extension boundary.
3. Bind and authenticate the browser profile, tab, document, run, session, and
   monotonically increasing event sequence. A reconnect must acknowledge or
   safely resume events instead of dropping them.
4. Retain exact frame-to-event evidence and bind each event to its current
   viewport coordinate system.
5. Send recordings to the existing compiler. Do not perform direct replay.
6. Pass the same three-trial record, compile, secret, ambiguity-refusal, and
   browser-lifecycle tests as the Playwright attach mode.

Until that contract exists, the extension is a prototype component. This label
does not apply to `openadapt-capture` as a whole. Capture is the canonical
native recorder; the browser recorder stays Playwright-native because browser
DOM identity and source-time secret handling are load-bearing.

## Current boundary

Attach mode supports local Chromium-family browsers that expose a CDP
endpoint. It requires a browser process started with remote debugging and a
separate user-data directory. It does not claim Firefox, WebKit, arbitrary
Chrome extensions, an ordinary browser process that was not started for local
debugging, cross-origin tab selection, separately qualified cross-frame/iframe
recording, multi-page/popup recording, or direct extension replay.
