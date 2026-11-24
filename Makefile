ROBOT      := pollen@reachy-mini.local
ROBOT_DIR  := /home/pollen/reachy_agent
ROBOT_PY   := /venvs/apps_venv/bin/python
MAC_VENV   := .venv
MAC_LOG    := /tmp/reachy_mac_server.log
# Outside robot_agent/, so `make sync --delete` keeps it.
ROBOT_LOG  := $(ROBOT_DIR)/agent.log
RUFF       := $(MAC_VENV)/bin/ruff

.DEFAULT_GOAL := help

.PHONY: help up run sync robot-deps robot-wifi mac-server dev lint format test doctor logs spotify-auth

help:
	@printf '\033[1mReachy — make targets\033[0m\n\n'
	@printf '  \033[1mmake up\033[0m             Everything: Mac server, dashboard, and the robot (waited for, synced, run)\n'
	@printf '  \033[1mmake run\033[0m            Sync, stop a running agent, then run the robot agent in the foreground over SSH\n'
	@printf '  \033[1mmake sync\033[0m           Push robot_agent/ + .env to the robot (rsync --delete)\n'
	@printf '  \033[1mmake robot-deps\033[0m     Install the robot'"'"'s Python dependencies\n'
	@printf '  \033[1mmake robot-wifi\033[0m     Tie the robot'"'"'s Wi-Fi to its access point: no background scan, no power save\n'
	@printf '  \033[1mmake mac-server\033[0m     Run the FastAPI server on the Mac (port 7777)\n'
	@printf '  \033[1mmake dev\033[0m            mac-server + Vite dashboard (port 5077); Ctrl-C stops both\n'
	@printf '  \033[1mmake lint\033[0m           ruff, prettier, eslint, knip and the size budget (read-only)\n'
	@printf '  \033[1mmake format\033[0m         Apply the ruff / prettier / eslint fixers\n'
	@printf '  \033[1mmake test\033[0m           pytest (offline) + vitest\n'
	@printf '  \033[1mmake doctor\033[0m         Check the tools, the .env keys and the robot'"'"'s google-genai\n'
	@printf '  \033[1mmake logs\033[0m           Tail the Mac server and the robot agent in one shell\n'
	@printf '  \033[1mmake spotify-auth\033[0m   One-shot Spotify authorisation (the Mac server must run)\n'

# ----- Run --------------------------------------------------------------------

UP_ENV := VENV="$(MAC_VENV)" MAC_LOG="$(MAC_LOG)" ROBOT="$(ROBOT)" ROBOT_DIR="$(ROBOT_DIR)" ROBOT_PY="$(ROBOT_PY)" \
	ROBOT_LOG="$(ROBOT_LOG)"

up:
	@$(UP_ENV) bash scripts/up.sh

# --delete(-excluded): nothing written inside robot_agent/ on the robot survives a deploy, __pycache__ included.
sync:
	rsync -avz --delete --delete-excluded --exclude __pycache__ --exclude '*.pyc' robot_agent $(ROBOT):$(ROBOT_DIR)/
	rsync -avz .env $(ROBOT):$(ROBOT_DIR)/

# An agent already running (`make up`'s, or one a dropped link left) is stopped first, as `make up` does. tee
# ignores Ctrl-C and the hang-up, so the agent's shutdown (sleep pose, session_end) is still logged. -tt forces a
# terminal even when make runs without one: killing ssh then hangs it up and the agent gets its SIGHUP.
run: sync
	@bash scripts/stop_agent.sh $(ROBOT) $(ROBOT_PY) $(ROBOT_DIR)
	ssh -tt $(ROBOT) 'cd $(ROBOT_DIR) && MAC_AGENT_HOSTS="$(shell bash scripts/mac_hosts.sh)" $(ROBOT_PY) -u -m robot_agent 2>&1 | (trap "" HUP; exec tee -i -a $(ROBOT_LOG))'

# Not part of `make run`: it changes nothing on a normal deploy. The google-genai range is pyproject.toml's dev
# extra, the one CI runs; it is also in robot_agent/boot.py (the agent refuses to boot outside it) and scripts/doctor.sh.
robot-deps:
	ssh -t $(ROBOT) '$(ROBOT_PY) -m pip install --upgrade "google-genai>=2.24,<3" "Pillow>=10.0"'

# Once per network the robot joins, not part of a deploy: `make robot-wifi WIFI=unlock` puts it back as it was (before
# changing the box: locked, the robot joins that access point alone), `WIFI=status` says where it stands.
robot-wifi:
	@bash scripts/robot_wifi.sh $(or $(WIFI),lock) $(ROBOT)

mac-server:
	$(MAC_VENV)/bin/python -m mac_server 2>&1 | tee -i -a $(MAC_LOG)

dev:
	@$(UP_ENV) bash scripts/up.sh --no-robot

# ----- Quality ----------------------------------------------------------------

lint:
	@bash scripts/lint.sh "$(RUFF)" "$(MAC_VENV)/bin/python"

format:
	$(RUFF) check --fix robot_agent mac_server scripts tests
	$(RUFF) format robot_agent mac_server scripts tests
	cd frontend && npx --no-install prettier --write . && npx --no-install eslint . --fix

test:
	$(MAC_VENV)/bin/pytest
	cd frontend && npx --no-install vitest run

doctor:
	@bash scripts/doctor.sh

# ----- Debug ------------------------------------------------------------------

logs:
	@bash scripts/logs.sh "$(ROBOT)" "$(MAC_LOG)" "$(ROBOT_LOG)"

SPOTIFY_LOGIN := http://127.0.0.1:7777/api/spotify/login

spotify-auth:
	@curl -s -o /dev/null --max-time 2 http://127.0.0.1:7777/api/settings || \
		{ echo "The Mac server is not running: start it with make mac-server, then run make spotify-auth again."; exit 1; }
	@echo "Spotify authorisation, on this Mac: $(SPOTIFY_LOGIN)"
	@open "$(SPOTIFY_LOGIN)" 2>/dev/null || echo "Open the link above in your browser."
