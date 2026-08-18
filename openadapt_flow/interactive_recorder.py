"""Interactive recorder: capture a demonstration the USER drives live.

``openadapt-flow record --url <app>`` opens a real (headed) Playwright browser
pointed at the user's OWN app. With ``--browser-cdp-endpoint`` it instead
attaches to one explicitly bound tab in an already-running local Chromium
browser, which preserves an existing SSO or 2FA session. Both modes listen to
the user's real clicks, typing, key presses and scrolls via in-page
capture-phase DOM listeners and write the EXACT recording format the compiler
already consumes (``meta.json`` + ``events.jsonl`` +
``frames/{i:04d}_before.png`` / ``_after.png``).

    record --url … → compile → replay

closes the self-serve loop for the user's own app, not just the bundled demo.

Design (why it looks the way it does):

* **Append-only binding, work in the loop.** Calling any Playwright page
  method from inside an ``expose_binding`` callback deadlocks the sync driver,
  so the binding callback does the ONE cheap thing it safely can — append the
  raw event to a Python list — and the main loop drains that list and does all
  the screenshotting/settling. Listeners are installed with ``add_init_script``
  so they survive navigations, and a navigating click's event is delivered
  over the pipe before the new document loads.
* **Frames chain like a driven demo.** A demonstration's screen is static
  between actions, so each step's BEFORE frame is simply the previous step's
  settled frame (captured before the current action happened — no post-
  navigation race), and its AFTER frame is captured once the screen settles.
* **Structured identity is captured in-page** at click time (pre-navigation),
  mirroring ``PlaywrightBackend.structured_text_at`` exactly, so the compiler's
  DOM-identity tier arms on interactively-recorded bundles too.

Secrets never touch Python: a field is secret when it is ``input[type=
password]`` or its name/id is passed via ``--secret``. For a secret field the
in-page listener emits NO value at all (only that a secret was typed, plus the
field rectangle for redaction); the literal is never read, never sent over the
pipe, never written to meta/events/frames/bundle. See ``ir.Step.secret`` and
``docs`` for the full contract.
"""

from __future__ import annotations

import ctypes
import errno
import io
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from PIL import Image

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.recorder import Recorder

# Named keys worth recording as their own KEY step (navigation/submit intent).
# Editing keys (Backspace/Delete) are intentionally omitted: their effect is
# already reflected in the field's value, read via the ``input`` event.
_SPECIAL_KEYS = (
    "Enter",
    "Tab",
    "Escape",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "PageUp",
    "PageDown",
    "Home",
    "End",
)


class BrowserAttachError(RuntimeError):
    """A safe browser-attachment precondition was not met."""


_PARTIAL_RECORDING_PREFIX = ".openadapt-recording-partial-"


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a directory without replacing any destination."""

    if os.name == "nt":
        os.rename(source, destination)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    result: int
    if sys.platform == "linux":
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace directory promotion is unavailable",
                destination,
            ) from exc
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = int(
            renameat2(
                -100,  # AT_FDCWD
                source_bytes,
                -100,  # AT_FDCWD
                destination_bytes,
                1,  # RENAME_NOREPLACE
            )
        )
    elif sys.platform == "darwin":
        try:
            renamex_np = libc.renamex_np
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace directory promotion is unavailable",
                destination,
            ) from exc
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = int(
            renamex_np(
                source_bytes,
                destination_bytes,
                0x00000004,  # RENAME_EXCL
            )
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory promotion is unavailable",
            destination,
        )

    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _secret_screenshot_selectors(
    secret_fields: set[str],
    *,
    marker_attribute: Optional[str] = None,
) -> tuple[str, ...]:
    """Return selectors that mask password and declared-secret fields."""

    selectors = ["input[type='password']"]
    for field in sorted(secret_fields):
        encoded = _css_string_literal(field)
        selectors.append(f"[name={encoded}], [id={encoded}]")
    if marker_attribute is not None:
        if not re.fullmatch(r"data-oaflow-secret-[0-9a-f]{32}", marker_attribute):
            raise BrowserAttachError("the browser secret marker is invalid")
        selectors.append(f"[{marker_attribute}]")
    return tuple(selectors)


def _css_string_literal(value: str) -> str:
    """Serialize an exact value as a valid double-quoted CSS string.

    JSON ``\\u`` escapes are not CSS Unicode escapes. Using ``json.dumps``
    therefore made a declared field such as ``päss`` select a different name
    and left its later frames unmasked. CSS strings accept Unicode directly;
    quotes, backslashes, and control characters need CSS-specific escapes.
    """

    escaped: list[str] = ['"']
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"}:
            escaped.append("\\" + character)
        elif codepoint == 0:
            raise BrowserAttachError(
                "a declared secret field name contains a null character and "
                "cannot be bound to a safe browser mask"
            )
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise BrowserAttachError(
                "a declared secret field name contains an invalid Unicode "
                "surrogate and cannot be bound to a safe browser mask"
            )
        elif codepoint < 0x20 or codepoint == 0x7F:
            # The trailing space terminates the variable-width CSS hex escape.
            escaped.append(f"\\{codepoint:x} ")
        else:
            escaped.append(character)
    escaped.append('"')
    return "".join(escaped)


def _http_origin(url: str, *, label: str) -> tuple[str, str, int]:
    """Return a normalized HTTP origin or refuse an unsafe attach target."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BrowserAttachError(f"{label} is not a valid URL") from exc
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise BrowserAttachError(f"{label} must be an http:// or https:// URL")
    return (scheme, host, port or (443 if scheme == "https" else 80))


def _origin_label(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    default_port = 443 if scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{suffix}"


def _safe_page_label(url: str) -> str:
    """Describe a tab without exposing URL credentials, query, or fragment."""

    try:
        parsed = urlsplit(url)
        origin = _http_origin(url, label="browser tab URL")
    except BrowserAttachError:
        return "<non-web page>"
    path = parsed.path or "/"
    if len(path) > 120:
        path = path[:117] + "..."
    return _origin_label(origin) + path


def validate_browser_cdp_endpoint(endpoint: str) -> str:
    """Require an explicit loopback CDP endpoint.

    Browser attachment is local-only. A remote CDP endpoint is effectively a
    remote-control credential and can also expose every page in that browser.
    The supported recorder does not accept that boundary implicitly.
    """

    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise BrowserAttachError("the browser CDP endpoint is not a valid URL") from exc
    if parsed.scheme.lower() not in {"http", "https", "ws", "wss"}:
        raise BrowserAttachError(
            "the browser CDP endpoint must use http, https, ws, or wss"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BrowserAttachError(
            "the browser CDP endpoint must not contain credentials, a query, "
            "or a fragment"
        )
    host = (parsed.hostname or "").lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise BrowserAttachError(
            "the browser CDP endpoint must use localhost or a loopback IP address"
        )
    if port is None:
        raise BrowserAttachError("the browser CDP endpoint must include a port")
    return endpoint


def select_attached_page(
    browser: Any,
    *,
    app_url: str,
    page_url: Optional[str] = None,
) -> Any:
    """Bind one existing same-origin web tab without guessing.

    A sole tab on the declared application origin is unambiguous. If two or
    more tabs use that origin, the operator must give the exact current URL.
    Query and fragment values are never included in an error message.
    """

    app_origin = _http_origin(app_url, label="the declared app URL")
    if (
        page_url is not None
        and _http_origin(page_url, label="the selected browser page URL") != app_origin
    ):
        raise BrowserAttachError(
            "the selected browser page URL must have the same origin as "
            f"the declared app URL ({_origin_label(app_origin)})"
        )

    matches: list[tuple[Any, str]] = []
    for context in browser.contexts:
        for page in context.pages:
            try:
                current_url = str(page.url)
                current_origin = _http_origin(current_url, label="browser tab URL")
            except Exception:
                continue
            if current_origin == app_origin:
                matches.append((page, current_url))

    if page_url is not None:
        exact = [page for page, current_url in matches if current_url == page_url]
        if len(exact) == 1:
            return exact[0]
        if not exact:
            raise BrowserAttachError(
                "no open tab has the exact --browser-page-url on the declared "
                f"app origin ({_origin_label(app_origin)})"
            )
        raise BrowserAttachError(
            "more than one open tab has the exact --browser-page-url; close "
            "the duplicate tabs and retry"
        )

    if len(matches) == 1:
        return matches[0][0]
    if not matches:
        raise BrowserAttachError(
            "no open tab matches the declared app origin "
            f"({_origin_label(app_origin)}); open the app in the attached "
            "browser and retry"
        )
    labels = sorted({_safe_page_label(current_url) for _, current_url in matches})
    rendered = ", ".join(labels[:5])
    if len(labels) > 5:
        rendered += f", and {len(labels) - 5} more"
    raise BrowserAttachError(
        f"{len(matches)} open tabs match the declared app origin; supply the "
        "exact current URL with --browser-page-url. Candidate paths "
        f"(query and fragment hidden): {rendered}"
    )


# In-page recorder script. Installed via add_init_script so it re-arms on every
# document (navigations). Emits raw events to the Python side via the
# __oaflow_emit binding. __SECRET_NAMES__ / __SPECIAL_KEYS__ are substituted in.
_INIT_JS = r"""
(() => {
  const SESSION_ID = __SESSION_ID__;
  const BINDING_NAME = __BINDING_NAME__;
  const GLOBAL_KEY = '__oaflowRecorder';
  const previous = window[GLOBAL_KEY];
  if (previous && previous.sessionId === SESSION_ID) return;
  if (previous && typeof previous.cleanup === 'function') {
    try { previous.cleanup(); } catch (e) {}
  }
  const SECRET_NAMES = __SECRET_NAMES__;
  const SECRET_MARKER = __SECRET_MARKER__;
  const IDENT_NAMES = __IDENT_NAMES__;
  const SPECIAL = __SPECIAL_KEYS__;
  const listeners = [];
  const secretStates = new WeakMap();
  const inputSessions = new WeakMap();
  const stickySecretElements = new Set();
  let nextInputSession = 0;
  let activeSecretElement = null;
  let activeSecretState = null;
  let resizeTimer = null;
  let secretObserver = null;

  function listenOn(target, type, handler) {
    target.addEventListener(type, handler, true);
    listeners.push([target, type, handler]);
  }

  function listen(type, handler) {
    listenOn(document, type, handler);
  }

  function cleanup() {
    if (resizeTimer !== null) clearTimeout(resizeTimer);
    if (secretObserver !== null) secretObserver.disconnect();
    for (const [target, type, handler] of listeners) {
      try { target.removeEventListener(type, handler, true); } catch (e) {}
    }
    // The Set retains disconnected elements for this exact cleanup. Remove
    // the temporary marker before releasing those references so reinserting a
    // page-owned node after Flow detaches cannot expose recorder metadata.
    for (const el of stickySecretElements) {
      try { el.removeAttribute(SECRET_MARKER); } catch (e) {}
    }
    stickySecretElements.clear();
    activeSecretElement = null;
    activeSecretState = null;
    const current = window[GLOBAL_KEY];
    if (current && current.sessionId === SESSION_ID) {
      try { delete window[GLOBAL_KEY]; } catch (e) { window[GLOBAL_KEY] = null; }
    }
  }
  window[GLOBAL_KEY] = {sessionId: SESSION_ID, cleanup};

  function identifierRect() {
    // Bounding rect of the operator-marked record-identifying field
    // (--identifier FIELD): the patient-banner / MRN element whose PIXELS the
    // compiler crops (anchor.identifier_crop) to arm the pixel-compare
    // identity tier on remote-display replays. Captured at CLICK time (the
    // rect the clicked frame shows), first marked field present wins.
    try {
      for (const key of IDENT_NAMES) {
        const el = document.getElementsByName(key)[0]
          || document.getElementById(key);
        if (!el || !el.getBoundingClientRect) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          return [Math.round(r.left), Math.round(r.top),
                  Math.round(r.width), Math.round(r.height)];
        }
      }
    } catch (e) {}
    return null;
  }

  function structuredIdentity(px, py) {
    // Mirrors PlaywrightBackend.structured_text_at: the REAL characters of the
    // clicked row (MRN/name/DOB), excluding the clicked target's own cell.
    try {
      const el = document.elementFromPoint(px, py);
      if (!el) return null;
      const row = el.closest('tr, [role="row"], li, [role="listitem"]');
      if (!row) return null;
      const declared = (
        row.getAttribute('data-openadapt-identity')
        || row.getAttribute('aria-label')
        || ''
      ).replace(/\s+/g, ' ').trim();
      if (declared) return declared;
      const own = el.closest('td, th, [role="cell"], [role="gridcell"]') || el;
      own.setAttribute('data-oaflow-own', '1');
      let body = '';
      try {
        const clone = row.cloneNode(true);
        const marked = clone.querySelector('[data-oaflow-own="1"]');
        if (marked) marked.remove();
        body = clone.textContent || '';
      } finally {
        own.removeAttribute('data-oaflow-own');
      }
      const joined = body.replace(/\s+/g, ' ').trim();
      return joined || null;
    } catch (e) { return null; }
  }

  function targetRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['button', 'submit', 'reset'].indexOf(type) >= 0) return 'button';
      return 'textbox';
    }
    return null;
  }

  function targetName(el) {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const labelledBy = (el.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      const value = labelledBy.split(/\s+/).map((id) => {
        const node = document.getElementById(id);
        return node ? (node.textContent || '').trim() : '';
      }).filter(Boolean).join(' ');
      if (value) return value;
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label && (label.textContent || '').trim()) return label.textContent.trim();
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping !== el && (wrapping.textContent || '').trim()) {
      return wrapping.textContent.trim();
    }
    for (const attr of ['alt', 'title', 'placeholder']) {
      const value = (el.getAttribute(attr) || '').trim();
      if (value) return value;
    }
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    return text ? text.slice(0, 200) : null;
  }

  function uniqueSelector(el) {
    if (el.id) {
      const selector = `#${CSS.escape(el.id)}`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }
    for (const attr of ['data-testid', 'data-test', 'name']) {
      const value = el.getAttribute(attr);
      if (!value) continue;
      const selector = `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(value)}"]`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }
    return null;
  }

  function structuralTarget(px, py) {
    try {
      const el = document.elementFromPoint(px, py);
      if (!el) return null;
      const target = {
        selector: uniqueSelector(el),
        role: targetRole(el),
        name: targetName(el),
      };
      return (target.selector || target.role || target.name) ? target : null;
    } catch (e) { return null; }
  }

  function isTextEntry(el) {
    try {
      return !!el && !!el.matches && el.matches(
        'input, textarea, [contenteditable=""], [contenteditable="true"],' +
        ' [role="textbox"]'
      );
    } catch (e) { return false; }
  }

  function bindSecretState(el, state, activate = true) {
    secretStates.set(el, state);
    stickySecretElements.add(el);
    if (activate) {
      activeSecretElement = el;
      activeSecretState = state;
    }
    try {
      el.setAttribute(SECRET_MARKER, '');
      return el.hasAttribute(SECRET_MARKER);
    } catch (e) { return false; }
  }

  function inputSessionFor(el) {
    let session = inputSessions.get(el) || null;
    if (!session) {
      nextInputSession += 1;
      session = SESSION_ID + ':input:' + String(nextInputSession);
      inputSessions.set(el, session);
    }
    return session;
  }

  function declaredSecretState(el) {
    if (!el) return null;
    const n = el.name || '', i = el.id || '';
    if ((el.type || '').toLowerCase() !== 'password'
        && SECRET_NAMES.indexOf(n) < 0 && SECRET_NAMES.indexOf(i) < 0) {
      return null;
    }
    return {field: n || i || null, inputSession: inputSessionFor(el)};
  }

  function secretStateForInput(el) {
    let state = secretStates.get(el) || null;
    if (!state) state = declaredSecretState(el);
    if (!state && activeSecretState && activeSecretElement
        && !activeSecretElement.isConnected && isTextEntry(el)) {
      // Some controlled inputs replace their DOM element after each change.
      // Programmatic focus transfer is still the same input session.
      state = activeSecretState;
    }
    if (!state) return null;
    return {state, maskBound: bindSecretState(el, state)};
  }

  function discoverDeclaredSecrets(root) {
    const candidates = [];
    try {
      if (root && root.nodeType === 1 && isTextEntry(root)) candidates.push(root);
      if (root && root.querySelectorAll) {
        candidates.push(...root.querySelectorAll(
          'input, textarea, [contenteditable=""], [contenteditable="true"],' +
          ' [role="textbox"]'
        ));
      }
    } catch (e) {}
    for (const el of candidates) {
      if (secretStates.has(el)) continue;
      const state = declaredSecretState(el);
      if (state) bindSecretState(el, state, false);
    }
  }

  function stateFromPriorDeclaration(mutation) {
    const el = mutation.target;
    if (!isTextEntry(el) || typeof mutation.oldValue !== 'string') return null;
    const oldValue = mutation.oldValue;
    const oldDeclaredName = (mutation.attributeName === 'name'
      || mutation.attributeName === 'id')
      && SECRET_NAMES.indexOf(oldValue) >= 0;
    const oldPasswordType = mutation.attributeName === 'type'
      && oldValue.toLowerCase() === 'password';
    if (!oldDeclaredName && !oldPasswordType) return null;
    const currentField = el.name || el.id || null;
    return {
      field: oldDeclaredName ? oldValue : currentField,
      inputSession: inputSessionFor(el),
    };
  }

  secretObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === 'attributes') {
        if (!secretStates.has(mutation.target)) {
          const priorState = stateFromPriorDeclaration(mutation);
          if (priorState) bindSecretState(mutation.target, priorState, false);
        }
        discoverDeclaredSecrets(mutation.target);
      } else {
        for (const node of mutation.addedNodes) discoverDeclaredSecrets(node);
      }
    }
    for (const el of stickySecretElements) {
      if (!el.isConnected) continue;
      try {
        if (!el.hasAttribute(SECRET_MARKER)) el.setAttribute(SECRET_MARKER, '');
      } catch (e) {}
    }
  });
  secretObserver.observe(document.documentElement || document, {
    attributes: true, attributeOldValue: true, childList: true, subtree: true,
  });
  discoverDeclaredSecrets(document);

  function fieldLabel(el) {
    // Best available human label for the receiving field, best-first:
    // associated <label for=...>, wrapping <label>, aria-label,
    // aria-labelledby, placeholder, name attribute, title. Mirrors
    // PlaywrightBackend.focused_field_label exactly. Passive metadata for
    // the compile-time parameter-proposal pass; NEVER the field's value.
    try {
      const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
      if (el.id) {
        try {
          const forLabel = document.querySelector(
            'label[for="' + CSS.escape(el.id) + '"]'
          );
          const t = clean(forLabel && forLabel.textContent);
          if (t) return t;
        } catch (e) {}
      }
      const wrapping = el.closest('label');
      if (wrapping) {
        // Label TEXT only: a control nested inside the label (e.g. a
        // <textarea>) contributes its typed VALUE to textContent -- strip
        // embedded controls so the captured label is never the value.
        const cloned = wrapping.cloneNode(true);
        for (const child of cloned.querySelectorAll(
            'input, textarea, select, [contenteditable=""],' +
            ' [contenteditable="true"], [role="textbox"]')) {
          child.remove();
        }
        const t = clean(cloned.textContent);
        if (t) return t;
      }
      const aria = clean(el.getAttribute('aria-label'));
      if (aria) return aria;
      const labelledby = el.getAttribute('aria-labelledby');
      if (labelledby) {
        const parts = [];
        for (const id of labelledby.split(/\s+/)) {
          const ref = document.getElementById(id);
          const t = clean(ref && ref.textContent);
          if (t) parts.push(t);
        }
        if (parts.length) return parts.join(' ');
      }
      const placeholder = clean(el.getAttribute('placeholder'));
      if (placeholder) return placeholder;
      const name = clean(el.getAttribute('name'));
      if (name) return name;
      const title = clean(el.getAttribute('title'));
      if (title) return title;
    } catch (e) {}
    return null;
  }

  function emit(o) {
    try {
      o.__oaflow_session = SESSION_ID;
      o.__oaflow_top_level = window === window.top;
      o.__oaflow_viewport = [Math.round(window.innerWidth),
                             Math.round(window.innerHeight)];
      o.__oaflow_dpr = Number(window.devicePixelRatio || 1);
      const binding = window[BINDING_NAME];
      if (typeof binding === 'function') binding(o);
    } catch (e) {}
  }

  let pointerDown = null;
  let suppressClick = false;
  listenOn(window, 'resize', () => {
    if (resizeTimer !== null) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      emit({kind: 'viewport', url: location.href, title: document.title});
    }, 100);
  });
  listen('focusin', (e) => {
    const el = e.target;
    const declared = declaredSecretState(el);
    if (declared) {
      // Bind before the first key event. Application code can remove a
      // declared name/id during keydown, beforeinput, or input dispatch.
      bindSecretState(el, declared);
      return;
    }
    if (!activeSecretState || el === activeSecretElement) return;
    const retained = secretStates.get(el) || null;
    if (retained) {
      bindSecretState(el, retained);
    } else if (activeSecretElement && !activeSecretElement.isConnected
               && isTextEntry(el)) {
      bindSecretState(el, activeSecretState);
    } else {
      activeSecretElement = null;
      activeSecretState = null;
    }
  });
  listen('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (activeSecretState && e.target !== activeSecretElement
        && !secretStates.has(e.target)) {
      // An explicit operator action on another target ends the sticky input
      // session. Programmatic replacement without a pointer action does not.
      activeSecretElement = null;
      activeSecretState = null;
    }
    pointerDown = {
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      sid: structuredIdentity(e.clientX, e.clientY),
      structural: structuralTarget(e.clientX, e.clientY),
      idr: identifierRect(),
    };
  });

  listen('pointerup', (e) => {
    if (e.button !== 0 || !pointerDown) return;
    const start = pointerDown;
    pointerDown = null;
    const endX = Math.round(e.clientX), endY = Math.round(e.clientY);
    if (Math.hypot(endX - start.x, endY - start.y) < 5) return;
    suppressClick = true;
    setTimeout(() => { suppressClick = false; }, 0);
    emit({
      kind: 'drag', x: start.x, y: start.y, end_x: endX, end_y: endY,
      sid: start.sid, structural: start.structural,
      end_structural: structuralTarget(endX, endY), idr: start.idr,
      url: location.href, title: document.title,
    });
  });

  listen('click', (e) => {
    if (e.button !== 0) return;
    if (suppressClick) { suppressClick = false; return; }
    emit({
      kind: 'click',
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      sid: structuredIdentity(e.clientX, e.clientY),
      structural: structuralTarget(e.clientX, e.clientY),
      idr: identifierRect(),
      url: location.href, title: document.title,
    });
  });

  listen('contextmenu', (e) => {
    emit({
      kind: 'right_click',
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      sid: structuredIdentity(e.clientX, e.clientY),
      structural: structuralTarget(e.clientX, e.clientY),
      idr: identifierRect(),
      url: location.href, title: document.title,
    });
  });

  listen('input', (e) => {
    const el = e.target;
    const secretBinding = secretStateForInput(el);
    const secret = secretBinding !== null;
    const r = (el.getBoundingClientRect && el.getBoundingClientRect())
      || { left: 0, top: 0, width: 0, height: 0 };
    const o = {
      kind: 'input',
      field: secret ? secretBinding.state.field : (el.name || el.id || null),
      label: fieldLabel(el),
      secret: secret,
      __oaflow_input_session: secret
        ? secretBinding.state.inputSession : inputSessionFor(el),
      rect: [Math.round(r.left), Math.round(r.top),
             Math.round(r.width), Math.round(r.height)],
      url: location.href, title: document.title,
    };
    // The literal value of a SECRET field is never read or transmitted.
    if (secret) {
      o.__oaflow_secret_mask_bound = secretBinding.maskBound;
    } else {
      o.value = (el.value != null ? String(el.value) : '');
    }
    emit(o);
  });

  listen('keydown', (e) => {
    const modifiers = [];
    if (e.ctrlKey) modifiers.push('ctrl');
    if (e.altKey) modifiers.push('alt');
    if (e.shiftKey) modifiers.push('shift');
    if (e.metaKey) modifiers.push('meta');
    const pureModifier = ['Control', 'Alt', 'Shift', 'Meta'].indexOf(e.key) >= 0;
    const shiftedText = modifiers.length === 1
      && modifiers[0] === 'shift' && e.key.length === 1;
    if (modifiers.length && !pureModifier && !shiftedText && !e.repeat) {
      emit({
        kind: 'hotkey', key: e.key, modifiers,
        url: location.href, title: document.title,
      });
      return;
    }
    if (SPECIAL.indexOf(e.key) < 0) return;
    emit({ kind: 'key', key: e.key, url: location.href, title: document.title });
  });

  listen('wheel', (e) => {
    emit({
      kind: 'scroll',
      dx: Math.round(e.deltaX), dy: Math.round(e.deltaY),
      url: location.href, title: document.title,
    });
  });
})();
"""


class InteractiveRecorder:
    """Drives a live headed browser and records what the user does.

    Use :func:`record_interactive` for the common case; this class is exposed
    for tests, which drive synthetic input via :attr:`page` and pump the loop
    deterministically.
    """

    def __init__(
        self,
        url: str,
        out_dir: Path | str,
        *,
        secret_fields: tuple[str, ...] = (),
        param_fields: tuple[str, ...] = (),
        identifier_fields: tuple[str, ...] = (),
        headless: bool = False,
        cdp_endpoint: Optional[str] = None,
        browser_page_url: Optional[str] = None,
        poll_ms: int = 60,
        settle_timeout_s: float = 5.0,
        settle_stable_frames: int = 2,
        settle_interval_s: float = 0.15,
        viewport: tuple[int, int] = (1280, 800),
        system_of_record_reader: Optional[
            Callable[[], Optional[list[dict[str, Any]]]]
        ] = None,
        stop_when: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._url = url
        self._out_dir = Path(out_dir)
        self._secret_fields = set(secret_fields)
        self._param_fields = set(param_fields)
        self._identifier_fields = set(identifier_fields)
        self._headless = headless
        if browser_page_url and not cdp_endpoint:
            raise BrowserAttachError("browser_page_url requires a browser CDP endpoint")
        if cdp_endpoint and headless:
            raise BrowserAttachError(
                "headless mode cannot be combined with an attached browser"
            )
        self._cdp_endpoint = (
            validate_browser_cdp_endpoint(cdp_endpoint) if cdp_endpoint else None
        )
        self._attached_origin = (
            _http_origin(url, label="the declared app URL")
            if self._cdp_endpoint
            else None
        )
        self._browser_page_url = browser_page_url
        self._owns_browser = self._cdp_endpoint is None
        self._session_id = uuid.uuid4().hex
        self._binding_name = f"__oaflow_emit_{self._session_id}"
        self._secret_marker_attribute = f"data-oaflow-secret-{self._session_id}"
        self._poll_ms = poll_ms
        self._viewport = viewport
        # Recording-only, read-only observation. This does not add an effect
        # verifier to the browser backend or grant the later runtime authority.
        self._system_of_record_reader = system_of_record_reader
        # Optional recording completion condition. It is evaluated only after
        # queued browser events have been persisted, while the page is open.
        self._stop_when = stop_when
        self._settle = dict(
            settle_timeout_s=settle_timeout_s,
            settle_stable_frames=settle_stable_frames,
            settle_interval_s=settle_interval_s,
        )
        self._pyq: list[dict[str, Any]] = []
        self._pending_type: Optional[dict[str, Any]] = None
        self._pending_scroll: Optional[dict[str, Any]] = None
        self._listener_error: Optional[BrowserAttachError] = None
        self.done = False

        # Set on start().
        self._recording_dir: Optional[Path] = None
        self._pw = None
        self._browser = None
        self.page = None
        self._page_close_listener = self._handle_page_close
        self._frame_navigation_listener = self._handle_frame_navigation
        self._frame_attached_listener = self._handle_frame_tree_change
        self._frame_detached_listener = self._handle_frame_tree_change
        self._popup_listener = self._handle_popup
        self._context_page_listener = self._handle_context_page
        self._page_lifecycle_listeners_installed = False
        self._context_page_listener_installed = False
        self._context_page_latches: list[tuple[Any, Any]] = []
        self._context_page_baselines: list[tuple[Any, tuple[Any, ...]]] = []
        self._context = None
        self._context_pages_at_start: tuple[Any, ...] = ()
        self._finalizing = False
        self.backend: Optional[PlaywrightBackend] = None
        self.recorder: Optional[Recorder] = None
        self._last_frame: bytes = b""
        self._last_structural: dict[str, Any] = {}
        self._attached_geometry: Optional[tuple[int, int, float]] = None
        self._initial_attached_viewport: Optional[tuple[int, int]] = None
        self._viewport_dirty = False
        self._viewport_history: list[dict[str, Any]] = []

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch or attach, install listeners, and capture the first frame."""

        self._prepare_recording_dir()
        try:
            if self._owns_browser:
                from openadapt_flow._browser_setup import ensure_chromium_installed

                ensure_chromium_installed()
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            if self._owns_browser:
                self._browser = self._pw.chromium.launch(headless=self._headless)
                self.page = self._browser.new_page(
                    viewport={
                        "width": self._viewport[0],
                        "height": self._viewport[1],
                    },
                    device_scale_factor=1,
                )
            else:
                try:
                    self._browser = self._pw.chromium.connect_over_cdp(
                        self._cdp_endpoint
                    )
                except Exception as exc:
                    raise BrowserAttachError(
                        "could not connect to the local Chromium CDP endpoint; "
                        "confirm that the browser was started with remote "
                        "debugging and that the endpoint is ready"
                    ) from exc
                self._install_candidate_context_page_latches()
                self.page = select_attached_page(
                    self._browser,
                    app_url=self._url,
                    page_url=self._browser_page_url,
                )

            self._context = self.page.context
            if self._owns_browser:
                self._context.on("page", self._context_page_listener)
                self._context_page_latches = [
                    (self._context, self._context_page_listener)
                ]
                self._context_page_listener_installed = True
                self._context_pages_at_start = tuple(self._context.pages)
            else:
                self._bind_selected_context_page_latch()
            if self._listener_error is not None:
                raise self._listener_error
            self.page.on("close", self._page_close_listener)
            self.page.on("framenavigated", self._frame_navigation_listener)
            self.page.on("frameattached", self._frame_attached_listener)
            self.page.on("framedetached", self._frame_detached_listener)
            self.page.on("popup", self._popup_listener)
            self._page_lifecycle_listeners_installed = True
            self.page.expose_binding(
                self._binding_name,
                lambda source, detail: self._enqueue_browser_event(
                    detail,
                    source=source,
                ),
            )
            init_js = (
                _INIT_JS.replace("__SESSION_ID__", json.dumps(self._session_id))
                .replace("__BINDING_NAME__", json.dumps(self._binding_name))
                .replace("__SECRET_NAMES__", json.dumps(sorted(self._secret_fields)))
                .replace(
                    "__SECRET_MARKER__",
                    json.dumps(self._secret_marker_attribute),
                )
                .replace(
                    "__IDENT_NAMES__",
                    json.dumps(sorted(self._identifier_fields)),
                )
                .replace("__SPECIAL_KEYS__", json.dumps(list(_SPECIAL_KEYS)))
            )
            self.page.add_init_script(init_js)
            if self._owns_browser:
                self.page.goto(self._url)
                try:
                    self.page.wait_for_load_state("load")
                except Exception:
                    pass
            else:
                # add_init_script applies after the next navigation. Install
                # the same session in every already-open document now. This
                # includes child frames, whose local coordinates cannot yet be
                # bound to top-level evidence and therefore emit an explicit
                # refusal instead of disappearing from the recording.
                for frame in list(self.page.frames):
                    try:
                        frame.evaluate(init_js)
                    except Exception as exc:
                        try:
                            detached = frame.is_detached()
                        except Exception:
                            detached = False
                        if detached:
                            continue
                        raise BrowserAttachError(
                            "could not install the recording listener in every "
                            "existing browser frame; recording was refused"
                        ) from exc

            self.backend = PlaywrightBackend(
                self.page,
                screenshot_scale="device" if self._owns_browser else "css",
                screenshot_mask_selectors=_secret_screenshot_selectors(
                    self._secret_fields,
                    marker_attribute=self._secret_marker_attribute,
                ),
            )
            assert self._recording_dir is not None
            self.recorder = Recorder(
                self.backend,
                self._recording_dir,
                app_url=self._url,
                system_of_record_reader=self._system_of_record_reader,
                **self._settle,
            )
            if self._owns_browser:
                self._last_frame = self.recorder._wait_settled()
                self._last_structural = self._structural_state()
            else:
                self._rebaseline_attached_viewport()
        except Exception:
            self.abort()
            raise

    def run(self) -> Path:
        """Pump until completion, an operator stop, or a closed window."""
        if self._stop_when is None:
            finish_instruction = (
                "Press Ctrl-C here to finish. Keep the selected browser tab open "
                "until Flow confirms the recording."
                if not self._owns_browser
                else "Press Ctrl-C here (or close the browser window) to finish."
            )
        else:
            finish_instruction = (
                "Complete the workflow. Recording stops automatically after "
                "the configured result is observed. Press Ctrl-C only to stop early."
            )
        print(
            f"Recording {self._url}\n"
            + (
                "  Perform your workflow in the selected existing browser tab.\n"
                if not self._owns_browser
                else "  Perform your workflow in the browser window.\n"
            )
            + f"  {finish_instruction}"
        )
        try:
            while not self.done:
                if not self._pump():
                    break
        except KeyboardInterrupt:
            print("\n[record] stopping…")
        except Exception:
            self.abort()
            raise
        return self.finish()

    def run_script(self, script: Callable[[Any, Callable[[], None]], None]) -> Path:
        """Scripted loop (tests): run ``script(page, pump)`` — which performs
        synthetic input and calls ``pump()`` to let the recorder drain — then
        flush and finish."""
        try:
            script(self.page, self.pump)
        except Exception:
            self.abort()
            raise
        return self.finish()

    def finish(self) -> Path:
        """Flush input, write metadata, and close or detach as appropriate."""
        # The first finalization operation touches the live page. Arm every
        # irreversible lifecycle latch before cleanup or the last queue drain.
        self._finalizing = True
        try:
            self._cleanup_page_listeners()
            self._drain_event_queue()
            self._flush_type()
            self._flush_scroll()
            assert self.recorder is not None
            out = self.recorder.finish()
            if self._listener_error is not None:
                raise self._listener_error
            meta_path = out / "meta.json"
            meta = json.loads(meta_path.read_text())
            meta["source"] = (
                "openadapt-flow-playwright"
                if self._owns_browser
                else "openadapt-flow-playwright-cdp"
            )
            if not self._owns_browser:
                assert self._initial_attached_viewport is not None
                meta["viewport"] = list(self._initial_attached_viewport)
                meta["viewport_mode"] = "per-event"
                meta["viewport_history"] = list(self._viewport_history)
            meta_path.write_text(json.dumps(meta, indent=2))
            if self._listener_error is not None:
                raise self._listener_error
            self._assert_no_new_pages()
            self._stop_browser_connection()
            if self._listener_error is not None:
                raise self._listener_error
            return self._promote_recording()
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        """Detach and remove only this session's unpublished temporary output."""

        self.done = True
        self._pyq.clear()
        try:
            self._cleanup_page_listeners()
        finally:
            try:
                self._stop_browser_connection()
            finally:
                self._discard_recording_dir()

    def _prepare_recording_dir(self) -> None:
        """Reserve a fresh sibling directory without changing the final path."""

        if self._recording_dir is not None:
            return
        if os.path.lexists(self._out_dir):
            raise BrowserAttachError(
                "the recording output already exists; choose a new --out directory"
            )
        try:
            self._out_dir.parent.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.mkdtemp(
                prefix=f"{_PARTIAL_RECORDING_PREFIX}{self._out_dir.name}-",
                dir=self._out_dir.parent,
            )
        except OSError as exc:
            raise BrowserAttachError(
                "the temporary recording output could not be created"
            ) from exc
        self._recording_dir = Path(temporary)

    def _promote_recording(self) -> Path:
        """Atomically publish the complete recording at the requested path."""

        assert self._recording_dir is not None
        try:
            _rename_directory_noreplace(self._recording_dir, self._out_dir)
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise BrowserAttachError(
                    "the recording output appeared during capture; Flow refused to "
                    "replace it"
                ) from exc
            raise BrowserAttachError(
                "the complete recording could not be published atomically"
            ) from exc
        self._recording_dir = None
        return self._out_dir

    def _discard_recording_dir(self) -> None:
        """Delete only the temporary directory that this session created."""

        recording_dir, self._recording_dir = self._recording_dir, None
        if recording_dir is None or not recording_dir.exists():
            return
        shutil.rmtree(recording_dir)

    def _handle_page_close(self, _page: Any = None) -> None:
        """Retain a refusal when an attached tab closes before finalization."""

        self.done = True
        if not self._owns_browser and self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser tab closed before Flow could retain the "
                "final evidence; recording stopped without complete metadata"
            )

    def _handle_frame_navigation(self, frame: Any) -> None:
        """Retain the first selected-main-frame origin violation."""

        if self._owns_browser or self.page is None or self._listener_error is not None:
            return
        try:
            if frame is not self.page.main_frame:
                if self._finalizing:
                    self._retain_late_frame_error()
                return
            current_origin = _http_origin(
                str(frame.url),
                label="the selected browser tab URL",
            )
        except Exception:
            current_origin = None
        if self._finalizing or current_origin != self._attached_origin:
            self._listener_error = BrowserAttachError(
                "the selected browser tab changed frame state after Flow "
                "retained its final evidence; recording was refused"
                if self._finalizing
                else "the selected browser tab left the declared application "
                "origin; recording was refused"
            )
            self.done = True

    def _retain_late_frame_error(self) -> None:
        """Retain one refusal for a post-snapshot frame-tree change."""

        if self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser tab changed frame state after Flow "
                "retained its final evidence; recording was refused"
            )
        self.done = True

    def _handle_frame_tree_change(self, _frame: Any = None) -> None:
        """Refuse a frame attach/detach after the final evidence snapshot."""

        if not self._owns_browser and self._finalizing:
            self._retain_late_frame_error()

    def _handle_popup(self, _popup: Any = None) -> None:
        """Refuse a second page that the selected recording tab opens."""

        self.done = True
        if self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser tab opened a popup or new tab; this "
                "recording is bound to one tab, so Flow stopped before "
                "publishing incomplete metadata"
            )

    def _handle_context_page(self, _page: Any = None) -> None:
        """Irreversibly refuse any page created after context binding."""

        self.done = True
        if self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser context opened a popup or new tab; this "
                "recording is bound to its accepted page baseline, so Flow "
                "stopped before publishing incomplete metadata"
            )

    def _install_candidate_context_page_latches(self) -> None:
        """Latch new pages on every context before attached-page selection."""

        assert self._browser is not None
        try:
            contexts = tuple(self._browser.contexts)
        except Exception as exc:
            raise BrowserAttachError(
                "the attached browser context inventory could not be read"
            ) from exc
        baselines: list[tuple[Any, tuple[Any, ...]]] = []
        for context in contexts:
            try:
                baseline = tuple(context.pages)
            except Exception as exc:
                raise BrowserAttachError(
                    "the attached browser page baseline could not be read"
                ) from exc
            baselines.append((context, baseline))
        self._context_page_baselines = baselines
        for context, baseline in baselines:
            try:
                context.on("page", self._context_page_listener)
                self._context_page_latches.append(
                    (context, self._context_page_listener)
                )
                current = tuple(context.pages)
            except Exception as exc:
                raise BrowserAttachError(
                    "the attached browser page baseline could not be guarded"
                ) from exc
            for candidate in current:
                if not any(candidate is existing for existing in baseline):
                    self._handle_context_page(candidate)
        self._context_page_listener_installed = bool(self._context_page_latches)
        if self._listener_error is not None:
            raise self._listener_error

    def _bind_selected_context_page_latch(self) -> None:
        """Keep the selected context latch and its pre-listener baseline."""

        assert self._context is not None
        selected_baseline = next(
            (
                baseline
                for context, baseline in self._context_page_baselines
                if context is self._context
            ),
            None,
        )
        if selected_baseline is None:
            raise BrowserAttachError(
                "the selected browser context was not in the guarded baseline"
            )
        retained: list[tuple[Any, Any]] = []
        for context, listener in self._context_page_latches:
            if context is self._context:
                retained.append((context, listener))
                continue
            try:
                context.remove_listener("page", listener)
            except Exception:
                pass
        self._context_page_latches = retained
        self._context_page_baselines = [(self._context, selected_baseline)]
        self._context_pages_at_start = selected_baseline
        self._context_page_listener_installed = True
        self._assert_no_new_pages()

    def _assert_no_new_pages(self) -> None:
        """Retain a refusal if this recording context gained another page."""

        if self.page is None or not self._context_pages_at_start:
            return
        try:
            current_pages = tuple(self.page.context.pages)
        except Exception as exc:
            raise BrowserAttachError(
                "the selected browser tab page inventory could not be read; "
                "recording was refused"
            ) from exc
        for candidate in current_pages:
            if not any(
                candidate is existing for existing in self._context_pages_at_start
            ):
                self._handle_popup(candidate)
                break
        if self._listener_error is not None:
            raise self._listener_error

    def _enqueue_browser_event(
        self,
        detail: Any,
        *,
        source: Optional[dict[str, Any]] = None,
    ) -> None:
        """Accept only a bounded event from this recorder session."""

        if not isinstance(detail, dict):
            return
        event = dict(detail)
        if event.pop("__oaflow_session", None) != self._session_id:
            return
        kind = event.get("kind")
        secret_mask_bound = event.pop("__oaflow_secret_mask_bound", None)
        if (
            kind == "input"
            and bool(event.get("secret"))
            and secret_mask_bound is not True
        ):
            self._listener_error = BrowserAttachError(
                "a secret input could not retain its screenshot mask identity; "
                "recording stopped before accepting the event"
            )
            self.done = True
            return
        raw_input_session = event.pop("__oaflow_input_session", None)
        if kind == "input":
            expected_prefix = f"{self._session_id}:input:"
            if (
                not isinstance(raw_input_session, str)
                or not raw_input_session.startswith(expected_prefix)
                or not raw_input_session.removeprefix(expected_prefix).isdigit()
            ):
                self._listener_error = BrowserAttachError(
                    "the browser emitted an input without a valid bound field "
                    "session; recording stopped before accepting the event"
                )
                self.done = True
                return
            event["_oaflow_input_session"] = raw_input_session
        reported_top_level = bool(event.pop("__oaflow_top_level", True))
        source_is_selected_top_level = reported_top_level
        if source is not None:
            try:
                source_is_selected_top_level = (
                    source.get("page") is self.page
                    and source.get("frame") is self.page.main_frame
                )
            except Exception:
                source_is_selected_top_level = False
        if not source_is_selected_top_level and kind == "viewport":
            return
        if not source_is_selected_top_level:
            self._listener_error = BrowserAttachError(
                "an event came from an iframe; cross-frame recording is not "
                "qualified, so recording stopped before accepting the event"
            )
            self.done = True
            return
        if kind not in {
            "click",
            "right_click",
            "drag",
            "input",
            "key",
            "hotkey",
            "scroll",
            "viewport",
        }:
            return
        if not self._owns_browser:
            try:
                event_origin = _http_origin(
                    str(event["url"]),
                    label="the browser event URL",
                )
            except Exception:
                event_origin = None
            if event_origin != self._attached_origin:
                self._listener_error = BrowserAttachError(
                    "a browser event came from outside the declared application "
                    "origin; recording stopped before accepting the event"
                )
                self.done = True
                return
            raw_viewport = event.pop("__oaflow_viewport", None)
            raw_dpr = event.pop("__oaflow_dpr", None)
            try:
                event_geometry = (
                    int(raw_viewport[0]),
                    int(raw_viewport[1]),
                    round(float(raw_dpr), 6),
                )
            except (IndexError, TypeError, ValueError):
                event_geometry = (0, 0, 0.0)
            if (
                event_geometry[0] <= 0
                or event_geometry[1] <= 0
                or not 0.1 <= event_geometry[2] <= 16.0
            ):
                self._listener_error = BrowserAttachError(
                    "the browser emitted invalid viewport evidence; recording "
                    "stopped before accepting the event"
                )
                self.done = True
                return
            event["_oaflow_geometry"] = event_geometry
            if kind == "viewport":
                self._viewport_dirty = True
                return
        else:
            event.pop("__oaflow_viewport", None)
            event.pop("__oaflow_dpr", None)
        try:
            encoded_size = len(json.dumps(event).encode("utf-8"))
        except (TypeError, ValueError):
            return
        if encoded_size > 1_000_000:
            self._listener_error = BrowserAttachError(
                "the browser emitted an event larger than 1 MB; recording "
                "stopped without accepting the event"
            )
            self.done = True
            return
        self._pyq.append(event)

    def _cleanup_page_listeners(self) -> None:
        """Remove this session's current-document listeners before detach."""

        if self.page is None:
            return
        try:
            frames = list(self.page.frames)
        except Exception:
            frames = []
        for frame in frames:
            try:
                frame.evaluate(
                    """sessionId => {
                      const current = window.__oaflowRecorder;
                      if (current && current.sessionId === sessionId
                          && typeof current.cleanup === 'function') {
                        current.cleanup();
                      }
                    }""",
                    self._session_id,
                )
            except Exception:
                continue

    def _cleanup_secret_markers(self) -> None:
        """Remove this session's temporary secret-mask attributes."""

        if self.page is None:
            return
        try:
            frames = list(self.page.frames)
        except Exception:
            frames = []
        for frame in frames:
            try:
                frame.evaluate(
                    """marker => {
                      for (const element of document.querySelectorAll('*')) {
                        if (element.hasAttribute(marker)) {
                          element.removeAttribute(marker);
                        }
                      }
                    }""",
                    self._secret_marker_attribute,
                )
            except Exception:
                continue

    def _stop_browser_connection(self) -> None:
        """Close an owned browser or detach without closing an external one."""

        self._cleanup_page_listeners()
        self._cleanup_secret_markers()
        browser, self._browser = self._browser, None
        playwright, self._pw = self._pw, None
        try:
            if self._owns_browser and browser is not None:
                browser.close()
        finally:
            try:
                if playwright is not None:
                    playwright.stop()
            finally:
                # Keep all local lifecycle latches active until the external
                # Playwright connection has detached. They do not remain in
                # Chromium after the connection closes.
                if self.backend is not None:
                    self.backend.stop_screenshot_mask_tracking()
                self._page_lifecycle_listeners_installed = False
                self._context_page_listener_installed = False
                self._context_page_latches.clear()
                self._context_page_baselines.clear()
                self._context = None

    def _drain_event_queue(self) -> bool:
        """Process all events already delivered by the page binding."""

        if self._listener_error is not None:
            raise self._listener_error
        self._assert_no_new_pages()
        batch = self._pyq[:]
        del self._pyq[:]
        self._validate_event_batch(batch)
        rebased = False
        if not self._owns_browser:
            current_geometry = self._read_attached_geometry()
            if self._viewport_dirty or current_geometry != self._attached_geometry:
                if batch:
                    raise BrowserAttachError(
                        "an action overlapped a browser resize or monitor-scale "
                        "change; recording stopped because no exact pre-action "
                        "frame exists in the new coordinate space"
                    )
                self._rebaseline_attached_viewport()
                rebased = True
        for event in batch:
            if not self._owns_browser:
                event_geometry = event.pop("_oaflow_geometry", None)
                if event_geometry != self._attached_geometry:
                    raise BrowserAttachError(
                        "an action overlapped a browser resize or monitor-scale "
                        "change; recording stopped because no exact pre-action "
                        "frame exists in the new coordinate space"
                    )
            self._process(event)
            if not self._owns_browser and (
                self._viewport_dirty
                or self._read_attached_geometry() != self._attached_geometry
            ):
                raise BrowserAttachError(
                    "the browser resized or changed monitor scale while an "
                    "action was being retained; recording stopped without "
                    "complete metadata"
                )
            self._assert_no_new_pages()
        if self._listener_error is not None:
            raise self._listener_error
        return bool(batch) or rebased

    @staticmethod
    def _validate_event_batch(batch: list[dict[str, Any]]) -> None:
        """Refuse a batch that lacks an exact frame between logical actions."""

        if len(batch) <= 1:
            return
        kinds = {event.get("kind") for event in batch}
        if kinds == {"scroll"}:
            return
        if kinds == {"input"}:
            sessions = {event.get("_oaflow_input_session") for event in batch}
            fields = {event.get("field") for event in batch}
            if len(sessions) == 1 and None not in sessions and len(fields) == 1:
                return
        raise BrowserAttachError(
            "more than one logical browser action arrived before Flow could "
            "retain an exact intermediate frame; recording was refused"
        )

    # -- event pump ----------------------------------------------------------

    def pump(self) -> bool:
        """One public pump tick (used by scripted tests). Returns False when
        the page/browser is gone."""
        return self._pump()

    def _pump(self) -> bool:
        if self._listener_error is not None:
            raise self._listener_error
        if self.done:
            return False
        try:
            self.page.wait_for_timeout(self._poll_ms)
        except Exception:
            self.done = True
            return False
        if not self._drain_event_queue():
            # Distinct scroll gestures are separated by pauses; flush a
            # completed scroll on idle so each becomes its own step. A type run
            # is NOT idle-flushed (a mid-word pause must not split it) — it
            # flushes on the next boundary event or at finish().
            self._flush_scroll()
            return not self._stop_condition_reached()
        return not self._stop_condition_reached()

    def _process(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "input":
            self._flush_scroll()
            self._accumulate_input(ev)
        elif kind == "scroll":
            self._flush_type()
            self._accumulate_scroll(ev)
        elif kind in {"click", "right_click", "drag"}:
            self._flush_type()
            self._flush_scroll()
            self._record_pointer(ev)
        elif kind in {"key", "hotkey"}:
            self._flush_type()
            self._flush_scroll()
            self._record_key(ev)

    # -- accumulation / flush ------------------------------------------------

    def _accumulate_input(self, ev: dict[str, Any]) -> None:
        field = ev.get("field")
        input_session = ev.get("_oaflow_input_session")
        if self._pending_type is not None and (
            self._pending_type.get("field") != field
            or self._pending_type.get("input_session") != input_session
        ):
            self._flush_type()  # focus moved to a different field
        if self._pending_type is None:
            self._pending_type = {
                "field": field,
                "input_session": input_session,
                "label": ev.get("label"),
                "secret": bool(ev.get("secret")),
                "value": "",
                "rect": ev.get("rect"),
            }
        pt = self._pending_type
        pt["secret"] = pt["secret"] or bool(ev.get("secret"))
        if ev.get("label"):
            pt["label"] = ev["label"]
        if ev.get("rect"):
            pt["rect"] = ev["rect"]
        if not pt["secret"]:
            pt["value"] = ev.get("value", pt["value"])
        # The structural context for the whole run is its FIRST input's frame.
        pt.setdefault("structural_before", dict(self._last_structural))
        # Capture the field-with-text after-frame NOW, while the typed value is
        # on screen and BEFORE any following navigating action executes. In a
        # human recording the pump cadence reaches here between the last
        # keystroke and the next click, so this frame is the settled field —
        # not a screen the next click has already navigated to.
        assert self.backend is not None
        pt["after_frame"] = self.backend.screenshot()
        pt["structural_after"] = self._structural_state()

    def _flush_type(self) -> None:
        pt = self._pending_type
        self._pending_type = None
        if pt is None:
            return
        field = pt.get("field")
        structural_before = pt.get("structural_before", self._last_structural)
        assert self.recorder is not None
        after_png = pt.get("after_frame")
        structural_after = pt.get("structural_after")
        # The receiving field's label rides on every TYPE event as PASSIVE
        # evidence (never the value): the compiler's deterministic
        # parameter-proposal pass names a proposed parameter from it, gated
        # behind operator confirmation (compiler.annotate.FieldLabelAnnotator).
        label_evidence: dict[str, Any] = {}
        if pt.get("label"):
            label_evidence["field_label"] = pt["label"]
        if pt["secret"]:
            rect = pt.get("rect") or None
            redact = tuple(rect) if rect and rect[2] and rect[3] else None
            self.recorder.record_observed(
                {"kind": "type", **label_evidence},
                before_png=self._last_frame,
                structural_before=structural_before,
                param=field or "secret",
                secret=True,
                redact_region=redact,
                after_png=after_png,
                structural_after=structural_after,
            )
        elif field and field in self._param_fields:
            self.recorder.record_observed(
                {"kind": "type", "text": pt["value"], **label_evidence},
                before_png=self._last_frame,
                structural_before=structural_before,
                param=field,
                after_png=after_png,
                structural_after=structural_after,
            )
        else:
            # Non-secret, unparameterized: recorded as a literal (replayed
            # verbatim), matching the demo driver's username/note handling.
            # The field rect rides along so the compiler's nearby-OCR label
            # fallback has a place to look when no DOM label exists.
            rect = pt.get("rect") or None
            if rect and rect[2] and rect[3]:
                label_evidence["field_rect"] = [int(v) for v in rect]
            self.recorder.record_observed(
                {"kind": "type", "text": pt["value"], **label_evidence},
                before_png=self._last_frame,
                structural_before=structural_before,
                after_png=after_png,
                structural_after=structural_after,
            )
        self._set_last(after_png, structural_after)

    def _accumulate_scroll(self, ev: dict[str, Any]) -> None:
        if self._pending_scroll is None:
            self._pending_scroll = {
                "dx": 0,
                "dy": 0,
                "structural_before": dict(self._last_structural),
            }
        ps = self._pending_scroll
        ps["dx"] += int(ev.get("dx", 0))
        ps["dy"] += int(ev.get("dy", 0))
        # Post-scroll after-state, captured now (before any following action).
        assert self.backend is not None
        ps["after_frame"] = self.backend.screenshot()
        ps["structural_after"] = self._structural_state()

    def _flush_scroll(self) -> None:
        ps = self._pending_scroll
        self._pending_scroll = None
        if ps is None or (ps["dx"] == 0 and ps["dy"] == 0):
            return
        assert self.recorder is not None
        after_png = ps.get("after_frame")
        structural_after = ps.get("structural_after")
        self.recorder.record_observed(
            {"kind": "scroll", "dx": ps["dx"], "dy": ps["dy"]},
            before_png=self._last_frame,
            structural_before=ps.get("structural_before", self._last_structural),
            after_png=after_png,
            structural_after=structural_after,
        )
        self._set_last(after_png, structural_after)

    def _record_pointer(self, ev: dict[str, Any]) -> None:
        assert self.recorder is not None
        kind = str(ev["kind"])
        event: dict[str, Any] = {
            "kind": kind,
            "x": int(ev["x"]),
            "y": int(ev["y"]),
        }
        if kind == "drag":
            event.update(end_x=int(ev["end_x"]), end_y=int(ev["end_y"]))
            if ev.get("end_structural"):
                event["drag_end_structural"] = ev["end_structural"]
        if ev.get("structural"):
            event["structural"] = ev["structural"]
        # Marked identifier field rect (--identifier FIELD), captured in-page
        # at click time: the compiler crops these pixels
        # (anchor.identifier_crop) to arm the pixel identity tier.
        idr = ev.get("idr")
        if idr:
            event["identifier_region"] = [int(v) for v in idr]
        self.recorder.record_observed(
            event,
            before_png=self._last_frame,
            structural_before=self._last_structural,
            structured_identity=ev.get("sid"),
        )
        self._advance()

    def _record_key(self, ev: dict[str, Any]) -> None:
        assert self.recorder is not None
        event: dict[str, Any] = {"kind": ev["kind"], "key": ev["key"]}
        if ev["kind"] == "hotkey":
            event["modifiers"] = [str(value) for value in ev.get("modifiers", [])]
        self.recorder.record_observed(
            event,
            before_png=self._last_frame,
            structural_before=self._last_structural,
        )
        self._advance()

    # -- internals -----------------------------------------------------------

    def _read_attached_geometry(self) -> tuple[int, int, float]:
        """Read the selected tab's origin, CSS viewport, and monitor scale."""

        assert not self._owns_browser
        assert self.page is not None
        try:
            current_origin = _http_origin(
                str(self.page.url),
                label="the selected browser tab URL",
            )
        except Exception as exc:
            raise BrowserAttachError(
                "the selected browser tab left the declared application origin; "
                "recording was refused"
            ) from exc
        if current_origin != self._attached_origin:
            raise BrowserAttachError(
                "the selected browser tab left the declared application origin; "
                "recording was refused"
            )
        try:
            raw = self.page.evaluate(
                """() => ({
                  width: window.innerWidth,
                  height: window.innerHeight,
                  dpr: window.devicePixelRatio || 1,
                })"""
            )
            geometry = (
                int(raw["width"]),
                int(raw["height"]),
                round(float(raw["dpr"]), 6),
            )
        except Exception as exc:
            raise BrowserAttachError(
                "the attached tab geometry could not be read; recording was refused"
            ) from exc
        if geometry[0] <= 0 or geometry[1] <= 0 or not 0.1 <= geometry[2] <= 16.0:
            raise BrowserAttachError(
                "the attached tab reported invalid viewport or monitor-scale "
                "geometry; recording was refused"
            )
        return geometry

    def _rebaseline_attached_viewport(self) -> None:
        """Resume after an idle resize with a fresh exact CSS-pixel baseline."""

        assert not self._owns_browser
        assert self.recorder is not None
        # A deferred input or scroll already has its exact old-space after
        # frame. Persist it before the new coordinate space becomes current.
        self._flush_type()
        self._flush_scroll()
        for _attempt in range(3):
            before = self._read_attached_geometry()
            frame = self.recorder._wait_settled()
            after = self._read_attached_geometry()
            if self._pyq or self._listener_error is not None:
                raise BrowserAttachError(
                    "an action occurred before the resized browser viewport was "
                    "rebound to a fresh frame; recording stopped without "
                    "complete metadata"
                )
            with Image.open(io.BytesIO(frame)) as image:
                frame_size = image.size
            if before == after and frame_size == after[:2]:
                self._attached_geometry = after
                self._last_frame = frame
                self._last_structural = self._structural_state()
                self._viewport_dirty = False
                viewport = after[:2]
                if self._initial_attached_viewport is None:
                    self._initial_attached_viewport = viewport
                entry = {
                    "before_event": self.recorder.event_count,
                    "viewport": list(viewport),
                    "device_scale_factor": after[2],
                }
                if (
                    self._viewport_history
                    and self._viewport_history[-1]["before_event"]
                    == entry["before_event"]
                ):
                    self._viewport_history[-1] = entry
                elif not self._viewport_history or (
                    self._viewport_history[-1]["viewport"] != entry["viewport"]
                    or self._viewport_history[-1]["device_scale_factor"]
                    != entry["device_scale_factor"]
                ):
                    self._viewport_history.append(entry)
                return
        raise BrowserAttachError(
            "the attached browser viewport did not settle long enough to bind "
            "a new exact frame and coordinate space"
        )

    def _advance(self) -> None:
        """After an IMMEDIATE step (click/key), the current settled frame
        becomes the next step's BEFORE frame."""
        assert self.backend is not None
        self._last_frame = self.backend.screenshot()
        self._last_structural = self._structural_state()

    def _set_last(
        self, after_png: Optional[bytes], structural_after: Optional[dict]
    ) -> None:
        """After a DEFERRED/coalesced step (type/scroll), the next step's
        BEFORE frame is the after-state captured when the step actually
        happened — NOT a live screenshot, which a later navigating action may
        already have moved on from."""
        if after_png is not None:
            self._last_frame = after_png
        else:
            self._advance()
            return
        if structural_after is not None:
            self._last_structural = structural_after

    def _structural_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for attr, key in (
            ("url", "url"),
            ("page_title", "title"),
            ("page_count", "pages"),
        ):
            try:
                value = getattr(self.backend, attr, None)
            except Exception:
                value = None
            if value is not None:
                state[key] = value
        if self._system_of_record_reader is not None:
            try:
                records = self._system_of_record_reader()
            except Exception:
                records = None
            if records is not None:
                state["sor"] = records
        return state

    def _stop_condition_reached(self) -> bool:
        if self._stop_when is None:
            return False
        try:
            done = bool(self._stop_when())
        except Exception:
            done = False
        if done:
            self.done = True
        return done


def record_interactive(
    url: str,
    out_dir: Path | str,
    *,
    secret_fields: tuple[str, ...] = (),
    param_fields: tuple[str, ...] = (),
    identifier_fields: tuple[str, ...] = (),
    headless: bool = False,
    cdp_endpoint: Optional[str] = None,
    browser_page_url: Optional[str] = None,
    script: Optional[Callable[[Any, Callable[[], None]], None]] = None,
    system_of_record_reader: Optional[
        Callable[[], Optional[list[dict[str, Any]]]]
    ] = None,
    stop_when: Optional[Callable[[], bool]] = None,
    **kwargs: Any,
) -> Path:
    """Record a live demonstration the user drives against ``url``.

    Args:
        url: The app to record against (the user's own app).
        out_dir: Recording output directory (meta.json + events.jsonl +
            frames/), the exact format ``compile`` consumes.
        secret_fields: Field ``name``/``id`` values to treat as secrets, in
            addition to any ``input[type=password]`` (auto-detected). A
            secret's literal value is never persisted (see module docstring).
        param_fields: Field ``name``/``id`` values recorded as PARAMETERS
            (their demonstrated value becomes the default, overridable at
            replay with ``--param``); all other non-secret typed fields are
            recorded as literals.
        identifier_fields: Field ``name``/``id`` values marking the
            RECORD-IDENTIFYING region (patient banner / MRN field). At each
            click the first marked field present on the page contributes its
            bounding rect to the event (``identifier_region``); the compiler
            crops those pixels (``anchor.identifier_crop``) so the pixel
            identity tier arms — including on a bundle later replayed over a
            remote-display/pixel substrate (Citrix/RDP).
        headless: Run the browser headless (used by scripted/CI recording;
            a human recording is headed).
        cdp_endpoint: Optional local-loopback Chromium DevTools endpoint. When
            set, the recorder attaches to an existing browser and never
            launches, navigates, or closes it.
        browser_page_url: Exact current tab URL used to disambiguate two or
            more open tabs on the declared app origin. Requires
            ``cdp_endpoint``. Query and fragment values are not written to
            recorder diagnostics.
        script: Test hook — ``script(page, pump)`` drives synthetic input and
            pumps the loop; when given, the human wait loop is skipped.
        system_of_record_reader: Optional read-only observation of the
            authoritative records. The recorder retains the observed
            before/after state so compilation can propose effect contracts.
            This observer is recording-only and does not become a runtime
            verifier.
        stop_when: Optional recording completion condition. It is evaluated
            after queued events are persisted and before the browser closes.

    Returns:
        The recording directory.
    """
    session = InteractiveRecorder(
        url,
        out_dir,
        secret_fields=secret_fields,
        param_fields=param_fields,
        identifier_fields=identifier_fields,
        headless=headless,
        cdp_endpoint=cdp_endpoint,
        browser_page_url=browser_page_url,
        system_of_record_reader=system_of_record_reader,
        stop_when=stop_when,
        **kwargs,
    )
    session.start()
    if script is not None:
        return session.run_script(script)
    return session.run()
