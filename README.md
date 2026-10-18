# Hermes Browser Handover

Prepare a page with Hermes Agent, open a private link on your phone, take control,
and hand the same browser session back when you are finished.

**Status: early development.** This repository currently contains project
scaffolding. The plugin is being built and tested separately; no installable release,
phone acceptance result or payment compatibility is claimed yet.

## Intended experience

- The agent prepares a page in its server-side browser.
- You open an owner-only HTTPS link in Safari over your private network.
- The page uses your phone's viewport, with touch and keyboard controls.
- Take control pauses agent browser activity. Explicit handback returns control.
- A dropped phone connection keeps agent access paused pending recovery.

This is a remote view of the existing server browser. It does not import cookies
into Safari's own browsing session or guarantee native wallet/autofill behaviour.

## Development

The first milestone is a harmless prefilled-form demonstration, followed by actual
iPhone testing. Installation and test commands will be added with the reviewed
plugin implementation. The initial CI workflow checks repository whitespace only;
it is not evidence that browser, authentication or control tests have passed.

See [contribution guidelines](CONTRIBUTING.md) and the
[catalogue preparation checklist](docs/catalog-submission.md).

## Acknowledgements and licence

The viewer design draws on [T3 Code](https://github.com/pingdotgg/t3code), including
its mobile keyboard, viewport and streamed-browser input approach. Reused source
will retain its original notices. This project is not affiliated with Nous Research
or T3 Tools Inc.

Project code is MIT licensed; [third-party notices](third_party/NOTICE.md) apply to
adapted components.
