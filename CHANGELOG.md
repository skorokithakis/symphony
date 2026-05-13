# Changelog

## [2.0.0](https://github.com/skorokithakis/symphony/compare/v1.0.0...v2.0.0) (2026-05-13)


### ⚠ BREAKING CHANGES

* the 'symphony' command no longer exists. Update any scripts, aliases, or service units to use 'symphony-linear' instead.

### Features

* Add symphony setup script for environment bootstrapping ([acf12f6](https://github.com/skorokithakis/symphony/commit/acf12f69ff5cf003975cb81020ad5f923e4e182f))
* rename CLI entry point from 'symphony' to 'symphony-linear' ([5606bd9](https://github.com/skorokithakis/symphony/commit/5606bd981489e94717f0d722f5e4e43ef0879ee3))


### Bug Fixes

* resolve pre-commit failures ([04ef96d](https://github.com/skorokithakis/symphony/commit/04ef96d5d3fa851a6ae4864dbb2b5636dab8057f))


### Documentation

* rewrite README for v1.0 release ([9feefaa](https://github.com/skorokithakis/symphony/commit/9feefaa1ecf6bc7817c9cf7e893cc5dd02a76a61))

## [1.0.0](https://github.com/skorokithakis/symphony/compare/v0.1.0...v1.0.0) (2026-05-13)


### ⚠ BREAKING CHANGES

* Any `opencode:` section in `config.yaml` must be removed. Existing configs with an `opencode:` section will fail at load time with a pydantic 'extra fields not permitted' error.

### Features

* Add auto_branch configuration option ([b4d1110](https://github.com/skorokithakis/symphony/commit/b4d1110fc6b65fb05a3d02903760c93c12fcd6dc))
* add CI workflows and rename package to symphony-linear ([8e28d6a](https://github.com/skorokithakis/symphony/commit/8e28d6a03648fcc4fc565e8819901a262864c4fe))
* Add QA serve lifecycle support ([bf5147d](https://github.com/skorokithakis/symphony/commit/bf5147d58c94347a2c576b90490c00cf7689307f))
* Add sandbox.extra_rw_paths config option ([f619ca7](https://github.com/skorokithakis/symphony/commit/f619ca72be3f301333d585b12ed49668a9a18efd))
* docs/debug: Update README overview and add debug logging for OpenCode ([ca58ac9](https://github.com/skorokithakis/symphony/commit/ca58ac94a7e2194973543debbb717a072a74bd49))
* Format final OpenCode message with tool-call separators ([0b8f96e](https://github.com/skorokithakis/symphony/commit/0b8f96e7888ad47a81b9311f159274fc08dc7b21))
* Inherit daemon PATH in sandbox by default ([0af972d](https://github.com/skorokithakis/symphony/commit/0af972d634c70d4c24e48a15cd2257405f719de1))
* orchestrator: Remove workspace when ticket is deleted from Linear ([14aa162](https://github.com/skorokithakis/symphony/commit/14aa162b3239516ad208af537ad86fe00f06a7ee))
* Orchestrator: transition QA tickets to needs_input when workspace is missing ([06db3c5](https://github.com/skorokithakis/symphony/commit/06db3c5fd2726137786d0c4b4b59dc74c9823cac))
* process human comments on QA tickets by resuming agent ([0fbad08](https://github.com/skorokithakis/symphony/commit/0fbad08aee1dceffe15a849e5047f67db68ab9c6))
* Relocate config and state to workspace directory ([e1680d4](https://github.com/skorokithakis/symphony/commit/e1680d401531d6ba10218e31ca86d8bbe5ab6046))
* remove opencode.model config option ([a38e03a](https://github.com/skorokithakis/symphony/commit/a38e03aba76eeb34c6b1841da6d680b97bdb7c74))
* Remove ticket management directory and files ([051173e](https://github.com/skorokithakis/symphony/commit/051173e8ad315de3c51b99332def08b9dedf5354))
* rewrite GitHub HTTPS browser URLs to SSH when cloning ([0610a3e](https://github.com/skorokithakis/symphony/commit/0610a3e0e38ca37cffd9902dae2119d470a4e5e6))
* support per-project config via .symphony/config.yaml ([07944f9](https://github.com/skorokithakis/symphony/commit/07944f9c81d85e1240af460882057ed4f9e2ff55))
* Unify ticket cleanup logic ([b846b52](https://github.com/skorokithakis/symphony/commit/b846b52aa29840b85fb177147fdf6b1329a40dde))


### Bug Fixes

* Improve Linear comment ordering and handle recovery restarts ([9088d3c](https://github.com/skorokithakis/symphony/commit/9088d3c35ad098c206ab85f32c873ce2675a9158))


### Documentation

* Clarify the README description ([73fa596](https://github.com/skorokithakis/symphony/commit/73fa59633e60865ac3289bf4ea735eb05adc35d4))
