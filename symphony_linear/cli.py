"""CLI entry-point for ``symphony-linear``."""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from pathlib import Path

from symphony_linear.config import AppConfig, load_config
from symphony_linear.github import GitHubClient
from symphony_linear.github_tracker import GitHubTracker, GitHubTrackerConfig
from symphony_linear.linear import LinearClient
from symphony_linear.linear_tracker import LinearTracker
from symphony_linear.local_tracker import LocalTracker
from symphony_linear.logging import get_logger, setup_logging
from symphony_linear.orchestrator import Orchestrator
from symphony_linear.presence import TicketPresence
from symphony_linear.sandbox_path import resolve_sandbox_path
from symphony_linear.state import load_state
from symphony_linear.tracker import Tracker, model_label_name
from symphony_linear.turn_activity import TurnActivity
from symphony_linear.webhook import WebhookServer
from symphony_linear.webpush import WebPushStore
from symphony_linear.webui import WebUIServer, summary

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

    sandbox_path = resolve_sandbox_path()
    if shutil.which(config.agent, path=sandbox_path) is None:
        logger.error(
            "Configured agent binary %r was not found on sandbox PATH %r. "
            "Install it there, set SYMPHONY_SANDBOX_PATH, or choose another "
            "'agent' value in config.yaml.",
            config.agent,
            sandbox_path,
        )
        sys.exit(1)

    if args.validate_config:
        logger.info("Config is valid.")
        logger.info("  workspace       = %s", workspace)
        logger.info("  poll_interval   = %s s", config.poll_interval_seconds)
        return

    # Load state and create the appropriate tracker backend.  The presence
    # record is one instance per process: the web UI writes to it, the local
    # tracker reads it to decide whether a human is already looking at the
    # ticket, so both must share the same object.
    state = load_state(workspace)
    presence = TicketPresence()
    # Like presence, the turn-activity record is one instance per process: the
    # orchestrator writes a ticket's agent output time, the web UI reads it to
    # show the age on the ticket page.
    turn_activity = TurnActivity()
    tracker = _create_tracker(config, workspace, presence=presence)

    # Auto-provision the trigger field/label and model alias labels on startup.
    model_labels = [model_label_name(alias) for alias in config.models]
    tracker.ensure_trigger_setup(state, model_labels)

    # Create and run the orchestrator daemon.
    orchestrator = Orchestrator(
        config=config,
        state=state,
        tracker=tracker,
        workspace=workspace,
        turn_activity=turn_activity,
    )
    if config.webhook is not None:
        webhook_server = WebhookServer(
            port=config.webhook.port,
            linear_secret=config.webhook.linear_secret,
            on_wake=orchestrator.wake,
        )
        orchestrator.set_webhook_server(webhook_server)
    webui_server: WebUIServer | None = None
    if config.webui is not None:
        if not isinstance(tracker, LocalTracker):
            raise RuntimeError("The webui backend requires the local tracker")
        webui_server = WebUIServer(
            store=tracker.store,
            state=state,
            config=config,
            on_wake=orchestrator.wake,
            presence=presence,
            turn_activity=turn_activity,
            webpush=tracker.webpush,
        )
    try:
        # Start inside the try so a failed bind/thread start still runs
        # stop(); WebUIServer.stop() is safe when the loop never started.
        if webui_server is not None:
            webui_server.start()
        # `is True` (not truthiness) so a mocked orchestrator in tests cannot
        # accidentally trigger a re-exec.
        restart = orchestrator.run() is True
    finally:
        # Stop the server before a re-exec so that requests in flight end
        # cleanly and the port is free when the new process binds it.
        if webui_server is not None:
            webui_server.stop()
    if restart:
        _reexec()


def _reexec() -> None:
    """Replace this process with the original command line.

    The orchestrator asks for a restart when its git checkout moved, but the
    re-exec lives here so the orchestrator stays testable.  ``os.execv`` keeps
    the same PID (and open descriptors), so a supervisor such as systemd sees
    one process that never exited and needs no configuration change.
    """
    argv = list(sys.orig_argv)
    if not argv:
        argv = [sys.executable, "-m", "symphony_linear", *sys.argv[1:]]
    os.execv(sys.executable, argv)


def _create_tracker(
    config: AppConfig, workspace: Path, *, presence: TicketPresence | None = None
) -> Tracker:
    """Instantiate the correct tracker backend based on config.

    *presence* is the web UI's viewer-presence record; only the local backend
    uses it, to suppress the Needs Input push while the ticket's page is
    being watched.  The other backends ignore it.
    """
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
            clone_protocol=config.github.clone_protocol,
        )
        return GitHubTracker(client=github_client, config=github_tracker_config)
    if config.webui is not None:
        return LocalTracker(
            workspace,
            ntfy=config.webui.ntfy,
            public_url=config.webui.public_url,
            presence=presence,
            on_agent_reply=summary.agent_reply_hook(config.webui),
            # Web Push is always on for this backend and has no config.  Build
            # the one store here; ``LocalTracker.webpush`` exposes it so the
            # web server adds subscriptions to the same instance the tracker
            # sends from.
            webpush=WebPushStore(workspace),
        )
    # The model_validator guarantees this is unreachable, but be defensive.
    raise RuntimeError("No tracker backend configured")
