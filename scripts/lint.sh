#!/usr/bin/env bash
# Every read-only check at once, in parallel, reported one after the other; exits non-zero if any
# failed. Usage: scripts/lint.sh <ruff> <python>
set -u
RUFF="${1:-.venv/bin/ruff}"
PY="${2:-.venv/bin/python}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [ -t 1 ]; then BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; OFF=$'\033[0m'; else BOLD=''; RED=''; GREEN=''; OFF=''; fi

run() {
    local name="$1"; shift
    if "$@" >"$TMP/$name" 2>&1; then echo 0 >"$TMP/$name.status"; else echo 1 >"$TMP/$name.status"; fi
}

PY_PATHS=(robot_agent mac_server scripts tests)
run 1-ruff-check "$RUFF" check "${PY_PATHS[@]}" &
run 2-ruff-format "$RUFF" format --check "${PY_PATHS[@]}" &
run 3-budget "$PY" scripts/budget.py &
(cd frontend && run 4-prettier npx --no-install prettier --check .) &
(cd frontend && run 5-eslint npx --no-install eslint .) &
(cd frontend && run 6-knip npx --no-install knip) &
wait

failed=0
for status in "$TMP"/*.status; do
    name="$(basename "$status" .status)"
    if [ "$(cat "$status")" = 0 ]; then
        printf '%s▸ %s%s  %sOK%s\n' "$BOLD" "${name#*-}" "$OFF" "$GREEN" "$OFF"
    else
        printf '%s▸ %s%s  %sFAILED%s\n' "$BOLD" "${name#*-}" "$OFF" "$RED" "$OFF"
        cat "$TMP/$name"
        failed=1
    fi
done
exit $failed
