"""Multi-source input adapters -> the common event-log format.

Routine discovery (:mod:`openadapt_flow.mining.routine_discovery`) consumes a
flat ``list[dict]`` in the recorder's ``events.jsonl`` schema. Demonstrations are
only ONE way to obtain such a log; the review's thesis is that PASSIVE capture
across many workers (agent runs, existing automation, telemetry) may be a
stronger source than asking someone to record a demo. These adapters prove
"multi-source" by converting two NON-demo sources into the SAME event-log format,
so the identical discovery pipeline mines all of them:

* :func:`from_agent_trajectory` — a computer-use agent's action log (Anthropic
  ``computer`` tool style) → events. Passively logging what agents already do is
  a zero-extra-effort routine source.
* :func:`from_playwright_script` — an existing RPA script (Playwright-codegen or
  Selenium) → events. Organizations already have automation scripts; each encodes
  a routine's control flow.

Design mirrors the openadapt-capture adapter (``openadapt_flow.adapters.capture``):
map each recognized input action to a flow event, and LOUDLY reject an actuating
action with no flow equivalent rather than silently dropping it (a dropped action
corrupts the mined control flow). Observational / non-actuating operations
(screenshots, cursor queries, navigation, waits, asserts) are skipped explicitly.

No model calls; pure parsing.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Sequence

# Common event-log format: a UI action dict in the events.jsonl schema.
Event = dict[str, Any]

# Key-name normalization to the flow/Playwright vocabulary (shared shape with
# openadapt_flow.adapters.capture._KEY_NAME_MAP, spelled for these sources).
_KEY_NAME_MAP = {
    "return": "Enter",
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": " ",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "pageup": "PageUp",
    "page_down": "PageDown",
    "pagedown": "PageDown",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
}


def _map_key(name: str) -> str:
    """Normalize a key name to the flow vocabulary, or raise if unmapped."""
    key = name.strip().strip("'\"")
    # Selenium Keys.RETURN / pynput-style names / bare characters.
    tail = key.rsplit(".", 1)[-1].lower()
    mapped = _KEY_NAME_MAP.get(tail)
    if mapped is not None:
        return mapped
    if len(key) == 1:  # a literal single character key press
        return key
    raise ValueError(f"unmapped key {name!r}; extend _KEY_NAME_MAP")


# -- (a) computer-use agent trajectory --------------------------------------

# Agent action names with NO workflow meaning on their own (observation / motion
# only). Skipped, not converted — they carry no control-flow step.
_AGENT_SKIP_ACTIONS = {
    "screenshot",
    "cursor_position",
    "mouse_move",
    "wait",
    "hold_key",
}
# Actuating agent actions with no flow equivalent — rejected loudly.
_AGENT_REJECT_ACTIONS = {
    "left_click_drag",
    "right_click",
    "middle_click",
    "triple_click",
}


def from_agent_trajectory(actions: Sequence[dict[str, Any]]) -> list[Event]:
    """Convert a computer-use agent action log into a flow event log.

    Args:
        actions: Ordered agent actions (Anthropic ``computer`` tool style). Each
            is a dict with an ``"action"`` name and action-specific fields:

            * ``left_click`` / ``double_click`` — ``"coordinate": [x, y]`` (and an
              optional ``"selector"`` / ``"target"`` the agent may have logged,
              carried through as a structural locator for stronger mining);
            * ``type`` — ``"text": str`` (optionally ``"param": name``);
            * ``key`` — ``"text": "Return"`` (a key name; chords unsupported);
            * ``scroll`` — ``"coordinate"``, ``"scroll_direction"``,
              ``"scroll_amount"``.

    Returns:
        Events in the common event-log format, in order.

    Raises:
        ValueError: On an actuating action with no flow equivalent (drag,
            right/middle/triple click) or an unknown action name — converting it
            would silently corrupt the mined control flow.
    """
    events: list[Event] = []
    for idx, action in enumerate(actions):
        name = str(action.get("action", "")).strip()
        if not name:
            raise ValueError(f"agent action #{idx} has no 'action' name")
        if name in _AGENT_SKIP_ACTIONS:
            continue
        if name in _AGENT_REJECT_ACTIONS:
            raise ValueError(
                f"agent action {name!r} (#{idx}) has no flow equivalent; "
                "converting would silently drop an action"
            )
        if name in ("left_click", "double_click", "click"):
            kind = "double_click" if name == "double_click" else "click"
            event: Event = {"kind": kind}
            coord = action.get("coordinate")
            if coord is not None:
                event["x"], event["y"] = int(coord[0]), int(coord[1])
            _attach_structural(event, action)
            events.append(event)
        elif name == "type":
            event = {"kind": "type", "text": str(action.get("text", ""))}
            if action.get("param"):
                event["param"] = str(action["param"])
            events.append(event)
        elif name == "key":
            events.append({"kind": "key", "key": _map_key(str(action.get("text", "")))})
        elif name == "scroll":
            direction = str(action.get("scroll_direction", "down")).lower()
            amount = int(action.get("scroll_amount", 1) or 1)
            magnitude = amount * 100
            dx, dy = 0, 0
            if direction in ("down", "up"):
                dy = magnitude if direction == "down" else -magnitude
            elif direction in ("right", "left"):
                dx = magnitude if direction == "right" else -magnitude
            else:
                raise ValueError(
                    f"scroll direction {direction!r} (#{idx}) is not recognized"
                )
            events.append({"kind": "scroll", "dx": dx, "dy": dy})
        else:
            raise ValueError(
                f"unknown agent action {name!r} (#{idx}); extend the adapter "
                "before converting"
            )
    return events


def _attach_structural(event: Event, action: dict[str, Any]) -> None:
    """Attach a structural locator to a click event when the agent logged one."""
    target = action.get("selector") or action.get("target")
    if isinstance(target, str) and target:
        event["structural"] = {"selector": target}
    elif isinstance(action.get("structural"), dict):
        event["structural"] = dict(action["structural"])


# -- (b) existing RPA / Playwright-codegen script ---------------------------

# Recognized Playwright / Selenium actuation call patterns. Each maps a source
# line to a flow event; a line containing an actuation verb that matches NONE of
# these is rejected (loud), while non-actuation lines (imports, goto, waits,
# expects, comments) are skipped.
_RE_LOCATOR_CLICK = re.compile(
    r"""\.locator\(\s*(?P<q>['"])(?P<sel>.+?)(?P=q)\s*\)"""
    r"""\s*\.(?P<verb>dblclick|click)\("""
)
_RE_PAGE_CLICK = re.compile(
    r"""\.(?P<verb>dblclick|click)\(\s*(?P<q>['"])(?P<sel>.+?)(?P=q)\s*\)"""
)
_RE_ROLE_CLICK = re.compile(
    r"""\.get_by_role\(\s*(?P<q1>['"])(?P<role>.+?)(?P=q1)\s*"""
    r"""(?:,\s*name\s*=\s*(?P<q2>['"])(?P<name>.+?)(?P=q2)\s*)?\)"""
    r"""\s*\.(?P<verb>dblclick|click)\("""
)
_RE_LOCATOR_FILL = re.compile(
    r"""\.locator\(\s*(?P<q>['"])(?P<sel>.+?)(?P=q)\s*\)\s*\.fill\(\s*"""
    r"""(?P<q2>['"])(?P<val>.*?)(?P=q2)\s*\)"""
)
_RE_PAGE_FILL = re.compile(
    r"""\.fill\(\s*(?P<q>['"])(?P<sel>.+?)(?P=q)\s*,\s*"""
    r"""(?P<q2>['"])(?P<val>.*?)(?P=q2)\s*\)"""
)
_RE_LOCATOR_PRESS = re.compile(
    r"""\.locator\(\s*(?P<q>['"])(?P<sel>.+?)(?P=q)\s*\)\s*\.press\(\s*"""
    r"""(?P<q2>['"])(?P<key>.+?)(?P=q2)\s*\)"""
)
_RE_PAGE_PRESS = re.compile(
    r"""\.press\(\s*(?P<q>['"])(?P<sel>.+?)(?P=q)\s*,\s*"""
    r"""(?P<q2>['"])(?P<key>.+?)(?P=q2)\s*\)"""
)
_RE_KEYBOARD_PRESS = re.compile(
    r"""\.keyboard\.press\(\s*(?P<q>['"])(?P<key>.+?)(?P=q)\s*\)"""
)
_RE_WHEEL = re.compile(
    r"""\.mouse\.wheel\(\s*(?P<dx>-?\d+)\s*,\s*(?P<dy>-?\d+)\s*\)"""
)
# Selenium: driver.find_element(By.CSS_SELECTOR, "sel").click()/.send_keys(...)
_RE_SELENIUM = re.compile(
    r"""find_element\(\s*By\.(?P<by>\w+)\s*,\s*(?P<q>['"])(?P<sel>.+?)(?P=q)\s*\)"""
    r"""\s*\.(?P<verb>click|send_keys)\((?P<args>.*)\)"""
)

# Any line invoking one of these verbs MUST match a recognized pattern; if it
# does not, it is an actuation the adapter cannot represent -> reject loudly.
_ACTUATION_VERBS = re.compile(
    r"\.(?:click|dblclick|fill|press|send_keys|wheel|type)\("
)
_BY_TO_SELECTOR = {
    "ID": lambda v: f"#{v}",
    "CSS_SELECTOR": lambda v: v,
    "NAME": lambda v: f"[name={v!r}]",
    "CLASS_NAME": lambda v: f".{v}",
}


def from_playwright_script(
    script: str, *, params: dict[str, str] | None = None
) -> list[Event]:
    """Convert an RPA script (Playwright-codegen / Selenium) into a flow log.

    Recognized actuations become flow events carrying a structural ``selector``
    (or ``role``/``name``), so the script's control flow mines cleanly against
    demonstration and agent logs. ``fill`` becomes a focus ``click`` on the field
    followed by a ``type`` (mirroring how a human demo records a fill: click the
    field, then type), so a filled form aligns with a demonstrated one.

    Args:
        script: Source of a Playwright or Selenium script (one actuation/line).
        params: Optional ``{typed_value: param_name}`` inverse map — a ``fill`` /
            ``send_keys`` whose value matches is marked as that parameter, so the
            typed value is abstracted as per-run data during mining.

    Returns:
        Events in the common event-log format, in order.

    Raises:
        ValueError: On a line that invokes an actuation verb
            (click/fill/press/send_keys/wheel) but matches no recognized pattern
            — silently skipping it would corrupt the mined control flow.
    """
    value_to_param = {v: k for k, v in (params or {}).items()}
    events: list[Event] = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _consume_line(line, events, value_to_param):
            continue
        if _ACTUATION_VERBS.search(line):
            raise ValueError(
                f"unrecognized actuation in script line: {line!r}; extend the "
                "RPA adapter before converting (silent skip would corrupt the "
                "mined control flow)"
            )
        # Non-actuation line (import, goto, wait_for, expect, assignment): skip.
    return events


def _consume_line(
    line: str, events: list[Event], value_to_param: dict[str, str]
) -> bool:
    """Append the event(s) for one recognized script line; True if recognized."""
    m = _RE_ROLE_CLICK.search(line)
    if m:
        struct = {"role": m["role"]}
        if m["name"]:
            struct["name"] = m["name"]
        kind = "double_click" if m["verb"] == "dblclick" else "click"
        events.append({"kind": kind, "structural": struct})
        return True
    m = _RE_LOCATOR_FILL.search(line) or _RE_PAGE_FILL.search(line)
    if m:
        _emit_fill(events, m["sel"], m["val"], value_to_param)
        return True
    m = _RE_LOCATOR_PRESS.search(line) or _RE_PAGE_PRESS.search(line)
    if m:
        events.append({"kind": "key", "key": _map_key(m["key"])})
        return True
    m = _RE_KEYBOARD_PRESS.search(line)
    if m:
        events.append({"kind": "key", "key": _map_key(m["key"])})
        return True
    m = _RE_LOCATOR_CLICK.search(line) or _RE_PAGE_CLICK.search(line)
    if m:
        kind = "double_click" if m["verb"] == "dblclick" else "click"
        events.append({"kind": kind, "structural": {"selector": m["sel"]}})
        return True
    m = _RE_WHEEL.search(line)
    if m:
        events.append({"kind": "scroll", "dx": int(m["dx"]), "dy": int(m["dy"])})
        return True
    m = _RE_SELENIUM.search(line)
    if m:
        _emit_selenium(events, m, value_to_param)
        return True
    return False


def _emit_fill(
    events: list[Event],
    selector: str,
    value: str,
    value_to_param: dict[str, str],
) -> None:
    """A codegen ``fill`` -> focus click on the field + a type event."""
    events.append({"kind": "click", "structural": {"selector": selector}})
    type_event: Event = {"kind": "type", "text": value}
    if value in value_to_param:
        type_event["param"] = value_to_param[value]
    events.append(type_event)


def _emit_selenium(
    events: list[Event], m: re.Match[str], value_to_param: dict[str, str]
) -> None:
    """A Selenium ``find_element(...).click()/.send_keys(...)`` -> event(s)."""
    to_sel = _BY_TO_SELECTOR.get(m["by"])
    if to_sel is None:
        raise ValueError(f"unsupported Selenium locator By.{m['by']}")
    selector = to_sel(m["sel"])
    if m["verb"] == "click":
        events.append({"kind": "click", "structural": {"selector": selector}})
        return
    # send_keys: a literal string is typed; Keys.RETURN etc. is a key press.
    args = m["args"].strip()
    if "Keys." in args:
        events.append({"kind": "key", "key": _map_key(args)})
        return
    try:
        value = ast.literal_eval(args)
    except (ValueError, SyntaxError):
        value = args.strip("'\"")
    events.append({"kind": "click", "structural": {"selector": selector}})
    type_event: Event = {"kind": "type", "text": str(value)}
    if str(value) in value_to_param:
        type_event["param"] = value_to_param[str(value)]
    events.append(type_event)
