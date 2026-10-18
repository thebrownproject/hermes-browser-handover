# Homelab Browser Handover

Standalone native Hermes plugin, version 0.2.0, built 18/10/2026 against the
installed Hermes v0.22.2 (`d923a5eb487a`) on VM101, revised the same day after
the parent review (see "Review fixes" below) and extended with the repeat-control
lifecycle (see "Repeat control and Finish session"). Not deployed yet. Source lives
here; the install path is `~/.hermes/plugins/homelab-browser-handover`.

The agent prepares a page in its own browser, then calls
`homelab_browser_handover`. The tool returns a private link. Fraser opens it in
Safari on his phone, sees the page streamed at phone width with **Finish session**
and **Take control** at the top, takes control, finishes the page himself with the
normal iPhone keyboard and scrolling, and taps **Release control**. The agent then
checks `homelab_browser_handover_status` and continues on the same page. The link
stays open: the button becomes **Take control again**, and he can take and release
as often as needed until he taps **Finish session**, which ends the handover.

What it does not do: navigate, fill, submit, read page text, send messages, import
the session into Safari's own cookie jar, or promise Apple Pay/autofill. A streamed
page is still the server's browser.

## Supported tool sequence

1. Prepare the exact page with the built-in browser tools (same task, local
   browser). For payments, run `homelab_payment_inspect` first if a card fill is
   planned; its `checkout_target_id` can be passed as `target_id`.
2. `homelab_browser_handover(page_origin, purpose[, target_id])` → `handover_id`,
   `link`, `target_id`, `connection_id`, `expires_in_seconds` (20 minutes to open).
   Exactly one open page on `page_origin` is required; ambiguity is refused. A
   human already holding the browser, a cloud or user-supplied CDP browser, or a
   foreign/ended session are refused. Only one active handover at a time.
3. Send only the link in the conversation. The link id is not a secret: the page,
   the ticket API and the websocket all require the owner's Tailscale identity.
4. Fraser opens the link, taps **Take control** (native Bot Desktop lease acquired,
   page resized to his screen, input accepted only from that connection), finishes,
   taps **Release control** (viewport restored, lease released, link kept open and
   its 20-minute window restarted). He may tap **Take control again** any number of
   times; each take acquires a fresh lease epoch. **Finish session** (tap twice)
   ends the handover: while he holds it hands back first and ends only if that
   succeeded.
5. `homelab_browser_handover_status(handover_id)` → `state`, `controller`,
   `human_connected`, `viewport_restored`, `released_by`, `cycles`,
   `human_may_retake`, `agent_may_continue`. `agent_may_continue` is true only while
   nobody holds the browser and the record is not held: after a release (link still
   open), after Finish, cancel or expiry. It is false before the first take and
   whenever he has taken control again, so the agent re-checks it immediately before
   each browser step until the state is terminal. `cancel: true` is accepted only
   while no human holds control.

States: `prepared → viewing → human → viewing → human … → finished`, with
`human_disconnected` (phone dropped, fence kept), `target_lost` (page closed while
held; its release ends the handover as `returned`), `superseded` (another viewer
took the native lease) and terminal `returned` (hand back outside the viewer),
`finished` (owner's Finish session), `cancelled`, `expired`, `failed`.

## Security model

- **Owner-only identity on every path.** The listener binds 127.0.0.1 on VM101 and
  is reached only through the VM102 SSH loopback forward and Tailscale Serve, which
  terminates TLS and adds `Tailscale-User-Login`. Every page, API and websocket
  request must carry exactly the configured login and the exact public `Host`.
  Anything running on the Hermes host could forge that header; that is the same
  same-host boundary the code-entry listener documents. No OS isolation from
  terminal or local-admin processes is claimed.
- **Session cookie + CSRF + single-use tickets.** The page sets an HttpOnly,
  SameSite=Strict cookie bound to the login. The ticket endpoint needs that cookie
  and the page's CSRF header; websocket upgrades need the cookie, an exact `Origin`
  and a 30-second single-use ticket bound to the cookie and handover id. Presenting
  a ticket to the wrong handover burns it. Oversize (>4 KB) or malformed viewer
  messages are dropped or close the socket; neither changes control.
- **Allowlisted operations only.** Viewer messages map to pointer, wheel, key,
  insertText, a fixed editable-at-point probe and viewport resize. No evaluate,
  navigation, network, download or file-chooser path exists for the phone.
- **Native lease, no second lock.** Take control acquires the real
  `tools.bot_desktop.lease` with a handover-scoped viewer id and records the epoch.
  Every input re-reads the lease before and after the awaited dispatch (holder,
  viewer id and epoch; no cache on these paths) and a result whose epoch moved
  mid-flight is discarded. Every screencast frame re-checks ownership through one
  coalesced fresh read, and a one-second heartbeat does the same while the phone is
  idle, so a Desktop-pane takeover stops frames without any phone input. While
  held, browser tools, screenshots, computer_use, vault fills and the janitor are
  fenced by Hermes itself; the payments plugin is fenced by its 0.3.6 fix (below).
- **Open sockets re-authenticate.** The websocket handshake validates the session
  cookie once; the listener then repeats the same check (12 h TTL, identity
  binding) every 5 s for as long as the socket lives. On failure frames and input
  stop and the socket closes with 4401. The lease is untouched: the owner reopens
  the link, is re-identified, and hands back from there.
- **One handover is prepared at a time.** The active check and the preparation
  reservation happen atomically on the service loop, so two concurrent tool calls
  cannot both bind the page; the loser is refused before the resolver or CDP are
  touched, and any failure clears the reservation and closes the CDP socket.
- **Binding is epoch fenced end to end.** Endpoint discovery uses Hermes's own
  fenced `_run_browser_command` (the same call its vault tool makes; signatures are
  pinned at start). The plugin additionally reads the native lease before and after
  discovery, including when the endpoint is already in the session record, and the
  manager repeats the check after CDP attach and origin verification. Any human
  hold or epoch change across those steps refuses the handover.
- **State persistence fails closed.** Take control first proves the 0600 state file
  is writable; if the record cannot be written after acquiring, the just-taken lease
  is handed straight back and the phone sees an error. An unreadable or malformed
  state file at start refuses to start the service (tools report it) rather than
  forgetting a held lease. The lease is never touched by either path.
- **Logs carry types, not text.** Unexpected exceptions are logged by class name
  only (no message, no traceback), so a chained websocket or CDP error can never
  place an endpoint, viewer text or identity in the gateway log. Tool errors for
  those paths are fixed generic strings.
- **Nothing silently returns control.** Disconnects, link expiry, service restart,
  plugin unload/reload, page loss and a failed hand back keep the human lease.
  Recovery is the owner reopening the same link (identity re-checked) and tapping
  Release control or Finish session, or handing back from the Hermes Desktop Screen
  pane. Held records persist 0600 under
  `<HERMES_HOME>/homelab-browser-handover/state.json` (including the cycle count and
  the epoch of the current hold) so a reload can resume them. There is deliberately
  no timeout release.
- **Bounded link lifetime, never at the human's expense.** A link expires 20 minutes
  after creation or after the most recent release, and only while the agent holds.
  A held handover never expires; its window restarts when the owner releases. A
  service restart while the agent holds between cycles drops the (unheld, unpersisted)
  link: the agent prepares a new one. Only held records survive a restart.
- **Finish never touches someone else's lease.** Finishing while this handover holds
  releases only its own viewer id after restoring the page, and keeps the session if
  the release did not take. Finishing while another controller holds a held record
  is refused; finishing while the agent holds just closes the link.
- **Frames go to the human only.** JPEG screencast frames stream transiently to the
  authenticated viewer while the agent holds (preview) or while this handover
  holds. They are never logged, persisted, recorded or returned to the model.
  Logs carry handover ids, states, target ids and a connection hash only.
- **Viewport.** Only the bound target is resized (device metrics override, no UA
  change). The prior layout is recorded on take and re-applied on a valid release;
  `viewport_restored` reports the outcome.
- **Navigation and popups.** Same-tab navigation is allowed and shown as an origin
  change notice; a popup opened by the page is announced but never followed.

## Installation

See [portable installation](installation.md). The homelab-specific deployment
scripts are maintained separately from this standalone plugin.

## Internal dependency risk

`lease.py` imports `tools.bot_desktop.lease` and `binding.py` imports
`tools.browser_tool` / `tools.browser_tool_session`. Hermes's validator reports
them as private core imports (warning, not refusal) and that report is accurate:
these are not documented plugin APIs. The adapters validate the exact surface they
use at start (`NativeLease` checks functions, signatures, dataclass fields and
holder constants; `binding.compat_check` checks five names) and refuse rather than
fall back. After any Hermes upgrade run Doctor and `tests/hermes_smoke.py` before
use. `tools.browser_supervisor` would be a public seam but offers no event stream
for screencast, so the plugin opens its own CDP websocket to the session's browser
endpoint obtained through Hermes's own fenced `get cdp-url` command.

Finding from 18/10/2026 source review: on this install `browser_use_cli_mode` is
false and the browser harness is absent, so there are no named `browser_exec`
sessions; the prepared page is the task's own local agent-browser session
(`_last_session_key(task_id)`), which is what the binding resolves.

## Development setup (any Linux host)

The only third-party dependency is `websockets` (tested with 15.0.1). Use an
isolated interpreter and point the tests at a Chromium/Chrome binary:

```sh
python3 -m venv ~/.venvs/hbh && ~/.venvs/hbh/bin/pip install 'websockets>=13,<16'
export HBH_CHROME=/path/to/chrome            # default: T3's chrome-headless-shell 154 under ~/.t3/tools
cd tests
~/.venvs/hbh/bin/python -W ignore -m unittest test_units test_viewer test_browser_ui test_regressions -v
```

On VM102 the existing isolated venv is `~/.venvs/hbh` (same contents). Tests use a
temporary profile, a temporary file lease and a free loopback port; no Hermes
profile is touched. `-W ignore` hides ResourceWarnings from deliberately abandoned
test client sockets.

## Demo without any real profile

```sh
~/.venvs/hbh/bin/python demo.py --help
~/.venvs/hbh/bin/python demo.py --chrome "$HBH_CHROME" \
  --public-origin https://your-server.your-tailnet.ts.net:8444 --login <login> --port 8791
```

The demo launches a temporary headless Chromium on the plugin's own synthetic form
(`/demo`, fake values only, no external requests), uses a temporary file lease with
the native on-disk format in a private `hbh-demo-*` temp dir, and prints the link.
Run it instead of the plugin (same port) to test the phone experience through the
real Serve/tunnel path before any live handover. Verified 18/10/2026 on VM102:
starts, prints the link, Ctrl-C removes the temp profile and leaves no Chromium
process. The demo is a script, not a module, so the VM101 launcher has no supported
way to run it; run it from VM102 (the parent's plan: T3's headless shell behind a
private Serve route). It never uses a Hermes profile wherever it runs.

## Repeat control and Finish session (0.2.0, 18/10/2026)

Fraser found that 0.1.1 closed the link on Release control, so he could not take the
page back. 0.2.0 changes the lifecycle (handover.py, viewer.py, tool prompt):

- `release` from a held state restores the prior viewport, releases input, releases
  this handover's native lease and, when the bound page is alive, returns the record
  to `viewing` (viewer connected) or `prepared` (after a disconnect) with
  `released_by: owner`, `cycles + 1`, `epoch: null` and a fresh 20-minute window.
  The preview stream resumes. If the page is gone (`target_lost`) the release ends
  the handover as `returned`, exactly as before.
- `take` from `viewing` works again on the same open socket and after a reconnect.
  Each take acquires the native lease afresh, records the new epoch and the current
  layout, and persists the held record (fail closed as before). `our_control` now
  requires holder, viewer id and the recorded epoch to match; a record whose epoch is
  unset never counts as holding, so a stale cycle can never be mistaken for the
  current one.
- New viewer message `finish` (button **Finish session**, armed by a first tap for 5 s
  and sent by a second). From a held state it performs the same hand back and ends
  only if the lease was released (`finished`, `released_by: owner`, cycles counted).
  From `viewing`/`prepared` it ends without touching the lease. If another controller
  holds, both `release` and `finish` are refused with "Another viewer holds control".
  An external hand back (Desktop pane) still ends the record as `returned`.
- `_finish` performs the terminal transition and persistence before any socket
  traffic, so a tool status read racing the close never sees the previous state.
  `unload` detaches each viewer before awaiting on it, so a viewer closing during a
  reload cannot be touched twice (previously an intermittent AttributeError on the
  service thread during shutdown).
- Whoever detaches a viewer owns its close (round 6, see below). `ViewerLink.done` is
  set in `close()` after the close code and reason have been queued; a session task
  that finds itself detached by another task (`_finish` from sweep, status, the
  heartbeat or a frame; `expire_viewer`; replacement; `unload`) waits up to 20 s for
  that event before returning, so websockets never closes the socket with a bare
  1000 ahead of the terminal `closed` message. A closer that stalls past the bound is
  logged by id only and the handler returns; the lease is not involved on any path.
- Status adds `cycles` and `human_may_retake`; `agent_may_continue` is defined
  above. The tool prompt tells the agent to re-check status before each browser step
  until the handover is finished, cancelled or expired.
- The demo loop stays up across cycles and exits on `finished`, `returned`,
  `cancelled`, `expired` or `failed`, printing `cycles`, `agent_may_continue` and
  `expires_in_seconds` every 5 s.

Compatibility: 0.1.1 state files restore (missing `cycles` defaults to 0; the epoch
was always persisted for held records). The `returned` terminal state and the
`closed` reasons `returned`/`replaced`/`viewer_restarting`/`session_expired` are
unchanged; `finished` and `expired` reasons are new. Agents written for 0.1.1 that
continue permanently after one `agent_may_continue: true` must be updated to re-check.

## Round 6 CI fixes (18/10/2026, after 91774dd)

CI run 39105023660 (Ubuntu 24.04, full Chrome) failed two `RepeatControlSuite` tests
that pass locally. Both were real races, not slow-runner artefacts, and both are now
covered by deterministic regressions in `tests/test_regressions.py` that fail on
91774dd and pass with the fix:

1. **Terminal `closed` message lost when another task ends the link.** Sweep's
   `_finish` (expiry) detaches the viewer and then awaits the terminal state message.
   If the viewer session was still finishing an earlier message (the release just
   handled, a resize, a ping) it observed `record.viewer is not link` or the terminal
   state and returned, so the websockets handler returned and the library closed the
   socket with code 1000 and no reason before `closed: expired` was sent. State and
   persistence were already correct; only the notification was lost. Fix: close
   ownership belongs to the detaching task (`ViewerLink.done`, `wait_closed`), the
   session handler waits for it when detached by someone else, `_finish`, `unload` and
   replacement close inside `finally`, and `attach_viewer` re-checks the record is
   still active after awaiting the previous viewer's close. The same viewer session
   calling `_finish` itself never waits (the close has completed before it returns),
   and `expire_viewer` is awaited by the server handler, so neither path can deadlock.
   Test `test_terminal_close_is_delivered_when_the_session_returns_during_expiry`
   holds the terminal state message until the session task has provably returned,
   forces that return with a pending ping, and asserts `closed: expired` plus close
   code 1000 with reason `expired`, no hang, and no "not closed in time" log.
2. **Refusal classification after a coalesced lease read.** `lease_view(fresh=True)`
   joins a read already in flight (frames and the heartbeat keep one going), so a
   finish or release issued right after a Desktop-pane takeover can pass the
   pre-check on a view read just before the takeover. `_hand_back` then re-read the
   lease and refused correctly, but with the "control changed" wording meant for a
   newer epoch under our own viewer id. Fix: `_hand_back_blocked` classifies both
   fresh re-reads exactly as the pre-check does: another viewer ("Another viewer holds
   control"), our viewer id under a newer epoch ("Browser control changed"), or the
   agent (the hand back already happened outside the viewer, so the record ends as
   `returned` instead of sitting in `human` until the heartbeat notices). No guard was
   weakened; the lease is never touched on any refusal. `lease_view` also ignores a
   finished in-flight future. Test
   `test_takeover_behind_a_coalesced_lease_read_is_refused_as_another_viewer` gates
   a lease read after it has read the file, takes the lease over underneath it, opens
   the gate only once the finish request has coalesced with that read, and asserts
   the refusal wording, the untouched `desktop-viewer` lease and `superseded` status.

Local result (VM102, same command and venv as the 0.2.0 section): 54 tests pass in
71.2 s; the two new tests fail on 91774dd exactly as CI did (`ConnectionClosedOK`
received 1000 and the "Browser control changed" wording). Mirrored runtime and test
files to the homelab project unchanged. Not re-run: the real-browser demo through
Serve, a real iPhone, any live Hermes profile or payment page.

## Review fixes (0.1.1, 18/10/2026)

Parent review findings and their fixes, each with a regression test in
`tests/test_regressions.py` (mutation-checked: the test fails on the old code):

1. Open websocket never re-validated its cookie: listener re-checks every 5 s,
   closes 4401, frames/input stop, lease kept, fresh identity recovers.
2. Two concurrent `create()` calls could both pass the active check: atomic
   reservation; loser refused before resolver/CDP; failures clear it.
3. Frames were not ownership-checked after start and input checks used a 250 ms
   cache: per-frame coalesced fresh check, 1 s heartbeat, fresh pre/post input
   checks, mid-flight results discarded.
4. `_get_service` raced and `Service.start/stop` cleared a live thread: module
   lock, abort flag honoured by the loop, thread reference kept while alive so no
   duplicate listener is bound, type-only start errors.
5. `log.exception` could serialize endpoint-bearing chains: type-only warnings;
   tool errors generic; sentinel test covers viewer session, housekeeping, CDP
   connect and tool wrapper paths.
6. Install/README assumed `venv/bin/python`: replaced with the launcher's supported
   `--run-module` entry (verified), portable dev setup, honest reload wording.
7. `rollback.sh` printed the raw lease (viewer capability) and interpolated STAMP
   unchecked: holder/epoch summary only, strict STAMP pattern, failures reported.
8. Binding fence: `_run_browser_command` is Hermes-fenced (`run_fenced_pair`) and
   its signature is now pinned; plugin-side lease epoch checks before/after
   discovery including the endpoint-in-record path; manager re-checks epoch after
   attach. Verified against the installed signature on VM101 with patched session
   tables (no browser created).
9. Persist/restore failed open: take control probes the state file and hands the
   lease back if the record cannot be written; corrupt/malformed state refuses
   start. Private-import warnings are unchanged and honest.

## Tests and results (18/10/2026, 0.2.0)

Local (VM102, isolated venv with websockets 15.0.1, T3's chrome-headless-shell 154
on a temp profile, temp file lease), same command as below: 52 tests pass (41 from
0.1.1 with their release assertions updated to "release → state viewing, then finish
→ closed finished", plus the 10-test `RepeatControlSuite` in `tests/test_viewer.py`):
three take/release cycles on one socket with epochs 1..6, viewport restored and text
typed in every cycle, input ignored between cycles, status flags per phase; release →
disconnect → reconnect → take again → disconnect while held → reconnect → release →
finish; finish while holding (lease released, viewport restored, nothing persisted);
finish before any take (lease untouched, epoch 0); finish and release refused while
another controller holds, then external hand back ends it as `returned`; another
viewer taking between cycles blocks the retake until they hand back; a failed lease
release keeps the hold for both release and finish, with input still working; the
link window restarts on release, a hold never expires, and the renewed window expires
while the agent holds (`closed: expired`, cycles kept); restart during a second cycle
keeps the fence (epoch 3 and cycles 1 persisted) and the link stays usable afterwards;
restart while the agent holds between cycles ends the link without touching the lease.
The real-browser UI test now also drives Release → "Take control again" → take again
(epoch 3, phone layout re-applied) → Finish session (two taps; the first does not
change control) → both buttons disabled, lease epoch 4, viewport 1280.

## Tests and results (18/10/2026, 0.1.1 review fixes)

Local (VM102, isolated venv `~/.venvs/hbh` with websockets 15.0.1, T3's
chrome-headless-shell 154 on a temp profile, temp file lease):

```sh
cd tests
~/.venvs/hbh/bin/python -W ignore -m unittest test_units test_viewer test_browser_ui test_regressions -v
```

41 tests passed (27 original + 13 regressions + demo CSP regression). Regressions: open-socket cookie
expiry with fake clock (close 4401, fence kept, old cookie refused, fresh identity
recovers and releases); concurrent creates (exactly one active, resolver called
once, reservation cleared after refusal/failure); external takeover with no phone
input stops frames in both human and preview states; epoch change inside an awaited
input discards the result; sentinel exception text absent from logs and tool errors
(RuntimeError type present, no traceback); lease epoch change across binding
refused; unpersistable state refuses take and keeps agent control; corrupt or
malformed state file refuses start and keeps the fence; eight concurrent first tool
calls start one service and unload leaves no thread/listener and keeps a foreign
lease; slow start times out without a bound listener; failed bind reports type only;
repeated start/stop cycles keep one thread and the lease.

The original 27: identity/host/duplicate-header guards; cookie, CSRF, Origin,
Sec-Fetch-Site and single-use/expired/replayed/wrong-handover tickets; cross-origin,
wrong-identity and cookieless websocket rejection; take → prefilled values kept →
pan-scroll → tap probe → text/backspace/composition-style input → resize → release
with viewport restored and lease released; input ignored before take; disconnect
keeps the fence and explicit release after reconnect; service restart keeps the
fence and restores the record (0600 state file, no frame data); second viewer
replaces the first and stale input is dropped; external lease takeover supersedes
the viewer mid-action; target loss while held keeps the fence until release;
malformed/oversize/bogus messages (including evaluate/navigate) change nothing;
creation refusals (path, empty purpose, no page, ambiguous tabs, second handover,
foreign task, human-held lease); link expiry only while the agent holds; logs and
status carry no endpoint, identity, frames, lease capability or page path. The UI
suite drives the real viewer JavaScript in a second Chromium tab with the identity
header injected: page load, Take control button, touch tap on the canvas focusing
the hidden textarea, soft-keyboard text landing in the target field, Release
control, no script errors, no endpoint or login in the served HTML.

VM101 (installed Hermes v0.22.2, isolated `HERMES_HOME`, staged copy under a
private hermes-owned temp dir that was deleted afterwards, live lease read-only and
unchanged: agent, epoch 19 before and after):

```sh
~/.local/bin/hermes plugins doctor <plugin dir> --ci
~/.local/bin/hermes plugins validate <plugin dir>
HERMES_HOME=$(mktemp -d) ~/.local/bin/hermes --run-module unittest discover -s <plugin dir>/tests -p hermes_smoke.py -v
```

Doctor: OK, 2 tools, 0 hooks. Validate: passed with warnings (private core imports
listed above; sudo mentions in install.sh/rollback.sh). Smoke: 8 tests OK: native
lease surface, fence and epochs; on-disk compatibility both ways; pinned
`_run_browser_command`/`run_fenced` signatures and `run_fenced_pair` still on the
discovery path; refusals (no session, foreign owner, unfenced CDP browser) without
any Hermes call; human-held lease refused before dispatch on both the discovery path
and the endpoint-in-record path, and through Hermes's own fence when no adapter is
given; epoch change or takeover during discovery refused; replaced record refused;
stable binding carries the epoch; registration/unconfigured refusal/unload.

Not tested: a real iPhone Safari session, the live Hermes headed browser and any
merchant page. Isolated demo transport acceptance is recorded separately in the
homelab runbook; it does not establish live plugin acceptance.

## Payments interaction

Homelab Payments 0.3.6 adds a lease fence to its direct CDP path (see its README).
Until that version is deployed, a live payment inspect/fill could run while a human
holds the browser; the handover must therefore not be used around payment steps
with 0.3.5. With 0.3.6 deployed, inspection/approval/fill are refused while held,
and an approval obtained before a takeover is void afterwards (epoch check).

## Files

`__init__.py` tools and prompt section; `service.py` loop thread and lifecycle;
`handover.py` records, tickets, state machine; `server.py` loopback HTTP/WS and
guards; `cdp.py` allowlisted CDP client; `viewer.py` phone page; `lease.py` native
and file lease adapters; `binding.py` session provenance; `config.py` metadata;
`demo.py` standalone demo; `install/` scripts and tunnel unit; `tests/`;
`third_party/` T3 licence and notice.

### Parent follow-up review

The original51-test implementation passed independently in82.467s. Parent added
one real-browser stale-epoch regression: a new hold using the same internal viewer
id must not be released or finished by an older record. It failed against the
initial implementation (the socket closed finished) and passes after fresh epoch
checks before cleanup and again after awaited viewport restoration. All52 tests
pass locally in66.706s. The actual viewer UI suite also passes after shortening
the armed Finish label to Confirm finish and asserting both buttons fit the phone
header. Finished wording no longer promises the agent holds if another controller
may be active. Native adapter files did not change; no live plugin deployment.

## Named browser_exec binding (0.2.1, 18/10/2026)

A real owner test reached checkout in a named harness session, while the old
adapter looked up only `_last_session_key(task)`. The harness stores the managed
backend under `_backend_cache_key(task, session_name, sidecar)` and attaches the
actual caller's supervisor separately. Version0.2.1 adds explicit `browser_session`
tool input and checks both records, local Bot Desktop sharing, matching live
task-supervisor endpoint, unchanged native lease epoch and registry identity.
No raw endpoint input or browser creation fallback was added. These additional
private APIs remain version-bound and are covered by installed-Hermes smoke.
The eight new portable checks cover named success, sidecar, foreign/stopped
supervision, replacement, cloud refusal, human hold and public-tool argument wiring.

## Gateway profile key correction (0.2.2, 18/10/2026)

The initial named-browser acceptance ran outside the gateway's explicit profile
context. The gateway binds a home override even for the default profile; Hermes
then appends `@<resolved home path>` to its backend key. The old short-slug regex
rejected the path separators before registry lookup. A deterministic profile-scoped
regression reproduces the exact malformed-key error on0.2.1.

Named keys now remain opaque strings produced by Hermes's own backend-key helper,
with a nonempty/type/size bound. The caller's session-name allowlist is unchanged.
No new user-supplied key/path/endpoint exists; existing-record, task supervisor,
local provenance and lease/identity checks still apply. Built-in key validation is
unchanged. Installed-Hermes smoke now invokes the actual key producer in profile
scope. Real isolated named browser_exec plus profile override passes through full
manager.create and produces a synthetic handover link, then cleans up its own
manager, daemon and screen. No real checkout or live profile is used.

## Native touch and Nous UI (0.2.3, 18/10/2026)

Owner reported iPhone scrolling failure. Chrome's corrected trusted-touch fixture
scrolls both root and nested panel on the previous implementation, so that alone
does not reproduce the physical Safari report. WebKit native-touch-event-path
acceptance fails at remote scrollY0 on the pointer-only viewer and reaches240 with
the new native Touch Events bridge. The WebKit events are synthetic DOM events;
this demonstrates the missing input-family path, not a physical iPhone diagnosis.
Input is still admitted only while this viewer holds the exact native lease.
Pointer touch events are suppressed when native touch is selected, avoiding duplicate
scroll/click; mouse/pen retain pointer handling. Final queued deltas flush on pan end.

Light/dark colours follow installed Hermes shared/src/theme-presets.ts Nous palette
and platform system fonts. Normal prose overlays were removed, with origin/status,
errors and two-tap Finish retained. No external fonts/assets or security changes.
