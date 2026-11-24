# CLAUDE.md

A Reachy Mini (Pollen Robotics) you talk to. Three processes ship together:

- `robot_agent/` — Python asyncio on the robot. One Gemini Live session (`gemini-3.8-live`, speech to
  speech) per wake; the whole conversation lives here, and the robot's single motion engine.
- `mac_server/` — FastAPI on the Mac (port 7777): settings (a JSON file), Spotify, the command queue the
  robot long-polls, robot health, the studio, scenes and apps, the dashboard's API and WebSocket.
- `frontend/` — React 19 + MUI 7 + Vite (port 5077). Talks only to the Mac.

Two constraints decide every trade-off: **conversational latency** (nothing blocks the robot's event loop,
nothing on the Mac is awaited on a turn's critical path) and **Gemini cost** (every turn re-bills the whole
context, so the context and the session stay small).

« The owner » below is whoever runs the robot and the Mac.

## Docs

- `README.md` — what the project is, and its setup from scratch. `.env.example` — every variable, explained.
- `docs/ARCHITECTURE.md` — the big picture: who talks to whom, a conversation from wake to sleep, the safety rules.
- `docs/CONTRACTS.md` — every route, event, command, WebSocket channel and the app bridge.

## Commands

- `make up` (`scripts/up.sh`) — everything in one shell: the Mac server, the dashboard, then the robot, waited
  for (SSH, then its daemon ready; `ROBOT_WAIT_S`, 180 s), synced and run; waited for again when the link
  drops; a leftover agent on the robot stopped first. One Ctrl-C stops all; the robot given up, the
  dashboard stays. `make run` — rsync `robot_agent/` + `.env` and run the agent in the foreground over SSH.
  The only two ways to deploy. `make sync` uses `--delete`: nothing written inside that tree on the robot
  survives.
- `make robot-deps` — the robot's own dependencies (`google-genai>=2.24,<3`, Pillow). Not part of a deploy.
- `make robot-wifi` (`scripts/robot_wifi.sh`) — once per network: the robot's Wi-Fi tied to the access point it is on
  (NetworkManager then runs no background scan and never roams: each one was seconds without the Mac, a manette
  frozen) and its power save off; `WIFI=unlock` puts it back (before changing the box), `WIFI=status` says.
- `make mac-server`, `make dev` (`make up` without the robot); `make spotify-auth` once.
- `make lint` (ruff, prettier, eslint, knip, size budget), `make format`, `make test` (pytest + vitest),
  `cd frontend && npx tsc -b` (CI runs them all; the pre-commit hook the lint), `make logs`, `make doctor` (what a
  fresh machine lacks, with CI's install for it).
- `.venv/bin/python scripts/smoke_live.py` — one real, short Gemini Live connection (costs money: by hand,
  never in CI). `scripts/export_contract_examples.py` after changing a contract or its example.
- `.venv/bin/python scripts/app_bench.py PAGE.html --check` (what a shipped page must pass), `--shots 800,4000`
  or `--log` — an app's page on a bench, without the robot nor the dashboard: served as the Mac serves it, a stand-in
  dashboard holding the manette, sending pads, answering a replay (`--gpu`: Chrome draws with the Mac's GPU, for a
  page in WebGL; without it, as in the page check, there is none). `scripts/fake_robot.py --port … --daemon …` — a
  stand-in robot for a test Mac server of its own (never the owner's), to try the dashboard and its apps end to end.
- One-shot, by hand: `scripts/robot_glb.py` (rebuilds `frontend/public/robot.glb` from Pollen's URDF).

Always use `.venv/bin/ruff` / `.venv/bin/pytest`: the `ruff` on PATH may be another version.

## The robot agent

`__main__` → `boot.py` (loads `.env`, refuses a google-genai below 2.24 or at 3) → `main.py` (`Robot`). Boot opens
the audio pipelines once, starts the motion thread, takes the sleep pose, starts the Mac threads and loads the
emotion library in the background. It sleeps until an antenna is wiggled (`antennas.py`) or the dashboard sends
`wake`. Waking runs the wake move, the settings fetch (2 s, no retry; a failure plays « confused » and goes back to
sleep) and the Live connect in parallel. Then `Session.run()` until the conversation ends; every ending — Ctrl-C and
a dropped SSH session (SIGHUP) included — reaches the sleep pose.

- `state.py` — `SessionState` and the pure `transition()`; IDLE means the answer is over. The dashboard's
  `phase`. Scenes added no state: a scene is a pending tool call (or a performer task) that mutes the model.
- `session.py` — one conversation: every happening becomes an `Event`; `_on_transition` is the one place a
  state change acts. Keep it under 600 lines and 12 reassigned fields.
- `live.py` (the config, the connect, `usage`), `tools.py` (declarations, all `NON_BLOCKING`, short French
  descriptions; subject modules add theirs through `Tools(extra=…)`), `audio.py`, `mac.py`, `adapters/`
  (`adapters/robot.py` is the only importer of `reachy_mini`).
- `motion.py` — the pure `Mixer` (mode animation, a move over it, slerped blends, the body turn; it never
  reads a clock), stepped at 30 Hz by `MotionRunner`; `rehearse()` replays cues for the studio's 3D preview.
  `wobble.py` (Pollen's motion while it talks, ported: their speech tapper's gains from what the speaker is handed
  (`Tap`), placed on the playback clock, wobble the head over whatever plays; their breathing swings the antennas in
  the talking animation; only under the comfort noise, never asleep or lent; no setting).
  `gaze.py` (regard actif, the setting `gaze_tracking`, off by default: in a conversation, a thread finds one face
  in the head camera (YuNet on small views, then a crop around the face followed, placed against the head pose sent
  when the frame was taken) and the mixer aims the mode's animation at it, damped; a move takes the head whole;
  never in a scene nor with a held body. Quick only under the comfort noise; the microphone open, still until the
  face moved 5°, then at 15°/s. Each aimed frame is asked of the daemon's own IK (`adapters.robot.reach`): the gaze
  gives way rather than let the daemon turn the body).
  `animations.py` (variants as channels scaled by amplitude/speed; keyframed gestures), `animation_names.py`
  (names, labels, sentences, tuning ranges: dependency-free, imported by the Mac), `recorded.py` (emotions
  interpolated by our code, equal to reachy_mini's).
- `studio.py` (the `try` command: a move at once, awake or asleep), `apps.py` (the `app` tool, bridge moves;
  `Builds`: a build asked by voice holds the idle sleep off until it ends, then a note tells the model),
  `notes.py` (a text turn no human said, sent at the next quiet point: it authorises no tool),
  `scene.py` (the `Performer`, `play_scene`; `write_scene` asks the Mac for a new scene, and `Writes` waits for it as
  `Builds` does for an app: once ready a note has the model announce it, then the robot asks the Mac to play it,
  as the dashboard would: a note authorises no tool), `manette.py` (the robot as an app's controller: the body lent, every
  other move dropped; still where it was told, its motion thread frozen, only its profile's motors freed: none for
  « gachettes », the base for « volant » and « paume », the base and the head for « tete »; « paume » holds the
  antennas where they lie in sleep, under the fingers of the hand that turns it; taken back once let go within the
  IK's reach, always before a sleep move or a scene, and at the agent's end; silent while held), `take.py` (what a hand makes the lent body do, taped 50 times a second and played back as it was at an
  app's `replay`: the head where it was in the room, the antennas, the base at its motor's pace), `poses.py` (each
  measure of the lent body sent to the Mac as a datagram, 30 a second: the dashboard's pads. Over TCP the daemon's
  stream stops whenever the box is late handing the robot the Mac's acknowledgements; a datagram waits for none),
  `ear.py` (the
  manette's « voix »: the microphone's level over the room's quiet, a number sent to the Mac ~20 times a second, with
  a body or alone; alone, nothing is held and the robot goes on moving).

Invariants — each one exists because breaking it broke the robot:

- **Turn-taking has one authority: Gemini's server VAD** (silence 800 ms, prefix padding 800 ms, start
  HIGH, end LOW). No local detector, no text heuristics. `interrupted` flushes the speaker.
- **The robot's own noise never reaches Gemini**: while its audio plays, it moves (wake, gesture, emotion,
  body turn, try, app move) and for `SELF_NOISE_TAIL_S` after, and for a whole scene, `ComfortNoise`
  replaces the microphone. `MIC_MUTE_WHILE_TALKING=0` trades barge-in back, scenes excepted.
- **The microphone is read with `asyncio.to_thread`**; **no empty array to the speaker** (the SDK segfaults).
- **`receive()` ends at each interaction's end**: the receive loop re-enters it.
- **A tool runs only if a human spoke or typed in the last 10 s** (`_last_human_at`); else refused silently.
- **One response per call id, never `will_continue`**; a repeated call id is ignored, a move repeated within
  2.5 s is answered without moving; cancellation and CLOSING answer nothing. A physical success is SILENT
  once the robot has spoken since the human's turn, WHEN_IDLE otherwise; Spotify and every error WHEN_IDLE.
- **During a scene** the model's audio and transcript are dropped, every other tool and every app move is
  refused, no idle sleep; `play_scene`'s one answer comes at the curtain, SILENT (the scene was the answer).
  Stops: dashboard, a typed say, an antenna held, sleep. A rehearsal waits on the rehearsed character's lines,
  silent and still: an antenna tap goes on.
- **During a hush** (a quiet app on screen: `AppSummary.quiet`, judged by its builder, a switch in its bar) the robot is
  muted as during a scene (`hush.py`, the session's `performing`), from the end of its answer until its speech is given
  back: the Mac (the app left), an antenna held, or a typed say. So is it while an app holds it as its manette:
  people play, and what its microphone hears may be the app's (`ear.py`: a level, never a sound).
- **Asleep, the wake antennas listen only at rest**: a try or a scene played asleep disarms them; they re-arm
  once nothing moves the robot and it is settled in the sleep pose.
- **Settings are read at wake** and apply at the next one (« Relancer la conversation »). Only the volume
  applies at once. A try plays a move explicitly; it never swaps settings live.
- **Cost caps**: `context_window_compression` (trigger 20 000, target 8 000 tokens), idle sleep, system
  instruction + tool declarations ≤ 10 000 characters (tested: `tests/test_cross.py`, extras included),
  ≤ 25 emotions offered. Never send `thinking_config`.
- **Reconnection**: `go_away` → resume at the next quiet point; a lost socket → backoff 0.5 / 2 / 5 s, at
  most 3 attempts; a network error at connect is never taken for a refusal; `CONNECT_TIMEOUT_S` bounds it.
- **One motion engine**: the 30 Hz thread; studio tries, scenes and app moves all go through it; the Mac
  never commands the daemon's motion. `turn_body` within ±100°. A freed motor is given no target: the
  manette freezes the thread before freeing one, and takes them all back (`enable_motors()`, no ids) before
  writing again. **Never free a motor on the real robot without the owner's consent and presence**: in the
  dashboard, that is the human's own click or key in the app that asks (« Jouer »), told by the dashboard's script
  in the frame and no older than 5 s; an ask that follows none waits for one.

## The Mac server

`server.py` builds one `Mac` object (store, command queue, health, broker, Spotify, and the subjects'
services `studio`, `scenes`, `apps`, each with its own router behind the key); `routes.py` holds the core
routes. Rules: the API key on every route and on the WebSocket (closed 1008 before accept), except the
loopback-only Spotify OAuth; no blocking I/O in an `async def` route; errors are `{"detail": "<French>"}`;
`contracts.py` is the only place payloads, defaults and rules live (examples in `scripts/contract_examples.py`);
commands carry their TTL (30 s; try, scene_stop 10 s; app_motion 1.5 s; manette 3 s) and the latest wins in its
group; the robot keeps a manette 10 s past the last renewal it got (`manette.LEASE_S`: its link stalls for seconds).

- `catalog.py` — `GET /api/catalog`, everything the robot can do, from the code and the stores (the palette).
- `studio.py` — previews sampled by `robot_agent/motion.rehearse`; emotions from Pollen's dataset JSON
  (cached in `REACHY_CACHE_DIR`); the daemon's state stream read (never written) only while a dashboard
  socket subscribes to `pose`, relayed latest-only; while the robot lends its body, its own pose datagrams (UDP,
  the server's port: `robot_agent/poses.py`) are the pose and the stream is closed.
- `scene_text.py` (strict play-script grammar, pure timeline: a move inside a line never splits its voice;
  a cast angle turns the robot to whoever speaks), `assets/moves.json` (every move a scene may use, by
  feeling), `scene_voice.py` (Gemini TTS, a cache key that never changes, 3 at a time, lite model on a per-day 429; the pauses
  measured), `scenes.py` (store, routes, find by spoken title).
- `claude.py` — headless Claude Code turns (`claude -p`, Opus 5.5, the owner's subscription: no
  `ANTHROPIC_API_KEY` in its environment): `Run` streams stream-json into a `Progress`, cancellable, every run
  `--restricted` (no tool that runs code, no peer session the owner's other sessions could message), Read and
  Edit fenced to a working directory when asked; `ask` for `scene_emotions.py`.
- `app_build.py` — an app's page: a creation is written whole, a retouch edits a copy of the page (Read, Edit), with
  the model and effort of the settings (`apps_model`, `apps_effort`: Opus at high by default, read at each build);
  each result is scanned (no network, storage, dialogs, external URL, reload or leaving, no channel of its own to
  the dashboard) and run once in a fenced headless Chrome (`app_check.py`: load errors, `__selfTest` rows), then
  repaired once by editing; the progress goes out on `app_build` (latest only, ≤ 4/s), `cancel` from the dashboard.
  Every page is served with the apps' toolbox after its CSP (`assets/kit.js`, `window.reachy`, its lists filled
  from the contracts: the robot's channel, « Jouer » with or without the robot, the manette's pad, the replay, the
  sound, the canvas, the loop), which the builder is told to use and the shipped pages use: a change of protocol
  is made there, once. What it offers only grows: a page stored yesterday must run tomorrow.
- `apps.py` — the store, the builds in flight, the voice route, the bridge: it refuses when asleep (409) and
  limits the rate (429); the CSP is prepended to every page served. Nine apps ship with the robot
  (`assets/apps/<id>.html`, `BUILTINS`: Slalom for « volant », Avant la nuit for « paume », Vol de nuit, Marionnette,
  Minuit à Manhattan and Bille en tête for « tete » (a marble led by the head held in both hands, an antenna to jump,
  along a course hung over a bay: ten levels, each one finished by an autopilot in the page's own tests; the head
  held high and leaned to turn, its stick the one « Régler la manette » measured on the owner's hands: that test
  takes eight gestures, sets the stick by them while the page stays open and shows its result to be copied; the
  head set down, the ball brakes and nothing turns; a hiccup of its pads changes nothing, 1.5 s without one and the
  world waits as well), Tam-tam
  for « gachettes »; Banc de manette for every profile, « voix » among them, the human's
  pick: a bench, not a game, that shows what a page gets of the manette (a drawn robot that copies the real one, each
  part's `pad` fields under what the profile leaves to the hand, the link's pace and its silences, a replay, the moves
  a lent body keeps back, a journal), its thresholds and bounds the code's own (`tests/test_cross.py`); Jardin
  abyssal for « tete » too: the underwater world of Abyssal Garden (Maksymilian Dendura, MIT: its code copied almost
  as it is, its notice kept in the page), swum through with the head held in both hands: held, you swim ahead;
  leaned, you turn; lifted or lowered from where the hands took it once it held still, you dive or climb; set down,
  you float; an antenna changes pace; six places to find. Minuit à Manhattan, Avant la nuit and Bille en
  tête draw their worlds in WebGL 2, with no library, Jardin abyssal in WebGPU as its original does (only where the
  dashboard is a secure context: `localhost`, not the Mac's LAN address): where Chrome has no GPU, as in the page
  check, they load, say so and still pass their tests), read where they ship and never stored: what you change of one (its name, its silence, a
  retouch) is kept as yours and shadows it, and deleting that brings it back as shipped. Each passes what a built
  page must (`tests/mac/test_shipped_apps.py`: the scan, its own tests in a real Chrome). A spoken name is
  matched word by word as it sounds (`designate`): the name decides, the app the dashboard shows breaks a tie,
  then the exact title, else the candidates go back for the robot to ask; no name (« celle-ci », « la dernière »)
  means the app shown, else the latest; an app asked for by voice opens at once, being built.
- `scene_emotions.py` — « Ajouter des émotions »: the Mac numbers the places where a move changes no word, Claude
  (medium) picks which emotion goes where, the Mac writes them in, spaced by their length, and checks no voice key
  changed. One at a time, the request held (seconds to minutes).
- `scene_write.py` — a scene asked for (« fais une scène où deux personnages débattent de… », in the dashboard or by
  voice): Claude (medium, `assets/scene_prompt.txt`) writes the play, streamed, as long as the ask calls for; the
  grammar reads it, and a play it refuses, or with a line it would give to another character (`misread`), goes back
  once with its errors; stored, its emotions (Claude, as above, in a run of its own) and its
  voices (Gemini) are prepared side by side. Progress on `scene_build` (the latest only, ≤ 4/s), `cancel` from the
  dashboard; one scene at a time; a scene written stays in the store however the rest ends, without its emotions
  if Claude gives none.
- `spotify.py` — `play_music` (7 kinds) and `music` (13 actions); only endpoints that survive Feb 2026.
- `gemini.py` — one REST call with French errors (per-day vs per-minute 429).

## The dashboard

`live.tsx` / `link.ts` own the one WebSocket (`/api/ws`): it re-reads `GET /api/state` and `GET /api/settings` after
every (re)connect and sends the optional channels it wants (`pose`) and what it shows (`useShowing`: the app or
scene a spoken « celle-ci » means). `launch.ts`: what the robot launches by voice shows in its surface at once (an
app being built, a scene it plays), or is offered beside unsaved work, never over it: a surface holding some calls
`useKeep`, and nothing replaces or closes it without asking. Every payload is parsed with the Zod schemas of
`src/api/schemas.ts`; each subject has its own `api.ts` on `send`/`read`. No router: `App.tsx` holds one `Surface`
(studio, scenes, apps: lazy full-screen dialogs; the studio's chunk is prefetched when idle) beside the settings
drawer and the Cmd+K palette.

- `theme.ts` — « Porcelaine et graphite »: dark only, whatever the system prefers; every colour is a CSS variable
  (`v('ink')`, the 3D bench included); Recursive for all type (casual: the robot's words; mono: a scene's script).
  No colour outside it (the 3D model's own materials and lights aside). `Sticker.tsx` — Pollen's and Seeed's
  drawings of Reachy (`public/stickers`, origins in its `SOURCES.md`): the robot, and one costume per place.
  `header/` — the robot's column: its picture, its state in a few words (`status.ts`), wake or sleep, the
  volume; a strip on a phone. `SurfaceBar.tsx` tops each workspace.
- `i18n.ts` — two languages, French and English, picked in the settings (« Tableau de bord »): the browser's own
  choice (its storage; the browser's language until one is picked), applied at once, never sent to the Mac. Every
  text the dashboard writes itself is in its subject's `words.ts` (`words(fr, en)`: one shape, the compiler refuses
  a text one language lacks; `useWords` in a component, `wordsNow` outside one, and whoever shows it follows the
  language), its figures in `format.ts`. What the Mac labels in French (the catalogue, the settings' options, a
  scene's vocabulary) is shown in English from `mac.en.json`, keyed by the Mac's own label (`mac.ts`;
  `tests/test_cross.py` fails on a label without its English, `frontend/tests/i18n.test.ts` on a text written
  outside a `words.ts`). What stays French: the robot's speech and the sentences said to it, a character's prompt,
  the Mac's errors, an app's page, a scene's text.
- `settings/` — the drawer: a form that holds only the edits (`draft.ts`). `personalities.ts`: fifteen ready-made
  characters, each an accent first (asked with its language and written into the words themselves), then a temper,
  then how it helps or belittles; picking one (`PersonalityPicker.tsx`) writes its text in the personality field and
  nothing else, an edit to save like any other.
- `palette/` — fuzzy, accent-blind search over the catalogue; arrows, Enter runs, Shift+Enter previews.
- `studio/` — plain three.js (no R3F), the committed GLB, the Stewart platform solved from its geometry.
- `scenes/` — the text over a painted copy of its lines (one scroll, wrapped alike; each character in its colour,
  moves and sounds apart, the voices and errors in the gutter; colour and background only, never a bold or a
  padding), « Insérer » on a right click (every move by feeling, searchable), « Ajouter des émotions », one play
  button (« Jouer » or « Arrêter »; waits for its voices). A field asks for a scene; `Writing.tsx` shows it being
  prepared in place of the editor: its three steps (écriture, then mise en scène and voix side by side), its cast,
  the play as it is written. Asked by voice, it opens on its own (`launch.ts`), or is offered beside unsaved work.
- `apps/` — `hold.ts` (the manette's hold as one value and one transition, pure: the human's OK is their gesture
  in the app, no question; a sleeping robot woken for the ask and put back to sleep when the app is left; the
  holds ended in the order they were granted), `Manette.tsx` (its shell: the lease's clock, the ordered posts to
  the Mac, the `pose` channel turned into `pad` messages by `pad.ts`, the `voice` channel's level in each);
  `<iframe sandbox="allow-scripts" srcdoc>` and nothing more: never `allow-same-origin`, never a
  key, no « open in a tab »; the bridge accepts only the frame's first document and is paced.
The Vite proxy adds the API key for the dashboard's own requests only (never `/api/robot/*`, never a
sandboxed `Origin: null`); never put the key in a `VITE_` variable.

## Environment (`.env` at the repo root, rsynced to the robot; never committed)

Robot: `GEMINI_API_KEY`, `MAC_AGENT_URL` (fixed IPs, not the mDNS name, comma-separated; the agent tries first
the address its SSH came from and the Mac's at launch, `MAC_AGENT_HOSTS`, the wired one first, and moves to one that
answers), `MAC_AGENT_API_KEY`,
`MIC_MUTE_WHILE_TALKING`, `MIC_LEVEL_LOG`. Mac: `MAC_AGENT_API_KEY`, `MAC_AGENT_PORT`, `GEMINI_API_KEY`
(scene voices), `REACHY_HOST` / `REACHY_PORT` (the host until a
heartbeat comes: then the address it came from), `SETTINGS_PATH`, `REACHY_STATE_DIR` (scenes and apps;
a test server uses its own), `REACHY_CACHE_DIR` (scene voices, emotions), `CORS_ORIGINS`,
`SPOTIFY_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI`, `SPOTIFY_DEVICE_NAME` (`SPOTIFY_REFRESH_TOKEN` only seeds
`~/.reachy/spotify.json`). Never an `ANTHROPIC_API_KEY`. A variable added is added to `.env.example` too.

## Working rules

- No incident stories in code or comments: history goes in commit messages.
- Before adding a mechanism, look for the cause to fix or the feature to drop.
- The size budget (`scripts/budget.py`, in CI) is a guide, not a wall: it flags growth to
  look at. When a feature needs room, raise the limit in `scripts/budget.py` in the same commit and say so;
  never golf code to fit. One line per subject (palette, studio, scenes, apps, spotify, manette, regard); comments ≤ 25 % of
  non-blank Python lines.
- A contract changes in `mac_server/contracts.py` first, then the Zod schema and the examples.
- Nothing claims to work on the robot until it ran there: a pull request says what only hardware can confirm,
  and whether it was.
- A text the dashboard shows is written in both languages, in its subject's `words.ts`; a label the Mac sends, in
  `frontend/src/mac.en.json` too.
- User-visible French keeps its signs with its words: a no-break space inside « » and before : ; ! ? (figures from
  `format.ts`). `tests/mac/test_french.py`, `tests/robot/test_french.py` and `frontend/tests/french.test.ts` fail on
  a plain one; a prompt only a model reads is exempt.
- No stash left lying around; small conventional commits that say what they removed.
