# Browser recording

OpenAdapt has one supported browser recording contract. `openadapt-flow`
uses a Playwright page to retain ordered input events, DOM identity, field
geometry, exact before/after frames, and capture-time secret masking. The
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
action. Those URLs can contain query or fragment values. Once a declared secret
field holds a value, Flow reports that document's URL and title only while
neither has changed since before the value existed, and withholds both
otherwise. Treat all other URL data as sensitive recording data.

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
  recording. Flow currently binds one page. It installs a new-page latch on
  every candidate context before it reads the accepted page baseline or selects
  the recording tab. Listener registration does not replay pre-existing tabs,
  so those tabs remain allowed. The selected-context latch records every later
  new-page signal, including a tab that acts and closes between recorder polls.
  Flow keeps this refusal active through the final Playwright detach so a late
  tab cannot produce complete metadata. A refusal leaves the external browser
  and its tabs open.
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
  one recorder poll, or the second arrives while Flow captures the first
  action's after-frame, Flow refuses the recording and discards the temporary
  output instead of publishing shared, incorrect evidence.
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
- `input[type=password]` and fields declared with `--secret FIELD` keep their
  literal values inside the page closure. Flow binds a private input-session
  identity and a temporary screenshot-mask marker when the field appears in the
  document. The
  identity remains secret if application code removes the field name or ID
  before focus, changes either attribute during input, or replaces the active
  input element during the same input session. The listener consumes queued DOM
  changes before each focus or input event, including a field that appears,
  changes, or receives a replacement inside one JavaScript task. Flow removes
  its marker when it detaches, including from a page-owned element that is no
  longer in the DOM.
- **Flow reports captured page text exactly, or it withholds that text and
  states why. Flow never rewrites captured text.** There is no placeholder
  substitution anywhere in the recorder. Removing a value from text that was
  already captured is a known-unsolved problem: Englehardt, Acar and Narayanan
  measured every major session-replay vendor in 2017 and found that none
  redacts displayed content automatically and that all of it leaked, and
  PostHog and Sentry still carry open issues for secrets in replay URLs. The
  working answer in production tools is capture-time, element-bound,
  deny-by-default masking, which is what Flow does.
- Matching uses only the values that bound elements hold at that moment, read
  live from the DOM. Flow keeps no value after the field stops holding it, and
  a node the page detached is not a source of values: a controlled input that
  replaces its element on every keystroke leaves keystroke prefixes on the
  nodes it dropped, and those prefixes match ordinary page text by chance. A
  replacement inherits the input session of the node it replaced, so a field
  with no `name` and no `id` keeps one identity across the swap.
- **Identity evidence** -- the DOM selector (an element ID, `data-testid`,
  `data-test`, or `name`), the control role, the accessible name, the clicked
  row's identity characters, and the receiving field's name -- is exact or
  withheld. Replay resolves and compares these against the live page, so a
  rewritten copy would silently compare against characters the page never
  showed. Every withheld item states why in the event (`identity_withheld`,
  `sid_withheld`), so a DOM identity check can never disarm silently, and the
  CLI summary counts the actions. A declared value too short to tell a real
  match from an ordinary coincidence is reported separately
  (`ambiguous-secret-in-identity`).
- **Reflected evidence** -- the page URL and the document title -- is sampled
  from Python at the settled boundary, the same boundary that captures the
  after-frame. The in-page listeners run in the capture phase, before the
  page's own handlers, so any URL or title they read describes the state before
  the action; they therefore emit none. Flow reports reflected text only while
  it has not changed since before that document held any declared value, and so
  cannot be a reflection of that value. Any other reflected text is withheld
  whole: an origin-only URL and an empty title. A page that writes the value
  into its URL or title on a timer can show a version the field no longer
  holds, and no rule that reads only the current DOM can tell that apart from
  ordinary page text, so Flow refuses rather than guesses.
- The page sends `location.origin` beside each event. The origin guard reads
  that value and never the reflected text, so withholding a URL never weakens
  the origin refusal.
- Flow traverses and observes open shadow roots at every event boundary. If a
  shadow field can lose its name, ID, or password type before the first event,
  give the shadow host the same `--secret FIELD` name or ID. Flow then masks the
  complete host. For a pre-existing closed shadow root, Chromium CDP searches
  only for the declared selector and runs the boundary check inside the page;
  no node content crosses into Python. Flow refuses before the first frame when
  the closed field exists but its host does not bind that declaration. It also
  refuses a later unbound shadow input before it accepts a value.
- Each document builds its own page closure, so a closure never saw a value a
  previous document received. Once a declared secret field receives input, Flow
  withholds the page URL and title for every later document: a same-origin GET
  form submit that reflects the value into the next query string leaves an
  origin-only URL and an empty title. `meta.json` records every distinct reason
  in `structural_text_withheld`, and `record` prints one line for each.
- Withholding reflected text costs evidence, and the cost is stated here. A
  page that changes its URL or title after a declared secret field has held a
  value reports an origin-only URL and an empty title for the rest of that
  document, whether or not the change had anything to do with the value. A
  single-page application that routes after a password entry is the common
  case. Identity evidence, action coordinates, and the recorded before/after
  frames are unaffected, so replay keeps its strongest identity tiers.
- Flow protects a declared value from the moment a bound field holds it. Text
  and pixels captured BEFORE that moment are ordinary recording evidence. If an
  operator opens a URL that already contains the password and then types that
  password into a declared field, the URL predates the value, passes the proof
  above, and is recorded. Checking the live value against unchanged text
  instead would withhold on a chance match with a keystroke prefix: a password
  beginning with an ordinary word would withhold the URL of every page whose
  text contains that word. Treat every recorded URL as sensitive recording
  data.
- Source-time field handling does not track an application-defined transform of
  a secret or a copy into an unrelated visible element. Those pixels, all other
  typed values, and other visible page content are recording evidence and can
  contain sensitive data. Keep raw recordings inside the approved local
  boundary.
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
