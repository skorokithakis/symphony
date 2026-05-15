"""CLI entry-point for ``symphony-linear``."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

from symphony_linear.config import AppConfig, load_config
from symphony_linear.github import GitHubClient
from symphony_linear.github_tracker import GitHubTracker, GitHubTrackerConfig
from symphony_linear.linear import LinearClient
from symphony_linear.linear_tracker import LinearTracker
from symphony_linear.logging import get_logger, setup_logging
from symphony_linear.orchestrator import Orchestrator
from symphony_linear.state import load_state
from symphony_linear.tracker import Tracker
from symphony_linear.webhook import WebhookServer

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony-linear",
        description="AI-powered ticket orchestration daemon",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Path to workspace directory (default: current working directory). "
        "Expects config.yaml and state.json inside this directory.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug-level logging",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Load and validate the config file, then exit",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``symphony-linear`` CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    setup_logging(debug=args.debug)

    workspace = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else Path.cwd().resolve()
    )

    try:
        config: AppConfig = load_config(workspace)
    except FileNotFoundError:
        config_path = workspace / "config.yaml"
        print(
            f"Config file not found: {config_path}\n"
            f"Create a config.yaml file in {workspace} with the required settings.",
            file=sys.stderr,
        )
        sys.exit(1)
    except ValueError as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    if args.validate_config:
        logger.info("Config is valid.")
        logger.info("  workspace       = %s", workspace)
        logger.info("  poll_interval   = %s s", config.poll_interval_seconds)
        return

    # Load state and create the appropriate tracker backend.
    state = load_state(workspace)
    tracker = _create_tracker(config)

    # Auto-provision the trigger field/label on startup.
    tracker.ensure_trigger_setup(state)

    # Create and run the orchestrator daemon.
    orchestrator = Orchestrator(
        config=config, state=state, tracker=tracker, workspace=workspace
    )
    if config.webhook is not None:
        webhook_server = WebhookServer(
            port=config.webhook.port,
            linear_secret=config.webhook.linear_secret,
            on_wake=orchestrator.wake,
        )
        orchestrator.set_webhook_server(webhook_server)
    orchestrator.run()


def _create_tracker(config: AppConfig) -> Tracker:
    """Instantiate the correct tracker backend based on config."""
    if config.linear is not None:
        linear_client = LinearClient(api_key=config.linear.api_key)
        return LinearTracker(linear=linear_client, config=config.linear)
    if config.github is not None:
        github_client = GitHubClient(token=config.github.token)
        github_tracker_config = GitHubTrackerConfig(
            token=config.github.token,
            project_ref=config.github.project,
            trigger_field=config.github.trigger_field,
            status_field=config.github.status_field,
            in_progress_status=config.github.in_progress_status,
            needs_input_status=config.github.needs_input_status,
            qa_status=config.github.qa_status,
        )
        return GitHubTracker(client=github_client, config=github_tracker_config)
    # The model_validator guarantees this is unreachable, but be defensive.
    raise RuntimeError("No tracker backend configured")
