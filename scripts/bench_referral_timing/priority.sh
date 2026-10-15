#!/bin/sh
# Priority order (Richard 23:36): primary OpenAdapt arm (main + DOM bundle) and agent, warm, first; then the rest.
W=/Users/abrichr/oa/src/agent-worktrees/bench-engine-0c642bc0; T=main-0c642bc0; H=/Users/abrichr/oa/scratch/benchmark/harness
while tmux has-session -t refbench 2>/dev/null; do sleep 15; done
rm -f /Users/abrichr/oa/scratch/benchmark/STOP
for k in 2 3 4 5 6 7 8 9 10; do for a in replay-dom agent-sonnet; do
  [ -e /Users/abrichr/oa/scratch/benchmark/STOP ] && exit 0
  echo "$(date '+%F %T') block $k $a warm (priority)" >> /Users/abrichr/oa/scratch/benchmark/logs/$T.schedule.log
  $H/run.sh $W $T $a warm $k
done; done
$H/schedule.sh $W $T 2 10 3
