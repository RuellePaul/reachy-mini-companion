#!/usr/bin/env bash
# The whole project in one shell: the Mac server, the dashboard and the robot agent, prefixed [mac] / [web] /
# [robot]. The robot is waited for (SSH, then its daemon ready), synced and run as `make run` does; when the
# link drops or the agent fails, it is waited for again. The robot is given up after ROBOT_WAIT_S without an
# answer, after 3 agent stops within a minute of their start, or to whoever stopped the agent (a `make run`): the
# dashboard stays up. Ctrl-C, or the Mac server or the dashboard exiting, stops everything; the agent takes the
# sleep pose on its SSH hang-up. `--no-robot`: the Mac side only (`make dev`). Works with macOS's bash 3.2 (no
# `wait -n`).
set -u
cd "$(dirname "$0")/.." || exit 1
VENV="${VENV:-.venv}"
MAC_LOG="${MAC_LOG:-/tmp/reachy_mac_server.log}" # what `make logs` tails
ROBOT="${ROBOT:-pollen@reachy-mini.local}"
ROBOT_DIR="${ROBOT_DIR:-/home/pollen/reachy_agent}"
ROBOT_PY="${ROBOT_PY:-/venvs/apps_venv/bin/python}"
ROBOT_LOG="${ROBOT_LOG:-$ROBOT_DIR/agent.log}"
STOPPED="$ROBOT_DIR/agent.stopped" # left by scripts/stop_agent.sh: another deploy stopped the agent
ROBOT_WAIT_S="${ROBOT_WAIT_S:-180}" # switched on, its daemon was ready at uptime 1 min
WITH_ROBOT=1
[ "${1:-}" = --no-robot ] && WITH_ROBOT=0

if [ -t 1 ]; then
    C_MAC=$'\033[36m'; C_WEB=$'\033[35m'; C_ROBOT=$'\033[33m'; C_UP=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_MAC=''; C_WEB=''; C_ROBOT=''; C_UP=''; C_OFF=''
fi
CR=$'\r' # a remote terminal ends its lines with \r\n
prefix() { sed -l -e "s/$CR\$//" -e "s/^/$1 /"; }
note() { printf '%s[up]%s %s\n' "$C_UP" "$C_OFF" "$*"; }
say() { printf '%s» %s%s\n' "$C_UP" "$*" "$C_OFF"; } # the robot loop's own lines, among the agent's
# A .env value as the Mac's python-dotenv reads it (the robot's boot.load_env reads it alike).
env_value() {
    [ -f .env ] && "$VENV/bin/python" -c 'import sys, dotenv; print(dotenv.dotenv_values(".env").get(sys.argv[1]) or "")' "$1"
}

# BatchMode: no password prompt nobody could answer. ServerAlive: a robot switched off is noticed in ~15 s.
SSH=(ssh -o ConnectTimeout=5 -o BatchMode=yes -o ServerAliveInterval=5 -o ServerAliveCountMax=3)

# 0: SSH answers and the daemon is running and ready. 1: not yet (the reason on stdout). 2: never will by waiting.
robot_status() {
    local out code
    out="$("${SSH[@]}" "$ROBOT" 'curl -s -m 2 http://localhost:8000/api/daemon/status' 2>&1)"
    code=$?
    # 255: ssh's own failure, its message last. Any other code is the remote command's, whose output (a daemon's error
    # may name a device it was denied) never means that SSH refuses this Mac.
    if [ "$code" -eq 255 ]; then
        case "$out" in
            *'Permission denied'* | *'Host key verification failed'*) echo "${out##*$'\n'}"; return 2 ;;
        esac
        echo "no SSH answer (${out##*$'\n'})"
        return 1
    fi
    case "$out" in
        *'"state":"running"'*'"ready":true'*) return 0 ;;
        '') echo 'SSH answers, the daemon does not yet' ;;
        *) echo "the daemon is not ready yet (state: $(printf '%s' "$out" | sed -n 's/.*"state":"\([^"]*\)".*/\1/p'))" ;;
    esac
    return 1
}

wait_for_robot() {
    local start=$SECONDS last='' why status
    while :; do
        why="$(robot_status)"
        status=$?
        [ "$status" -eq 0 ] && return 0
        [ "$why" != "$last" ] && say "waiting for the robot ($((SECONDS - start)) s): $why" && last="$why"
        if [ "$status" -eq 2 ]; then say 'SSH refuses this Mac: fix the key, then run make run'; return 1; fi
        if [ $((SECONDS - start)) -ge "$ROBOT_WAIT_S" ]; then
            say "no robot after $ROBOT_WAIT_S s: given up, the dashboard stays up. Switch it on, then make run"
            return 1
        fi
        sleep 3
    done
}

sync_agent() {
    local try
    for try in 1 2 3; do
        rsync -az --delete --delete-excluded --exclude __pycache__ --exclude '*.pyc' -e "${SSH[*]}" robot_agent "$ROBOT:$ROBOT_DIR/" &&
            rsync -az -e "${SSH[*]}" .env "$ROBOT:$ROBOT_DIR/" && return 0
        say "rsync failed (attempt $try of 3)"
        sleep 2
    done
    return 1
}

# The robot reads its settings from the Mac: say so now if it reaches none of the Mac's addresses, not at the first
# wake. The agent tries the same ones (robot_agent/config.py): the Mac's own now, then MAC_AGENT_URL's. A bare TCP
# connection: an HTTP probe without the key would be logged as refused.
check_mac_link() {
    local fixed port host targets='' url
    fixed="$(env_value MAC_AGENT_URL)"
    port="$(env_value MAC_AGENT_PORT)"
    host="${fixed%%,*}"
    host="${host#*://}"
    host="${host%%/*}"
    [[ "$host" == *:* ]] && port="${host##*:}"
    for host in $(bash scripts/mac_hosts.sh); do targets+=" $host:${port:-7777}"; done
    for url in ${fixed//,/ }; do
        url="${url#*://}"
        targets+=" ${url%%/*}"
    done
    if [ -z "$targets" ]; then say 'MAC_AGENT_URL is empty in .env and this Mac has no address: every wake will go back to sleep'; return; fi
    "${SSH[@]}" "$ROBOT" "for hp in $targets; do timeout 2 bash -c \"</dev/tcp/\${hp%:*}/\${hp##*:}\" 2>/dev/null && exit 0; done; exit 1" &&
        return 0
    say "the robot reaches the Mac at none of its addresses ($targets ): every wake will go back to sleep"
}

# One left by a dropped link, or by `make run`: the same stop as `make run`'s.
stop_leftover_agent() {
    local line
    bash scripts/stop_agent.sh "$ROBOT" "$ROBOT_PY" "$ROBOT_DIR" "${SSH[@]:1}" |
        while IFS= read -r line; do say "$line"; done
}

robot_loop() {
    local quick=0 started code
    while :; do
        wait_for_robot || return 1
        if ! sync_agent; then
            say 'rsync keeps failing: given up, the dashboard stays up'
            return 1
        fi
        check_mac_link
        stop_leftover_agent
        say "agent starting (its log on the robot: $ROBOT_LOG)"
        started=$SECONDS
        # -tt: a terminal on the robot even from here, so killing this ssh hangs it up and the agent gets SIGHUP.
        # tee outlives the hang-up, so the agent's shutdown still reaches its log. An agent another deploy stopped ends
        # with 0 whatever its own exit: a signal before its handlers (143), or the kill past its bound (137).
        "${SSH[@]}" -tt "$ROBOT" "set -o pipefail; cd $ROBOT_DIR && rm -f $STOPPED &&" \
            "MAC_AGENT_HOSTS='$(bash scripts/mac_hosts.sh)' $ROBOT_PY -u -m robot_agent 2>&1 | (trap '' HUP; exec tee -i -a $ROBOT_LOG);" \
            "code=\$?; [ -e $STOPPED ] && exit 0; exit \$code"
        code=$?
        # 0: a signal ended the agent, or another deploy stopped it, and not this loop (Ctrl-C ends the loop with it): a
        # `make run`, or a hand. Starting ours again would stop theirs, so the last deploy keeps the robot.
        if [ "$code" -eq 0 ]; then
            say 'the agent was stopped from outside (make run?): the robot is left to it, the dashboard stays up'
            return 1
        fi
        if [ $((SECONDS - started)) -ge 60 ]; then quick=0; else quick=$((quick + 1)); fi
        if [ "$quick" -ge 3 ]; then
            say "the agent stopped 3 times within a minute of its start (exit $code): given up, see above"
            return 1
        fi
        if [ "$code" -eq 255 ]; then say 'the link to the robot dropped'; else say "the agent stopped (exit $code)"; fi
        sleep 2
    done
}

( "$VENV/bin/python" -m mac_server 2>&1 | tee -i -a "$MAC_LOG" | prefix "${C_MAC}[mac]${C_OFF}" ) &
MAC_PID=$!
( cd frontend && npm run dev 2>&1 | prefix "${C_WEB}[web]${C_OFF}" ) &
WEB_PID=$!
ROBOT_PID=''

descendants() {
    local kid
    for kid in $(pgrep -P "$1" 2>/dev/null); do echo "$kid"; descendants "$kid"; done
}

# Every process of every tree, gathered before any dies (an orphan leaves its tree), then signalled at once.
cleanup() {
    local pids='' pid
    echo ''
    note 'shutting down…'
    for pid in $MAC_PID $WEB_PID $ROBOT_PID; do pids="$pids $pid $(descendants "$pid")"; done
    kill -TERM $pids 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8; do
        kill -0 $pids 2>/dev/null || break
        sleep 0.5
    done
    kill -KILL $pids 2>/dev/null
    exit 0
}
trap cleanup INT TERM

if [ "$WITH_ROBOT" = 1 ]; then
    MAC_PORT="$(env_value MAC_AGENT_PORT)"
    for _ in $(seq 60); do # the robot checks it can reach the Mac: start it once the server answers
        nc -z 127.0.0.1 "${MAC_PORT:-7777}" 2>/dev/null && break
        kill -0 "$MAC_PID" 2>/dev/null || break
        sleep 0.5
    done
    ( robot_loop 2>&1 | prefix "${C_ROBOT}[robot]${C_OFF}" ) &
    ROBOT_PID=$!
fi

while kill -0 "$MAC_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; do sleep 0.5; done
note 'the Mac server or the dashboard exited: stopping everything'
cleanup
