#!/bin/sh
# Round-robin schedule over arm x mode, blocks 1..10 (see PROTOCOL.md). usage: schedule.sh ENGINE_WT TAG [FIRST] [LAST] [AGENT_COLD_MAX]
wt=$1; tag=$2; first=${3:-1}; last=${4:-10}; acold=${5:-3}
H=/Users/abrichr/oa/scratch/benchmark/harness
for k in $(seq $first $last); do
  for spec in "replay-main warm" "replay-dom warm" "agent-sonnet warm" "replay-main cold" "replay-dom cold" "agent-sonnet cold"; do
    set -- $spec
    [ "$1" = agent-sonnet ] && [ "$2" = cold ] && [ $k -gt $acold ] && continue
    [ "$1" = agent-sonnet ] && [ "${SKIP_AGENT:-0}" = 1 ] && continue
    [ -e /Users/abrichr/oa/scratch/benchmark/STOP ] && { echo "STOP file"; exit 0; }
    echo "$(date '+%F %T') block $k $1 $2" >> /Users/abrichr/oa/scratch/benchmark/logs/$tag.schedule.log
    $H/run.sh $wt $tag $1 $2 $k || echo "$(date '+%F %T') FAILED block $k $1 $2 rc=$?" >> /Users/abrichr/oa/scratch/benchmark/logs/$tag.schedule.log
  done
done
echo "$(date '+%F %T') DONE" >> /Users/abrichr/oa/scratch/benchmark/logs/$tag.schedule.log
