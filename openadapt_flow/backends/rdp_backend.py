"""FreeRDP / RDP backend: drive a pixel-only remote desktop over RDP.

The L1/Retinology wedge (docs/L1_INTEGRATION.md) reaches a legacy
ophthalmology EMR over **RDP**, read **pixel-only** — there is no accessibility
tree, no DOM, no structured layer of any kind. That is exactly the substrate
the vision-only runtime was built for: PNG frames in, pixel-coordinate clicks
and keys out. So RDP is *an adapter, not a rewrite* — this module implements
the :class:`openadapt_flow.backend.Backend` protocol on top of a small,
swappable RDP transport.

Design (two layers, so the adapter is CI-testable without a live RDP server
and the RDP library stays replaceable):

* :class:`RDPTransport` — a minimal, honest protocol for *any* RDP client:
  ``connect`` / ``disconnect`` / ``framebuffer`` / ``pointer`` / ``key`` /
  ``wheel``. Nothing flow-specific leaks in.
* :class:`FreeRDPBackend` — implements the flow ``Backend`` protocol in terms
  of an ``RDPTransport``: ``screenshot`` PNG-encodes the framebuffer,
  ``viewport`` reports its size, ``click`` sends a pointer down/up (double =
  the sequence twice), ``type_text`` sends per-character key down/up,
  ``press`` decomposes a key/chord into ordered key down/up events, ``scroll``
  sends a wheel gesture.
* :class:`AardwolfTransport` — a real transport over the pure-Python async
  ``aardwolf`` RDP client, bridged to the sync ``RDPTransport`` API with a
  dedicated event-loop thread. Lazily imported and gated behind the optional
  ``rdp`` extra (``pip install 'openadapt-flow[rdp]'``); importing this module
  never imports aardwolf.

Coordinate space: the backend works entirely in **framebuffer pixels** — the
same pixels the resolver emits and the same pixels :meth:`screenshot` encodes,
because both come from :meth:`RDPTransport.framebuffer`. A transport that
downsamples the remote desktop MUST report the downsampled ``(width, height)``
from ``framebuffer`` so screenshot pixels and click pixels stay in one space;
no scaling then happens here (see :class:`AardwolfTransport`, which is 1:1).

Identity note: RDP is a **pure-pixel substrate** — there is no structured
(DOM / a11y) text to read, so this backend deliberately does NOT implement the
optional ``IdentityBackend.structured_text_at`` (nor the ``StructuralBackend``
url/title/page-count observations). Claiming a capability it cannot honor would
be a lie; identity instead falls back to the OCR name+DOB-primary tier exactly
as documented for pixel-only substrates (openadapt_flow/backend.py,
docs/LIMITS.md). This mirrors how WindowsBackend omits StructuralBackend.
"""

from __future__ import annotations

import io
from typing import Optional, Protocol, Union, runtime_checkable

from PIL import Image

# What a transport may hand back as the current frame: a PIL image, or raw
# pixel bytes (RGB or RGBA, row-major) that the backend wraps with the
# reported (width, height).
Framebuffer = tuple[Union["Image.Image", bytes], int, int]


@runtime_checkable
class RDPTransport(Protocol):
    """Minimal transport an RDP client must expose to back a flow backend.

    Deliberately tiny and honest: a single framebuffer read plus three input
    primitives. Anything that can connect to a remote desktop, hand back the
    current frame, and inject pointer/key/wheel events can be a transport —
    the real :class:`AardwolfTransport`, or a scripted fake in tests.

    All methods are SYNCHRONOUS. An async RDP client (aardwolf) is bridged to
    this API behind a dedicated event-loop thread (see
    :class:`AardwolfTransport`).
    """

    def connect(self) -> None:
        """Establish the RDP session (blocks until a frame is available)."""
        ...

    def disconnect(self) -> None:
        """Tear the RDP session down (idempotent; never raises)."""
        ...

    def framebuffer(self) -> Framebuffer:
        """Return ``(frame, width, height)`` for the current desktop.

        ``frame`` is either a PIL ``Image`` or raw RGB/RGBA bytes; ``width``
        and ``height`` are the pixel dimensions of THAT frame (already
        downsampled if the transport downsamples), so they define the single
        coordinate space the backend clicks in.
        """
        ...

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        """Send a pointer button transition at framebuffer pixel (x, y).

        ``button`` is ``'left'`` / ``'right'`` / ``'middle'``; ``down`` is the
        press (True) or release (False) edge.
        """
        ...

    def key(self, keysym_or_char: str, down: bool) -> None:
        """Send a key transition.

        ``keysym_or_char`` is either a single character to type (``'a'``,
        ``'1'``, ``'!'``) or a normalized key name (``'enter'``, ``'tab'``,
        ``'ctrl'``, ``'shift'``, ``'up'`` ...); ``down`` is press/release.
        """
        ...

    def wheel(self, dx: int, dy: int) -> None:
        """Send a wheel gesture by ``(dx, dy)`` framebuffer pixels (Backend
        convention: positive ``dy`` scrolls content up / view down)."""
        ...


# -- key normalization (shared by the backend; understood by every transport) --

# Playwright/recorder-style modifier names -> normalized transport tokens.
_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "controlormeta": "ctrl",  # on a Windows RDP target ControlOrMeta = Ctrl
    "meta": "meta",
    "cmd": "meta",
    "command": "meta",
    "win": "meta",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
}

# Playwright/recorder-style named keys -> normalized transport tokens.
_NAMED_KEYS = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
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


def normalize_key_part(part: str) -> str:
    """Normalize one key/modifier name to a transport token.

    Modifiers and named keys are canonicalized (``'Meta'`` -> ``'meta'``,
    ``'ArrowDown'`` -> ``'down'``); a single character passes through
    unchanged so its case (which encodes Shift for letters) is preserved.
    """
    lower = part.lower()
    if lower in _MODIFIER_ALIASES:
        return _MODIFIER_ALIASES[lower]
    if lower in _NAMED_KEYS:
        return _NAMED_KEYS[lower]
    return part


def normalize_chord(key: str) -> list[str]:
    """Split a key or ``+``-joined chord into normalized transport tokens.

    Raises:
        ValueError: If the chord is empty.
    """
    parts = [normalize_key_part(p) for p in key.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    return parts


class FreeRDPBackend:
    """`Backend` implementation over an :class:`RDPTransport`.

    Args:
        transport: The RDP transport to drive (real or fake).
        viewport: Optional ``(width, height)`` override; when omitted it is
            derived once from the first framebuffer and cached.
        connect: When True (default) connect the transport on construction.
            Pass False if the caller manages the transport lifecycle.
    """

    def __init__(
        self,
        transport: RDPTransport,
        *,
        viewport: Optional[tuple[int, int]] = None,
        connect: bool = True,
    ) -> None:
        self._transport = transport
        self._viewport = viewport
        if connect:
            self._transport.connect()

    # -- Backend protocol ----------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the remote desktop, from the framebuffer."""
        if self._viewport is None:
            _, w, h = self._transport.framebuffer()
            self._viewport = (int(w), int(h))
        return self._viewport

    def screenshot(self) -> bytes:
        """Return the current remote frame as PNG bytes.

        The frame comes straight from :meth:`RDPTransport.framebuffer`; a PIL
        image is PNG-encoded directly, raw bytes are wrapped with the reported
        ``(width, height)`` (RGB or RGBA inferred from the byte length) and
        then encoded. The PNG's pixels are the exact coordinate space
        :meth:`click` expects.

        Raises:
            RuntimeError: If the transport hands back nothing usable.
        """
        frame, w, h = self._transport.framebuffer()
        img = self._to_image(frame, int(w), int(h))
        # Cache the viewport from the frame we actually encoded, so viewport
        # and screenshot can never disagree.
        self._viewport = img.size
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at framebuffer pixel coordinates.

        A single click is a pointer down then up at (x, y); a double click is
        that press/release sequence sent twice.
        """
        presses = 2 if double else 1
        for _ in range(presses):
            self._transport.pointer(int(x), int(y), "left", True)
            self._transport.pointer(int(x), int(y), "left", False)

    def type_text(self, text: str) -> None:
        """Type text into the focused control, one key down/up per character."""
        for ch in text:
            self._transport.key(ch, True)
            self._transport.key(ch, False)

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'ControlOrMeta+a'``.

        A chord is pressed by sending every part down in order, then releasing
        every part in reverse order — the natural nesting a human produces
        (modifiers wrap the key). A single key is a plain down/up.
        """
        parts = normalize_chord(key)
        for part in parts:
            self._transport.key(part, True)
        for part in reversed(parts):
            self._transport.key(part, False)

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture by ``(dx, dy)`` pixels."""
        if dx == 0 and dy == 0:
            return
        self._transport.wheel(int(dx), int(dy))

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Disconnect the underlying transport (idempotent)."""
        self._transport.disconnect()

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _to_image(frame: Union["Image.Image", bytes], w: int, h: int) -> Image.Image:
        """Coerce a transport framebuffer to a PIL image of size (w, h)."""
        if isinstance(frame, Image.Image):
            return frame
        if isinstance(frame, (bytes, bytearray)):
            data = bytes(frame)
            if w <= 0 or h <= 0:
                raise RuntimeError("framebuffer reported non-positive size")
            pixels = w * h
            if pixels and len(data) % pixels == 0:
                channels = len(data) // pixels
                mode = {3: "RGB", 4: "RGBA"}.get(channels)
                if mode is not None:
                    return Image.frombytes(mode, (w, h), data)
            raise RuntimeError(
                f"cannot interpret {len(data)} framebuffer bytes as {w}x{h} RGB/RGBA"
            )
        raise RuntimeError(f"unsupported framebuffer type: {type(frame)!r}")


# =============================================================================
# Real transport over aardwolf (lazy; behind the optional `rdp` extra).
# =============================================================================

# aardwolf virtual-key names for our normalized key tokens. Anything not here
# (a single printable character) is typed via aardwolf's send_key_char. The
# bool is is_extended (the "grey" keys — arrows, nav cluster, right-hand
# modifiers — that carry the extended-scancode flag over the RDP wire).
_AARDWOLF_VK = {
    "enter": ("VK_RETURN", False),
    "tab": ("VK_TAB", False),
    "escape": ("VK_ESCAPE", False),
    "backspace": ("VK_BACK", False),
    "delete": ("VK_DELETE", True),
    "space": ("VK_SPACE", False),
    "home": ("VK_HOME", True),
    "end": ("VK_END", True),
    "pageup": ("VK_PRIOR", True),
    "pagedown": ("VK_NEXT", True),
    "up": ("VK_UP", True),
    "down": ("VK_DOWN", True),
    "left": ("VK_LEFT", True),
    "right": ("VK_RIGHT", True),
    "ctrl": ("VK_LCONTROL", False),
    "shift": ("VK_LSHIFT", False),
    "alt": ("VK_LMENU", False),
    "meta": ("VK_LWIN", True),
}


class AardwolfTransport:
    """`RDPTransport` over the async ``aardwolf`` pure-Python RDP client.

    aardwolf is fully asynchronous; this class owns a private asyncio event
    loop on a daemon thread and marshals every operation onto it, presenting
    the synchronous :class:`RDPTransport` API the backend expects. The video
    out format is PIL at 1:1 (no downsampling), so framebuffer pixels equal
    remote-desktop pixels equal click pixels.

    Construct with :meth:`from_credentials` (host/user/pass) or pass a full
    aardwolf connection URL
    (``rdp+ntlm-password://DOMAIN\\user:password@host:port``).

    Args:
        url: aardwolf RDP connection URL.
        width: Requested remote desktop width (framebuffer width).
        height: Requested remote desktop height (framebuffer height).
        connect_timeout_s: Seconds to wait for the session + first frame.
        op_timeout_s: Per-operation timeout for input/framebuffer calls.
    """

    def __init__(
        self,
        url: str,
        *,
        width: int = 1280,
        height: int = 800,
        connect_timeout_s: float = 30.0,
        op_timeout_s: float = 10.0,
    ) -> None:
        self._url = url
        self._width = int(width)
        self._height = int(height)
        self._connect_timeout_s = connect_timeout_s
        self._op_timeout_s = op_timeout_s
        self._loop = None
        self._thread = None
        self._conn = None

    @classmethod
    def from_credentials(
        cls,
        host: str,
        username: str,
        password: str,
        *,
        domain: Optional[str] = None,
        port: int = 3389,
        auth: str = "ntlm-password",
        **kwargs: object,
    ) -> "AardwolfTransport":
        """Build a transport from plain host/user/password (the common case).

        Args:
            host: RDP host/IP.
            username: Account name.
            password: Account password.
            domain: Optional Windows domain (omit for local accounts).
            port: RDP port (default 3389).
            auth: aardwolf auth scheme (default ``ntlm-password``).
            **kwargs: Forwarded to :meth:`__init__` (width/height/timeouts).
        """
        from urllib.parse import quote

        user = f"{domain}\\{username}" if domain else username
        userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
        url = f"rdp+{auth}://{userinfo}@{host}:{port}"
        return cls(url, **kwargs)  # type: ignore[arg-type]

    # -- event-loop bridge ---------------------------------------------------

    def _ensure_loop(self) -> None:
        import asyncio
        import threading

        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="aardwolf-rdp"
        )
        self._thread.start()

    def _run(self, coro, timeout: float):
        import asyncio

        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    # -- RDPTransport --------------------------------------------------------

    def connect(self) -> None:
        """Open the RDP session and block until the first frame arrives."""
        import asyncio

        from aardwolf.commons.factory import RDPConnectionFactory
        from aardwolf.commons.iosettings import RDPIOSettings
        from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

        self._ensure_loop()

        iosettings = RDPIOSettings()
        iosettings.video_width = self._width
        iosettings.video_height = self._height
        iosettings.video_out_format = VIDEO_FORMAT.PIL
        iosettings.video_bpp_min = 15
        iosettings.video_bpp_max = 32

        async def _connect():
            factory = RDPConnectionFactory.from_url(self._url, iosettings)
            conn = factory.get_connection(iosettings)
            _, err = await conn.connect()
            if err is not None:
                raise err
            # Drain the out-queue until the desktop buffer has real pixels, so
            # the first framebuffer() is not a blank canvas.
            deadline = asyncio.get_event_loop().time() + self._connect_timeout_s
            while not getattr(conn, "desktop_buffer_has_data", False):
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("no RDP frame within connect timeout")
                try:
                    await asyncio.wait_for(conn.ext_out_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            return conn

        self._conn = self._run(_connect(), self._connect_timeout_s + 5.0)

    def disconnect(self) -> None:
        """Terminate the session and stop the event loop (idempotent)."""
        conn, loop = self._conn, self._loop
        self._conn = None
        if conn is not None and loop is not None:
            try:
                self._run(conn.terminate(), self._op_timeout_s)
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        self._loop = None
        self._thread = None

    def framebuffer(self) -> Framebuffer:
        """Return ``(PIL image, width, height)`` for the current desktop."""
        from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

        if self._conn is None:
            raise RuntimeError("transport not connected")
        img = self._conn.get_desktop_buffer(VIDEO_FORMAT.PIL)
        if not isinstance(img, Image.Image):
            raise RuntimeError("aardwolf returned no desktop buffer")
        return img, img.width, img.height

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON

        btn = {
            "left": MOUSEBUTTON.MOUSEBUTTON_LEFT,
            "right": MOUSEBUTTON.MOUSEBUTTON_RIGHT,
            "middle": MOUSEBUTTON.MOUSEBUTTON_MIDDLE,
        }.get(button, MOUSEBUTTON.MOUSEBUTTON_LEFT)
        if self._conn is None:
            raise RuntimeError("transport not connected")
        self._run(
            self._conn.send_mouse(btn, int(x), int(y), bool(down)),
            self._op_timeout_s,
        )

    def key(self, keysym_or_char: str, down: bool) -> None:
        if self._conn is None:
            raise RuntimeError("transport not connected")
        vk = _AARDWOLF_VK.get(keysym_or_char)
        if vk is not None:
            vk_name, is_extended = vk
            self._run(
                self._conn.send_key_virtualkey(vk_name, bool(down), is_extended),
                self._op_timeout_s,
            )
            return
        # A single printable character: let aardwolf resolve scancode+shift.
        for ch in keysym_or_char:
            self._run(
                self._conn.send_key_char(ch, bool(down)),
                self._op_timeout_s,
            )

    def wheel(self, dx: int, dy: int) -> None:
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON

        if self._conn is None:
            raise RuntimeError("transport not connected")
        # aardwolf wheel "steps" are wheel-delta units; approximate one notch
        # (~120 units) per ~100 px, matching WindowsBackend's notch ratio. The
        # replayer re-resolves after each scroll, so the exact ratio is not
        # load-bearing.
        if dy:
            btn = (
                MOUSEBUTTON.MOUSEBUTTON_WHEEL_DOWN
                if dy > 0
                else MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP
            )
            steps = max(1, round(abs(dy) / 100)) * 120
            self._run(
                self._conn.send_mouse(btn, 0, 0, False, steps),
                self._op_timeout_s,
            )
