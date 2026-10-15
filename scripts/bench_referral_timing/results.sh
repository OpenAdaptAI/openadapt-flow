#!/bin/sh
# Regenerate data/summary.md and RESULTS.md (status header + tables + fixed caveats).
cd /Users/abrichr/oa/scratch/benchmark
/Users/abrichr/oa/worktrees/flow-openemr-stop/.venv/bin/python harness/analyze.py > /dev/null 2>&1
{
echo "# OpenEMR referral timing benchmark: results"
echo
echo "Status: $(tail -1 logs/main-0c642bc0.schedule.log 2>/dev/null) (regenerated $(date '+%F %H:%M ET')). Agent spend so far: \$$(python3 -c "import json;print(round(json.load(open('spend.json'))['total_list_price_usd'],2))" 2>/dev/null) of the \$15 cap."
echo
echo "Protocol: PROTOCOL.md. Raw per-run data: data/runs.csv (one row per run, all phases). Per-run evidence: runs/<engine>/<arm>-<mode>/block-NN/<pos>-<referral>/ (result.json, engine/ report or agent sent/ screenshots, screen/screen.mp4 footage, final.png)."
cat data/summary.md
cat CAVEATS.md 2>/dev/null
} > RESULTS.md
