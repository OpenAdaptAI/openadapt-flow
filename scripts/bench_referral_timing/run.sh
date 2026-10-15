#!/bin/sh
# usage: run.sh ENGINE_WT TAG ARM MODE BLOCK
set -e
wt=$1; tag=$2; arm=$3; mode=$4; blk=$5
avail=$(df -g /System/Volumes/Data | awk 'NR==2{print $4}')
[ "$avail" -lt 20 ] && { echo "STOP: disk below 20 GB"; exit 1; }
if [ "$arm" = agent-sonnet ]; then set -a; . /Users/abrichr/oa/src/OpenAdapt/.env; set +a; fi
for k in $(env | sed -n 's/^\([A-Z_]*_\(API_KEY\|TOKEN\|DSN\)\)=.*/\1/p'); do [ "$k" = ANTHROPIC_API_KEY ] || unset $k; done
export DOCKER_HOST='unix:///var/folders/35/pqjnmmwx24d2mtf4frt5dhcc0000gn/T/podman/openadapt-benchmark-api.sock'
export PYTHONPATH=$wt PYTHONUNBUFFERED=1
mkdir -p /Users/abrichr/oa/scratch/benchmark/logs
exec /Users/abrichr/oa/worktrees/flow-openemr-stop/.venv/bin/python /Users/abrichr/oa/scratch/benchmark/harness/bench.py block --arm $arm --mode $mode --block $blk --tag $tag >> /Users/abrichr/oa/scratch/benchmark/logs/$tag.log 2>&1
