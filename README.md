# Hermes Browser Handover

A native Hermes plugin for handing the agent's prepared browser page to your phone.
Open its private link in Safari, tap **Take control**, finish the task, then tap
**Release control**. The agent gets the browser back, but the same link stays open:
tap **Take control again** whenever you need the page, and tap **Finish session**
(twice, to confirm) when the handover is really over. The existing browser session
stays on the server; the viewer streams it at phone width with touch input and a
keyboard bridge.

Version **0.2.0**, development preview. Automated tests pass; real iPhone and live
Hermes handover acceptance are still pending. This is not a payment processor and
does not transfer cookies into Safari or promise native autofill/Apple Pay.

## Setup

See [installation](docs/installation.md) and the detailed
[implementation, security model and test evidence](docs/implementation.md).
Requires local Hermes browser tools, Chromium, the Bot Desktop lease API and an
owner-authenticated Tailscale Serve route. Tested against Hermes v0.22.2. Private
core adapters are version-bound and fail closed; they are not public API promises.

There are two tools:

1. `homelab_browser_handover(page_origin, purpose[, target_id])` returns an owner-only link.
2. `homelab_browser_handover_status(handover_id[, cancel])` reports control, completed
   hand-back cycles, whether the owner may still take control again and whether the
   agent may act right now (`agent_may_continue`, re-checked before each browser step).

Disconnects, expired authentication and restarts keep the human control fence.
Only explicit handback resumes the agent, and only the owner's Finish session (or
cancel/expiry while the agent holds) ends a link. Phone input and frames are not
logged or returned to the model. The viewer exposes no general CDP or terminal API.
Popups are announced but not followed; checkout flows opening a separate bank tab
are not yet supported.

## Development

Python 3.12 is the local/CI baseline; the installed Hermes adapter was also smoke
tested on its managed Python 3.14 runtime. `websockets` is the only third-party
runtime dependency, declared in plugin.yaml. Development uses the tested version:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
export HBH_CHROME=/path/to/chrome
cd tests
../.venv/bin/python -W ignore -m unittest test_units test_viewer test_browser_ui test_regressions -v
```

The 54 tests cover authentication, CSRF and tickets, ownership and expiry,
concurrent creation, reconnect/restart, target binding, persistence failures,
real browser input, viewport restoration, repeated take/release cycles with fresh
lease epochs, Finish session from every state, terminal close delivery when the
link is ended by another task, and the actual viewer JavaScript.
They use temporary synthetic profiles, never a live Hermes profile. CI runs these
checks on Ubuntu 24.04 with its installed Chrome. Runtime adapters need the
separate Hermes smoke check in the installation guide.

For a synthetic phone demo, with a separate unused private Serve route:

```sh
.venv/bin/python demo.py --chrome "$HBH_CHROME" \
  --public-origin https://your-server.your-tailnet.ts.net:8444 \
  --login '<owner Tailscale login>' --port 8791
```

The script creates its own temporary browser and file lease, prints a link, stays
up across repeated Take/Release cycles and cleans up on Finish session, expiry
while the agent holds, an external hand back or Ctrl-C. It cannot reach a real profile.
Do not use real personal or payment data in the demo.

## Status and attribution

Private development repository. No public release or Hermes catalogue submission
has been made. Publication requires iPhone acceptance and disclosure of internal
API dependencies. See [catalogue checklist](docs/catalog-submission.md).

MIT licensed. Viewer techniques adapted from T3 Code v0.1.0 retain their original
[MIT licence and notice](third_party/NOTICE.md). See [security guidance](SECURITY.md).
