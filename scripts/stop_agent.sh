#!/usr/bin/env bash
# Stops the robot agent already running on the robot, if any, before `make run` or `make up` starts theirs: a second
# agent would fight the first over the motors and the audio. Killed only past its own bound (45 s): ~10 s to end its
# conversation and reach the sleep pose, and up to 30 s more when an app holds it as a controller, freed, until it is
# let go within reach (tests/test_cross.py). It leaves <robot dir>/agent.stopped behind: a `make up` whose agent this
# stops then leaves the robot to this deploy, whatever that agent's exit (scripts/up.sh).
# Usage: scripts/stop_agent.sh <robot> <robot python> <robot dir> [ssh option…]
set -u
ROBOT="$1"
ROBOT_PY="$2"
STOPPED="$3/agent.stopped"
shift 3
AGENT="^$ROBOT_PY -u -m robot_agent" # pgrep pattern: the agent itself, not the shell that launched it

pids="$(ssh "$@" "$ROBOT" "pgrep -f '$AGENT'" 2>/dev/null)" || exit 0
echo "an agent already runs on the robot (pid $(echo $pids)): stopping it"
ssh "$@" "$ROBOT" "touch '$STOPPED'; pkill -TERM -f '$AGENT'; for _ in \$(seq 90); do pgrep -f '$AGENT' >/dev/null || exit 0; sleep 0.5; done; pkill -KILL -f '$AGENT'"
