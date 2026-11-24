#!/usr/bin/env bash
# Check what uv/npm cannot install for us: the tools, the .env keys (names only, never values) and
# the robot's google-genai; and that the .venv is uv.lock's, as CI installs it. Exits non-zero when
# something required is missing.
set -u
ROBOT="${ROBOT:-pollen@reachy-mini.local}"
ROBOT_PY=/venvs/apps_venv/bin/python
MIN_GENAI=2.24.0 # also robot_agent/boot.py, robot-deps (Makefile) and the dev extra of pyproject.toml
NEXT_GENAI=3.0.0  # a major CI never ran: the agent refuses it
SYNC='uv sync --locked --extra dev --python 3.12' # CI's install, at the Mac's Python

if [ -t 1 ]; then RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'; else RED=''; GREEN=''; YELLOW=''; BOLD=''; OFF=''; fi
missing=0
ok() { printf '  %sOK%s       %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '  %sWARN%s     %s\n' "$YELLOW" "$OFF" "$1"; }
fail() { printf '  %sMISSING%s  %s\n' "$RED" "$OFF" "$1"; missing=$((missing + 1)); }
at_least() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]; } # at_least MIN VERSION

printf '%sMac%s\n' "$BOLD" "$OFF"
if [ -x .venv/bin/python ]; then
    v="$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    if at_least 3.11 "$v"; then ok ".venv Python $v"; else fail ".venv Python $v (need >= 3.11)"; fi
    .venv/bin/python -c 'import fastapi, uvicorn, pydantic, requests' 2>/dev/null && ok 'server packages' ||
        fail "server packages — $SYNC"
    if ! command -v uv >/dev/null; then
        : # reported with the tools
    elif ! lock="$(uv export --locked --extra dev --no-hashes --no-emit-project -q 2>/dev/null)"; then
        warn 'uv.lock is behind pyproject.toml — uv lock'
    else
        plan="$(uv pip sync --dry-run --python .venv/bin/python - <<<"$lock" 2>&1)"
        if grep -q 'Would make no changes' <<<"$plan"; then
            ok '.venv is uv.lock'
        else
            warn ".venv is not uv.lock ($(grep -c '^ - ' <<<"$plan") to remove, $(grep -c '^ + ' <<<"$plan") to install) — $SYNC"
        fi
    fi
else
    fail ".venv — $SYNC"
fi
# CI's uv: another may write a lock CI's refuses. Its release is installed by uv's standalone installer: `uv self update`
# refuses a uv installed another way, or whose receipt names another place. Below frontend/package.json's engines floor
# (CI's npm), npm refuses to install the frontend (frontend/.npmrc).
ci_uv="$(sed -n "s/^[[:space:]]*version: '\([0-9.]*\)'\$/\1/p" .github/workflows/ci.yml 2>/dev/null | head -n1)"
npm_floor="$(sed -n 's/^[[:space:]]*"npm":[[:space:]]*">=\([0-9.]*\)".*/\1/p' frontend/package.json 2>/dev/null)"
for bin in uv node npm rsync ssh; do
    if ! command -v "$bin" >/dev/null; then
        fail "$bin"
    elif [ "$bin" = uv ] && [ -n "$ci_uv" ] && [ "$(uv --version | cut -d' ' -f2)" != "$ci_uv" ]; then
        warn "uv $(uv --version | cut -d' ' -f2), not CI's $ci_uv — curl -LsSf https://astral.sh/uv/$ci_uv/install.sh | sh"
    elif [ "$bin" = npm ] && [ -n "$npm_floor" ] && ! at_least "$npm_floor" "$(npm --version)"; then
        fail "npm $(npm --version) < $npm_floor, the frontend's floor — npm install -g npm@$npm_floor"
    else
        ok "$bin"
    fi
done

printf '%s.env%s\n' "$BOLD" "$OFF"
if [ -f .env ]; then
    for key in GEMINI_API_KEY MAC_AGENT_URL MAC_AGENT_API_KEY SPOTIFY_CLIENT_ID SPOTIFY_CLIENT_SECRET; do
        if grep -Eq "^${key}=.+" .env; then ok "$key set"; else warn "$key empty or absent"; fi
    done
else
    fail '.env at the repo root'
fi

printf '%sRobot (%s)%s\n' "$BOLD" "$ROBOT" "$OFF"
out="$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$ROBOT" \
    "$ROBOT_PY -c 'import google.genai as g, PIL; print(g.__version__)'" 2>/dev/null | tail -n1)"
if [ -z "$out" ]; then
    warn 'unreachable, or google-genai/Pillow missing — make robot-deps'
elif ! at_least "$MIN_GENAI" "$out"; then
    fail "google-genai $out < $MIN_GENAI — make robot-deps"
elif at_least "$NEXT_GENAI" "$out"; then
    fail "google-genai $out, a major CI never ran (need < $NEXT_GENAI) — make robot-deps"
else
    ok "google-genai $out, Pillow"
fi

[ "$missing" -eq 0 ] && printf '%sAll required pieces are here.%s\n' "$GREEN" "$OFF"
exit $((missing > 0))
