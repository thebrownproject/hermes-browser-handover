# Preparing for the Hermes plugin catalogue

The plugin is not submitted or approved. The repository stays private during
initial development and phone testing.

Before a future submission:

- Finish the implementation and document actual install and test commands.
- Verify the harmless-form flow on iPhone Safari and exercise failure paths.
- Generalise configuration; remove all homelab-specific/private runtime material.
- Resolve or disclose internal Hermes API dependencies. Request a public hook if
  required; runtime core overrides are not accepted by the catalogue.
- Keep the MIT licence and all T3-derived attribution.
- Declare tools, hooks, dependencies, compatibility and network/background behaviour.
- Run `hermes plugins doctor . --ci` and
  `hermes plugins validate . --install-deps` against the exact candidate release.
- With the owner's publishing instruction, make a reviewed release publicly
  cloneable and pin its full 40-character commit in a catalogue entry PR.
- Submit `plugin-catalog/<name>.yaml` to NousResearch/hermes-agent with screenshots,
  disclosures and validation evidence. Catalogue maintainers decide admission.

Source: [official catalogue submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
