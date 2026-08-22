# Changelog

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
