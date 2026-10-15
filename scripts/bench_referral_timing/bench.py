"""OpenEMR referral timing benchmark harness. See ../PROTOCOL.md.

  bench.py block --arm replay-main|replay-dom|agent-sonnet --mode warm|cold --block K --tag ENGINE_TAG
  bench.py one ...   (internal: one cold run in a fresh process)
Engine = first entry of PYTHONPATH (a pinned openadapt-flow worktree).
"""
import time
T_MAIN = time.time()
import argparse, csv, json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
REF_H = Path("/Users/abrichr/oa/scratch/site-v2/visuals/referral/harness")
AGENT_H = Path("/Users/abrichr/oa/scratch/site-v2/visuals/agent/harness")
CLONE = Path("/Users/abrichr/oa/scratch/site-v2/realrun/openadapt-flow")
FIX = Path("/Users/abrichr/oa/scratch/benchmark/fixture")  # private copy of the realrun fixture on ports 9311/9310
BUNDLES = {"replay-main": Path("/Users/abrichr/oa/scratch/site-v2/visuals/referral/bundle"),
           "replay-dom": Path("/Users/abrichr/oa/scratch/replay-speed/dom-identity/out/bundle")}
MODEL = "claude-sonnet-5-5"
CONTAINER = "openadapt-openemr-bench-openemr-1"
CDP_PORT = 9334  # 9333 is used by the other executor's probe recorder
NODE = "/opt/homebrew/bin/node"
sys.path[:0] = [str(HERE), str(REF_H), str(AGENT_H)]  # HERE first: our spend.py shadows the prior ledger

G = {}

# Copied verbatim from site-v2/visuals/agent/harness/referrals_agent.py (that module imports a
# helper pinned to a different engine worktree, so it is not imported).
INSTRUCTION = ("Register this referred patient in the EMR. Do not create a duplicate chart. "
               "If the EMR shows a possible existing patient, stop and report instead of saving.")
HOLD_S = 2.5
LEAD_IN_S = 1.5


def task_prompt(spec):
    fields = json.dumps(spec.params(), indent=2)
    return (
        "You are operating OpenEMR, an electronic medical record, in a web browser. It is a "
        "local test copy and every patient in it is made up. You are already signed in, and "
        "the New Patient form is open.\n\n"
        f"{INSTRUCTION}\n\n"
        "Referred patient (structured fields):\n"
        f"{fields}\n\n"
        "When you have finished, or if you stop, reply with a short report: what you did, "
        "whether a chart was saved, and anything that did not match."
    )




def do_imports():
    import importlib.util
    import openadapt_flow
    wt = Path(os.environ["PYTHONPATH"].split(":")[0])
    assert str(wt) in openadapt_flow.__file__, openadapt_flow.__file__
    import benchmark.openemr_local.fixture as fx
    assert str(wt) in fx.__file__
    fx.LOCK_PATH = FIX / "environment.lock.json"

    class BenchFixture(fx.OpenEMRFixture):
        """Same pinned images, compose file, baseline and reset as realrun; own project + ports."""
        @property
        def ui_base_url(self):
            return "http://127.0.0.1:9311"

        @property
        def api_base_url(self):
            return "https://127.0.0.1:9310"

        def _validate_runtime_env(self):
            v = self._runtime_values()
            assert v.get("OPENEMR_HTTP_PORT") == "9311" and v.get("OPENEMR_HTTPS_PORT") == "9310"
            assert v.get("OPENEMR_IMAGE") == self.lock["services"]["openemr"] and v.get("MARIADB_IMAGE") == self.lock["services"]["mariadb"]
            self.runtime_env.chmod(0o600)
    G["Fixture"] = lambda: BenchFixture(FIX / "state")
    s = importlib.util.spec_from_file_location("openemr_local_demo", wt / "scripts/openemr_local_demo.py")
    demo = importlib.util.module_from_spec(s); sys.modules["openemr_local_demo"] = demo; s.loader.exec_module(demo)
    from referrals import REFERRALS, EXISTING_ROSA
    from openadapt_flow.benchmark.openemr_local import OpenEMRPatientOracle, SyntheticPatientSpec
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer
    from playwright.sync_api import sync_playwright
    from openadapt_flow._browser_setup import ensure_chromium_installed
    import openadapt_flow.vision.ocr  # noqa: F401
    ocrmod = sys.modules["openadapt_flow.vision.ocr"]
    G.update(fx=fx, demo=demo, REFERRALS=REFERRALS, EXISTING_ROSA=EXISTING_ROSA, Oracle=OpenEMRPatientOracle,
             Spec=SyntheticPatientSpec, Workflow=Workflow, Replayer=Replayer, sync_playwright=sync_playwright,
             ensure_chromium=ensure_chromium_installed, ocrmod=ocrmod, wt=wt)


def engine_head():
    return subprocess.run(["git", "-C", str(G["wt"]), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def container_started():
    return subprocess.run(["podman", "inspect", "-f", "{{.State.StartedAt}}", CONTAINER],
                          capture_output=True, text=True).stdout.strip()


FOREIGN = r"probe[_a-z0-9]*\.py|realrun_dom|referrals_agent|quiet_openadapt|harness/batch\.py|claims_agent"


def foreign_users():
    r = subprocess.run(["pgrep", "-fl", FOREIGN], capture_output=True, text=True).stdout.strip()
    return [l for l in r.splitlines() if "pgrep" not in l]


def wait_exclusive(quiet_checks=3, every_s=20, max_s=3600):
    """Other sessions use the same fixture and CPU; wait until none has run for ~1 min."""
    t0 = time.time(); ok = 0; seen = set()
    while True:
        f = foreign_users()
        seen.update(f)
        ok = 0 if f else ok + 1
        if ok >= quiet_checks:
            return {"waited_s": round(time.time() - t0, 1), "gave_up": False, "seen": sorted(seen)[:5]}
        if time.time() - t0 > max_s:
            return {"waited_s": round(time.time() - t0, 1), "gave_up": True, "seen": sorted(seen)[:5]}
        time.sleep(every_s if f else 2)


def q(v):
    assert "'" not in v and "\\" not in v
    return "'" + v + "'"


SQL_FIELDS = ("id", "pid", "uuid", "title", "fname", "lname", "DOB", "sex", "street", "city", "state",
              "postal_code", "phone_home", "email", "country_code", "date")


def sql_patients(f, where):
    out = f._db("SELECT " + ",".join("LOWER(HEX(uuid))" if n == "uuid" else n for n in SQL_FIELDS)
                + " FROM openemr.patient_data WHERE " + where + " ORDER BY id").decode()
    return [{k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))} for line in out.splitlines()]


class Browser:
    def __init__(self):
        G["ensure_chromium"]()
        self.pw = G["sync_playwright"]().start()
        self.browser = self.pw.chromium.launch(headless=True, args=[
            f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1"])

    def page(self, url):
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
        page = ctx.new_page(); page.goto(url)
        return ctx, G["demo"]._BasePlaywrightBackend(page)

    def close(self):
        try:
            self.browser.close()
        finally:
            self.pw.stop()


class ScreenRecorder:
    def __init__(self, outdir):
        outdir.mkdir(parents=True)
        self.outdir = outdir
        self.proc = subprocess.Popen([NODE, str(REF_H / "cdp_screencast.mjs"), str(CDP_PORT), str(outdir), "main.php", "jpeg", "95"],
                                     stdout=open(outdir / "recorder.log", "w"), stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            if (outdir / "STARTED").exists() and any((outdir / "frames").glob("f_*")):
                return
            if self.proc.poll() is not None:
                raise RuntimeError("screen recorder exited early")
            time.sleep(0.05)
        raise RuntimeError("screen recorder produced no first frame")

    def stop(self):
        (self.outdir / "STOP").write_text(str(time.time()))
        self.proc.wait(timeout=30)
        try:
            return json.loads((self.outdir / "frames.json").read_text())
        except Exception:
            return {}


def make_client():
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=300.0)


def execute(arm, ref_idx, out, marks, fixture, browser, client, run_id):
    """Everything after browser launch. marks gets absolute time.time() marks."""
    demo, Spec = G["demo"], G["Spec"]
    ref = G["REFERRALS"][ref_idx - 1]
    spec = Spec(**ref.spec_kwargs())
    res = {"arm": arm, "ref": ref.key, "ref_idx": ref_idx, "mismatch": ref.mismatch, "error": None}
    ctx, backend = browser.page(fixture.ui_base_url)
    marks["browser_end"] = time.time()
    rec = None
    try:
        demo._preauthenticate_browser(backend, fixture)
        before_counts = demo._stable_pre_trial_counts(fixture)
        before_ids = {r["id"] for r in sql_patients(fixture, "1=1")}
        oracle = G["Oracle"](fixture.api_base_url, fixture.token_session("oracle"), spec) if arm.startswith("replay") else None
        marks["signin_end"] = time.time()
        rec = ScreenRecorder(out / "screen")
        marks["rec_end"] = time.time()
        try:
            if arm.startswith("replay"):
                G["ocrmod"].clear_ocr_memo()
                n_shots = [0]; orig = backend.screenshot
                def shot(*a, **k):
                    n_shots[0] += 1; return orig(*a, **k)
                backend.screenshot = shot
                bundle = BUNDLES[arm]
                rep = G["Replayer"](backend, effect_verifier=oracle.verifier).run(
                    G["Workflow"].load(bundle), params=spec.params(), bundle_dir=bundle, run_dir=out / "engine")
                marks["task_end"] = time.time()
                raw = rep.model_dump(mode="json")
                res.update(outcome=str(raw.get("execution_outcome")), engine_success=raw.get("success"),
                           engine_total_ms=raw.get("total_ms"), actions=len(raw.get("results") or []),
                           local_screenshots=n_shots[0], screens_sent=0, model_calls=0, cost_usd=0.0,
                           steps=[(r["step_id"], r["intent"][:40], round(r["elapsed_ms"])) for r in raw["results"]])
            else:
                import cua, spend
                from openadapt_flow.vision.settle import wait_settled
                spend.open_run(run_id, MODEL, {"referral_key": ref.key, "mismatch_case": ref.mismatch})
                run = cua.run_agent(backend, task_prompt(spec), client=client, model=MODEL, run_id=run_id,
                                    out_dir=out, capture=lambda: wait_settled(backend, timeout_s=2.0), effort=cua.EFFORT)
                marks["task_end"] = time.time()
                s = run.summary()
                spend.close_run(run_id, s.get("stopped", "error"), {"actions": s.get("actions")})
                res.update(outcome=s.get("stopped"), actions=s.get("actions"), screens_sent=(s.get("screenshots_sent") or 0) + (s.get("zoom_images_sent") or 0),
                           model_calls=s.get("model_calls"), cost_usd=s.get("cost_usd"), model_wait_s=s.get("model_wait_s"),
                           action_s=s.get("action_s"), agent_error=s.get("error"), report=(s.get("final_report") or "")[:800],
                           tokens={k: s.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")})
        except Exception as exc:  # noqa: BLE001
            marks.setdefault("task_end", time.time())
            res["error"] = f"{type(exc).__name__}: {exc}"[:500]
        time.sleep(demo.ORACLE_SETTLE_S)
        after = sql_patients(fixture, "1=1")
        new_rows = [r for r in after if r["id"] not in before_ids]
        target = sql_patients(fixture, f"lname={q(spec.lname)} AND email={q(spec.email)}")
        row = new_rows[0] if len(new_rows) == 1 else None
        fields = [{"field": k, "want": v, "got": None if row is None else row.get(k), "match": row is not None and row.get(k) == v}
                  for k, v in spec.fields.items()]
        nmatch = sum(x["match"] for x in fields)
        correct = (len(new_rows) == 0) if ref.mismatch else (len(new_rows) == 1 and len(target) == 1 and nmatch == len(fields))
        marks["verified"] = time.time()
        after_counts = fixture.table_counts()
        res.update(new_charts=len(new_rows), readback=f"{nmatch}/{len(fields)}", correct=correct, field_check=fields,
                   table_deltas={k: after_counts.get(k, 0) - before_counts.get(k, 0) for k in set(after_counts) | set(before_counts)
                                 if after_counts.get(k, 0) != before_counts.get(k, 0)})
    except Exception as exc:  # noqa: BLE001 - setup failure
        res["error"] = f"setup {type(exc).__name__}: {exc}"[:500]; res["correct"] = False
    finally:
        try:
            if rec:
                fr = rec.stop(); res["frames"] = len(fr.get("frames", []))
            try:
                (out / "final.png").write_bytes(backend.page.screenshot(type="png"))
            except Exception:
                pass
            ctx.close()
        except Exception as exc:  # noqa: BLE001
            res["teardown_error"] = str(exc)[:200]
        marks["teardown_end"] = time.time()
    return res


def phases(m):
    t0 = m["t0"]; seq = []
    if "imports_start" in m:
        seq += [("proc_start", t0, m["imports_start"]), ("imports", m["imports_start"], m["imports_end"]),
                ("browser_launch", m["imports_end"], m.get("browser_end"))]
    else:
        seq += [("browser_launch", t0, m.get("browser_end"))]
    seq += [("signin_form", m.get("browser_end"), m.get("signin_end")), ("recorder_start", m.get("signin_end"), m.get("rec_end")),
            ("task", m.get("rec_end"), m.get("task_end")), ("readback_s", m.get("task_end"), m.get("verified"))]
    p = {k: (round(b - a, 3) if a and b else None) for k, a, b in seq}
    p["wall"] = round(m["verified"] - t0, 3) if m.get("verified") else None
    p["wall_ex_recorder"] = round(p["wall"] - (p["recorder_start"] or 0), 3) if p["wall"] else None
    p["teardown"] = round(m["teardown_end"] - m["verified"], 3) if m.get("verified") and m.get("teardown_end") else None
    return p


CSV_COLS = ["engine_tag", "engine_sha", "arm", "mode", "block", "pos", "ref", "mismatch", "valid", "correct", "new_charts", "readback",
            "outcome", "error", "wall", "wall_ex_recorder", "proc_start", "imports", "browser_launch", "signin_form", "recorder_start",
            "task", "readback_s", "teardown", "engine_total_s", "model_wait_s", "action_s", "actions", "model_calls", "screens_sent",
            "local_screenshots", "cost_usd", "load1_mean", "load1_max", "gate_waited_s", "gate_gave_up", "start_local", "dir"]


def encode(screen):
    fj = screen / "frames.json"; frames_dir = screen / "frames"
    if not fj.exists() or not frames_dir.exists():
        return
    fr = json.loads(fj.read_text()).get("frames", [])
    lines = []
    for a, b in zip(fr, fr[1:] + [None]):
        f = frames_dir / Path(a.get("file", "")).name if a.get("file") else None
        if not f or not f.exists():
            continue
        d = (b["ts"] - a["ts"]) if b and b.get("ts") and a.get("ts") else 0.5
        lines += [f"file '{f}'", f"duration {max(d, 0.001):.3f}"]
    if not lines:
        return
    (screen / "concat.txt").write_text("\n".join(lines) + "\n")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(screen / "concat.txt"),
                        "-vsync", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(screen / "screen.mp4")])
    if r.returncode == 0 and (screen / "screen.mp4").stat().st_size > 10000:
        subprocess.run(["rm", "-rf", str(frames_dir)])


def cmd_one(a):
    marks = {"t0": a.t0, "imports_start": T_MAIN}
    do_imports(); marks["imports_end"] = time.time()
    fixture = G["Fixture"]()
    browser = Browser()
    client = make_client() if a.arm == "agent-sonnet" else None
    out = Path(a.out)
    try:
        res = execute(a.arm, a.ref, out, marks, fixture, browser, client, a.run_id)
    finally:
        browser.close(); marks["teardown_end"] = time.time()
    res["marks"] = marks
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))


def cmd_block(a):
    do_imports()
    from loadgate import wait_quiet, LoadSampler
    import spend
    root = BENCH / "runs" / a.tag / f"{a.arm}-{a.mode}" / f"block-{a.block:02d}"
    if root.exists():
        raise SystemExit(f"exists: {root}")
    root.mkdir(parents=True)
    sha = engine_head()
    fixture = G["Fixture"]()
    excl = {"foreign_at_block_start": foreign_users()}
    t = time.time(); digest = fixture.reset()
    seed = fixture.token_session("actor").post(fixture.api_base_url + "/apis/default/api/patient",
                                               json=dict(G["Spec"](**G["EXISTING_ROSA"]).fields)).status_code
    meta = {"arm": a.arm, "mode": a.mode, "block": a.block, "tag": a.tag, "engine_sha": sha, "baseline": digest,
            "reset_seed_s": round(time.time() - t, 2), "seed_status": seed, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "exclusive_wait": excl}
    cstart = container_started()
    order = [((a.block + i) % 8) + 1 for i in range(8)][:int(os.environ.get("SMOKE_N", "8"))]
    meta["order"] = order
    browser = client = None
    if a.mode == "warm":
        t = time.time()
        browser = Browser()
        if a.arm == "agent-sonnet":
            client = make_client()
        else:
            ocr = G["ocrmod"]; ocr._get_engine()
            ctx, be = browser.page(fixture.ui_base_url); ocr.ocr(be.page.screenshot(type="png")); ctx.close(); ocr.clear_ocr_memo()
        meta["warm_setup_s"] = round(time.time() - t, 2)
    csv_path = BENCH / "data" / "runs.csv"; csv_path.parent.mkdir(exist_ok=True)
    new = not csv_path.exists()
    rows = []
    for pos, idx in enumerate(order, 1):
        if a.arm == "agent-sonnet":
            ok, why = spend.may_start_run()
            if not ok:
                meta["spend_stop"] = why; print("SPEND STOP", why, flush=True); break
        ref = G["REFERRALS"][idx - 1]
        out = root / f"{pos}-{ref.key}"; out.mkdir()
        run_id = f"{a.tag}-{a.arm}-{a.mode}-b{a.block:02d}-{ref.key}"
        gate = wait_quiet(float(os.environ.get("GATE_LOAD1", "8"))); gate["foreign_at_start"] = foreign_users(); sampler = LoadSampler(); c0 = container_started()
        start_local = time.strftime("%H:%M:%S")
        if a.mode == "warm":
            marks = {"t0": time.time()}
            res = execute(a.arm, idx, out, marks, fixture, browser, client, run_id); res["marks"] = marks
            (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
        else:
            env = dict(os.environ)
            if a.arm != "agent-sonnet":
                env.pop("ANTHROPIC_API_KEY", None)
            t0 = time.time()
            p = subprocess.run([sys.executable, __file__, "one", "--arm", a.arm, "--ref", str(idx), "--out", str(out),
                                "--t0", repr(t0), "--run-id", run_id], env=env, stdout=open(out / "child.log", "w"), stderr=subprocess.STDOUT)
            try:
                res = json.loads((out / "result.json").read_text())
            except Exception:
                res = {"error": f"child exit {p.returncode}, no result", "correct": False, "marks": {"t0": t0}, "ref": ref.key, "mismatch": ref.mismatch}
        load = sampler.stop(); c1 = container_started()
        res["load"] = {k: v for k, v in load.items() if k != "samples"}; res["load_samples"] = load["samples"]; res["gate"] = gate
        res["foreign_after"] = foreign_users(); res["valid"] = (c0 == c1 == cstart)
        (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
        ph = phases(res["marks"])
        row = {"engine_tag": a.tag, "engine_sha": sha[:8], "arm": a.arm, "mode": a.mode, "block": a.block, "pos": pos, "ref": ref.key,
               "mismatch": ref.mismatch, "valid": res["valid"], "correct": res.get("correct"), "new_charts": res.get("new_charts"),
               "readback": res.get("readback"), "outcome": res.get("outcome"), "error": res.get("error"), **{k: ph.get(k) for k in CSV_COLS if k in ph},
               "engine_total_s": round(res["engine_total_ms"] / 1000, 3) if res.get("engine_total_ms") else None,
               "model_wait_s": res.get("model_wait_s"), "action_s": res.get("action_s"), "actions": res.get("actions"),
               "model_calls": res.get("model_calls"), "screens_sent": res.get("screens_sent"), "local_screenshots": res.get("local_screenshots"),
               "cost_usd": res.get("cost_usd"), "load1_mean": round(load["load1_mean"] or 0, 2), "load1_max": load["load1_max"],
               "gate_waited_s": gate["waited_s"], "gate_gave_up": gate["gave_up"], "start_local": start_local, "dir": str(out)}
        rows.append(row)
        with open(csv_path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
            if new:
                w.writeheader(); new = False
            w.writerow(row)
        if not res["valid"]:
            meta["aborted"] = f"invalid at pos {pos}: fixture container restarted mid-block"
            print("ABORT BLOCK", meta["aborted"], flush=True)
        print(json.dumps({k: row[k] for k in ("arm", "mode", "block", "ref", "valid", "correct", "readback", "outcome", "wall", "task", "cost_usd", "load1_mean", "error")}), flush=True)
        if meta.get("aborted"):
            break
    if browser:
        browser.close()
    meta["ended"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (root / "block.json").write_text(json.dumps(meta, indent=1))
    for d in sorted(root.glob("*/screen")):
        encode(d)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("block"); b.add_argument("--arm", required=True); b.add_argument("--mode", required=True)
    b.add_argument("--block", type=int, required=True); b.add_argument("--tag", required=True)
    o = sub.add_parser("one"); o.add_argument("--arm"); o.add_argument("--ref", type=int); o.add_argument("--out")
    o.add_argument("--t0", type=float); o.add_argument("--run-id")
    a = ap.parse_args()
    cmd_block(a) if a.cmd == "block" else cmd_one(a)
