#!/bin/sh
# usage: run.sh ENGINE_WT TAG ARM MODE BLOCK
set -e
wt=$1; tag=$2; arm=$3; mode=$4; blk=$5
# Amendment 2026-10-14 23:45 (see PROTOCOL.md): shared machine too busy for the full plan.
case "$arm" in replay-*) [ "$mode" = cold ] && [ "$blk" -gt "${REPLAY_COLD_MAX:-5}" ] && { echo "skip $arm cold $blk (amendment)"; exit 0; };; esac
b=$(printf "/Users/abrichr/oa/scratch/benchmark/runs/%s/%s-%s/block-%02d" $tag $arm $mode $blk); [ -e $b/block.json ] && { echo "skip done $b"; exit 0; }
export GATE_LOAD1=${GATE_LOAD1:-12}
avail=$(df -g /System/Volumes/Data | awk 'NR==2{print $4}')
[ "$avail" -lt 20 ] && { echo "STOP: disk below 20 GB"; exit 1; }
if [ "$arm" = agent-sonnet ]; then set -a; . /Users/abrichr/oa/src/OpenAdapt/.env; set +a; fi
for k in $(env | sed -n 's/^\([A-Z_]*_\(API_KEY\|TOKEN\|DSN\)\)=.*/\1/p'); do [ "$k" = ANTHROPIC_API_KEY ] || unset $k; done
export DOCKER_HOST='unix:///var/folders/35/pqjnmmwx24d2mtf4frt5dhcc0000gn/T/podman/openadapt-benchmark-api.sock'
export PYTHONPATH=$wt PYTHONUNBUFFERED=1
mkdir -p /Users/abrichr/oa/scratch/benchmark/logs
exec /Users/abrichr/oa/worktrees/flow-openemr-stop/.venv/bin/python /Users/abrichr/oa/scratch/benchmark/harness/bench.py block --arm $arm --mode $mode --block $blk --tag $tag >> /Users/abrichr/oa/scratch/benchmark/logs/$tag.log 2>&1
