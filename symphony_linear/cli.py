"""CLI entry-point for ``symphony-lite``."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

from symphony_linear.config import AppConfig, load_config
from symphony_linear.linear import LinearClient
from symphony_linear.logging import get_logger, setup_logging
from symphony_linear.orchestrator import Orchestrator
from symphony_linear.state import load_state

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony",
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
    """Entry point for the ``symphony-lite`` CLI."""
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
        logger.info("  model           = %s", config.opencode.model)
        return

    # Load state and create the Linear client.
    state = load_state(workspace)
    linear = LinearClient(api_key=config.linear.api_key)

    # Create and run the orchestrator daemon.
    orchestrator = Orchestrator(
        config=config, state=state, linear=linear, workspace=workspace
    )
    orchestrator.run()
