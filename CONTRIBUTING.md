# Contributing

Start with a small issue describing the user behaviour and a reproducible case.
Use synthetic pages and data in reproductions. Do not include private session
links, cookies, credentials or live checkout information.

Changes to viewer input, authentication and control ownership need behavioural
coverage. Test keyboard composition/backspace, viewport resize, reconnect,
conflicting viewers and prepared-page continuity as applicable.

Keep all payment submission outside automated tests. Public release requires
actual iPhone Safari acceptance as well as automated Chromium checks.

Retain the T3 licence notice for adapted source and identify its source commit.
Prefer supported Hermes hooks; disclose any internal dependency and the versions
actually tested. Do not disable guards to satisfy compatibility checks.

See README.md for test commands and docs/installation.md for installation.
