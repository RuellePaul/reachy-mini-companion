#!/usr/bin/env bash
# The Mac server's log and the robot agent's, in one shell, prefixed [mac] / [robot]. Both are files the
# make targets tee to: `make mac-server` / `make dev` write MAC_LOG, `make run` writes ROBOT_LOG on the
# robot. Ctrl-C stops both tails.  Usage: scripts/logs.sh <robot ssh target> <mac log> <robot log>
set -u
ROBOT="${1:-pollen@reachy-mini.local}"
MAC_LOG="${2:-/tmp/reachy_mac_server.log}"
ROBOT_LOG="${3:-/home/pollen/reachy_agent/agent.log}"

if [ -t 1 ]; then C_MAC=$'\033[36m'; C_ROBOT=$'\033[35m'; C_OFF=$'\033[0m'; else C_MAC=''; C_ROBOT=''; C_OFF=''; fi

touch "$MAC_LOG"
( tail -F -n 30 "$MAC_LOG" 2>/dev/null | sed -l "s/^/${C_MAC}[mac]${C_OFF} /" ) &
MAC_PID=$!
( ssh -o ConnectTimeout=5 "$ROBOT" "tail -F -n 30 $ROBOT_LOG" 2>&1 | sed -l "s/^/${C_ROBOT}[robot]${C_OFF} /" ) &
ROBOT_PID=$!

stop() {
    pkill -P "$MAC_PID" 2>/dev/null; pkill -P "$ROBOT_PID" 2>/dev/null
    kill "$MAC_PID" "$ROBOT_PID" 2>/dev/null
    exit 0
}
trap stop INT TERM
wait
