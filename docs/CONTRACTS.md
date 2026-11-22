# Contracts

Three processes ship together: `robot_agent` (on the robot), `mac_server` (FastAPI, port 7777) and
`frontend` (Vite, port 5077, talks only to the Mac). Every payload that crosses between them is
defined **once**:

| What | Single definition | Mirrors and checks |
|---|---|---|
| HTTP bodies, robot events, commands, WebSocket messages | `mac_server/contracts.py` (Pydantic) | the robot sends plain dicts, validated against these models in `tests/`; the dashboard's Zod schemas (`frontend/src/api/schemas.ts`) parse `frontend/tests/contract-examples.json`, generated from `scripts/contract_examples.py`; each call the dashboard sends (method, path, body) is written in `frontend/tests/dashboard-calls.json` by `frontend/tests/routes.test.ts` and checked against the Mac's routes and request models by `tests/mac/test_routes.py` |
| Animation modes, variants, tuning ranges, gestures, the emotions offered to the model (names, labels, sentences) | `robot_agent/animation_names.py` (no dependency) | imported by the robot, `contracts.py` and `mac_server/catalog.py` |
| Every move a scene may use (Pollen's 81 emotions and 19 dances: family, French label, seconds, Pollen's description) | `mac_server/assets/moves.json` | read by `scene_text.py` (grammar, insert menu) and `scene_emotions.py` (with `assets/emotions_prompt.txt`, Claude's prompt) |
| What the robot can do (the palette; the transcript names a call's gesture, emotion or Spotify request with its labels) | `GET /api/catalog`, built by `mac_server/catalog.py` from the code and the stores | `tests/test_cross.py`: every tool, animation, music action and play kind is in it |
| Voice catalogue | `mac_server/voices.py` | served in `SettingsView.options` |

To change a contract: edit the Pydantic model (and its example in `scripts/contract_examples.py`), run
`.venv/bin/python scripts/export_contract_examples.py`,
update the Zod schema, then `make test` (pytest fails if the examples file is stale, vitest fails if
Zod disagrees). A dashboard call changed: `cd frontend && npx vitest run -u tests/routes.test.ts` writes
`dashboard-calls.json` again, and pytest fails if the Mac does not take it.

## Authentication

Every route and the WebSocket require `X-API-Key: $MAC_AGENT_API_KEY`: a missing or wrong key is a 401
(the WebSocket is closed with 1008 before `accept`, which a browser sees as a refused handshake). An
empty `MAC_AGENT_API_KEY` leaves every route open, with one warning at boot. Exceptions: `GET
/api/spotify/login` and `/callback`, which answer loopback clients only, carry a single-use `state` and
return HTML pages. The Vite proxy adds the key for the dashboard, never on `/api/robot/*` (robot-only
routes). Errors are `{"detail": ...}`, a French sentence whenever the Mac raises them itself; the robot
reads it to the model as is. Besides 401: 422 for a body that fails validation (French detail), 500
for a crash.

## HTTP routes

| Route | Caller | Request → response |
|---|---|---|
| `GET /api/settings` | robot at wake (2 s timeout), dashboard | → `SettingsView` |
| `PATCH /api/settings` | dashboard | `SettingsPatch` (changed fields only) → `SettingsView`, broadcast on `settings`; applies at the next wake (`apps_model` and `apps_effort`: at the next app build). A stored value the rules refuse reads as its default and stays stored until a patch sets it; 500 « illisible » when settings.json is not JSON or not `{"settings": {...}}` (nothing written: it is never replaced unread) |
| `GET /api/voices/{name}/sample` | dashboard | → `audio/wav`; 404 for a voice not in the catalogue |
| `GET /api/state` | dashboard, after every (re)connect | → `DashboardState` |
| `POST /api/session/wake` · `/sleep` · `/restart` | dashboard | → 204; queues the command(s). `wake`: 409 when the robot is not running (its detail says whether the robot is off or unreachable, or only its agent stopped): nothing is queued |
| `POST /api/session/say` | dashboard | `SayIn` → 204; 409 when the robot is not in conversation, closing included |
| `PUT /api/volume` | dashboard, robot (`set_volume`) | `Volume` → `Volume`; stored, broadcast on `volume` and pushed to the daemon at once; 503 when the daemon has not taken it (stored all the same: each health probe, every 2 s, pushes it again until the daemon takes it); 500 « illisible », like the PATCH, nothing stored |
| `POST /api/spotify/play` | robot (`play_music`) | `PlayIn` (kind track, artist, album, playlist, liked, audiobook, podcast) → `PlayOut`; 404 nothing found |
| `POST /api/spotify/control` | robot (`music`) | `ControlIn` (pause, resume, next, previous, now, like, queue, seek, shuffle, repeat, volume, device, playlist) → `ControlOut` |
| `GET /api/catalog` | dashboard (palette; the transcript, once per link) | → `Catalog`. Each entry's `action`: `session`, `try`, `scene`, `app` (without `app_id`: the Apps surface on its create field), `say` (typed in the conversation as is: only a sentence without a parameter), `compose` (the start of a sentence put in the composer, on a line after what is typed there, for the user to finish; nothing is sent), `settings` (`field`: a field of the drawer, or `volume`; the drawer opens focused on it) |
| `POST /api/studio/try` | dashboard | `TryMove` → 204, queues `try`; 409 when the robot is not running (its detail says whether the robot is off or unreachable, `off`, or only its agent stopped, `idle`), or while an app holds it as its manette |
| `GET /api/studio/preview` | dashboard | `?kind=gesture\|emotion\|animation&name=&mode=&amplitude=&speed=&seconds=` → `PreviewFrames`, sampled by `robot_agent/motion.rehearse`; 404 a move it does not know (or an emotion it cannot fetch), 422 an unknown `kind` or `mode`, or a tuning or `seconds` out of range |
| `GET /api/scenes` · `POST /api/scenes` | dashboard | → `SceneList` · `SceneIn` → `SceneOut` |
| `GET` · `PUT` · `DELETE /api/scenes/{id}` | dashboard | → `SceneOut` · `SceneIn` → `SceneOut` (editing a built-in saves a copy that shadows it) · 204 (yours goes, a built-in edited comes back as shipped; 409 a built-in as shipped); 404 an unknown id. A save (POST, PUT) or a delete answers 500 « illisible » while the scenes file is not read in full (nothing written) |
| `POST /api/scenes/check` | dashboard (live, debounced) | `SceneIn` → `SceneCheck` (errors and warnings, each with its line) |
| `GET /api/scenes/vocabulary` | dashboard (insert menu) | → `SceneVocabulary` (each word's `family`: the feeling an emotion shows, a voice's gender) |
| `POST /api/scenes/emotions` | dashboard (« Ajouter des émotions ») | `SceneIn` (the draft) → `SceneEmotions` (the draft with Claude's emotions written in, only at places that change no word: no voice to render again; nothing saved); one Claude run at a time, the latest request's: a newer one cancels the run in progress, whose request answers 409 (and 409 if that run has not ended within 10 s), 422 nothing said, 502 nothing usable, 503 Claude failed or missing. The request is held while Claude reads (seconds to minutes) |
| `GET /api/scenes/audio/{key}` | robot | `?rate=` (8000–48000: the robot asks for its speaker's) → `audio/wav`, mono s16le, resampled to `rate`; without it 24 kHz as rendered |
| `POST /api/scenes/{id}/play` · `POST /api/scenes/stop` | dashboard | optional `SceneOptions` (`moves`, `rehearse`: a cast name, whose lines the robot leaves to the human until an antenna tap; `start`: a line, the play begins at the voice written there, the body first turned as the turns before left it, and replaces the scene playing; no body: a play with every move) → 204, queues `scene` (the timeline, options inside) · `scene_stop`; 409 robot not running (off or only its agent stopped, as for a try), or voices missing (« se préparent »: they render meanwhile, progress on `scene_render`, and the dashboard asks again once they are all done; the rehearsed character's are not needed; « ne sont pas prêtes », with the reason, when no render can work now: no `GEMINI_API_KEY`, the day's quota spent on both models, or Gemini refusing a missing line the last time it was asked, which is asked again all the same); 422 errors, an unknown character, or no line to say from `start` on. Saving a scene without errors renders its missing voices too |
| `POST /api/scenes/find` | robot (`play_scene`) | `SceneFind` → `SceneFound`: the scene the title names; a title naming none (« celle-ci », « la scène ») means the scene a dashboard shows (`showing`); empty, or naming none with no scene shown, `titles`; 404 with the titles, 409 voices not ready, 422 errors |
| `POST /api/scenes/write` | dashboard | `SceneRequest` (what the scene must be) → `SceneBuild`: Claude writes the play, checked against the grammar and written once more with its errors if it refuses it; stored, its emotions (Claude) and its voices (Gemini) are prepared side by side; progress on `scene_build`. 409 while another scene is being written: one at a time |
| `POST /api/scenes/voice` | robot (`write_scene`) | `SceneRequest` → `SceneAsked`: the same build, `voiced` (the robot plays the scene once it is ready), and what the model is told, in French; a build refused (one is under way) is said in `message`, with no `build` |
| `GET /api/scenes/builds` | dashboard, robot | → `SceneBuilds`: the scene being written, then the last five that ended, the latest first: whoever watched one reads how it ended |
| `POST /api/scenes/builds/{id}/cancel` | dashboard | → 204: Claude's runs end; a scene already written stays in the store, its voices rendered so far too. 409 when that build is not the one under way |
| `GET /api/apps` · `POST /api/apps` | dashboard | → `AppList` (yours, and the nine apps shipped with the robot, `builtin`: `mac_server/assets/apps`, between them every body a manette lends; what you change of one, its name, its silence, a retouch, is kept as yours and shadows it, `edited`) · `AppRequest` → `AppSummary` (building; progress on `app_build`, the end on `apps`); 429 while two builds are under way (`BUILDS_MAX`). The apps' index.json is never written over unless read in full (a key another version wrote is kept): a create, a retouch, a rename or a delete answers 500 « illisible » while it is not, and a build that ends then fails with that error |
| `GET /api/apps/{id}/html` | dashboard | → `AppHtml` (CSP first in `<head>`) |
| `PATCH /api/apps/{id}` · `DELETE /api/apps/{id}` · `POST /api/apps/{id}/update` | dashboard | `AppRename` → `AppSummary` · 204 (yours goes; a shipped app you changed comes back as shipped, said on `apps` without `deleted`; 409 a shipped app as shipped) · `AppRequest` → `AppSummary` (409 while it builds already, 429 as a create; a shipped app's retouch edits a copy of its page); 404 an unknown app |
| `POST /api/apps/{id}/cancel` | dashboard (« Annuler » during a build) | → 204; Claude is stopped: a creation is dropped (`apps` with `deleted`), a retouch or a rebuild keeps the app as it was, with `error` « Modification annulée. »; 409 when nothing is building |
| `GET /api/apps/{id}/draft` | dashboard (the build's preview) | → `AppHtml`: the page as written so far, its CSP first; 404 while nothing is written, or nothing is building |
| `POST /api/apps/{id}/quiet` | dashboard (the app's switch « Silence pendant le jeu ») | `AppQuiet` → `AppSummary`; 404 an unknown app. `AppSummary.quiet`: the robot keeps silent while the app is on screen. Judged by its builder at its creation (`<meta name="reachy-quiet" content="non">` in the page: people talk to the robot there; any other page, an app made before included: quiet); a retouch keeps it, this switch overrides it |
| `POST /api/apps/{id}/bridge` | dashboard (an app's message) | `BridgeIn` (`dashboard`: an id it draws as it loads) → 204, queues `app_motion`; 409 robot not in conversation, or another dashboard's app moved it within the last 2 s (the robot follows one dashboard at a time); 429 too fast |
| `POST /api/apps/{id}/manette` | dashboard (an app holding the robot as its controller, the human having said yes) | `ManetteIn` (`profile`: gachettes · volant · paume · tete · voix, the first four also with `+voix`, or null to let go; `grant`: the human's yes, sent once) → 204, queues `manette`; then renewed every second (the lease: the command lives 3 s, the robot lets go once it runs out; a renewal never takes the body again, only a grant does), each post after the last one's answer (an app closed lets go after the one on its way, those queued behind it dropped); 409 robot not in conversation (closing included), or another app holding the robot (one at a time; another app's letting go is ignored) |
| `POST /api/apps/voice` | robot (`app`) | `AppVoice` → `AppVoiceOut`. The name is matched word by word as it sounds: the app the dashboard shows (`WsClientMessage.showing`) among the close ones, then the exact title, then the only close one; else the candidates are named for the robot to ask, or the titles listed. No name: the app shown, else the one opened or changed last. Create and update open the app on the dashboard at once (`apps` with `open`, status building; with no dashboard connected, the message says it shows nowhere); the message says the app is on the dashboard, never that it is open there, since a dashboard holding unsaved work only offers it; the robot polls `GET /api/apps` until it is built. A create or an update refused answers as the dashboard's would (429, 409, 500) |
| `GET /api/spotify/login` · `/callback` | the owner's browser on the Mac (`make spotify-auth`) | → a redirect to Spotify · an HTML page; no key: loopback clients only, a single-use `state` (§ Authentication) |
| `POST /api/robot/events` | robot (one event per request, kept-alive connection) | `RobotEvent` → 204 |
| `POST /api/robot/heartbeat` | robot, every 3 s | `Heartbeat` (`antenna_wake`: asleep, whether a wiggled antenna wakes it; not while a try, a scene or the sleep move moves it; `scene`: the scene it plays, from its `started` to its end, so a `scene` event lost on the way is put right) → 204 |
| `POST /api/robot/voice` | robot, ~20 a second while a manette lends its ear (« voix »), the latest only | `Voice` (`level`: 0, the room's quiet as learnt before the hold, to 1, 36 dB over it; a number, no sound) → 204, relayed on the `voice` channel |
| `GET /api/robot/commands?wait=20` | robot, long-poll (`wait`: 0–25 s) | → `CommandPoll` |
| `WS /api/ws` | dashboard | server → client `WsMessage`; client → server `WsClientMessage` (subscriptions) only |

Spotify plays on the Connect device the Mac picks: the active one, else the Mac's own Spotify app (its
macOS computer name, or `SPOTIFY_DEVICE_NAME`), never one that takes no command (`is_restricted`); with neither,
409. Every Spotify error carries a French `detail` the robot says out loud: 409 no device, 404 nothing found
(or nothing playing), 422 a missing `value` or `amount` (or an `amount` not finite, or beyond ±86 400, a day
of seconds: `AMOUNT_MAX_S`), 503 not configured, not authorised or a missing scope (`make spotify-auth`), 502
any other Spotify or network failure.

## Robot events (`RobotEvent`, discriminated by `kind`)

Every event carries a non-empty `session_id`: the wake's; asleep, a `scene` or `manette` event gets a key of its own
(`scene-…`, `manette-…`). Order is preserved: one sender thread, one connection.

| `kind` | Fields | Notes |
|---|---|---|
| `session_start` | — | never dropped by the robot's bounded queue |
| `session_end` | `reason`: goto_sleep · dashboard · idle · connection_lost · refused · shutdown · error | never dropped; connection_lost and refused: the reconnect budget is spent, refused when the last connect was refused by Gemini itself (its close frame, an HTTP 4xx: credits, key, setup), not lost on the way |
| `user_delta` / `user_end` | `turn_id`, `text`; `user_end.typed` | `typed` = a dashboard `say` |
| `robot_delta` / `robot_end` | `turn_id`, `text` | output transcription |
| `tool_call` | `call_id`, `name`, `args` | |
| `tool_result` | `call_id`, `name`, `status` (ok · error · refused · cancelled), `message`, `data` | `message` is what happened, in French: the text the model got, less what the robot told it to do about it (« Dis-le simplement. », « N'en parle pas. »), so the transcript shows an error's or a refusal's to the owner; `data.track`, `data.image_base64` (look), `data.volume` are structured, never parsed out of text |
| `phase` | `phase`: connecting · reconnecting · idle · listening · thinking · speaking · closing | the session state machine's state; connecting until the first connection serves (the wake), reconnecting when a connection that served is lost (Gemini lost mid-conversation, a go_away) |
| `usage` | `prompt`/`response` tokens by modality (text, audio, image), `total_tokens` | one per Gemini `usage_metadata`; the Mac adds `cost_usd` and `session_cost_usd` before broadcasting |
| `latency` | `turn_id`, `ms` | end of the user's turn (VAD end or typed say) → first robot audio; none for a turn left unanswered |
| `hush` | `on`, `why` (mac · antenna · say) | the robot silent while a quiet app is on screen: on once its answer is over (its microphone replaced by comfort noise, its audio and every tool refused, notes held, no idle sleep, as during a scene); off when the Mac asks (the app left), an antenna is held, or a say is typed: the Mac then leaves this app's speech given back until it leaves the screen. `SessionSnapshot.hushed` from the on to the off |
| `manette` | `state` (on · returning · off · refused), `profile`, `why` (app · lease · scene · sleep · profile · pose) | the robot as an app's controller: on once it holds still where it was told and has freed its profile's motors, and again in answer to each renewal or grant of that hold (a lost on is put right within a second), never for an entry that failed (a renewal neither answers nor keeps it: once its lease runs out it is given back, `why` pose); off (`why` lease) in answer to a renewal of a hold that ended, unless a let-go was asked after it (that end was said already); returning (`profile`: the hold let go) while it waits, freed, to be let go within the IK's reach (base ≤ 150°, head ≤ 60° off it) before its torque comes back; off once taken back, or once let go before anything was freed, or once it could not take its still pose (`why` pose; no on then in either case); refused while a scene plays (`why` scene) or the conversation ends, a sleep asked before it opened included (`why` sleep); taken from the wake on, before the conversation opens too. The Mac keeps the profile in `SessionSnapshot.manette` from the on to the off, through the returning and another grant's refusal (the dashboard puts a lost event right from it). Holds end in the order granted, each grant the robot got with one off or refused before the next is taken; a grant it never got (replaced in the Mac's queue, or come while it sleeps) has none. The dashboard gives each off to the oldest grant not yet ended (a hold the snapshot says is in force when an app grants with none of its own open, another app's still given back, is the oldest, counted no more once the Mac refuses that grant), takes an on of its grant as the end of every older one and a snapshot without `manette` as the end of the hold it saw on (its off lost), and gives the ons a hold it let go still owes (its renewals not yet answered) to that hold |
| `replay` | `state` (started · ended), `seconds`, `loops` | an app's `replay` (`ReplayMotion`) on the robot: started as the taped stretch itself begins, after the lead-in to its first pose (`seconds`: one loop's length; `loops`: how many, a 0.4 s seam between two); ended however it ends (played out, stopped, replaced by another move, a scene), at once and alone when there was nothing to play |
| `scene` | `scene_id`, `title`, `state` (started · line · waiting · ended · stopped · failed), `line`, `message` | sent by the scene player, not the session; `line` = the source line being heard; `waiting` = the human's turn in a rehearsal (`line` its first line, `message` its text), until an antenna tap |

## Commands (Mac → robot)

`wake`, `sleep`, `say` (with `text`), `manette` (with `manette`: a profile, or `off`, and `grant`; TTL 3 s, the latest
wins, a renewal keeping the `grant` of the same profile's pending command: the robot has not taken the human's yes),
`hush` (with `hush`: silent, or speaking again; TTL 10 s, the latest wins; the Mac sends it at each change of the app a
dashboard shows, of its quiet switch, of the speech given back, and at each new wake while a quiet app is shown),
`try` (with `move`, a `TryMove`), `scene` (with `scene`, a
`SceneTimeline`: its `moves` and `rehearse` are the play's options; the rehearsed character's segments
last 0 ms and carry no `key`), `scene_stop`, `app_motion` (with `motion`, an `AppMotion`: a move, or a `replay`). Each carries its
`ttl_s`: `COMMAND_TTL_S` = 30 s, `try` 10 s, `scene_stop` 10 s, `app_motion` 1.5 s. The Mac measures age as the older
of wall-clock and monotonic age (the Mac's monotonic clock stops while it sleeps). Within each of these
groups the latest wins (it replaces any pending one): `wake`/`sleep`, `try`, `scene`/`scene_stop`,
`app_motion`, `hush`; `say` commands queue in order. The robot keeps that rule for a `scene` it took that waits to start (an
app giving the body back, the scene before it ending): any stop or a newer `scene` voids it. A stop says so (`scene`
event `stopped`, its `message` why, with no `started` before it); a newer `scene` answers for both.
A long-poll whose client has gone takes nothing, and a new long-poll answers any older one still parked
`{"command": null}` at once: there is one robot, so its newest poll is the live one (an older one may sit on a
socket gone silent, which no disconnect reveals). `restart` is not a command: awake, the Mac queues `sleep`, then `wake`
once the robot reports the session ended (its `session_end`, or a heartbeat asleep) within 60 s; asleep, `wake` at once.

## WebSocket channels (`WsMessage`, discriminated by `channel`)

| `channel` | `data` |
|---|---|
| `conversation` | a `RobotEvent`, relayed as received (`usage` enriched with costs) |
| `session` | `SessionSnapshot` — awake, session id, phase, session cost, last latency, the profile of the hold while an app holds the body as its manette (`manette`), whether an antenna wakes it asleep (the header asks for a wiggle only then; false from `session_end`, through the sleep move, until a heartbeat says the antennas listen), the scene it plays awake or asleep (`scene`, from its `scene` events and each heartbeat; a wake or a sleep clears it), whether it keeps silent during an app (`hushed`, from its `hush` events); set at once by `session_start` / `session_end`, the heartbeat decides when the robot is gone |
| `robot_status` | `{status}`: off (daemon unreachable) · idle (no heartbeat for 10 s) · live |
| `settings` | `SettingsView`, after every change |
| `volume` | `Volume` |
| `pose` | `PoseFrame` — only to sockets that subscribed; each holds the latest frame (a slow one skips) |
| `voice` | `Voice` — only to sockets that subscribed (an app's frame holding a manette that listens): the robot's microphone level as it comes, ~20 a second |
| `scene_render` | `SceneRender` — a scene's voices being rendered, by its latest render only (a save or a play while one is under way starts another, which counts the lines they share; a line it does not count, still on its way for an older one, such as the character a rehearsal skips, gives its frame too, unchanged, so the dashboard re-reads which lines are ready); `error`: the first line that failed, kept in each later frame of that render (it will not complete; the other lines go on) |
| `scene_build` | `SceneBuild` — a scene written at someone's ask, the latest only, at most 4 a second: `status` (building · ready · failed · cancelled), its three steps `write`, `stage` and `voice` (waiting · running · done · failed; `stage` and `voice` run side by side once the play is stored: `scene_id`, `title`), what Claude does while it writes (`phase`, `thinking_tokens`, `round` 2 with its `problems`), the play so far then as stored (`text`), `moves`, `voices_done`/`voices_total`, `error` (why it failed), `warning` (ready without its emotions), `voiced` |
| `app_build` | `AppBuild` — a build under way as Claude works: `phase` (starting · thinking · writing · reading · editing · checking), `round` (2: a repair, with its `problems`), `started_at`, `thinking_tokens`, `chars` and `tail` (a creation: the page so far, its last lines), `edits` (a retouch: the last ones), `cost_usd`; the latest only, at most 4 a second; it ends with the `apps` event of the built app |
| `apps` | `AppEvent` — an app built, renamed, deleted, or to open (`open`: asked by voice, sent once, as its build starts or when it is opened; the dashboard shows it unless a surface holds unsaved work, and offers it once built if it is no longer on screen) |

The dashboard sends one kind of message, `WsClientMessage` `{"subscribe": ["pose", "voice"], "showing": {"kind": "app",
"id": "…", "since": 1793710000.5}}`, on every (re)connect and whenever either part changes: the optional
channels it wants (an empty list: none) and what its workspace shows (`kind` app or scene; null: nothing;
`since`, on the dashboard's clock, when it began showing it, so a tab that reconnects is not taken for the
latest), which the Mac keeps per socket until it closes. The Mac reads the daemon's state stream only while some socket wants `pose`, and forwards at
most 30 frames/s. While an app holds the robot's body, the robot sends its pose itself, as datagrams (UDP, the
server's own port; `robot_agent/poses.py`: 116 bytes, an 8-byte hash of the measure under the API key, then the
robot's clock, its 7 head joints, the head's 4×4 and the 2 antennas), 30 a second, each published as a `PoseFrame`:
the Mac closes the daemon's stream while they come (one within the last second) and reads it again once they stop. The robot starting a scene (`scene` event `started`) opens it in the dashboard's editor,
playing, unless it is the scene shown there.

No application-level ping: the server pushes only real changes. A socket that falls 256 messages behind is
closed with 1013, and the dashboard reconnects. After every (re)connect the dashboard
re-reads `GET /api/state` and `GET /api/settings`. The event channels are not re-read: what they carried meanwhile
is put right from the snapshot (a scene's play button follows `SessionSnapshot.scene`, a session it no longer reports
is settled in the transcript) or asked again (a play waiting for its voices, an open Apps surface's list).

## The manette (bridge version 1, from the page)

A page asks for the robot as its controller with `{reachy: 1, type: 'manette', profile}` (gachettes: the antennas
are buttons, torque kept; volant: the base is also freed, read as a wheel; paume: the base freed too, the antennas
held where they lie in sleep, for a palm laid on the head that turns the base while its fingers rest on them; tete:
the head and the base are freed from the sleep pose, the head read as a joystick; « voix »: the robot's ear, alone
or after any of the four, as in `tete+voix`) and lets go with `profile: null`. The human's yes is their own click or key in the app (« Jouer »):
the dashboard's script in the frame (§ App bridge) tells each trusted `pointerdown` and `keydown` there, and an ask
that follows one within 5 s is granted at once; any other waits, a line above the frame saying so, for the next.
A robot asleep is woken first (`POST /api/session/wake`; the grant goes once the session says awake and not
closing; given up after 20 s, or at once when the robot is not running: the page is told `off`), and put back to
sleep (`POST /api/session/sleep`) when the frame goes: it is left as it was found. The dashboard then renews
`POST /api/apps/{id}/manette` every second while the frame shows it (the Mac keeps a renewal 3 s, and lets another
app take the manette once the holder has renewed nothing for as long; the robot gives the body back by itself 10 s
after the last renewal it got, `why` lease: its link to the Mac stalls for seconds at a time); the frame unloading,
the Apps surface closing or « Rendre la main » let go. The dashboard posts back `{reachy: 1,
type: 'manette', state: 'asking' | 'on' | 'off', profile}` and, while on, one `{reachy: 1, type: 'pad', left,
right, antennas: {left, right}, body, head: {yaw, pitch, roll, x, y, z}, voice, t}` per frame of the robot's measured pose (~30 a
second, the `pose` channel): an antenna is pressed past 4° from its measured rest and let go under 1.5°, left and
right being the robot's own (their rest is the measured one if steady within 2° of where they are held, else where
they are held: upright, or lowered for « paume », where a push is counted within ±180° since a lowered antenna pushed
further down passes the half turn: positive back up toward upright, negative further down; and where the player
reads the turn from their hand: turned to the left, it gives a positive `body`); `body` is the base's yaw and `head` the head's angles relative to the base, in degrees from where
they rested as the hold began (the « tete » sleep pose is the stick's centre), as `look` takes them (yaw positive
toward the page's right, pitch positive up), with where a hand carried it from there (`x`, `y`, `z`, mm: toward
the person facing it, toward the page's right, up); `t` is when the frame was measured, ms on the robot's clock
(the daemon's timestamp). A page asking again for the profile it holds is told at once where that hold is (`asking`
or `on`). While the robot is a controller it writes no target at all (its motion thread is frozen),
its moves, the app's moves, tries and `play_scene` are refused or ignored, it is silent as during a hush (Gemini
hears the comfort noise), and there is no idle sleep; a scene from
the dashboard, sleep or a shutdown take the body back first (`enable_motors()` with no ids: every target pinned to
the present pose).

While a profile lends a body, the robot tapes its own measured pose 50 times a second (head pose with its
translation, antennas, base yaw: `robot_agent/take.py`), and keeps the tape after the hold. The page's `{reachy: 1,
type: 'replay', start, end, loops}` (`start`, `end`: two pads' `t`; `loops` 1 to 8, 0 stops the one playing; one
of the app's moves for the pacing and the rate) has the robot play that stretch (0.3 to 30 s) back as it was,
once the body is its own again: a lead-in of 0.8 s at least to the first pose, the base turning there, then the
tape through the one motion thread, a 0.4 s seam back to the first pose between two loops. The head is played
where it was in the room; the base no faster than 90°/s nor past ±100° (its motor). The robot says `replay`
events (started, ended), which the dashboard posts to the frame as `{reachy: 1, type: 'replay', state, seconds,
loops}`.

A profile that listens adds `voice` to each pad: the robot's microphone level, 0 (the room's quiet, the 20th
percentile of the 5 s before the hold, the robot's own noise left out) to 1 (36 dB over it; 0 up to 6 dB over
it), one value per 50 ms, the latest in each pad; 0 for a profile that does not listen. Only that number leaves
the robot (`POST /api/robot/voice`, the `voice` channel). The level is everything the microphone hears: the robot's own motors
and sounds, and the Mac's speakers. « voix » alone lends no body: nothing is frozen or freed, the robot goes on
playing its moves and the app's (a look, an emotion), and its pads come one per level, the body's fields at rest.

## The apps' toolbox

Every page is served as `<meta CSP>` + the toolbox + the page (`GET /api/apps/{id}/html`, the draft of a build, the
page check alike). The toolbox (`mac_server/assets/kit.js`, `window.reachy`, its emotions, gestures and profiles
filled from the contracts) is the page's one channel to the dashboard: `reachy.emotion`, `geste`, `regard`,
`antennes`, `rejouer` send the bridge's messages, bounded; `reachy.lancer(profil, {attendre, partir})` is « Jouer »
(the manette asked in the click, the wait while the robot wakes or gets into place, the start with it or without,
« Jouer sans Reachy »); `reachy.pad` is the pad in the screen's sides; `reachy.quand` tells the robot's waking, the
manette's state, each pad, each press, a replay's start and end; `reachy.feter` gives the manette back then plays
an emotion once the body is the robot's again. With it: the sound (`reachy.son`), a canvas that follows its parent
(`reachy.toile`: one the page's CSS gave no size covers its parent's inside; one held by WebGL has no `ctx`, its
size alone is kept), a fixed-step loop (`reachy.boucle`), a seeded random, the dashboard's colours as CSS variables.
The builder's scan refuses a page that posts to the dashboard itself. What the toolbox offers only grows.

## App bridge (version 1)

An app runs in `<iframe sandbox="allow-scripts" srcdoc=…>`, its CSP first in `<head>` (`default-src 'none';
script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src
data:; connect-src 'none'`). It posts `{reachy: 1, type: 'emotion' | 'gesture', name}`, `{reachy: 1, type:
'look', yaw, pitch, roll}` (page-centric degrees; `roll`, optional, as the pad's `head.roll`) or `{reachy: 1,
type: 'antennas', left, right}` (or a `replay`, § The manette) to
`window.parent`; the dashboard accepts it only from that frame's own page, parses it with Zod, coalesces it,
and posts `BridgeIn` to the Mac, which checks it again, limits the rate, and queues `app_motion`. Its own page:
the dashboard puts a script of its own right after the CSP, which removes itself before the page's scripts run
and posts, with a key made for each mount, `{reachy: 1, type: 'page', key, here: true}` on the document's
trusted `load` and `here: false` on its `pagehide`; the bridge opens at the first and is cut at the second (or
a second `load`), and a document the page goes to never knew the key. The same script posts `{reachy: 1, type:
'touch', key}` at each trusted `pointerdown` and `keydown` in the frame (a key held repeats none), heard before the
page's own listeners: the human's gesture a manette ask is granted on, which a page cannot say for itself. The dashboard posts `{reachy: 1, type:
'robot', awake}` to the app once its page has loaded and on every change. Pacing (dashboard): the latest look
and antennas, one per 200 ms; one emotion or gesture per 1.6 s (two wait at most, more are dropped), poses
dropped meanwhile. Rate (Mac, per app): look and antennas 6/s each, emotions and gestures together 1 per 1.5 s
(a margin over the pacing, whose ticks jitter). Every dashboard connected runs an app opened by voice: one out of
sight (`document.visibilityState` not visible) sends nothing, and the Mac follows one dashboard at a time, the
one whose app moved the robot first, until that one has sent nothing for 2 s (longer than its pacing is still
after a move). A failed update leaves the app `ready` with its `error`. Every page Claude writes or edits is
scanned (what the sandbox forbids, and a page that reloads or leaves: its bridge would be cut), then run once in
a headless Chrome with no network (load errors, `window.__selfTest` rows that fail); refused, it goes back once
to Claude, who edits it with the reasons.
