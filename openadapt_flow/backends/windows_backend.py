"""Windows backend over the WAA (Windows Agent Arena) HTTP API.

Implements the `openadapt_flow.backend.Backend` protocol against the WAA
Flask server that runs inside the Windows VM (the WAADirect pattern from
openadapt-evals — plain HTTP, no adapter layer):

    GET  /screenshot        -> raw PNG bytes (Flask send_file, NOT base64 JSON)
    POST /execute_windows   -> server does exec(command, ...) with pyautogui
                               importable. The payload is
                               ``{"command": "<bare Python statements>"}`` —
                               NOT wrapped in ``python -c "..."``.

The backend is vision-only by construction for RESOLUTION: PNG frames in,
pixel-coordinate input out. It deliberately does NOT implement the optional
`StructuralBackend` observations (url/title/page count) — native Windows has
no cheap equivalent, so those steps stay honestly unverified (docs/LIMITS.md).

It DOES implement the optional `IdentityBackend.structured_text_at`: identity
verification (unlike resolution) can use a higher-fidelity signal than OCR
where one exists. On native Windows that signal is the UI Automation tree --
the element under a point exposes ``Name``/``Value``/text even when it has no
stable ``AutomationId`` (the Phase-2 "no AutomationId" finding does not block
UIA *text* extraction). The read runs a UIA ``ElementFromPoint`` snippet on the
VM and returns the row-like element's real characters when the WAA execute
channel echoes them back, or None when it cannot (older WAA server that does
not return command output, UIA unavailable, pixel-only session) -- in which
case the identity ladder falls back to the OCR name+DOB-primary tier.

Typed text is embedded into the command via ``repr()`` (a valid Python
literal, immune to quoting bugs). Non-ASCII text cannot be typed by
pyautogui.write (it silently drops unknown characters — a wrong-write mode),
so it is routed through the Windows clipboard instead: the value travels
base64-encoded to PowerShell ``Set-Clipboard`` and is pasted with Ctrl+V.
"""

from __future__ import annotations

import base64
import struct
from typing import Optional

import requests

from openadapt_flow.ir import StructuralHandle, StructuralLocator

DEFAULT_SERVER_URL = "http://localhost:5001"

# Screenshot retry defaults (mirrors WAADirect in openadapt-evals).
SCREENSHOT_MAX_RETRIES = 3
SCREENSHOT_RETRY_DELAY_S = 2.0

# The Backend protocol expresses scroll in pixels (a Playwright wheel
# gesture); pyautogui expresses it in wheel notches. One notch scrolls
# roughly this many pixels in common Windows apps. Approximate by design —
# to be tuned against the real VM (Phase 2); replay's closed-loop scroll
# re-resolves after each gesture, so the exact ratio is not load-bearing.
SCROLL_PIXELS_PER_NOTCH = 100

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Playwright-style modifier names (as recorded / emitted by the replayer,
# e.g. 'ControlOrMeta+a') -> pyautogui key names. On Windows the Meta/Command
# key maps to the Windows key and ControlOrMeta resolves to Ctrl.
_MODIFIER_MAP = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "controlormeta": "ctrl",
    "meta": "win",
    "cmd": "win",
    "command": "win",
    "win": "win",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
}

# Playwright-style named keys -> pyautogui key names.
_NAMED_KEY_MAP = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "esc",
    "esc": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "space": "space",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def _png_size(png: bytes) -> tuple[int, int]:
    """Return (width, height) parsed from a PNG's IHDR chunk.

    Args:
        png: PNG file bytes.

    Returns:
        (width, height) in pixels.

    Raises:
        ValueError: If the bytes are not a PNG.
    """
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG frame")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


def _pyautogui_key(part: str) -> str:
    """Map one Playwright-style key/modifier name to pyautogui's name."""
    lower = part.lower()
    if lower in _MODIFIER_MAP:
        return _MODIFIER_MAP[lower]
    if lower in _NAMED_KEY_MAP:
        return _NAMED_KEY_MAP[lower]
    # Single characters and F-keys: pyautogui uses lowercase names.
    return lower


def normalize_chord(key: str) -> list[str]:
    """Normalize a key or ``+``-joined chord to pyautogui key names.

    Args:
        key: e.g. ``'Enter'``, ``'ControlOrMeta+a'``, ``'Meta+d'``.

    Returns:
        List of pyautogui key names, modifiers first (input order kept).

    Raises:
        ValueError: If the chord is empty.
    """
    parts = [p for p in key.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    return [_pyautogui_key(p) for p in parts]


class WindowsBackend:
    """`Backend` implementation over the WAA HTTP API.

    Args:
        server_url: WAA Flask server base URL (default matches the standard
            SSH tunnel ``localhost:5001`` -> VM ``:5000``).
        viewport: Optional (width, height) override. When omitted it is
            derived once from the first screenshot's PNG header and cached.
        type_interval_s: Inter-key delay for ``pyautogui.write``.
        timeout_s: HTTP timeout per request.
        screenshot_max_retries: Screenshot retry attempts (server may be
            momentarily unready).
        screenshot_retry_delay_s: Delay between screenshot retries.
        session: Optional ``requests.Session`` (injected in tests).
    """

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        *,
        viewport: Optional[tuple[int, int]] = None,
        type_interval_s: float = 0.05,
        timeout_s: float = 30.0,
        screenshot_max_retries: int = SCREENSHOT_MAX_RETRIES,
        screenshot_retry_delay_s: float = SCREENSHOT_RETRY_DELAY_S,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self._viewport = viewport
        self._type_interval_s = type_interval_s
        self._timeout_s = timeout_s
        self._screenshot_max_retries = max(1, int(screenshot_max_retries))
        self._screenshot_retry_delay_s = screenshot_retry_delay_s
        self._session = session if session is not None else requests.Session()

    # -- Backend protocol ----------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the VM screen, derived from a screenshot."""
        if self._viewport is None:
            self._viewport = _png_size(self.screenshot())
        return self._viewport

    def screenshot(self) -> bytes:
        """Return the current frame as PNG bytes (with retries).

        WAA's ``/screenshot`` returns raw PNG via Flask ``send_file()`` —
        read ``resp.content``, never ``resp.json()``.

        Raises:
            RuntimeError: If all attempts fail or return a non-PNG payload.
        """
        import time

        last_error: Optional[Exception] = None
        for attempt in range(1, self._screenshot_max_retries + 1):
            try:
                resp = self._session.get(
                    f"{self.server_url}/screenshot", timeout=self._timeout_s
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"screenshot HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                png = resp.content
                _png_size(png)  # validates signature and header
                return png
            except Exception as e:  # noqa: BLE001 - retried, then re-raised
                last_error = e
                if attempt < self._screenshot_max_retries:
                    time.sleep(self._screenshot_retry_delay_s)
        raise RuntimeError(
            f"screenshot failed after {self._screenshot_max_retries} attempts"
        ) from last_error

    # -- structured-text identity (openadapt_flow.backend.IdentityBackend) --

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the UI Automation text of the element/row under (x, y).

        Identity verification prefers STRUCTURED text over OCR where the
        backend can provide it (see :class:`IdentityBackend`): UIA hands back
        the REAL characters of the control under the point, so the
        same-name/same-DOB glyph-collapse that defeats OCR (an MRN whose only
        difference is an O/0 or l/1 glyph) cannot occur -- the two rows are
        different strings in the a11y tree.

        Runs a ``uiautomation.ControlFromPoint`` snippet on the VM: it walks
        up to the enclosing ROW / list-item control (so identity is judged on
        the whole record row, not one cell), EXCLUDES the clicked control's own
        cell (its label is mutable evidence the ladder heals through -- mirror
        of the OCR band excluding the target's own crop), and prints the
        remaining cells' ``Name`` text between sentinel markers. Returns None when there
        is no row-like ancestor (a standalone control whose own text is a
        mutable, healable label -- identity stays on the OCR / heal path),
        when the WAA server does not echo command output, when UIA is
        unavailable, or when nothing is under the point (never raises) -- the
        identity ladder then falls back to the OCR tier.
        """
        snippet = (
            "import json\n"
            "def _oaflow_structured_text_at(px, py):\n"
            "    try:\n"
            "        import uiautomation as auto\n"
            "    except Exception:\n"
            "        return None\n"
            "    try:\n"
            "        el = auto.ControlFromPoint(px, py)\n"
            "    except Exception:\n"
            "        return None\n"
            "    if el is None:\n"
            "        return None\n"
            "    row = el\n"
            "    found_row = False\n"
            "    for _ in range(6):\n"
            "        try:\n"
            "            ct = row.ControlTypeName\n"
            "        except Exception:\n"
            "            ct = ''\n"
            "        if ct in ('DataItemControl', 'ListItemControl',\n"
            "                  'TreeItemControl', 'TableRowControl'):\n"
            "            found_row = True\n"
            "            break\n"
            "        parent = getattr(row, 'GetParentControl', None)\n"
            "        nxt = parent() if parent else None\n"
            "        if nxt is None:\n"
            "            break\n"
            "        row = nxt\n"
            "    if not found_row:\n"
            "        return None\n"
            "    own = el\n"
            "    for _ in range(6):\n"
            "        p2 = getattr(own, 'GetParentControl', None)\n"
            "        par = p2() if p2 else None\n"
            "        if par is None:\n"
            "            own = None\n"
            "            break\n"
            "        if par is row:\n"
            "            break\n"
            "        own = par\n"
            "    parts = []\n"
            "    got_child = False\n"
            "    try:\n"
            "        for c in row.GetChildren():\n"
            "            if own is not None and c is own:\n"
            "                continue\n"
            "            nm = getattr(c, 'Name', '') or ''\n"
            "            if nm:\n"
            "                parts.append(str(nm))\n"
            "                got_child = True\n"
            "    except Exception:\n"
            "        pass\n"
            "    if not got_child:\n"
            "        v = getattr(row, 'Name', '') or ''\n"
            "        if v:\n"
            "            parts.append(str(v))\n"
            "    text = ' '.join(parts).split()\n"
            "    return ' '.join(text) if text else None\n"
            "print('<<OAFLOW_STRUCTURED>>' + json.dumps("
            f"_oaflow_structured_text_at({int(x)}, {int(y)})) "
            "+ '<<END_OAFLOW_STRUCTURED>>')\n"
        )
        body = self._execute_read(snippet)
        if not body:
            return None
        import json as _json

        start = body.find("<<OAFLOW_STRUCTURED>>")
        end = body.find("<<END_OAFLOW_STRUCTURED>>")
        if start == -1 or end == -1 or end <= start:
            return None
        payload = body[start + len("<<OAFLOW_STRUCTURED>>") : end]
        try:
            value = _json.loads(payload)
        except Exception:
            return None
        if not value:
            return None
        return str(value)

    def _execute_read(self, command: str) -> Optional[str]:
        """POST bare Python to WAA and return the response body if any.

        Unlike :meth:`_execute` (which discards the body), this returns the
        server's textual output so a UIA read can travel back. Tolerant of the
        WAA server's exact response shape (raw stdout, or a JSON envelope with
        an ``output``/``stdout``/``result`` field); returns None on any
        non-200, missing output, or transport failure -- identity then falls
        back to OCR.
        """
        try:
            resp = self._session.post(
                f"{self.server_url}/execute_windows",
                json={"command": command},
                timeout=self._timeout_s,
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        text = resp.text or ""
        if "<<OAFLOW_STRUCTURED>>" in text:
            return text
        try:
            data = resp.json()
        except Exception:
            return text or None
        if isinstance(data, dict):
            for key in ("output", "stdout", "result", "data"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    # -- structural action (openadapt_flow.backend.StructuralActionBackend) --

    def _read_structured_json(self, snippet: str) -> object:
        """Run ``snippet`` on the VM and decode its sentinel-wrapped JSON.

        The snippet must ``print`` its result as
        ``<<OAFLOW_STRUCTURED>>` + json.dumps(value) + `<<END_OAFLOW_STRUCTURED>>``.
        Returns the decoded value (which may itself be None), or None when the
        server echoes nothing, the markers are absent, or decoding fails --
        callers then fall back to the visual ladder.
        """
        body = self._execute_read(snippet)
        if not body:
            return None
        import json as _json

        start = body.find("<<OAFLOW_STRUCTURED>>")
        end = body.find("<<END_OAFLOW_STRUCTURED>>")
        if start == -1 or end == -1 or end <= start:
            return None
        payload = body[start + len("<<OAFLOW_STRUCTURED>>") : end]
        try:
            return _json.loads(payload)
        except Exception:
            return None

    def structural_locator_at(self, x: int, y: int) -> Optional[StructuralLocator]:
        """Return a stable UIA locator for the control under (x, y).

        Runs a ``uiautomation.ControlFromPoint`` snippet on the VM: it climbs to
        the nearest ACTIONABLE control (button / hyperlink / menu-item / ...) and
        reads its ``AutomationId``, ``ControlType`` (mapped to an ARIA-style
        role) and ``Name``. Returns None when there is no actionable control,
        neither an AutomationId nor a usable role+name exists, UIA is
        unavailable, or the WAA server does not echo output (never raises) --
        the step then relies on the visual anchor.
        """
        snippet = (
            "import json\n"
            "def _oaflow_locator_at(px, py):\n"
            "    try:\n"
            "        import uiautomation as auto\n"
            "    except Exception:\n"
            "        return None\n"
            "    try:\n"
            "        el = auto.ControlFromPoint(px, py)\n"
            "    except Exception:\n"
            "        return None\n"
            "    if el is None:\n"
            "        return None\n"
            "    actionable = None\n"
            "    node = el\n"
            "    actionable_types = ('ButtonControl', 'HyperlinkControl',\n"
            "        'MenuItemControl', 'TabItemControl', 'ListItemControl',\n"
            "        'CheckBoxControl', 'RadioButtonControl',\n"
            "        'SplitButtonControl', 'EditControl')\n"
            "    for _ in range(6):\n"
            "        try:\n"
            "            ct = node.ControlTypeName\n"
            "        except Exception:\n"
            "            ct = ''\n"
            "        if ct in actionable_types:\n"
            "            actionable = node\n"
            "            break\n"
            "        p = getattr(node, 'GetParentControl', None)\n"
            "        nxt = p() if p else None\n"
            "        if nxt is None:\n"
            "            break\n"
            "        node = nxt\n"
            "    if actionable is None:\n"
            "        actionable = el\n"
            "    try:\n"
            "        ct = actionable.ControlTypeName\n"
            "    except Exception:\n"
            "        ct = ''\n"
            "    role_map = {'ButtonControl': 'button',\n"
            "        'HyperlinkControl': 'link', 'MenuItemControl': 'menuitem',\n"
            "        'TabItemControl': 'tab', 'ListItemControl': 'listitem',\n"
            "        'CheckBoxControl': 'checkbox',\n"
            "        'RadioButtonControl': 'radio', 'EditControl': 'textbox',\n"
            "        'SplitButtonControl': 'button'}\n"
            "    role = role_map.get(ct)\n"
            "    aid = str(getattr(actionable, 'AutomationId', '') or '')\n"
            "    name = ' '.join(str(getattr(actionable, 'Name', '') or ''"
            ").split())\n"
            "    aid = aid or None\n"
            "    name = name or None\n"
            "    if not aid and not (role and name):\n"
            "        return None\n"
            "    return {'automation_id': aid, 'role': role, 'name': name}\n"
            "print('<<OAFLOW_STRUCTURED>>' + json.dumps("
            f"_oaflow_locator_at({int(x)}, {int(y)})) "
            "+ '<<END_OAFLOW_STRUCTURED>>')\n"
        )
        value = self._read_structured_json(snippet)
        if not isinstance(value, dict):
            return None
        return StructuralLocator(
            automation_id=value.get("automation_id"),
            role=value.get("role"),
            name=value.get("name"),
        )

    def locate_structural(
        self, locator: StructuralLocator
    ) -> Optional[StructuralHandle]:
        """Locate ``locator``'s control via UIA; return its center point.

        Searches the UIA tree from the root by ``AutomationId`` first, else by
        the mapped ``ControlType`` + ``Name``, and returns the matched control's
        ``BoundingRectangle`` center in :meth:`click` coordinate space. Returns
        None on no/ambiguous match, an empty rectangle, unavailable UIA, or no
        echoed output (never raises) -- the resolver then uses the visual ladder.
        """
        aid = locator.automation_id or ""
        role = locator.role or ""
        name = locator.name or ""
        if not aid and not (role and name):
            return None
        snippet = (
            "import json\n"
            "def _oaflow_locate(aid, role, name):\n"
            "    try:\n"
            "        import uiautomation as auto\n"
            "    except Exception:\n"
            "        return None\n"
            "    try:\n"
            "        root = auto.GetRootControl()\n"
            "    except Exception:\n"
            "        return None\n"
            "    ctrl_map = {'button': 'ButtonControl',\n"
            "        'link': 'HyperlinkControl', 'menuitem': 'MenuItemControl',\n"
            "        'tab': 'TabItemControl', 'listitem': 'ListItemControl',\n"
            "        'checkbox': 'CheckBoxControl',\n"
            "        'radio': 'RadioButtonControl', 'textbox': 'EditControl'}\n"
            "    el = None\n"
            "    if aid:\n"
            "        try:\n"
            "            cand = auto.Control(searchFromControl=root,\n"
            "                AutomationId=aid)\n"
            "            if cand.Exists(0, 0):\n"
            "                el = cand\n"
            "        except Exception:\n"
            "            el = None\n"
            "    if el is None and role and name:\n"
            "        cls = getattr(auto, ctrl_map.get(role, ''), None)\n"
            "        if cls is not None:\n"
            "            try:\n"
            "                cand = cls(searchFromControl=root, Name=name)\n"
            "                if cand.Exists(0, 0):\n"
            "                    el = cand\n"
            "            except Exception:\n"
            "                el = None\n"
            "    if el is None:\n"
            "        return None\n"
            "    try:\n"
            "        r = el.BoundingRectangle\n"
            "    except Exception:\n"
            "        return None\n"
            "    if r is None or r.right <= r.left or r.bottom <= r.top:\n"
            "        return None\n"
            "    cx = int((r.left + r.right) / 2)\n"
            "    cy = int((r.top + r.bottom) / 2)\n"
            "    return [cx, cy]\n"
            "print('<<OAFLOW_STRUCTURED>>' + json.dumps(_oaflow_locate("
            f"{aid!r}, {role!r}, {name!r})) + '<<END_OAFLOW_STRUCTURED>>')\n"
        )
        value = self._read_structured_json(snippet)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(v, (int, float)) for v in value)
        ):
            return None
        return StructuralHandle(point=(int(value[0]), int(value[1])))

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at pixel coordinates via pyautogui."""
        fn = "doubleClick" if double else "click"
        self._execute(f"import pyautogui; pyautogui.{fn}({int(x)}, {int(y)})")

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element.

        ASCII text goes through ``pyautogui.write``. Text containing
        characters pyautogui cannot type (it silently drops them — a
        wrong-write failure mode) goes through the Windows clipboard:
        base64 -> PowerShell ``Set-Clipboard`` -> Ctrl+V.
        """
        if not text:
            return
        if text.isascii():
            self._execute(
                "import pyautogui; "
                f"pyautogui.write({text!r}, "
                f"interval={self._type_interval_s})"
            )
            return
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        ps = (
            "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{b64}')))"
        )
        self._execute(
            "\n".join(
                [
                    "import subprocess; import time; import pyautogui",
                    "subprocess.run(['powershell', '-NoProfile', '-Command', "
                    f"{ps!r}], capture_output=True)",
                    "time.sleep(0.2)",
                    "pyautogui.hotkey('ctrl', 'v')",
                ]
            )
        )

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'ControlOrMeta+a'``."""
        keys = normalize_chord(key)
        if len(keys) == 1:
            self._execute(f"import pyautogui; pyautogui.press({keys[0]!r})")
        else:
            args = ", ".join(repr(k) for k in keys)
            self._execute(f"import pyautogui; pyautogui.hotkey({args})")

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture (Backend convention: positive ``dy``
        scrolls content up / view down).

        pyautogui's convention is inverted for vertical scrolling (positive
        = view up), so ``dy`` changes sign; horizontal signs agree.
        """
        parts: list[str] = ["import pyautogui"]
        v = self._notches(dy)
        h = self._notches(dx)
        if v:
            parts.append(f"pyautogui.scroll({-v})")
        if h:
            parts.append(f"pyautogui.hscroll({h})")
        if len(parts) == 1:
            return
        self._execute("; ".join(parts))

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _notches(pixels: int) -> int:
        """Convert a pixel delta to wheel notches (>=1 notch when nonzero)."""
        if pixels == 0:
            return 0
        notches = round(abs(pixels) / SCROLL_PIXELS_PER_NOTCH)
        return max(1, notches) * (1 if pixels > 0 else -1)

    def _execute(self, command: str) -> None:
        """POST bare Python statements to WAA's ``/execute_windows``.

        Raises:
            RuntimeError: On a non-200 response.
        """
        resp = self._session.post(
            f"{self.server_url}/execute_windows",
            json={"command": command},
            timeout=self._timeout_s,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"execute_windows HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def probe(self) -> bool:
        """True if the WAA server answers with a valid PNG screenshot."""
        try:
            self.screenshot()
            return True
        except RuntimeError:
            return False
