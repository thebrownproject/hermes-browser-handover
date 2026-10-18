# Repository instructions

Build a standalone Hermes plugin, not a fork of Hermes or T3 Code. Preserve
third-party attribution and avoid runtime monkey-patching of core functions.
Use documented extension points where available. Isolate and disclose any
version-bound internal API adapter; validation warnings must not be hidden.

Keep source portable: no personal hostnames, identity values, browser profiles,
private links, keys, wallet data, captured pages or live runtime configuration.
Synthetic browser data belongs in tests. Runtime state belongs outside the repo.

Human takeover must fence agent reads and writes. Lost connections or expired
authentication must not silently resume the agent. Do not record typed values
or frames. Do not expose general CDP, terminal or gateway APIs to the viewer.

Run meaningful authentication, lifecycle and real-browser tests before release.
A passing scaffold check is not plugin acceptance. Real merchant, account or
payment tests require separate scope. Never deploy or change a running gateway
as an incidental consequence of running tests.

The initial implementation is being built in a separate local checkout. Do not
assume this scaffold contains it. Preserve other agents' active work when importing.
