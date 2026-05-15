# Changelog

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
