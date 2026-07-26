"""Deterministic scene runner for the OpenAdapt demo videos (real OpenEMR).

Drives every on-screen sequence of the two demo cuts reproducibly against a
REAL third-party application: the pinned local OpenEMR 8.0.0.3 reference
environment from ``benchmark/openemr_local`` (official OpenEMR + MariaDB
images, synthetic data only, loopback only, zero model calls).

The demonstrated task is everyday clinic screen-work: look up a patient in
OpenEMR's own Patient Finder, open the chart, and update the callback number
on the Contact tab. The failure-first arc:

    routine   - the same task performed by hand, twice in a row (product-cut
                b-roll for "you do this every day").
    naive     - a DOM-selector macro (the kind of automation every team
                already has) runs the same task while an OLDER DUPLICATE CHART
                for the same patient name exists (a real EMR hazard). It
                clicks the first Finder row, writes the number to the WRONG
                chart, and still reports success. Proof: direct SQL readback
                from the application's own MariaDB.
    record    - the real Recorder captures one human demonstration on the
                clean roster (the callback number is captured as the ``phone``
                parameter).
    compile   - the recording compiles into a workflow bundle; the scene emits
                the program-graph HTML and films it, and prints the compiled
                steps, the mined parameter, and the attached system-of-record
                effect contract.
    verified  - the compiled bundle replays deterministically with a NEW
                ``phone`` value (fail-closed run gate, Standard profile,
                zero model calls) and an out-of-band verifier reading
                OpenEMR's official REST API through a separately
                authenticated read-only OAuth client; the run reports
                VERIFIED and SQL proves exactly one write on the intended
                chart.
    montage   - two more governed replays with two more phone values
                (product-cut b-roll for "it does it for you, every time").
    halt      - the SAME duplicate-chart drift from the naive scene: the
                identity gate sees a different record in the demonstrated
                Finder row, refuses to act, and the run HALTS with an
                evidence report. SQL proves nothing was written.

Every scene writes into ``--out``:

    <scene>.webm              raw browser capture (Playwright context video)
    <scene>.transcript.jsonl  timestamped terminal lines (t seconds from the
                              moment the scene's browser capture starts)
    <scene>.timeline.jsonl    journaled input timeline (clicks/typing with
                              video-relative timestamps) for cursor overlays
    <scene>.*.json            machine-checkable proofs (SQL snapshots, run
                              outcome), so no on-screen claim is hand-authored

Prerequisite (one-time, see benchmark/openemr_local/README.md):

    python scripts/openemr_local_demo.py preflight/prepare/up/bootstrap/snapshot

Then:

    python -m scripts.demo_video_scenes --out demo-video-out --prepare-state
    python -m scripts.demo_video_scenes --out demo-video-out

``--prepare-state`` restores the pinned baseline and seeds the synthetic demo
roster (five fictional patients; reserved 555-01xx numbers, example.invalid
emails). Scenes restore their own state via SQL so they can re-run.

This is a presentation driver over the SHIPPED product paths (Recorder,
compiler, fail-closed run gate, governed Replayer, RestRecordVerifier). The
only actor code is the deliberately NAIVE macro, which demonstrates the
failure class OpenAdapt exists to prevent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = REPO_ROOT / "benchmark"

import sys  # noqa: E402

for _p in (str(REPO_ROOT), str(BENCHMARK_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from openemr_local.fixture import OpenEMRFixture  # noqa: E402

from openadapt_flow.backend import StructuralResolutionRefused  # noqa: E402
from openadapt_flow.ir import (  # noqa: E402
    ActionDeliveryReceipt,
    StructuralHandle,
    StructuralLocator,
)
from scripts.openemr_local_demo import (  # noqa: E402
    _BIND_STRUCTURAL_TARGET_JS,
    OpenEMRPlaywrightBackend,
)

# -- the demo world -----------------------------------------------------------

VIEWPORT = {"width": 1280, "height": 800}

#: Synthetic roster (fictional people; reserved 555-01xx numbers and
#: example.invalid emails). Seeded through OpenEMR's official REST API.
ROSTER: list[dict[str, str]] = [
    {
        "title": "Ms.",
        "fname": "Dana",
        "lname": "Rivera",
        "DOB": "1979-04-12",
        "sex": "Female",
        "street": "44 Cedar Ave",
        "phone_home": "555-0123",
    },
    {
        "title": "Mr.",
        "fname": "Morgan",
        "lname": "Chen",
        "DOB": "1985-09-23",
        "sex": "Male",
        "street": "9 Birch Ln",
        "phone_home": "555-0134",
    },
    {
        "title": "Mr.",
        "fname": "Jordan",
        "lname": "Sample",
        "DOB": "1987-03-14",
        "sex": "Male",
        "street": "12 Synthetic Way",
        "phone_home": "555-0142",
    },
    {
        "title": "Mr.",
        "fname": "Sam",
        "lname": "Okafor",
        "DOB": "1991-12-05",
        "sex": "Male",
        "street": "301 Oak St",
        "phone_home": "555-0156",
    },
    {
        "title": "Ms.",
        "fname": "Priya",
        "lname": "Patel",
        "DOB": "1983-06-18",
        "sex": "Female",
        "street": "78 Elm Rd",
        "phone_home": "555-0167",
    },
]
INTENDED = {"fname": "Jordan", "lname": "Sample", "DOB": "1987-03-14"}
OLD_PHONE = "555-0142"
#: The number typed in the demonstration (mined as the ``phone`` parameter).
DEMO_PHONE = "555-0199"
#: The numbers the governed replays run with (parameterized replays).
REPLAY_PHONE = "555-0177"
MONTAGE_PHONES = ("555-0161", "555-0186")
#: The older duplicate chart the drift scene inserts (same name, different
#: person-identity fields) -- the classic duplicate-chart EMR hazard.
DUPLICATE = {
    "DOB": "1962-07-30",
    "phone_home": "555-0102",
    "street": "17 Old Charts Rd",
}

SEARCH_TERM = "Sample"
SCENES = ("routine", "naive", "record", "compile", "verified", "montage", "halt")


# -- journaling ---------------------------------------------------------------


class Transcript:
    """Print terminal lines and journal them with scene-relative timestamps."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._t0: Optional[float] = None
        self._lines: list[dict[str, Any]] = []

    def start(self) -> None:
        self._t0 = time.monotonic()

    @property
    def t(self) -> float:
        return 0.0 if self._t0 is None else time.monotonic() - self._t0

    def say(self, line: str = "") -> None:
        print(line, flush=True)
        self._lines.append({"t": round(self.t, 3), "line": line})

    def close(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            for row in self._lines:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class Timeline:
    """Journal pointer/keyboard moments (video-relative) for overlay drawing."""

    def __init__(self, path: Path, transcript: Transcript) -> None:
        self._path = path
        self._tr = transcript
        self._events: list[dict[str, Any]] = []

    def mark(self, kind: str, **fields: Any) -> None:
        self._events.append({"t": round(self._tr.t, 3), "kind": kind, **fields})

    def close(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            for row in self._events:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _newest_webm(directory: Path) -> Path:
    webms = sorted(directory.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not webms:
        raise RuntimeError(f"no video captured in {directory}")
    return webms[-1]


def _finish_video(video_tmp: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = _newest_webm(video_tmp)
    if target.exists():
        target.unlink()
    src.rename(target)
    for leftover in video_tmp.glob("*.webm"):
        leftover.unlink()
    return target


# -- fixture state ------------------------------------------------------------


def sql_patients(fx: OpenEMRFixture) -> list[dict[str, str]]:
    """Direct SQL readback of the demo-relevant patient fields."""
    out = fx._db(
        "SELECT pid, fname, lname, DOB, phone_home, street "
        "FROM openemr.patient_data ORDER BY pid"
    ).decode()
    rows = []
    for line in out.splitlines():
        pid, fname, lname, dob, phone, street = line.split("\t")
        rows.append(
            {
                "pid": pid,
                "fname": fname,
                "lname": lname,
                "DOB": dob,
                "phone_home": phone,
                "street": street,
            }
        )
    return rows


def intended_pid(fx: OpenEMRFixture) -> str:
    out = (
        fx._db(
            "SELECT pid FROM openemr.patient_data WHERE "
            f"lname='{INTENDED['lname']}' AND DOB='{INTENDED['DOB']}'"
        )
        .decode()
        .strip()
    )
    if not out.isdigit():
        raise RuntimeError("demo roster is not seeded; run with --prepare-state first")
    return out


def prepare_state(fx: OpenEMRFixture) -> None:
    """Restore the pinned baseline and seed the synthetic demo roster."""
    print("[state] restoring pinned OpenEMR baseline (SQL restore) ...")
    fx.reset()
    # Reserve pid 1 during seeding so the drift scene can later insert the
    # OLDER duplicate chart below every roster pid (the Finder lists by pid).
    fx._db(
        "INSERT INTO openemr.patient_data (pid, fname, lname, DOB) "
        "VALUES (1, 'Zz', 'Placeholder', '1900-01-01')"
    )
    actor = fx.token_session("actor")
    for spec in ROSTER:
        resp = actor.post(
            f"{fx.api_base_url}/apis/default/api/patient",
            json={
                **spec,
                "city": "Testville",
                "state": "NY",
                "postal_code": "10001",
                "email": f"{spec['fname']}.{spec['lname']}@example.invalid".lower(),
                "country_code": "US",
            },
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"seed failed for {spec['lname']}: HTTP {resp.status_code}"
            )
    fx._db("DELETE FROM openemr.patient_data WHERE pid=1 AND lname='Placeholder'")
    print("[state] seeded roster:")
    for row in sql_patients(fx):
        print("  ", row)


def restore_demo_baseline(fx: OpenEMRFixture) -> None:
    """SQL-level scene reset: no drift row, intended phone at its old value."""
    fx._db("DELETE FROM openemr.patient_data WHERE pid=1")
    fx._db(
        f"UPDATE openemr.patient_data SET phone_home='{OLD_PHONE}' WHERE "
        f"lname='{INTENDED['lname']}' AND DOB='{INTENDED['DOB']}'"
    )
    fx._db(
        f"UPDATE openemr.patient_data SET phone_home='{ROSTER[4]['phone_home']}' "
        f"WHERE lname='{ROSTER[4]['lname']}' AND DOB='{ROSTER[4]['DOB']}'"
    )


def inject_duplicate_chart(fx: OpenEMRFixture) -> None:
    """Insert the older duplicate chart at the reserved pid 1 (the drift)."""
    fx._db(
        "INSERT INTO openemr.patient_data "
        "(pid, uuid, pubpid, title, fname, lname, DOB, sex, street, city, "
        "state, postal_code, phone_home, email, country_code) "
        "SELECT 1, UNHEX(LEFT(MD5('openadapt-demo-duplicate-chart'), 32)), "
        f"'1', title, fname, lname, '{DUPLICATE['DOB']}', sex, "
        f"'{DUPLICATE['street']}', city, state, postal_code, "
        f"'{DUPLICATE['phone_home']}', 'jordan.sample.old@example.invalid', "
        "country_code FROM openemr.patient_data "
        f"WHERE lname='{INTENDED['lname']}' AND DOB='{INTENDED['DOB']}'"
    )


def print_sql_proof(
    tr: Transcript, fx: OpenEMRFixture, *, pid: str, phone: str
) -> list[dict[str, str]]:
    """Render the application-database readback as the on-camera proof block."""
    rows = sql_patients(fx)
    tr.say(
        "$ mariadb openemr -e 'SELECT pid, name, DOB, phone FROM"
        " patient_data'   # the application's own database"
    )
    for row in rows:
        marker = ""
        if row["phone_home"] == phone:
            marker = "  <- INTENDED CHART" if row["pid"] == pid else "  <- WRONG CHART"
        tr.say(
            f"  pid={row['pid']}  {row['lname']}, {row['fname']}"
            f"  DOB={row['DOB']}  phone={row['phone_home']}{marker}"
        )
    return rows


# -- OpenEMR access -----------------------------------------------------------


def login(page: Any, fx: OpenEMRFixture, timeline: Optional[Timeline] = None) -> None:
    """Authenticate through the real login form (unmeasured setup)."""
    values = fx._runtime_values()
    page.goto(fx.ui_base_url)
    page.locator("#authUser").fill(values["OPENEMR_ACTOR_USER"])
    page.locator("#clearPass").fill(values["OPENEMR_ACTOR_PASSWORD"])
    page.locator("#login-button").click()
    page.wait_for_url("**/interface/main/tabs/main.php**", timeout=60_000)
    page.wait_for_timeout(4500)
    registration = page.locator(".product-registration-modal")
    if registration.count() and registration.is_visible():
        telemetry = registration.locator("#allowTelemetry")
        if telemetry.count() and telemetry.is_checked():
            telemetry.uncheck()
        registration.get_by_role("button", name="Ask again later", exact=True).click()
        registration.wait_for(state="hidden", timeout=30_000)
    page.wait_for_timeout(800)


def _center(locator: Any) -> tuple[int, int]:
    locator.wait_for(state="visible", timeout=40_000)
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError("target has no bounding box")
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _frame_with(page: Any, fragment: str, *, timeout_s: float = 40.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in page.frames:
            if fragment in frame.url:
                return frame
        page.wait_for_timeout(200)
    raise RuntimeError(f"frame {fragment!r} did not appear")


def oracle_reader(fx: OpenEMRFixture, session: Any):
    """Read-only REST readback via the separately authenticated oracle client."""

    def read() -> Optional[list[dict[str, Any]]]:
        try:
            resp = session.get(
                f"{fx.api_base_url}/apis/default/api/patient"
                f"?lname={INTENDED['lname']}&_limit=100"
            )
            if resp.status_code != 200:
                return None
            return [
                {k: r.get(k) for k in ("pid", "fname", "lname", "DOB", "phone_home")}
                for r in resp.json().get("data", [])
            ]
        except Exception:
            return None

    return read


# -- structural adapter: OpenEMR Patient Finder rows --------------------------


FINDER_URL_FRAGMENT = "main/finder/dynamic_finder.php"
DASHBOARD_URL_FRAGMENT = "/demographics.php"
DASHBOARD_EDIT_SELECTOR = "openemr://demographics-edit"
DEMOGRAPHICS_EDITOR_URL_FRAGMENT = "demographics_full.php"
DEMOGRAPHICS_EDITOR_SELECTORS = {
    "#header_tab_Contact",
    "#form_phone_home",
    "#submit_btn",
}
_FINDER_CELL_JS = """([px, py]) => {
    const hit = document.elementFromPoint(px, py);
    if (!hit) return null;
    const cell = hit.closest('#pt_table tbody td');
    if (!cell) return null;
    const row = cell.parentElement;
    const body = row.parentElement;
    const rowIndex = Array.prototype.indexOf.call(body.children, row);
    const cellIndex = Array.prototype.indexOf.call(row.children, cell);
    if (rowIndex < 0 || cellIndex < 0) return null;
    return {
        row_index: rowIndex,
        cell_index: cellIndex,
        row_text: row.innerText.replace(/\\s+/g, ' ').trim(),
        cell_text: cell.innerText.replace(/\\s+/g, ' ').trim(),
    };
}"""


class DemoOpenEMRBackend(OpenEMRPlaywrightBackend):
    """Benchmark backend + a structural adapter for the Patient Finder.

    OpenEMR's Finder rows are plain table cells behind one delegated jQuery
    row handler, so the general backend records them as pixels only. This
    adapter (the same pattern the benchmark uses for the patient form's
    iframe) resolves the demonstrated row POSITION structurally and exposes
    the row's text as STRUCTURED IDENTITY, so the shipped identity gate can
    refuse a different record in that position instead of clicking it.
    """

    _FINDER_PREFIX = "openemr://finder-cell/"

    def _finder_frame_element(self) -> tuple[Any, Any] | None:
        for handle in self.page.query_selector_all("iframe"):
            frame = handle.content_frame()
            if frame is not None and FINDER_URL_FRAGMENT in frame.url:
                return handle, frame
        return None

    def _finder_target(self, x: int, y: int) -> dict[str, Any] | None:
        try:
            found = self._finder_frame_element()
            if found is None:
                return None
            element, frame = found
            box = element.bounding_box()
            if box is None or not (
                box["x"] <= x < box["x"] + box["width"]
                and box["y"] <= y < box["y"] + box["height"]
            ):
                return None
            topmost = element.evaluate(
                "(el, pt) => document.elementFromPoint(pt[0], pt[1]) === el",
                [int(x), int(y)],
            )
            if not topmost:
                return None
            cell = frame.evaluate(
                _FINDER_CELL_JS, [int(x - box["x"]), int(y - box["y"])]
            )
        except Exception:
            return None
        if not isinstance(cell, dict):
            return None
        return {
            "selector": (
                f"{self._FINDER_PREFIX}{cell['row_index']}/{cell['cell_index']}"
            ),
            "role": "cell",
            "name": cell["cell_text"],
            "row_index": cell["row_index"],
            "row_text": cell["row_text"],
        }

    def _finder_locator(self, selector: str) -> tuple[Any, Any] | None:
        try:
            row_index, cell_index = (
                int(part) for part in selector[len(self._FINDER_PREFIX) :].split("/")
            )
        except ValueError:
            return None
        found = self._finder_frame_element()
        if found is None:
            return None
        _element, frame = found
        return frame, frame.locator(
            f"#pt_table tbody tr:nth-child({row_index + 1}) "
            f"> td:nth-child({cell_index + 1})"
        )

    def _dashboard_edit_target(self, x: int, y: int) -> dict[str, Any] | None:
        """Recognize the unique demographics-card edit link under a point."""
        try:
            matches: list[tuple[Any, Any, Any]] = []
            for element in self.page.query_selector_all("iframe"):
                frame = element.content_frame()
                if frame is None or DASHBOARD_URL_FRAGMENT not in frame.url:
                    continue
                locator = frame.locator("a[href*='demographics_full']")
                if locator.count() == 1 and locator.is_visible():
                    matches.append((element, frame, locator))
            if len(matches) != 1:
                return None
            element, _frame, locator = matches[0]
            iframe_box = element.bounding_box()
            target_box = locator.bounding_box()
            if iframe_box is None or target_box is None:
                return None
            if not (
                target_box["x"] <= x < target_box["x"] + target_box["width"]
                and target_box["y"] <= y < target_box["y"] + target_box["height"]
            ):
                return None
            if not element.evaluate(
                "(el, pt) => document.elementFromPoint(pt[0], pt[1]) === el",
                [int(x), int(y)],
            ):
                return None
        except Exception:
            return None
        return {
            "selector": DASHBOARD_EDIT_SELECTOR,
            "role": "link",
            "name": "Edit demographics",
        }

    def _dashboard_edit_locator(self) -> tuple[Any, Any] | None:
        matches: list[tuple[Any, Any]] = []
        for element in self.page.query_selector_all("iframe"):
            frame = element.content_frame()
            if frame is None or DASHBOARD_URL_FRAGMENT not in frame.url:
                continue
            locator = frame.locator("a[href*='demographics_full']")
            if locator.count() == 1 and locator.is_visible():
                matches.append((frame, locator))
        return matches[0] if len(matches) == 1 else None

    def _demographics_editor_locator(self, selector: str) -> tuple[Any, Any] | None:
        """Resolve one visible control in the active demographics editor."""
        matches: list[tuple[Any, Any]] = []
        for frame in self.page.frames:
            if DEMOGRAPHICS_EDITOR_URL_FRAGMENT not in frame.url:
                continue
            locator = frame.locator(selector)
            if locator.count() == 1 and locator.is_visible():
                matches.append((frame, locator))
        return matches[0] if len(matches) == 1 else None

    def _demographics_editor_target(self, x: int, y: int) -> dict[str, Any] | None:
        """Recognize one qualified editor control under the exact point."""
        for selector in sorted(DEMOGRAPHICS_EDITOR_SELECTORS):
            target = self._demographics_editor_locator(selector)
            if target is None:
                continue
            _frame, locator = target
            box = locator.bounding_box()
            if box is None or not (
                box["x"] <= x < box["x"] + box["width"]
                and box["y"] <= y < box["y"] + box["height"]
            ):
                continue
            return {
                "selector": selector,
                "role": {
                    "#header_tab_Contact": "link",
                    "#form_phone_home": "textbox",
                    "#submit_btn": "button",
                }[selector],
                "name": {
                    "#header_tab_Contact": "Contact",
                    "#form_phone_home": None,
                    "#submit_btn": "Save",
                }[selector],
            }
        return None

    def _custom_target(self, x: int, y: int) -> dict[str, Any] | None:
        return (
            self._finder_target(x, y)
            or self._dashboard_edit_target(x, y)
            or self._demographics_editor_target(x, y)
        )

    def _iframe_target(self, locator: StructuralLocator) -> tuple[Any, Any] | None:
        selector = locator.selector or ""
        if selector.startswith(self._FINDER_PREFIX):
            return self._finder_locator(selector)
        if selector == DASHBOARD_EDIT_SELECTOR:
            return self._dashboard_edit_locator()
        if selector in DEMOGRAPHICS_EDITOR_SELECTORS:
            return self._demographics_editor_locator(selector)
        return super()._iframe_target(locator)

    def structural_locator_at(self, x: int, y: int) -> StructuralLocator | None:
        target = self._custom_target(x, y)
        if target is None:
            return super().structural_locator_at(x, y)
        return StructuralLocator(
            selector=target["selector"],
            role=target["role"],
            name=target["name"],
        )

    def structured_text_at(self, x: int, y: int) -> str | None:
        target = self._finder_target(x, y)
        if target is None:
            dashboard_target = self._dashboard_edit_target(x, y)
            if dashboard_target is not None:
                return json.dumps(
                    {
                        "surface": "openemr-patient-dashboard",
                        "action": "edit-demographics",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            editor_target = self._demographics_editor_target(x, y)
            if editor_target is not None:
                return json.dumps(
                    {
                        "surface": "openemr-demographics-editor",
                        "selector": editor_target["selector"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            return super().structured_text_at(x, y)
        return json.dumps(
            {
                "surface": "openemr-patient-finder",
                "row_index": target["row_index"],
                "row_text": target["row_text"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def text_value_at(self, x: int, y: int) -> str | None:
        target = self._demographics_editor_target(x, y)
        if target is not None and target["selector"] == "#form_phone_home":
            resolved = self._demographics_editor_locator("#form_phone_home")
            if resolved is None:
                return None
            _frame, locator = resolved
            try:
                return locator.input_value()
            except Exception:
                return None
        return super().text_value_at(x, y)

    def focused_text_value(self) -> str | None:
        resolved = self._demographics_editor_locator("#form_phone_home")
        if resolved is not None:
            _frame, locator = resolved
            try:
                if locator.evaluate("el => el === document.activeElement"):
                    return locator.input_value()
            except Exception:
                return None
        return super().focused_text_value()

    def locate_structural(self, locator: StructuralLocator) -> StructuralHandle | None:
        selector = locator.selector or ""
        if not (
            selector.startswith(self._FINDER_PREFIX)
            or selector == DASHBOARD_EDIT_SELECTOR
            or selector in DEMOGRAPHICS_EDITOR_SELECTORS
        ):
            return super().locate_structural(locator)
        target = self._iframe_target(locator)
        if target is None:
            return None
        try:
            frame, loc = target
            candidate_count = loc.count()
            if candidate_count == 0:
                return None
            if candidate_count != 1:
                raise StructuralResolutionRefused(
                    "OpenEMR finder locator is ambiguous: "
                    f"candidate_count={candidate_count}"
                )
            box = loc.bounding_box()
            if box is None or box["width"] <= 0 or box["height"] <= 0:
                return None
            cx = int(round(box["x"] + box["width"] / 2))
            cy = int(round(box["y"] + box["height"] / 2))
            vw, vh = self.viewport
            if not (0 <= cx < vw and 0 <= cy < vh):
                return None
            observed = self._custom_target(cx, cy)
            if observed is None or observed.get("selector") != selector:
                return None
            token = uuid.uuid4().hex
            bound = loc.evaluate(
                _BIND_STRUCTURAL_TARGET_JS,
                {
                    "storeKey": self._structural_store_key,
                    "tokenAttribute": self._token_attribute(token),
                    "token": token,
                    "requireRowIdentity": selector.startswith(self._FINDER_PREFIX),
                },
            )
            if not isinstance(bound, dict):
                self._cleanup_iframe_guard(token, frame)
                return None
            region = bound.get("region")
            if (
                not isinstance(region, list)
                or len(region) != 4
                or not all(isinstance(value, int) for value in region)
            ):
                self._cleanup_iframe_guard(token, frame)
                return None
            fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
            self._iframe_guards[fingerprint] = (token, frame)
            return StructuralHandle(
                point=(cx, cy),
                region=(
                    int(round(box["x"])),
                    int(round(box["y"])),
                    int(round(box["width"])),
                    int(round(box["height"])),
                ),
                target_fingerprint=fingerprint,
                supported_operations=["dom_click", "dom_double_click"],
            )
        except StructuralResolutionRefused:
            raise
        except Exception:
            return None

    def act_structural(
        self,
        locator: StructuralLocator,
        handle: StructuralHandle,
        *,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        selector = locator.selector or ""
        if not (
            selector.startswith(self._FINDER_PREFIX)
            or selector == DASHBOARD_EDIT_SELECTOR
            or selector in DEMOGRAPHICS_EDITOR_SELECTORS
        ):
            return super().act_structural(locator, handle, double=double)
        fingerprint = handle.target_fingerprint
        if not fingerprint:
            raise StructuralResolutionRefused(
                "guarded finder actuation requires a target fingerprint"
            )
        pending = self._iframe_guards.pop(fingerprint, None)
        if pending is None:
            raise StructuralResolutionRefused(
                "finder actuation token is missing, stale, or consumed"
            )
        token, armed_frame = pending
        target = self._iframe_target(locator)
        try:
            if target is None:
                raise StructuralResolutionRefused(
                    "finder target vanished before delivery"
                )
            current_frame, loc = target
            if current_frame != armed_frame or loc.count() != 1:
                raise StructuralResolutionRefused(
                    "finder target context changed before delivery"
                )
            if not self._guard_is_current(loc, token):
                raise StructuralResolutionRefused(
                    "finder target identity changed before delivery"
                )
            token_locator = current_frame.locator(f"[{self._token_attribute(token)}]")
            if token_locator.count() != 1:
                raise StructuralResolutionRefused(
                    "finder guard token is missing or ambiguous"
                )
            if double:
                token_locator.dblclick(timeout=2000)
            else:
                token_locator.click(timeout=2000)
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "finder target changed or became unactionable"
            ) from exc
        finally:
            self._cleanup_iframe_guard(token, armed_frame)
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-openemr-finder-{uuid.uuid4().hex}",
            operation="dom_double_click" if double else "dom_click",
            native=False,
            target_fingerprint=fingerprint,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )


# -- shared UI waypoints ------------------------------------------------------


def wait_finder_row(page: Any) -> Any:
    finder = _frame_with(page, FINDER_URL_FRAGMENT)
    row = finder.locator("#pt_table tbody tr").first
    row.wait_for(state="visible", timeout=40_000)
    return finder


def wait_edit_frame(page: Any) -> Any:
    return _frame_with(page, "demographics_full.php")


def _header_identifier_region(page: Any) -> list[int]:
    """Union of the patient-name title and DOB line in the tabs-shell header."""
    name = f"{INTENDED['fname']} {INTENDED['lname']}"
    name_box = page.get_by_text(name, exact=False).first.bounding_box()
    dob_box = page.get_by_text("DOB:", exact=False).first.bounding_box()
    if not name_box or not dob_box:
        raise RuntimeError("patient header is not visible for identifier marking")
    x0 = int(min(name_box["x"], dob_box["x"])) - 6
    y0 = int(min(name_box["y"], dob_box["y"])) - 4
    x1 = (
        int(max(name_box["x"] + name_box["width"], dob_box["x"] + dob_box["width"])) + 6
    )
    y1 = (
        int(max(name_box["y"] + name_box["height"], dob_box["y"] + dob_box["height"]))
        + 4
    )
    return [max(0, x0), max(0, y0), x1 - x0, y1 - y0]


def _mark_last_click_identifier(recording_dir: Path, region: list[int]) -> None:
    """Bind the consequential Save click to the on-screen patient identity."""
    events_path = recording_dir / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    last_click = max(
        i for i, event in enumerate(events) if event.get("kind") == "click"
    )
    events[last_click]["identifier_region"] = region
    events_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    )


# -- scene: routine (product-cut b-roll) --------------------------------------


def _manual_pass(
    page: Any,
    tr: Transcript,
    tl: Timeline,
    *,
    lname: str,
    label: str,
    phone: str,
) -> None:
    """One human-paced pass of the everyday task, journaled for overlays."""

    def click(target: Any, note: str) -> None:
        x, y = _center(target)
        tr.say(f"[routine] {note}")
        tl.mark("click", x=x, y=y)
        page.mouse.click(x, y)

    search = page.locator("#anySearchBox").first
    click(search, f"search for {label}")
    tl.mark("type", text=lname)
    search.type(lname, delay=95)
    page.wait_for_timeout(350)
    tl.mark("key", key="Enter")
    search.press("Enter")
    finder = wait_finder_row(page)
    page.wait_for_timeout(1600)
    click(
        finder.locator("#pt_table tbody tr").first.locator("td").first, "open the chart"
    )
    dash = _frame_with(page, "demographics.php")
    pencil = dash.locator("xpath=//a[contains(@href,'demographics_full')]").first
    pencil.wait_for(state="visible", timeout=40_000)
    page.wait_for_timeout(2200)
    click(pencil, "edit demographics")
    edit = wait_edit_frame(page)
    contact = edit.locator("#header_tab_Contact")
    contact.wait_for(state="visible", timeout=40_000)
    page.wait_for_timeout(1300)
    click(contact, "open the Contact tab")
    field = edit.locator("#form_phone_home")
    field.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(700)
    click(field, "update the callback number")
    page.keyboard.press("Meta+a")
    tl.mark("type", text=phone)
    page.keyboard.type(phone, delay=90)
    page.wait_for_timeout(500)
    click(edit.locator("#submit_btn"), "save")
    page.wait_for_timeout(4200)


def scene_routine(fx: OpenEMRFixture, out: Path, *, headed: bool) -> None:
    """The same screen-work, twice in a row, by hand (nothing automated)."""
    from playwright.sync_api import sync_playwright

    tr = Transcript(out / "routine.transcript.jsonl")
    tl = Timeline(out / "routine.timeline.jsonl", tr)
    restore_demo_baseline(fx)
    video_tmp = out / ".routine-video"
    video_tmp.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(video_tmp),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        tr.start()
        tr.say("# the everyday routine, done by hand (twice)")
        login(page, fx)
        _manual_pass(
            page,
            tr,
            tl,
            lname=INTENDED["lname"],
            label="the first patient",
            phone=DEMO_PHONE,
        )
        _manual_pass(
            page,
            tr,
            tl,
            lname=ROSTER[4]["lname"],
            label="the next patient",
            phone="555-0188",
        )
        tr.say("[routine] ... and again tomorrow, and the day after")
        page.wait_for_timeout(1200)
        context.close()
        browser.close()
    _finish_video(video_tmp, out / "routine.webm")
    tl.close()
    tr.close()
    restore_demo_baseline(fx)


# -- scene: naive (the failure class) -----------------------------------------


def scene_naive(fx: OpenEMRFixture, out: Path, *, headed: bool) -> None:
    """A DOM-selector macro on the duplicate-chart roster miswrites silently.

    The macro is the kind every team already has: find the patient by name,
    click the FIRST result row, put the new number in the phone field, save,
    and judge success from the screen. With an older duplicate chart present
    (same name, different person fields), the first row is the WRONG chart.
    """
    from playwright.sync_api import sync_playwright

    tr = Transcript(out / "naive.transcript.jsonl")
    tl = Timeline(out / "naive.timeline.jsonl", tr)
    restore_demo_baseline(fx)
    inject_duplicate_chart(fx)
    pid = intended_pid(fx)
    before = sql_patients(fx)
    video_tmp = out / ".naive-video"
    video_tmp.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(video_tmp),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        tr.start()
        tr.say(
            f"$ python update_phone_macro.py --patient Sample --phone {REPLAY_PHONE}"
        )
        tr.say("[macro] selector-based browser macro, screen-judged")
        login(page, fx)
        search = page.locator("#anySearchBox").first
        tr.say(f"[macro] search patient: {SEARCH_TERM!r}")
        x, y = _center(search)
        tl.mark("click", x=x, y=y)
        search.click()
        tl.mark("type", text=SEARCH_TERM)
        search.type(SEARCH_TERM, delay=70)
        tl.mark("key", key="Enter")
        search.press("Enter")
        finder = wait_finder_row(page)
        page.wait_for_timeout(1500)
        tr.say("[macro] click first result row (tbody tr:first-child)")
        first_cell = finder.locator("#pt_table tbody tr").first.locator("td").first
        tl.mark("click", x=_center(first_cell)[0], y=_center(first_cell)[1])
        first_cell.click()
        dash = _frame_with(page, "demographics.php")
        pencil = dash.locator("xpath=//a[contains(@href,'demographics_full')]").first
        pencil.wait_for(state="visible", timeout=40_000)
        page.wait_for_timeout(2200)
        tr.say("[macro] open demographics editor")
        tl.mark("click", x=_center(pencil)[0], y=_center(pencil)[1])
        pencil.click()
        edit = wait_edit_frame(page)
        contact = edit.locator("#header_tab_Contact")
        contact.wait_for(state="visible", timeout=40_000)
        page.wait_for_timeout(1200)
        tl.mark("click", x=_center(contact)[0], y=_center(contact)[1])
        contact.click()
        field = edit.locator("#form_phone_home")
        field.wait_for(state="visible", timeout=30_000)
        page.wait_for_timeout(600)
        tr.say(f"[macro] set #form_phone_home = {REPLAY_PHONE!r}")
        tl.mark("click", x=_center(field)[0], y=_center(field)[1])
        field.click()
        page.keyboard.press("Meta+a")
        tl.mark("type", text=REPLAY_PHONE)
        page.keyboard.type(REPLAY_PHONE, delay=60)
        tr.say("[macro] click Save")
        save = edit.locator("#submit_btn")
        tl.mark("click", x=_center(save)[0], y=_center(save)[1])
        save.click()
        page.wait_for_timeout(5000)
        tr.say(
            "[macro] no error on screen -> exit 0 -- SUCCESS (according to the screen)"
        )
        tr.say("")
        rows = print_sql_proof(tr, fx, pid=pid, phone=REPLAY_PHONE)
        page.wait_for_timeout(1200)
        context.close()
        browser.close()
    _finish_video(video_tmp, out / "naive.webm")
    wrong = [r for r in rows if r["phone_home"] == REPLAY_PHONE and r["pid"] != pid]
    intended_rows = [r for r in rows if r["pid"] == pid]
    tr.say("")
    tr.say(
        "RESULT: the macro reported success; the application database shows"
        " the write on the WRONG chart (the older duplicate). The intended"
        " chart was never touched."
        if wrong and intended_rows[0]["phone_home"] == OLD_PHONE
        else "RESULT: unexpected - naive arm did not miswrite"
    )
    _write_json(
        out / "naive.db_proof.json",
        {
            "arm": "naive-dom-selector-macro",
            "drift": "older duplicate chart (same name) present at pid 1",
            "screen_reported_success": True,
            "intended_pid": pid,
            "wrote_wrong_chart": bool(wrong),
            "db_before": before,
            "db_after": rows,
        },
    )
    tl.close()
    tr.close()
    restore_demo_baseline(fx)
    if not wrong:
        raise RuntimeError("naive arm did not reproduce the wrong-chart write")


# -- scene: record ------------------------------------------------------------


def scene_record(fx: OpenEMRFixture, out: Path, work: Path, *, headed: bool) -> Path:
    """Record the canonical demonstration through the real Recorder."""
    from openadapt_flow.recorder import Recorder

    tr = Transcript(out / "record.transcript.jsonl")
    tl = Timeline(out / "record.timeline.jsonl", tr)
    restore_demo_baseline(fx)
    recording_dir = work / "recording"
    if recording_dir.exists():
        shutil.rmtree(recording_dir)
    video_tmp = out / ".record-video"
    video_tmp.mkdir(parents=True, exist_ok=True)
    session = fx.token_session("oracle")
    backend, close = DemoOpenEMRBackend.launch(
        fx.ui_base_url, headless=not headed, record_video_dir=str(video_tmp)
    )
    try:
        page = backend.page
        tr.start()
        tr.say("$ openadapt-flow record --url $OPENEMR  # demonstrate once")
        login(page, fx)
        recorder = Recorder(
            backend,
            recording_dir,
            app_url=page.url,
            system_of_record_reader=oracle_reader(fx, session),
            settle_timeout_s=10.0,
            settle_stable_frames=3,
            settle_interval_s=0.3,
        )

        def rclick(target: Any, note: str) -> None:
            x, y = _center(target)
            tr.say(f"[record] {note}")
            tl.mark("click", x=x, y=y)
            recorder.click(x, y)

        search = page.locator("#anySearchBox").first
        rclick(search, "click the patient search box")
        tr.say(f"[record] type search: {SEARCH_TERM!r}")
        tl.mark("type", text=SEARCH_TERM)
        recorder.type_text(SEARCH_TERM)
        tl.mark("key", key="Enter")
        recorder.press("Enter")
        finder = wait_finder_row(page)
        page.wait_for_timeout(1600)
        rclick(
            finder.locator("#pt_table tbody tr").first.locator("td").first,
            "open Jordan Sample's chart (the demonstrated Finder row)",
        )
        dash = _frame_with(page, "demographics.php")
        pencil = dash.locator("xpath=//a[contains(@href,'demographics_full')]").first
        pencil.wait_for(state="visible", timeout=40_000)
        page.wait_for_timeout(2400)
        rclick(pencil, "open the demographics editor")
        edit = wait_edit_frame(page)
        contact = edit.locator("#header_tab_Contact")
        contact.wait_for(state="visible", timeout=40_000)
        page.wait_for_timeout(1300)
        rclick(contact, "open the Contact tab")
        field = edit.locator("#form_phone_home")
        field.wait_for(state="visible", timeout=30_000)
        page.wait_for_timeout(700)
        rclick(field, "click the callback-number field")
        recorder.press("Meta+a")
        recorder.press("Backspace")
        tr.say(f"[record] type number (captured as parameter 'phone'): {DEMO_PHONE!r}")
        tl.mark("type", text=DEMO_PHONE)
        recorder.type_text(DEMO_PHONE, param="phone")
        page.wait_for_timeout(500)
        header_region = _header_identifier_region(page)
        rclick(edit.locator("#submit_btn"), "click Save")
        page.wait_for_timeout(5000)
        recorder.finish()
        _mark_last_click_identifier(recording_dir, header_region)
        tr.say(f"[record] demonstration saved -> {recording_dir.name}/")
    finally:
        close()
    _finish_video(video_tmp, out / "record.webm")
    tl.close()
    tr.close()
    restore_demo_baseline(fx)
    return recording_dir


# -- scene: compile -----------------------------------------------------------


def _demo_effects(pid: str) -> list[Any]:
    """The Save step's system-of-record contract for the intended chart."""
    from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

    selector = {"pid": ValueExpr(literal=pid)}
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=selector,
            expected_count=1,
            risk="irreversible",
            # A demographics update is idempotent on the written field: a
            # retried submission of the same number cannot land as a second
            # divergent write.
            idempotency_key=ValueExpr(param="phone"),
            key_field="phone_home",
            probe="exactly one chart exists for the intended patient",
            timeout_s=8.0,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=selector,
            field="phone_home",
            value=ValueExpr(param="phone"),
            risk="irreversible",
            probe="the intended chart's callback number equals the run's"
            " 'phone' parameter",
            timeout_s=8.0,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=selector,
            field="DOB",
            value=ValueExpr(literal=INTENDED["DOB"]),
            risk="irreversible",
            probe="the written chart still carries the intended patient's DOB",
            timeout_s=8.0,
        ),
    ]


def scene_compile(
    fx: OpenEMRFixture,
    recording_dir: Path,
    out: Path,
    work: Path,
    *,
    headed: bool,
) -> Path:
    """Compile the recording; film the program graph; print the contract."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.visualize import build_program_graph, render_html

    tr = Transcript(out / "compile.transcript.jsonl")
    bundle_dir = work / "bundle"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    tr.say("$ openadapt-flow compile recording/ -o bundle/")
    workflow = compile_recording(
        recording_dir, bundle_dir, name="openemr-update-callback-number"
    )
    pid = intended_pid(fx)
    save = workflow.steps[-1]
    if save.action.value != "click":
        raise RuntimeError("last compiled step is not the Save click")
    save.risk = "irreversible"
    save.effects = _demo_effects(pid)
    workflow.save(bundle_dir)
    tr.say(f"[compile] {len(workflow.steps)} steps compiled; zero model calls")
    for step in workflow.steps:
        risk = f" risk={step.risk}" if step.risk == "irreversible" else ""
        effects = (
            " effects=[" + ", ".join(e.kind.value for e in step.effects) + "]"
            if step.effects
            else ""
        )
        tr.say(f"  {step.id}: {step.intent}{risk}{effects}")
    tr.say(f"[compile] parameters: {sorted(workflow.params)}")
    tr.say(
        "[compile] consequential Save compiled with a system-of-record"
        " contract: exactly one chart for the intended patient;"
        " phone read-back equals the run's 'phone' parameter"
    )

    graph_html = out / "compile.graph.html"
    graph_html.write_text(
        render_html(
            build_program_graph(workflow), title="openemr-update-callback-number"
        ),
        encoding="utf-8",
    )

    from playwright.sync_api import sync_playwright

    video_tmp = out / ".compile-video"
    video_tmp.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(video_tmp),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        tr.start()
        page.goto(graph_html.as_uri())
        page.wait_for_timeout(2500)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(1800)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(1800)
        context.close()
        browser.close()
    _finish_video(video_tmp, out / "compile.webm")
    tr.close()
    return bundle_dir


# -- governed replays ---------------------------------------------------------


def _governed_replay(
    fx: OpenEMRFixture,
    bundle_dir: Path,
    run_dir: Path,
    video_tmp: Path,
    *,
    phone: str,
    headed: bool,
    tr: Transcript,
    hold_ms: int = 2500,
) -> Any:
    """One governed replay through the real Replayer + out-of-band verifier."""
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.report import render_run_report
    from openadapt_flow.run_gate import (
        build_runtime_authorization,
        evaluate_run_gate,
    )
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.effects import RestRecordVerifier

    workflow = Workflow.load(bundle_dir)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    video_tmp.mkdir(parents=True, exist_ok=True)
    session = fx.token_session("oracle")
    verifier = RestRecordVerifier(
        fx.api_base_url,
        records_path=(
            f"/apis/default/api/patient?lname={INTENDED['lname']}&_limit=100"
        ),
        records_key="data",
        session=session,
        timeout_s=8.0,
        poll_interval_s=0.3,
    )
    backend, close = DemoOpenEMRBackend.launch(
        fx.ui_base_url, headless=not headed, record_video_dir=str(video_tmp)
    )
    try:
        page = backend.page
        tr.start()
        login(page, fx)
        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle_dir,
            deployment=DeploymentConfig(policy=PolicySection(policy="clinical-write")),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
        )
        if not gate.passed:
            raise RuntimeError(gate.render())
        authorization = build_runtime_authorization(
            workflow,
            gate,
            approval_source="demo-video-scene-runner",
            params={"phone": phone},
        )
        report = Replayer(
            backend,
            effect_verifier=verifier,
            governed_authorization=authorization,
            durable=True,
            require_settled=True,
            use_structural=True,
        ).run(
            workflow.model_copy(deep=True),
            params={"phone": phone},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            execution_target_kind="web",
            execution_origin=fx.ui_base_url,
            execution_entry_url=fx.ui_base_url,
        )
        page.wait_for_timeout(hold_ms)
    finally:
        close()
    render_run_report(run_dir)
    return report


def scene_verified(
    fx: OpenEMRFixture, bundle_dir: Path, out: Path, work: Path, *, headed: bool
) -> None:
    """Deterministic replay + out-of-band verification on the clean roster."""
    tr = Transcript(out / "verified.transcript.jsonl")
    restore_demo_baseline(fx)
    pid = intended_pid(fx)
    tr.say(f"$ openadapt-flow replay bundle/ --param phone={REPLAY_PHONE} \\")
    tr.say("      --verify rest   # out-of-band read: OpenEMR's official API,")
    tr.say("                      # separately authenticated read-only client")
    run_dir = work / "run-verified"
    report = _governed_replay(
        fx,
        bundle_dir,
        run_dir,
        out / ".verified-video",
        phone=REPLAY_PHONE,
        headed=headed,
        tr=tr,
    )
    tr.say(f"[replay] outcome: {report.execution_outcome}")
    tr.say(f"[replay] model calls: {report.model_calls} (deterministic replay)")
    tr.say(
        "[replay] effect verifier: CONFIRMED against the system of record"
        " (an independent API read, not the screen)"
    )
    tr.say("")
    rows = print_sql_proof(tr, fx, pid=pid, phone=REPLAY_PHONE)
    right = [r for r in rows if r["pid"] == pid and r["phone_home"] == REPLAY_PHONE]
    wrong = [r for r in rows if r["pid"] != pid and r["phone_home"] == REPLAY_PHONE]
    tr.say("")
    tr.say(
        "RESULT: exactly one write, on the intended chart, with the run's"
        " parameter value - independently confirmed."
        if len(right) == 1 and not wrong
        else "RESULT: unexpected system-of-record state"
    )
    _finish_video(out / ".verified-video", out / "verified.webm")
    _write_json(
        out / "verified.outcome.json",
        {
            "arm": "governed-structural",
            "execution_outcome": report.execution_outcome,
            "model_calls": report.model_calls,
            "replay_phone_param": REPLAY_PHONE,
            "intended_pid": pid,
            "db_after": rows,
        },
    )
    tr.close()
    restore_demo_baseline(fx)
    if report.execution_outcome != "VERIFIED" or len(right) != 1 or wrong:
        raise RuntimeError("verified scene did not reproduce VERIFIED outcome")


def scene_montage(
    fx: OpenEMRFixture, bundle_dir: Path, out: Path, work: Path, *, headed: bool
) -> None:
    """Two more governed replays with two more parameter values (b-roll)."""
    pid = intended_pid(fx)
    for index, phone in enumerate(MONTAGE_PHONES, start=1):
        tr = Transcript(out / f"montage_{index}.transcript.jsonl")
        restore_demo_baseline(fx)
        tr.say(f"$ openadapt-flow replay bundle/ --param phone={phone}")
        report = _governed_replay(
            fx,
            bundle_dir,
            work / f"run-montage-{index}",
            out / f".montage-{index}-video",
            phone=phone,
            headed=headed,
            tr=tr,
            hold_ms=1500,
        )
        rows = sql_patients(fx)
        ok = [r for r in rows if r["pid"] == pid and r["phone_home"] == phone]
        tr.say(
            f"[replay] outcome: {report.execution_outcome};"
            f" model calls: {report.model_calls}"
        )
        _finish_video(out / f".montage-{index}-video", out / f"montage_{index}.webm")
        _write_json(
            out / f"montage_{index}.outcome.json",
            {
                "arm": "governed-structural",
                "execution_outcome": report.execution_outcome,
                "model_calls": report.model_calls,
                "replay_phone_param": phone,
                "intended_pid": pid,
                "db_after": rows,
            },
        )
        tr.close()
        restore_demo_baseline(fx)
        if report.execution_outcome != "VERIFIED" or len(ok) != 1:
            raise RuntimeError(f"montage replay {index} did not verify")


def scene_halt(
    fx: OpenEMRFixture, bundle_dir: Path, out: Path, work: Path, *, headed: bool
) -> None:
    """Same duplicate-chart drift as the naive scene; the governed run HALTS."""
    tr = Transcript(out / "halt.transcript.jsonl")
    restore_demo_baseline(fx)
    inject_duplicate_chart(fx)
    pid = intended_pid(fx)
    before = sql_patients(fx)
    tr.say(f"$ openadapt-flow replay bundle/ --param phone={REPLAY_PHONE} \\")
    tr.say("      --verify rest   # same duplicate-chart roster as the macro")
    run_dir = work / "run-halt"
    report = _governed_replay(
        fx,
        bundle_dir,
        run_dir,
        out / ".halt-video",
        phone=REPLAY_PHONE,
        headed=headed,
        tr=tr,
        hold_ms=4200,
    )
    rows = sql_patients(fx)
    tr.say(f"[replay] outcome: {report.execution_outcome}")
    halt = report.halt
    if halt is not None:
        tr.say(f"[replay] halted at: {halt.intent or halt.state_id}")
        tr.say(f"[replay] reason: {halt.reason}")
    tr.say("")
    changed = [row for row in rows if row not in before]
    tr.say("$ mariadb openemr -e 'SELECT ...'   # the application's own database")
    tr.say("  (no rows changed)" if not changed else f"  changed rows: {changed}")
    tr.say("")
    tr.say(
        "RESULT: the demonstrated row held a DIFFERENT record, so the run"
        " STOPPED before acting. Nothing was written. Evidence report saved."
        if report.execution_outcome == "HALTED" and not changed
        else "RESULT: unexpected outcome"
    )
    _finish_video(out / ".halt-video", out / "halt.webm")

    report_md = run_dir / "REPORT.md"
    evidence_html = out / "halt.evidence.html"
    _render_report_page(report_md, evidence_html)
    _film_scroll(
        evidence_html,
        out / ".halt-report-video",
        out / "halt_report.webm",
        headed=headed,
    )
    _write_json(
        out / "halt.outcome.json",
        {
            "arm": "governed-structural",
            "drift": "older duplicate chart (same name) present at pid 1",
            "execution_outcome": report.execution_outcome,
            "model_calls": report.model_calls,
            "halt_reason": halt.reason if halt is not None else None,
            "intended_pid": pid,
            "db_rows_changed": changed,
            "db_after": rows,
        },
    )
    tr.close()
    restore_demo_baseline(fx)
    if report.execution_outcome != "HALTED" or changed:
        raise RuntimeError("halt scene did not reproduce a clean HALT")


def _render_report_page(report_md: Path, out_html: Path) -> None:
    """Wrap the REAL run REPORT.md in a large-type page for on-camera scrolling.

    Presentation-only: the report text is shown verbatim, not edited.
    """
    text = (
        report_md.read_text(encoding="utf-8")
        if report_md.exists()
        else "(REPORT.md was not generated)"
    )
    out_html.write_text(
        "<title>Run evidence report</title>"
        "<style>body{background:#0e121b;color:#e8ecf4;font:22px/1.6 "
        "ui-monospace,SFMono-Regular,Menlo,monospace;max-width:1100px;"
        "margin:40px auto;padding:0 24px;white-space:pre-wrap}</style>"
        "<body>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</body>",
        encoding="utf-8",
    )


def _film_scroll(html: Path, video_tmp: Path, target: Path, *, headed: bool) -> None:
    from playwright.sync_api import sync_playwright

    video_tmp.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(video_tmp),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        page.goto(html.as_uri())
        page.wait_for_timeout(2200)
        for _ in range(10):
            page.mouse.wheel(0, 260)
            page.wait_for_timeout(650)
        page.wait_for_timeout(1500)
        context.close()
        browser.close()
    _finish_video(video_tmp, target)


# -- entry point --------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, required=True, help="output directory for raw clips"
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="working directory for recording/bundle/runs (default: <out>/work)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=BENCHMARK_DIR / "openemr_local" / "state",
        help="prepared benchmark/openemr_local fixture state directory",
    )
    parser.add_argument(
        "--scenes",
        default="all",
        help=f"comma list from {','.join(SCENES)} (default all)",
    )
    parser.add_argument(
        "--prepare-state",
        action="store_true",
        help="restore the pinned baseline and seed the demo roster, then exit",
    )
    parser.add_argument(
        "--headed", action="store_true", help="show the browser while filming"
    )
    args = parser.parse_args(argv)

    fx = OpenEMRFixture(state_dir=args.state_dir)
    if args.prepare_state:
        prepare_state(fx)
        return 0

    out: Path = args.out
    work: Path = args.work or out / "work"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    want = SCENES if args.scenes == "all" else tuple(args.scenes.split(","))
    unknown = [scene for scene in want if scene not in SCENES]
    if unknown:
        parser.error(f"unknown scenes: {unknown}")

    intended_pid(fx)  # fail fast when the roster is not seeded
    print(
        f"[demo] real OpenEMR reference environment: {fx.ui_base_url}"
        " (synthetic data, loopback only)"
    )
    recording_dir = work / "recording"
    bundle_dir = work / "bundle"
    if "routine" in want:
        print("\n=== scene: routine (the everyday screen-work) ===")
        scene_routine(fx, out, headed=args.headed)
    if "naive" in want:
        print("\n=== scene: naive (the failure class) ===")
        scene_naive(fx, out, headed=args.headed)
    if "record" in want:
        print("\n=== scene: record ===")
        recording_dir = scene_record(fx, out, work, headed=args.headed)
    if "compile" in want:
        print("\n=== scene: compile ===")
        bundle_dir = scene_compile(fx, recording_dir, out, work, headed=args.headed)
    if "verified" in want:
        print("\n=== scene: verified replay ===")
        scene_verified(fx, bundle_dir, out, work, headed=args.headed)
    if "montage" in want:
        print("\n=== scene: montage replays ===")
        scene_montage(fx, bundle_dir, out, work, headed=args.headed)
    if "halt" in want:
        print("\n=== scene: halt (same drift, governed) ===")
        scene_halt(fx, bundle_dir, out, work, headed=args.headed)
    print(f"\n[demo] raw clips + transcripts + proofs -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
