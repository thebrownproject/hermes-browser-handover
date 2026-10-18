# Third-party notices

`viewer.py` adapts the browser-viewer input and streaming techniques from
T3 Code v0.1.0 (commit 81493efb71e8e8244f95d3e9f71d8369654ab151), files
`apps/mobile/src/features/browser/preview-stream.browser.ts`,
`apps/mobile/src/features/browser/preview-stream-document.ts`,
`packages/client-runtime/src/preview/serverBrowserStream.ts` and
`apps/server/src/preview/ServerBrowser.ts` (editable-at-point probe, viewport
resize, screencast acknowledgement pacing, hidden textarea keyboard bridge,
pan-to-wheel, multi-click counting). The adapted code was rewritten in plain
JavaScript and Python for this plugin.

T3 Code is MIT licensed, Copyright (c) 2026 T3 Tools Inc. The full licence text
is in `T3-CODE-LICENSE.txt` and must stay with any copy of this plugin.

The default Nous colour tokens and control CSS in `viewer.py` are adapted from
Hermes Agent v0.22.2 (commit d923a5eb487a), specifically
`apps/shared/src/theme-presets.ts`, `apps/desktop/src/themes/context.tsx`,
`apps/desktop/src/styles.css`, `apps/desktop/src/themes/presets.ts`,
`apps/desktop/src/components/ui/control.ts` and
`apps/desktop/src/components/ui/button.tsx`.
Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research. The full licence
text is in `HERMES-LICENSE.txt` and must stay with this plugin. This independent
plugin does not bundle the Hermes app, fonts or branding assets and is not
endorsed by Nous Research.
