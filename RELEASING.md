# Releasing Insoma RF

This directory is ready to be the root of the public GitHub repository
`twasilczyk/harf-insoma`. Publish its contents as a separate repository, with
`custom_components/harf_insoma` directly under the repository root.

## First publication

1. Create the public repository `twasilczyk/harf-insoma`, enable issues, and set a
   description such as “Timed Insoma garden hose valve control for Home Assistant
   over 433 MHz RF.”
2. Add topics such as `home-assistant`, `hacs`, `insoma`, `irrigation`, and `rf`.
3. Push this directory's contents. Check that GitHub Actions is enabled and all
   jobs in **Validate** pass. HACS validates GitHub repository metadata as well as
   files, so its hosted check needs the published repository.
4. Complete the hardware checks below before advertising the integration as
   tested with a particular valve.
5. Install from HACS as a custom repository using the README instructions.

A GitHub release is optional for custom-repository installation: without
releases, HACS downloads the default branch. This workflow validates changes;
it does not create releases or update users' installations automatically.
[HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/)
describe the supported layout and update sources.

## Publish a versioned release

1. Update `version` in `custom_components/harf_insoma/manifest.json`.
2. Run the tests and push the change. Wait for the HACS, Hassfest, and test jobs
   to pass.
3. Create a tag `v<version>` on that commit, for example `v0.1.0` for manifest
   version `0.1.0`. Push the tag and wait for its validation, including the
   tag/manifest check, to pass.
4. Publish a GitHub release for that tag with a description of the changes.
   No separate ZIP upload is needed: HACS installs the integration files from
   the tagged repository.
5. Confirm the new version appears in HACS. Users following releases can install
   the update and restart Home Assistant.

A tag alone is not a GitHub release. After releases are introduced, publish
another release for each update intended for users following releases.
[HACS versioning](https://www.hacs.xyz/docs/publish/start/#versions)

## Hardware checks

The protocol uses captured remote commands and the transmitter timings documented
in the README. Automated tests do not verify RF reception or physical
valve operation. Before the first release, verify with the actual transmitter
and valve:

1. Add the valve using its captured OFF code and confirm it exposes one switch
   and the duration setting, initially 120 minutes.
2. Set the duration to 5 minutes. Confirm changing the setting alone does not
   transmit or open the valve.
3. Turn on and confirm the valve opens. Turn off early and confirm it closes.
4. Start another 5-minute run. Stop Home Assistant and confirm the valve closes
   on its own after the selected duration. Restart Home Assistant and confirm
   it does not transmit an on command.
5. During an active run, change the duration setting. Confirm the current run
   keeps its original deadline and the next on command uses the new duration.
6. Verify the remaining duration choices and repeat with each valve or remote
   variant you intend to support.
7. Capture the updated waveform with the USRP and compare its carrier, HIGH/LOW
   durations, sync LOW, and frame period against the OEM SGW12-RX measurements
   in the README. The configured frequency and compensated timings are estimates
   that still require over-the-air validation.

Record the tested valve model, transmitter/firmware version, Home Assistant
version, and results in the release notes.

## HACS default catalog

Users can install immediately through HACS custom repositories. For discovery
in HACS search, first pass HACS validation without ignored checks and Hassfest,
then publish a full GitHub release. Submit the repository to the `integration`
list in [`hacs/default`](https://github.com/hacs/default), following the
[catalog submission instructions](https://www.hacs.xyz/docs/publish/include/).
