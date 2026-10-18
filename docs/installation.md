# Installation and private transport

Community preview. Review compatibility before enabling, and test the synthetic
demo before using any live browser. No live payment acceptance has been established.

## Runtime and configuration

Copy the repository to the intended profile's plugin directory, normally
`~/.hermes/plugins/homelab-browser-handover`. Keep the MIT licence and third_party
notices. Configure only that profile; other profiles are independent.

Use the installed Hermes launcher's supported runtime, rather than assuming a
`venv/bin/python` executable exists:

```sh
hermes plugins doctor ~/.hermes/plugins/homelab-browser-handover --ci
hermes plugins validate ~/.hermes/plugins/homelab-browser-handover --install-deps
```

The manifest declares `websockets>=13,<16`. Install dependencies through your
Hermes version's supported plugin dependency workflow. Doctor must confirm that
they are available. Do not pip-install into a managed dependency generation.

Create `~/.config/homelab-browser-handover/viewer.json` with mode 0600, in a
private directory. This metadata belongs outside the repository:

```json
{"public_origin":"https://your-server.your-tailnet.ts.net:8444",
 "tailscale_login":"<exact owner Tailscale login>","listen_port":8791}
```

The listener binds loopback only. Runtime state is stored privately under the
active HERMES_HOME. The URL must be an HTTPS origin without a path or query.

## Compatibility smoke check

On the Hermes host run the following under an isolated home, using its supported
launcher. This verifies the installed internal adapter surface, not live-browser
acceptance. First use can prepare a temporary dependency generation.

```sh
smoke_root=$(mktemp -d)
HERMES_HOME="$smoke_root" hermes --run-module unittest discover \
  -s ~/.hermes/plugins/homelab-browser-handover/tests -p hermes_smoke.py -v
# Remove only this newly created temporary root after reviewing the result.
```

Private imports of `tools.bot_desktop.lease`, `tools.browser_tool` and
`tools.browser_tool_session` are expected validation warnings. They are real
compatibility risks, never suppress them. Retest after Hermes updates.

## Private transport

On a Tailscale host, choose an unused Serve port and proxy only the loopback
listener. Keep all existing routes intact:

```sh
tailscale serve status
tailscale serve --bg --https=8444 http://127.0.0.1:8791
```

If Hermes runs on another trusted host, first establish a pinned, verified SSH
forward such as `ssh -N -T -L 127.0.0.1:8791:127.0.0.1:8791 YOUR_SSH_ALIAS`.
Use your own supervised service unit for persistence. Tailscale Serve must add
the authenticated `Tailscale-User-Login` header on HTTP and websocket requests;
the plugin requires the configured owner on every production route.
Never use Funnel, a public reverse proxy, or a client-supplied identity header.
Local processes able to reach the loopback listener are trusted by this design.

For demo testing, run demo.py on this listener port instead of the live plugin.
It uses its own temporary browser and lease. Stop the demo before starting the
real listener or an SSH forward on the same port.

## Activation and recovery

Enable with `hermes plugins enable homelab-browser-handover`, then perform the
supported plugin reload or an idle gateway restart for your installed version.
Inspect active work and persistence first. A new chat is not required merely to
refresh tools. Enabling this plugin does not itself deploy its transport.

Prepare a harmless page with the built-in local browser tools and use the two
handover tools as described in README.md. Verify exact target continuity, phone
keyboard/touch, responsive layout, release followed by Take control again on the
same link, and Finish session before any real checkout.

Homelab Payments 0.3.5 has an unfenced direct CDP path. If that plugin is installed,
upgrade it to a verified lease-fenced version before live handover. This repository
does not bundle or upgrade the payment plugin. Secure approval requirements remain.

For rollback, explicitly hand back any human-held lease (Release control or Finish
session on the phone, or the Desktop pane), disable this plugin, reload when idle
and stop only its own listener/forward and Serve route. Do not
remove or rewrite lease state to force the agent to continue. A lost phone
connection alone must never resume automation. Restore the previous plugin copy
if needed; leave unrelated routes, authentication and profiles untouched.
