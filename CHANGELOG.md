# Changelog

## [0.9.0](https://github.com/skorokithakis/symphony/compare/v0.8.0...v0.9.0) (2026-11-25)


### ⚠ BREAKING CHANGES

* The webui.ntfy config block is no longer accepted and fails validation. Remove it from config.yaml.

### Features

* Add a bearer-authenticated agent API at /api/v1 ([3dbab99](https://github.com/skorokithakis/symphony/commit/3dbab99aecd5ea76e84dda8dfd6a815485804544))
* Add a built-in tracker with a web UI ([0a3b80b](https://github.com/skorokithakis/symphony/commit/0a3b80bd56c9d9c122888c4aa3ef882716e07da6))
* Add a copy Markdown button to ticket page messages ([6ff13b7](https://github.com/skorokithakis/symphony/commit/6ff13b738a41b38d6bfeeb5999450a509f4b2898))
* Add a Needs Input dot and keyboard shortcuts to the web UI ([d5b7a97](https://github.com/skorokithakis/symphony/commit/d5b7a971d35234214bb5cfc5a13a0899a8016eb6))
* Add a rule after each ticket group heading in the web UI ([dfd7ba3](https://github.com/skorokithakis/symphony/commit/dfd7ba3102f3884523c69863bf7916f89df86cf1))
* Add a shortcuts help dialog and a Ctrl+K repository picker to the web UI ([a710b1a](https://github.com/skorokithakis/symphony/commit/a710b1a110786460e3277b4819c11806577ac77b))
* Add a Symphony icon to ntfy notifications and the web UI ([c9dba4e](https://github.com/skorokithakis/symphony/commit/c9dba4e7e2240871201dd2cc6cb43634fd0dbc0f))
* Add configurable Anthropic base URL for automatic titles ([0e69a0b](https://github.com/skorokithakis/symphony/commit/0e69a0bfcc0980e152417f475b8b0eb3b0cc1b52))
* Add model and repository dropdowns to the web UI editor ([c4c6b4f](https://github.com/skorokithakis/symphony/commit/c4c6b4f3906b31c2430e3376d9a6a0edecc424b5))
* Add navigation and next Needs Input shortcuts to the web UI ([897f76b](https://github.com/skorokithakis/symphony/commit/897f76bd87e137fff497c7a7040830d6d653d109))
* Add Soniox microphone dictation to the web UI ([e2e2fea](https://github.com/skorokithakis/symphony/commit/e2e2fea73b002336d2c3e316b4cb85e9dbe59a34))
* Add Web Push notifications to the web UI ([ac03750](https://github.com/skorokithakis/symphony/commit/ac03750ebb3811967b1d2f8819bbe7fd2ff52fd9))
* Adopt the new web UI stylesheet ([7e64851](https://github.com/skorokithakis/symphony/commit/7e648517a39546d7d4abc1e8c16fa35666a68e3b))
* Cache-bust web UI static files ([e4d6dca](https://github.com/skorokithakis/symphony/commit/e4d6dca98e4baa53c1a64fd1f28839b46078b382))
* Change the icon and re-tint the web UI to match it ([9cd7ca2](https://github.com/skorokithakis/symphony/commit/9cd7ca2e2aea21d44ee0e05c2cfc03c1a85dec37))
* Focus the ticket text input on the new ticket page ([49cea6e](https://github.com/skorokithakis/symphony/commit/49cea6e1ac1ab029c719c8bc945907e6b07bf716))
* Improve web UI Markdown images and list editing ([e78d521](https://github.com/skorokithakis/symphony/commit/e78d5212e5a2a8f25b48843aba700e6c51ed8276))
* Make the web UI installable as a PWA ([b0ad1ec](https://github.com/skorokithakis/symphony/commit/b0ad1ecdc02fb4bfadd01564c6bc81ec15933df0))
* Replace the c new-ticket shortcut with g n on every page ([5cdc8a6](https://github.com/skorokithakis/symphony/commit/5cdc8a6150b16719ec5592cf5993267698f69908))
* Send ntfy notifications when a web UI ticket needs input ([1c95368](https://github.com/skorokithakis/symphony/commit/1c953689ad57e0659f186868c85c87ab0d45678e))
* Show last agent output, add Retry and a per-turn model choice, and summarise replies in the web UI ([79c31f4](https://github.com/skorokithakis/symphony/commit/79c31f4bfec276480efa1e32ca1a2693a97d303f))
* Show reply summaries in full on the home page ([3be8d92](https://github.com/skorokithakis/symphony/commit/3be8d9246aac6cf44eee5b0e57e85e05fa8954cb))
* Show whole reply summaries up to 500 characters ([7571488](https://github.com/skorokithakis/symphony/commit/7571488bc501113f5a7936865e6d8dc7c24f5efc))
* Skip the Needs Input push while the ticket page is being watched ([78545ff](https://github.com/skorokithakis/symphony/commit/78545ffbbd69de1c5e95c4cc8d37c2107fcf9285))
* Sort the editor's repository list by name and add a Recent group ([8bec91d](https://github.com/skorokithakis/symphony/commit/8bec91ddd108210c83d33903941e91791f9dedc0))
* Start dictation immediately and allow sending while recording ([ea38f03](https://github.com/skorokithakis/symphony/commit/ea38f03f8fb9522174d0667905db2e9cef93c11b))
* Type into the reply box on unrecognised keys in the ticket page ([4291158](https://github.com/skorokithakis/symphony/commit/42911582b1a437b6f652396310ac4a167c17a154))
* Use letter state shortcuts and show key badges while a prefix is armed ([f2a4785](https://github.com/skorokithakis/symphony/commit/f2a4785b976d400386f8f162d78ffc7a4bc2321a))


### Bug Fixes

* Always right-align the ticket composer buttons ([1829aea](https://github.com/skorokithakis/symphony/commit/1829aeaf2a4186dc85a9213bfbc0b4c9c90b2857))
* Delete ticket dirs that contain read-only directory trees ([8d82ad5](https://github.com/skorokithakis/symphony/commit/8d82ad548f5857226181502437a30eeba0b1ec20))
* Disable thinking in Anthropic calls so reply summaries get text ([3c5705c](https://github.com/skorokithakis/symphony/commit/3c5705c4da38baa5776886bd45d69d06d2f212b9))
* Don't create a Draft from the editor until it has text ([7696f09](https://github.com/skorokithakis/symphony/commit/7696f099512ddc1d17069942aee0c6078213c214))
* Keep the editor out of history after Start or Discard ([edf506f](https://github.com/skorokithakis/symphony/commit/edf506f7cee4274c07e7f4b3e2822dfd07843259))
* Keep the editor's Start enabled during dictation ([c46fcd3](https://github.com/skorokithakis/symphony/commit/c46fcd3f44357b9dd27be5c262a1c0cd15087d03))
* Keep the push bell on the wordmark row in the desktop rail ([f088193](https://github.com/skorokithakis/symphony/commit/f088193799304ebe860cf74cec8c66f5e32e48c0))
* Keep ticket actions blocked during dictation across refreshes ([3fce850](https://github.com/skorokithakis/symphony/commit/3fce850cafaddc85953a939c8680609ad81a779e))
* Make OpenCode's lock directory writable inside the sandbox ([756b515](https://github.com/skorokithakis/symphony/commit/756b515840e138aa941e1ab6b5bfc2bf504cbef9))
* Remove the double focus outline on the reply box ([fc20adb](https://github.com/skorokithakis/symphony/commit/fc20adba2c863319a14800629df38d0a0fe0949b))
* Serve the ntfy icon from GitHub instead of an unauthenticated route ([ea22a5c](https://github.com/skorokithakis/symphony/commit/ea22a5c166edd5c60d5e8547668019ddd0b5f392))
* Show saved repository names in the web UI ticket list and ticket page ([acb9952](https://github.com/skorokithakis/symphony/commit/acb9952cef563aff722197a7b9aa123457f3d5f9))
* Sort the repository list by name everywhere ([ae210d8](https://github.com/skorokithakis/symphony/commit/ae210d8e95cc9dc93edc3785ff1cadf2f5487401))
* Stop cutting reply summaries mid-sentence ([5aa8f86](https://github.com/skorokithakis/symphony/commit/5aa8f8666911f3abf85de9345e1879f71a92f5b0))


### Documentation

* Describe the agent API's capabilities and limits in the README ([b2ce17c](https://github.com/skorokithakis/symphony/commit/b2ce17cc86cbbe60139afa440f5f42939b032f80))
* List the editor Ctrl+Enter shortcut in the web UI help dialog ([38d182c](https://github.com/skorokithakis/symphony/commit/38d182cac3b871e2204bdd776d35fcaf6332cff6))


### Code Refactoring

* Remove ntfy notifications ([2ffdcc0](https://github.com/skorokithakis/symphony/commit/2ffdcc0013fa5a2060a383b9c2e27c8692205cde))

## [0.8.0](https://github.com/skorokithakis/symphony/compare/v0.7.0...v0.8.0) (2026-10-16)


### ⚠ BREAKING CHANGES

* OpenCode V1 is no longer supported.

### Features

* Add a project-wide default model ([bc8772a](https://github.com/skorokithakis/symphony/commit/bc8772a16e54b4dcb87bd4a7125301fff07cbf83))
* Add OMP as a selectable coding agent alongside OpenCode ([b5a1a6d](https://github.com/skorokithakis/symphony/commit/b5a1a6d25effb6afb0030d0893964bfc74d0d300))
* Add per-repo secrets files for sandbox launches ([795ae3c](https://github.com/skorokithakis/symphony/commit/795ae3c04171f9389dce56c97e89d278f1181394))
* Add pi as a selectable coding agent ([36a3d8f](https://github.com/skorokithakis/symphony/commit/36a3d8fa4dfa5fd67af5341040f02e10f1df3d6f))
* Add sandbox.dir_map for per-ticket and shared bind mounts ([7424d9a](https://github.com/skorokithakis/symphony/commit/7424d9a217d9baf489b6a43f0b1fa63cbce324b3))
* Download non-image file attachments into the sandbox ([f8b9d0d](https://github.com/skorokithakis/symphony/commit/f8b9d0d3f5a3d47b0ecd505d3c9a9b771ebce449))
* Log auto-restart state and deferred restarts ([f9b3241](https://github.com/skorokithakis/symphony/commit/f9b32414c0351918cfc5f4d384acf06d75559aed))
* Make the Linear trigger label optional ([3eb911c](https://github.com/skorokithakis/symphony/commit/3eb911c6a0c6f3dd5aea7c8ae248e4ae851c5783))
* Prepare workspace and run setup when a new ticket starts in QA ([873ed23](https://github.com/skorokithakis/symphony/commit/873ed237bb65529bf0499f96411aa23f1188c6d5))
* Restart the daemon automatically when its git checkout changes ([251cbd3](https://github.com/skorokithakis/symphony/commit/251cbd387dc352db8f6dd5e4ffa0bdb9d6a9801f))
* Stop the running agent turn and say so when a ticket leaves In Progress ([3ab3701](https://github.com/skorokithakis/symphony/commit/3ab37012920d851db5c62308a041e56442ddb5ec))


### Bug Fixes

* Detect pi-family mid-turn failures despite a zero exit code ([1f0f1ba](https://github.com/skorokithakis/symphony/commit/1f0f1ba42d88c8176828d35d181a1822674f962b))
* Keep a QA-interrupted ticket reachable by comment ([2ea5f07](https://github.com/skorokithakis/symphony/commit/2ea5f07798976207821a0050381f5fb91c702b93))
* Keep tickets in QA after a QA move stops a running turn ([c03950b](https://github.com/skorokithakis/symphony/commit/c03950b90b252bc245476ef3a313607a0c8ddefc))
* Log tracker transitions and move resumed tickets to In Progress first ([2bf4b92](https://github.com/skorokithakis/symphony/commit/2bf4b922d76a1d2baab95c275b1fba21d23c2b21))
* Move new tickets to In Progress before cloning the workspace ([200cf90](https://github.com/skorokithakis/symphony/commit/200cf909d66c2a7d5e2c0ea6fa54c0cb7dc2ca51))
* Post a single Restored comment when resuming a session from Done ([49f20fb](https://github.com/skorokithakis/symphony/commit/49f20fb29cc8918b73db54b126b0757ef7e7c6f6))
* Retry transient errors when posting the final reply ([bba4e90](https://github.com/skorokithakis/symphony/commit/bba4e904b2d9652cf7166c5cec370cd6dce35414))
* Retry transient tracker errors on pipeline state transitions ([34ae6d9](https://github.com/skorokithakis/symphony/commit/34ae6d975c760fab04a75c728b78b4cb20a695fb))
* Stop the running agent turn when a ticket is moved out of In Progress ([bc8456b](https://github.com/skorokithakis/symphony/commit/bc8456b73ac431ff796536c56350e4fb53f0459a))
* Stop the sandboxed agent reading daemon credentials ([#16](https://github.com/skorokithakis/symphony/issues/16)) ([34cf7a4](https://github.com/skorokithakis/symphony/commit/34cf7a4f0d58f8d5f3450b5f34f50a5ef535f2d8))
* Support OpenCode V2 CLI ([d155e66](https://github.com/skorokithakis/symphony/commit/d155e66f30d3177abcde90f61814bf68e1f91c4c))
* Tell the user when a mid-turn comment is ignored ([e4da13a](https://github.com/skorokithakis/symphony/commit/e4da13a3cd49751f0f02402a46eeea9ff9a617a6))


### Documentation

* Document trigger_label: null in the example config ([7a67339](https://github.com/skorokithakis/symphony/commit/7a67339091b4ccb87bef6d3741205756b09756f6))
* Explain how the secrets file path is derived from the repo URL ([23c7ca6](https://github.com/skorokithakis/symphony/commit/23c7ca651bf317227724964473a2713571dbc6ae))
* Record QA stop comment-anchor decision ([b16c2a4](https://github.com/skorokithakis/symphony/commit/b16c2a4dba1653f540ca8d569bb0150041a18b47))
* Record tracker transition retry decision ([54bfc02](https://github.com/skorokithakis/symphony/commit/54bfc02d9ed3f790baf7d90f2b04270e2f6c60e1))

## [0.7.0](https://github.com/skorokithakis/symphony/compare/v0.6.0...v0.7.0) (2026-08-22)


### Features

* Add per-issue model override via Model labels ([c7ad2e3](https://github.com/skorokithakis/symphony/commit/c7ad2e3cd4ce981989dfd7753fb6ca8a4a2e2546))
* Reject malformed model ids at config load ([e92838d](https://github.com/skorokithakis/symphony/commit/e92838d89fe9a8ce0615cbb0a3c67ebb52f81943))
* Trim tracker comments to the agent's closing reply ([b33d183](https://github.com/skorokithakis/symphony/commit/b33d18328700a37b406183e0b23f35d7928aa3ed))


### Bug Fixes

* Move failed tickets to Needs Input ([2682e1d](https://github.com/skorokithakis/symphony/commit/2682e1d7a329a2d4144310571f97b977ad2c2c8e))
* Report the tail of OpenCode stderr on failure ([0492f8d](https://github.com/skorokithakis/symphony/commit/0492f8d1c6b429baf283adb9ec1917b68f59a9fb))
* Skip the misleading rehydrate message when a comment is pending ([bc0d4ce](https://github.com/skorokithakis/symphony/commit/bc0d4ce34a9d60eabf07826f61141c2baf2c14b0))


### Documentation

* Clarify systemd configuration for sandbox PATH and SSH agent ([7e0f474](https://github.com/skorokithakis/symphony/commit/7e0f4745c4538855cf94707d348dad6228067718))

## [0.6.0](https://github.com/skorokithakis/symphony/compare/v0.5.0...v0.6.0) (2026-08-12)


### ⚠ BREAKING CHANGES

* The per-ticket on-disk layout changed from <root>/<id> plus <root>/.attachments/<id> to <root>/<id>/{repo,attachments,tmp}. Existing workspaces are not migrated — drain or wipe workspace_root before upgrading.

### Features

* Bind per-ticket tmp dir at /tmp inside the bwrap sandbox ([eb462c8](https://github.com/skorokithakis/symphony/commit/eb462c8597e31d6e991225e97fa6d79465ba881e))
* Restructure per-ticket layout to &lt;root&gt;/&lt;id&gt;/{repo,attachments,tmp} ([e1de8ba](https://github.com/skorokithakis/symphony/commit/e1de8ba537d6e4edc090b494098378bfc5029e84))
* Stop OpenCode turns hanging forever, and add an idle watchdog ([97df7c3](https://github.com/skorokithakis/symphony/commit/97df7c3039854db117a070b774c2a834b3800814))


### Bug Fixes

* Re-run the interrupted turn after a daemon restart ([6471b43](https://github.com/skorokithakis/symphony/commit/6471b438ea855acff820881bc5b616ac3cb6f592))


### Documentation

* Update docs for the per-ticket directory layout and sandbox /tmp ([c5b1af4](https://github.com/skorokithakis/symphony/commit/c5b1af49e263fc7f15077db47c61d1ed68247fdc))

## [0.5.0](https://github.com/skorokithakis/symphony/compare/v0.4.0...v0.5.0) (2026-08-08)


### Features

* Pass ticket images to OpenCode as file attachments ([a58f700](https://github.com/skorokithakis/symphony/commit/a58f7009454e9606af75be6d46e89609479dfc1e))
* Post partial output and salvage session id on turn timeout ([4d043ab](https://github.com/skorokithakis/symphony/commit/4d043ab4fdc6abe6a1462c1dc3f52b84139d79ef))
* Refuse to delete a workspace that has uncommitted work ([2e05798](https://github.com/skorokithakis/symphony/commit/2e05798b4ea1226799b4b01fdbefea82ec2f314e))


### Bug Fixes

* Break QA serve retry loop when serve fails to start ([e455005](https://github.com/skorokithakis/symphony/commit/e455005d48ffc9cd72df64fab4645e46594071bc))
* Preserve OpenCode session across workspace cleanup and re-trigger ([3b45d2d](https://github.com/skorokithakis/symphony/commit/3b45d2d4236633c94eac67afbdb379af45fabdee))
* Quiet transient errors in poll-loop issue fetch ([392f979](https://github.com/skorokithakis/symphony/commit/392f97992601652a241168b0f3fdf6f1f65e8d69))
* Run the full initial prompt when a ticket has no session ([9ec2b58](https://github.com/skorokithakis/symphony/commit/9ec2b589f27ec801d48b0754de3ffa472ef413bb))

## [0.4.0](https://github.com/skorokithakis/symphony/compare/v0.3.0...v0.4.0) (2026-05-24)


### ⚠ BREAKING CHANGES

* The 'bot_user_email' field in the Linear config section has been removed. Configs that still set it will fail to load.

### Features

* Add clone_protocol option to GitHub backend for HTTPS cloning ([54b9612](https://github.com/skorokithakis/symphony/commit/54b961257d91f344bdc4fc45a341c631e4244fa1))
* add optional webhook receiver to wake the poll loop on Linear updates ([571f5e8](https://github.com/skorokithakis/symphony/commit/571f5e8e8de1c26600577cc4e05c07b492d628cb))
* Append context-tokens footer to tracker comments ([cdf6c34](https://github.com/skorokithakis/symphony/commit/cdf6c347023ed414535461d277be9eed34292893))
* Identify daemon comments by sentinel instead of bot user identity ([e5fc532](https://github.com/skorokithakis/symphony/commit/e5fc53248baa20aa7913f4d7d5e608d9ac051958))


### Bug Fixes

* Auto-recover workspace on git fetch failure when clean ([d9394f0](https://github.com/skorokithakis/symphony/commit/d9394f08a6fad7b5f061f7a635ac8af89a676c16))
* Baseline last_seen_comment_id when error comment post fails ([2983210](https://github.com/skorokithakis/symphony/commit/298321057c8d088ff782c82af06b4bfaf9b61c5d))
* Tolerate per-item GraphQL errors when listing GitHub project items ([336161a](https://github.com/skorokithakis/symphony/commit/336161a4278cc7da28894dd7ea60b90a2305185d))


### Documentation

* Document clone_protocol option in config.yaml.example ([f7deceb](https://github.com/skorokithakis/symphony/commit/f7deceb1570821920956c8d99582de4eb076d8c4))
* Note pre-commit tooling in AGENTS.md ([d05a97e](https://github.com/skorokithakis/symphony/commit/d05a97e2f0f5d8443d95dfd73456317fb05fb03d))
* Require capitalized description in commit messages ([4d0053f](https://github.com/skorokithakis/symphony/commit/4d0053faed8adfecc131abe7ecf250fa890e8f6a))

## [0.3.0](https://github.com/skorokithakis/symphony/compare/v0.2.0...v0.3.0) (2026-05-15)


### Features

* Add experimental support for GitHub Projects v2 as an issue tracker ([112076c](https://github.com/skorokithakis/symphony/commit/112076c7eff100edc4214e29c69248dd632a0fc0))
* Sandbox: enable SSH functionality with opt-in configuration ([f3bb111](https://github.com/skorokithakis/symphony/commit/f3bb1111d220c43dd1389abbe6172778f87a760f))

## [0.2.0](https://github.com/skorokithakis/symphony/compare/v0.1.0...v0.2.0) (2026-05-14)


### ⚠ BREAKING CHANGES

* The default trigger label has been renamed from "agent" to "Agent" to match Linear canonical capitalization. Existing deployments that use the lowercase "agent" label must either rename their Linear label to "Agent" or set `trigger_label: agent` in their config.yaml to preserve current behaviour.

### Features

* Add screenshot to README ([ac7b65f](https://github.com/skorokithakis/symphony/commit/ac7b65ff918add6104acfc251c0c76f0aa95072d))
* auto-provision Linear trigger label on daemon startup ([9a087e0](https://github.com/skorokithakis/symphony/commit/9a087e01b7006dbe567af904a377cb0df0007da4))
* rename default trigger label from "agent" to "Agent" ([33dec8d](https://github.com/skorokithakis/symphony/commit/33dec8d69deb9e51344e9d44bea89ae534c62dd6))


### Documentation

* Add new screenshot to README ([2cb4c91](https://github.com/skorokithakis/symphony/commit/2cb4c915bf02df979ad66bbaeed2aab6dc9ce9f0))

## 0.1.0 (2026-05-13)


### ⚠ BREAKING CHANGES

* the 'symphony' command no longer exists. Update any scripts, aliases, or service units to use 'symphony-linear' instead.

### Features

* Add symphony setup script for environment bootstrapping ([acf12f6](https://github.com/skorokithakis/symphony/commit/acf12f69ff5cf003975cb81020ad5f923e4e182f))
* rename CLI entry point from 'symphony' to 'symphony-linear' ([5606bd9](https://github.com/skorokithakis/symphony/commit/5606bd981489e94717f0d722f5e4e43ef0879ee3))


### Bug Fixes

* correct release-please bootstrap config ([c98511f](https://github.com/skorokithakis/symphony/commit/c98511f1f861ddbd61712fa6607a6ac86cb2ada1))
* resolve pre-commit failures ([04ef96d](https://github.com/skorokithakis/symphony/commit/04ef96d5d3fa851a6ae4864dbb2b5636dab8057f))


### Documentation

* rewrite README for v1.0 release ([9feefaa](https://github.com/skorokithakis/symphony/commit/9feefaa1ecf6bc7817c9cf7e893cc5dd02a76a61))
