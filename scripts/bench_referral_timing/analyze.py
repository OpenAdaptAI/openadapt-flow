"""Summarise data/runs.csv -> data/summary.md and data/summary.json (see PROTOCOL.md 'Statistics')."""
import csv, json, sys
from collections import defaultdict
import numpy as np
from pathlib import Path
B = Path("/Users/abrichr/oa/scratch/benchmark")
rng = np.random.default_rng(20261014)
rows = list(csv.DictReader(open(B / "data/runs.csv")))
f = lambda r, k: float(r[k]) if r.get(k) not in ("", None, "None") else None
T = lambda r: r["valid"] == "True"
ARMS = ["replay-main", "replay-dom", "agent-sonnet"]


def stats(x):
    x = np.array([v for v in x if v is not None], float)
    if len(x) == 0:
        return None
    bs = np.median(rng.choice(x, (10000, len(x))), axis=1)
    sd = x.std(ddof=1) if len(x) > 1 else 0
    from scipy import stats as st
    tq = st.t.ppf(0.975, len(x) - 1) if len(x) > 1 else float("nan")
    return dict(n=len(x), median=np.median(x), p10=np.percentile(x, 10), p90=np.percentile(x, 90), min=x.min(), max=x.max(),
                mean=x.mean(), med_lo=np.percentile(bs, 2.5), med_hi=np.percentile(bs, 97.5),
                mean_lo=x.mean() - tq * sd / np.sqrt(len(x)), mean_hi=x.mean() + tq * sd / np.sqrt(len(x)))


def diffci(a, b):
    a = np.array(a, float); b = np.array(b, float)
    d = np.median(rng.choice(a, (10000, len(a))), 1) - np.median(rng.choice(b, (10000, len(b))), 1)
    return np.median(a) - np.median(b), np.percentile(d, 2.5), np.percentile(d, 97.5)


out = []; J = {}
tags = sorted({r["engine_tag"] for r in rows})
for tag in tags:
    R = [r for r in rows if r["engine_tag"] == tag]
    sha = sorted({r["engine_sha"] for r in R})
    out.append(f"\n## Engine {tag} (sha {', '.join(sha)})\n")
    groups = [(a, m) for a in ARMS for m in ("warm", "cold") if any(r["arm"] == a and r["mode"] == m for r in R)]
    out.append("### End-to-end wall clock, normal referrals, valid and correct runs (s)\n")
    out.append("| arm | start | n | median | 95% CI (median) | p10 | p90 | min | max | mean (95% CI) |")
    out.append("|---|---|---:|---:|---|---:|---:|---:|---:|---|")
    walls = {}
    for a, m in groups:
        G = [r for r in R if r["arm"] == a and r["mode"] == m and T(r) and r["mismatch"] == "False" and r["correct"] == "True"]
        s = stats([f(r, "wall") for r in G]); walls[(a, m)] = [f(r, "wall") for r in G]
        if s:
            out.append(f"| {a} | {m} | {s['n']} | {s['median']:.1f} | {s['med_lo']:.1f}-{s['med_hi']:.1f} | {s['p10']:.1f} | {s['p90']:.1f} | {s['min']:.1f} | {s['max']:.1f} | {s['mean']:.1f} ({s['mean_lo']:.1f}-{s['mean_hi']:.1f}) |")
            J[f"{tag}|{a}|{m}|wall"] = s
    out.append("\n### Phase medians, normal referrals (s); wall = sum of phases\n")
    out.append("| arm | start | proc start + imports | browser | sign-in + form | recorder | task | read-back | wall | wall excl. recorder | task: model wait | task: browser actions |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for a, m in groups:
        G = [r for r in R if r["arm"] == a and r["mode"] == m and T(r) and r["mismatch"] == "False" and r["correct"] == "True"]
        if not G:
            continue
        med = lambda k: np.median([f(r, k) for r in G if f(r, k) is not None]) if any(f(r, k) is not None for r in G) else float("nan")
        pi = np.median([(f(r, "proc_start") or 0) + (f(r, "imports") or 0) for r in G]) if m == "cold" else 0.0
        mw = med("model_wait_s"); ac = med("action_s")
        out.append(f"| {a} | {m} | {pi:.2f} | {med('browser_launch'):.2f} | {med('signin_form'):.2f} | {med('recorder_start'):.2f} | {med('task'):.2f} | {med('readback_s'):.2f} | {med('wall'):.2f} | {med('wall_ex_recorder'):.2f} | {'' if np.isnan(mw) else f'{mw:.1f}'} | {'' if np.isnan(ac) else f'{ac:.1f}'} |")
    out.append("\n### Correctness, effort, cost (all valid runs)\n")
    out.append("| arm | start | normal runs saved 12/12 | duplicate case (r6) halted, no chart | errors | median actions | median screens sent to a model | median model calls | cost per run median (min-max) | total cost |")
    out.append("|---|---|---|---|---:|---:|---:|---:|---|---:|")
    for a, m in groups:
        G = [r for r in R if r["arm"] == a and r["mode"] == m and T(r)]
        N = [r for r in G if r["mismatch"] == "False"]; M = [r for r in G if r["mismatch"] == "True"]
        c = [f(r, "cost_usd") or 0 for r in G]
        mm = lambda k, S=G: np.median([f(r, k) for r in S if f(r, k) is not None]) if S else float("nan")
        out.append(f"| {a} | {m} | {sum(r['correct']=='True' for r in N)}/{len(N)} | {sum(r['correct']=='True' for r in M)}/{len(M)} | {sum(bool(r['error']) for r in G)} | {mm('actions'):.0f} | {mm('screens_sent'):.0f} | {mm('model_calls'):.0f} | ${np.median(c):.3f} (${min(c):.3f}-${max(c):.3f}) | ${sum(c):.2f} |")
    inval = [r for r in R if not T(r)]
    out.append(f"\nInvalid runs (fixture restarted mid-run), excluded: {len(inval)}")
    bad = [r for r in R if T(r) and r["correct"] != "True"]
    for r in bad:
        out.append(f"- INCORRECT: {r['arm']} {r['mode']} block {r['block']} {r['ref']}: new_charts={r['new_charts']} readback={r['readback']} outcome={r['outcome']} error={r['error'][:150]}")
    out.append("\n### Median wall per patient (s), normal referrals\n")
    refs = sorted({r["ref"] for r in R if r["mismatch"] == "False"})
    out.append("| arm | start | " + " | ".join(x.split("-", 1)[0] for x in refs) + " |")
    out.append("|---|---|" + "---:|" * len(refs))
    for a, m in groups:
        cells = []
        for ref in refs:
            v = [f(r, "wall") for r in R if r["arm"] == a and r["mode"] == m and r["ref"] == ref and T(r) and r["correct"] == "True"]
            cells.append(f"{np.median(v):.1f} (n={len(v)})" if v else "-")
        out.append(f"| {a} | {m} | " + " | ".join(cells) + " |")
    out.append("\n### r6 duplicate case wall to verified halt (s), median (n)\n")
    for a, m in groups:
        v = [f(r, "wall") for r in R if r["arm"] == a and r["mode"] == m and r["mismatch"] == "True" and T(r)]
        if v:
            out.append(f"- {a} {m}: {np.median(v):.1f} (n={len(v)}, min {min(v):.1f}, max {max(v):.1f})")
    out.append("\n### Differences of medians (normal referrals), bootstrap 95% CI (s)\n")
    for (a1, m1), (a2, m2) in [(("replay-dom", "warm"), ("replay-main", "warm")), (("replay-dom", "cold"), ("replay-main", "cold")),
                               (("replay-main", "warm"), ("agent-sonnet", "warm")), (("replay-dom", "warm"), ("agent-sonnet", "warm")),
                               (("replay-main", "cold"), ("replay-main", "warm")), (("replay-dom", "cold"), ("replay-dom", "warm")),
                               (("agent-sonnet", "cold"), ("agent-sonnet", "warm"))]:
        if walls.get((a1, m1)) and walls.get((a2, m2)):
            d, lo, hi = diffci(walls[(a1, m1)], walls[(a2, m2)])
            out.append(f"- {a1} {m1} minus {a2} {m2}: {d:+.1f} s (95% CI {lo:+.1f} to {hi:+.1f})")
    lo = [f(r, "load1_mean") for r in R if T(r)]
    out.append(f"\nLoad (1-min average, mean during each run): median {np.median(lo):.1f}, max {max(lo):.1f}; gate gave up on {sum(r['gate_gave_up']=='True' for r in R)} runs.")
(B / "data/summary.md").write_text("\n".join(out) + "\n")
(B / "data/summary.json").write_text(json.dumps(J, indent=1, default=float))
print("\n".join(out))
