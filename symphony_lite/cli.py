"""CLI entry-point for ``symphony-lite``."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

from symphony_lite.config import AppConfig, load_config
from symphony_lite.linear import LinearClient
from symphony_lite.logging import get_logger, setup_logging
from symphony_lite.orchestrator import Orchestrator
from symphony_lite.state import load_state

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony-lite",
        description="AI-powered ticket orchestration daemon",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: ~/.config/symphony-lite/config.yaml, "
        "overridable via $SYMPHONY_CONFIG)",
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

    try:
        config_path = Path(args.config) if args.config else None
        config: AppConfig = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    if args.validate_config:
        logger.info("Config is valid.")
        logger.info("  workspace_root = %s", config.workspace_root)
        logger.info("  poll_interval   = %s s", config.poll_interval_seconds)
        logger.info("  model           = %s", config.opencode.model)
        return

    # Load state and create the Linear client.
    state = load_state()
    linear = LinearClient(api_key=config.linear.api_key)

    # Create and run the orchestrator daemon.
    orchestrator = Orchestrator(config=config, state=state, linear=linear)
    orchestrator.run()
