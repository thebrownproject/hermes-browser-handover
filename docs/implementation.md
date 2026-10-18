# Homelab Browser Handover

Standalone native Hermes plugin, version 0.1.1, built 18/10/2026 against the
installed Hermes v0.22.2 (`d923a5eb487a`) on VM101 and revised the same day after
the parent review (see "Review fixes" below). Not deployed yet. Source lives here;
the install path is `~/.hermes/plugins/homelab-browser-handover`.

The agent prepares a page in its own browser, then calls
`homelab_browser_handover`. The tool returns a private link. Fraser opens it in
Safari on his phone, sees the page streamed at phone width with **Take control**
and **Release control** at the top, finishes the page himself with the normal
iPhone keyboard and scrolling, and taps Release control. The agent then checks
`homelab_browser_handover_status` and continues on the same page.

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
   taps **Release control** (viewport restored, lease released).
5. `homelab_browser_handover_status(handover_id)` → `state`, `controller`,
   `human_connected`, `viewport_restored`, `released_by`, `agent_may_continue`.
   Continue only when `agent_may_continue` is true. `cancel: true` is accepted only
   while no human holds control.

States: `prepared → viewing → human → returned`, with `human_disconnected`
(phone dropped, fence kept), `target_lost` (page closed while held), `superseded`
(another viewer took the native lease) and terminal `cancelled`/`expired`/`failed`.

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
  plugin unload/reload and page loss keep the human lease. Recovery is the owner
  reopening the same link (identity re-checked) and tapping Release control, or
  handing back from the Hermes Desktop Screen pane. Held records persist 0600 under
  `<HERMES_HOME>/homelab-browser-handover/state.json` so a reload can resume them.
  There is deliberately no timeout release.
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

## Tests and results (18/10/2026, after review fixes)

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
