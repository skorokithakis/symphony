"""Daemon orchestrator: poll loop, per-ticket lifecycle, concurrency, error handling."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony_linear.config import AppConfig
from symphony_linear.linear import (
    Comment,
    Issue,
)
from symphony_linear.opencode import (
    OpenCodeCancelled,
    OpenCodeError,
    OpenCodeTimeout,
    run_initial,
    run_resume,
)
from symphony_linear.project_config import (
    ProjectConfigError,
    load_project_config,
)
from symphony_linear.state import StateManager, TicketState, TicketStatus
from symphony_linear.tracker import (
    Tracker,
    TrackerError,
    TrackerNotFoundError,
    TransitionTarget,
)
from symphony_linear.webhook import WebhookServer
from symphony_linear.workspace import (
    ServeScriptMissing,
    WorkspaceError,
    clone_workspace,
    finalize_workspace,
    remove,
    start_serve,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SHUTDOWN_GRACE_SECONDS = 5
_RESTART_NOTICE_BODY = (
    "**Symphony**: Restarted before setup completed. "
    "Picking this ticket up again on the next poll."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_metadata_comment(workspace_path: str) -> str:
    return f"**Symphony**\n- workspace: `{workspace_path}`\n- session: _pending_"


def _build_metadata_comment_final(workspace_path: str, session_id: str) -> str:
    return f"**Symphony**\n- workspace: `{workspace_path}`\n- session: `{session_id}`"


def _build_initial_prompt(title: str, description: str | None) -> str:
    desc = description.strip() if description else "(no description)"
    return (
        "You're working on a Linear ticket. Anything you say will be posted as a "
        "comment on the ticket. The human will reply by commenting on the ticket, "
        "and their replies will be delivered to you as user messages. There's no "
        "other way to talk to them.\n\n---\n\n"
        f"# {title}\n\n{desc}"
    )


def _format_comments_message(comments: list[Comment]) -> str:
    parts: list[str] = []
    for c in comments:
        author = c.user_id or "unknown"
        parts.append(f"[{author} at {c.created_at}]\n{c.body}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# QA serve container
# ---------------------------------------------------------------------------

_DRAINER_CAP = 1000  # bytes captured per pipe


@dataclass
class _ActiveServe:
    """Mutable container for a running QA serve process."""

    ticket_id: str
    ticket_identifier: str
    proc: subprocess.Popen[bytes]
    start_monotonic: float
    stdout_head: bytearray = field(default_factory=bytearray)
    stderr_head: bytearray = field(default_factory=bytearray)
    intentional_kill: threading.Event = field(default_factory=threading.Event)
    failure_comment_posted: bool = False


def _format_serve_died_comment(rc: int | None, stdout: str, stderr: str) -> str:
    """Format the 'QA serve exited' Linear comment body."""
    stdout_body = stdout[:_DRAINER_CAP] if stdout else "(empty)"
    stderr_body = stderr[:_DRAINER_CAP] if stderr else "(empty)"
    return (
        f"**Symphony**: QA serve exited (rc={rc}). "
        "Transitioning ticket back to Needs Input — re-enter QA to retry.\n\n"
        f"**stdout** (first 1000 chars):\n```\n{stdout_body}\n```\n\n"
        f"**stderr** (first 1000 chars):\n```\n{stderr_body}\n```"
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        state: StateManager,
        tracker: Tracker,
        workspace: Path,
        webhook_server: WebhookServer | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._tracker = tracker
        self._workspace = workspace
        self._webhook_server = webhook_server

        self._executor = ThreadPoolExecutor(max_workers=5)

        # Subprocess tracking + cancellation flags (guarded by _subprocess_lock).
        self._subprocesses: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()
        self._subprocess_lock = threading.Lock()

        # Active task guard.
        self._active_tasks: dict[str, Future[None]] = {}
        self._task_lock = threading.Lock()

        # Active QA serve process (in-memory only; not persisted).
        self._active_serve: _ActiveServe | None = None
        self._serve_lock = threading.Lock()

        # Serialises upsert+save pairs.
        self._state_lock = threading.Lock()

        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._tick_lock = threading.Lock()
        self._bot_user_id: str | None = None

    # ==================================================================
    # Public API
    # ==================================================================

    def wake(self) -> None:
        """Signal the poll loop to run a tick immediately.

        Safe to call from any thread (e.g. a webhook handler).
        """
        self._wake.set()

    def set_webhook_server(self, server: WebhookServer) -> None:
        """Attach a WebhookServer to be started/stopped with the daemon.

        Must be called before ``run()``.  The CLI uses this to break the
        construction-order cycle: WebhookServer needs ``orchestrator.wake``
        as its callback, but the orchestrator must exist first.
        """
        self._webhook_server = server

    def run(self) -> None:
        self._install_signal_handlers()
        logger.info(
            "symphony-lite daemon starting (poll interval=%ds)",
            self._config.poll_interval_seconds,
        )
        self._recover_state()
        if self._webhook_server is not None:
            self._webhook_server.start()
            logger.info(
                "Webhook server listening on port %d at %s",
                self._webhook_server.port,
                "/webhooks/linear/",
            )
        else:
            logger.info("Webhook disabled; relying on polling only")
        try:
            while not self._shutdown.is_set():
                self._wake.clear()
                # Check shutdown flag again after clearing the wake event.
                # A signal landing between the loop-condition check and the
                # clear/wait could otherwise still run one more tick before
                # exiting.
                if self._shutdown.is_set():
                    break
                try:
                    self._tick()
                except Exception:
                    logger.exception("Unhandled error during poll tick")
                self._wake.wait(timeout=self._config.poll_interval_seconds)
        finally:
            self._shutdown_handler()

    # ==================================================================
    # Startup recovery
    # ==================================================================

    def _recover_state(self) -> None:
        for ticket_state in list(self._state.tickets):
            if ticket_state.status == TicketStatus.bootstrapping:
                logger.info(
                    "Recovery: dropping bootstrapping %s", ticket_state.ticket_id
                )
                if ticket_state.metadata_comment_id:
                    # A metadata comment was already posted; edit it rather
                    # than leave it looking like a normal run.
                    try:
                        self._tracker.edit_comment(
                            ticket_state.metadata_comment_id,
                            _RESTART_NOTICE_BODY,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to edit metadata comment %s during recovery of %s",
                            ticket_state.metadata_comment_id,
                            ticket_state.ticket_id,
                        )
                self._state.remove(ticket_state.ticket_id)
            elif ticket_state.status == TicketStatus.working:
                logger.info(
                    "Recovery: found orphaned working %s", ticket_state.ticket_id
                )
                self._recover_working_ticket(ticket_state)
        self._state.save()
        logger.info("Startup recovery complete")

    def _recover_working_ticket(self, ticket_state: TicketState) -> None:
        tid = ticket_state.ticket_id
        if self._is_cancelled(tid):
            return
        if ticket_state.metadata_comment_id:
            recovery_msg = (
                "**Symphony**: Daemon restarted while I was working on this. "
                "Reply to continue the conversation, or "
                f"{self._tracker.human_trigger_description()} to stop."
            )
            try:
                self._tracker.post_comment(tid, recovery_msg)
            except TrackerError:
                logger.exception("Failed to post recovery comment for %s", tid)
                return
        if self._is_cancelled(tid):
            return
        try:
            self._tracker.transition_to(tid, TransitionTarget.needs_input)
        except TrackerError:
            logger.exception("Failed to transition %s during recovery", tid)
            return
        if self._is_cancelled(tid):
            return
        with self._state_lock:
            ticket_state.status = TicketStatus.needs_input
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()
        logger.info("Recovery: %s transitioned to needs_input", tid)

    # ==================================================================
    # Tick
    # ==================================================================

    def _tick(self) -> None:
        with self._tick_lock:
            logger.debug("Poll tick starting")
            issues = self._fetch_triggered_issues()

            # --- Step 2: new tickets + setup-error / failed-no-session retries ---
            for issue in issues:
                # Skip any pipeline scheduling while the ticket is in QA — the serve
                # handles it; the agent should not run concurrently.
                if self._tracker.is_in_qa(issue):
                    logger.debug(
                        "Skipping step-2 scheduling for %s: ticket is in QA state",
                        issue.id,
                    )
                    continue

                existing = self._state.get(issue.id)
                if existing is not None:
                    # Retry setup-error tickets if user commented.
                    if existing.setup_error is not None:
                        if self._has_new_human_comment(
                            issue.id, existing.last_seen_comment_id
                        ):
                            logger.info(
                                "User commented on setup-error %s – retrying", issue.id
                            )
                            with self._state_lock:
                                existing.setup_error = None
                                existing.updated_at = _iso_now()
                                self._state.upsert(existing)
                                self._state.save()
                            # Route to resume if the existing state already has a
                            # session (e.g. ProjectConfigError fired during a resume
                            # turn).  Otherwise start a fresh initial pipeline.
                            if existing.session_id:
                                self._schedule_task(
                                    issue.id, self._resume_pipeline, existing
                                )
                            else:
                                self._schedule_task(
                                    issue.id, self._new_ticket_pipeline, issue
                                )
                    # Retry failed-no-session tickets if user commented (B1).
                    elif (
                        existing.status == TicketStatus.failed
                        and existing.session_id is None
                        and existing.setup_error is None
                    ):
                        if self._has_new_human_comment(
                            issue.id, existing.last_seen_comment_id
                        ):
                            logger.info(
                                "User commented on failed-no-session %s – retrying initial",
                                issue.id,
                            )
                            self._schedule_task(
                                issue.id, self._new_ticket_pipeline, issue
                            )
                    continue  # known ticket

                # Genuinely new ticket.
                self._schedule_task(issue.id, self._new_ticket_pipeline, issue)

            # --- Step 3: cleanup tickets that are no longer triggered ---
            # Build a lookup from the trigger list.  Tickets that appear there
            # already have the label, are in an active state, and are not archived
            # (Linear excludes archived by default), so they are still triggered
            # and we can skip the per-ticket get_issue call for them.
            issues_by_id = {i.id: i for i in issues}

            for ticket_state in list(self._state.tickets):
                tid = ticket_state.ticket_id

                if tid in issues_by_id:
                    continue

                try:
                    current = self._tracker.get_issue(tid)
                except TrackerNotFoundError:
                    logger.info("Ticket %s not found — cleaning up", tid)
                    self._cancel_ticket(tid)
                    identifier = ticket_state.ticket_identifier
                    self._state.remove(tid)
                    try:
                        remove(identifier, str(self._workspace))
                    except Exception:
                        logger.exception("Failed to remove workspace for %s", tid)
                    self._state.save()
                    continue
                except TrackerError:
                    logger.exception(
                        "Tracker error fetching %s — skipping cleanup", tid
                    )
                    continue

                if self._is_still_triggered(current):
                    continue

                logger.info(
                    "Ticket %s no longer triggered (state=%s labels=%s archived=%s) — cleaning up",
                    tid,
                    current.state,
                    current.labels,
                    current.archived_at is not None,
                )
                self._cancel_ticket(tid)
                identifier = ticket_state.ticket_identifier
                self._state.remove(tid)
                try:
                    remove(identifier, str(self._workspace))
                except Exception:
                    logger.exception("Failed to remove workspace for %s", tid)
                self._state.save()

            # --- Step 3b: QA serve reconciliation ---
            self._reconcile_serve(issues, issues_by_id)

            # --- Step 4: per-status tasks ---
            for ticket_state in self._state.tickets:
                tid = ticket_state.ticket_id
                st = ticket_state.status

                # _resume_pipeline handles its own early-return when there are no new
                # human comments, so QA tickets naturally fall through here — only
                # tickets with actual new human comments will get an agent turn.
                # _reconcile_serve on the next tick kills the serve when the ticket
                # leaves QA.  Recovery, however, is unconditional (no comment gating),
                # so we skip it for QA tickets to avoid clobbering the QA state.

                if st == TicketStatus.failed and ticket_state.setup_error is not None:
                    continue
                if st == TicketStatus.working:
                    fetched = issues_by_id.get(tid)
                    if fetched is not None and self._tracker.is_in_qa(fetched):
                        logger.debug("Skipping recovery for working QA ticket %s", tid)
                        continue
                    self._schedule_task(tid, self._recover_working_ticket, ticket_state)
                elif st == TicketStatus.needs_input:
                    self._schedule_task(tid, self._resume_pipeline, ticket_state)
                elif st == TicketStatus.failed:
                    if ticket_state.session_id:
                        self._schedule_task(tid, self._resume_pipeline, ticket_state)
                    # no-session + no setup_error: handled in step 2 (gated on new comment)

    # ==================================================================
    # QA serve reconciliation
    # ==================================================================

    def _reconcile_serve(
        self, issues: list[Issue], issues_by_id: dict[str, Issue]
    ) -> None:
        """Reconcile the active QA serve process against the current set of issues.

        Called once per tick after pipeline scheduling.  No-op when QA is
        not configured.
        """
        if not self._tracker.qa_enabled:
            return

        # Fix 5: if the active serve process has already exited (without the
        # watchdog noticing — e.g. it exited after the 10s window), post a
        # 'serve died' comment, transition the ticket back to needs_input, and
        # prune it from qa_tickets so we don't re-serve it this tick.
        qa_tickets = [i for i in issues if self._tracker.is_in_qa(i)]
        qa_ids = {i.id for i in qa_tickets}

        with self._serve_lock:
            av = self._active_serve
        if av is not None and av.proc.poll() is not None:
            rc = av.proc.returncode
            logger.info(
                "QA serve for %s exited (rc=%s) post-watchdog — notifying",
                av.ticket_identifier,
                rc,
            )
            if not av.failure_comment_posted:
                stdout_text = bytes(av.stdout_head).decode(errors="replace")
                stderr_text = bytes(av.stderr_head).decode(errors="replace")
                body = _format_serve_died_comment(rc, stdout_text, stderr_text)
                try:
                    self._tracker.post_comment(av.ticket_id, body)
                except Exception:
                    logger.exception(
                        "Failed to post serve-died comment for %s", av.ticket_id
                    )
            try:
                self._tracker.transition_to(av.ticket_id, TransitionTarget.needs_input)
            except Exception:
                logger.exception(
                    "Failed to transition %s after serve died", av.ticket_id
                )
            # Prune from qa_tickets so we don't re-serve this tick.
            qa_tickets = [t for t in qa_tickets if t.id != av.ticket_id]
            qa_ids = {t.id for t in qa_tickets}
            with self._serve_lock:
                if self._active_serve is av:
                    self._active_serve = None

        with self._serve_lock:
            active_id = self._active_serve.ticket_id if self._active_serve else None

        # 1. Kill the active serve if its owner left QA.
        if active_id is not None and active_id not in qa_ids:
            logger.info("QA serve owner %s left QA — killing serve", active_id)
            self._kill_active_serve()

        if not qa_tickets:
            return

        # 2. Determine the winner: the ticket with the most recent updated_at.
        winner = max(qa_tickets, key=lambda i: i.updated_at)
        winner_id = winner.id

        # Re-read active_id (may have been cleared by kill above).
        with self._serve_lock:
            active_id = self._active_serve.ticket_id if self._active_serve else None

        # If the winner changed, kill the current serve so we can start a new one.
        if active_id is not None and active_id != winner_id:
            logger.info(
                "QA winner changed from %s to %s — killing old serve",
                active_id,
                winner_id,
            )
            self._kill_active_serve()
            active_id = None

        # 3. Bump losers: Fix 3 — transition first, comment only on success.
        for loser in qa_tickets:
            if loser.id == winner_id:
                continue
            logger.info(
                "Bumping %s out of QA — %s is the winner",
                loser.identifier,
                winner.identifier,
            )
            try:
                self._tracker.transition_to(loser.id, TransitionTarget.needs_input)
            except TrackerError:
                logger.exception(
                    "Failed to transition bumped ticket %s to needs_input", loser.id
                )
                continue  # skip comment — loser still in QA, will retry next tick
            try:
                self._tracker.post_comment(
                    loser.id,
                    f"**Symphony**: Bumped out of QA — {winner.identifier} took over.",
                )
            except TrackerError:
                logger.exception("Failed to post bump comment for %s", loser.id)

        # 4. Start serve for winner if not already running.
        if active_id == winner_id:
            return  # already serving the winner

        ts = self._state.get(winner_id)
        if ts is None:
            self._bail_qa_no_workspace(winner_id, "has no state entry")
            return

        workspace_path = ts.workspace_path
        if not workspace_path:
            self._bail_qa_no_workspace(winner_id, "has empty workspace_path")
            return

        # Cancel any in-flight agent task for the winner before starting the serve.
        with self._task_lock:
            has_inflight = (
                winner_id in self._active_tasks
                and not self._active_tasks[winner_id].done()
            )
        if has_inflight:
            logger.info(
                "Cancelling in-flight task for QA winner %s before starting serve",
                winner.identifier,
            )
            self._cancel_ticket(winner_id)

        logger.info(
            "Starting QA serve for %s (workspace=%s)", winner.identifier, workspace_path
        )
        try:
            proc = start_serve(
                workspace_path=workspace_path,
                hide_paths=self._config.sandbox.hide_paths,
                extra_rw_paths=self._config.sandbox.extra_rw_paths,
            )
        except (ServeScriptMissing, WorkspaceError, FileNotFoundError) as exc:
            logger.error("Failed to start QA serve for %s: %s", winner.identifier, exc)
            self._post_comment_safe(
                winner_id,
                f"**Symphony**: QA serve failed to start:\n```\n{exc}\n```",
            )
            return

        av = _ActiveServe(
            ticket_id=winner_id,
            ticket_identifier=winner.identifier,
            proc=proc,
            start_monotonic=time.monotonic(),
        )
        with self._serve_lock:
            self._active_serve = av

        # Fix 4: start drainer threads immediately so pipes never block.
        self._start_drainers(av)

        # Spawn watchdog thread: monitors the first 10s of the serve process.
        t = threading.Thread(
            target=self._serve_watchdog,
            args=(av,),
            daemon=True,
            name=f"serve-watchdog-{winner.identifier}",
        )
        t.start()

    def _bail_qa_no_workspace(self, ticket_id: str, log_reason: str) -> None:
        """Transition QA ticket to needs_input and post a comment when workspace is missing.

        Transition first (the atomic de-dup).  Only post the comment if the
        transition succeeds, to avoid comment spam when the transition is flaky.
        """
        logger.warning("QA winner %s %s — cannot start serve", ticket_id, log_reason)
        try:
            self._tracker.transition_to(ticket_id, TransitionTarget.needs_input)
        except TrackerError:
            logger.exception(
                "Failed to transition QA winner %s to needs_input (%s)",
                ticket_id,
                log_reason,
            )
            return
        self._post_comment_safe(
            ticket_id,
            f"**Symphony**: Can't start QA — no workspace exists for this ticket. "
            f"This usually happens after the ticket was moved out of an active state "
            f"(e.g. to Done), which cleans up the workspace. "
            f"Transitioning back to "
            f"`{self._tracker.transition_name_for(TransitionTarget.needs_input)}`; "
            f"re-trigger the agent to reclone, then move to QA again.",
        )

    def _serve_watchdog(self, av: _ActiveServe) -> None:
        """Watch the serve process for the first 10 seconds.

        - If it exits with rc != 0 within 10s and was NOT intentionally killed:
          post a failure comment and clear _active_serve.
        - If it exits with rc == 0 within 10s: clear _active_serve silently.
        - If still alive after 10s: exit the watchdog (drainers are already running).
        """
        try:
            av.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Serve is still alive after 10s — healthy.  Drainers are already
            # running (started immediately after start_serve returned).
            return

        # Process exited within 10s.
        rc = av.proc.returncode
        if rc == 0:
            logger.info(
                "QA serve for %s exited cleanly (rc=0) within 10s", av.ticket_identifier
            )
        elif av.intentional_kill.is_set():
            # Fix 1: we killed it ourselves — suppress the failure comment.
            logger.info(
                "QA serve for %s killed intentionally (rc=%s) — suppressing comment",
                av.ticket_identifier,
                rc,
            )
        else:
            # Brief pause to let drainers capture post-mortem output.
            time.sleep(0.2)
            stderr_text = bytes(av.stderr_head).decode(errors="replace")
            stdout_text = bytes(av.stdout_head).decode(errors="replace")
            body = _format_serve_died_comment(rc, stdout_text, stderr_text)
            logger.error(
                "QA serve for %s exited with rc=%s within 10s", av.ticket_identifier, rc
            )
            self._post_comment_safe(av.ticket_id, body)
            av.failure_comment_posted = True
            # Transition the ticket out of QA so the next tick doesn't respawn the serve.
            try:
                self._tracker.transition_to(av.ticket_id, TransitionTarget.needs_input)
            except TrackerError:
                logger.exception(
                    "Failed to transition %s out of QA after serve failure",
                    av.ticket_id,
                )

        # Clear _active_serve (only if it still points to this _ActiveServe).
        with self._serve_lock:
            if self._active_serve is av:
                self._active_serve = None

    def _start_drainers(self, av: _ActiveServe) -> None:
        """Start two daemon threads that drain stdout and stderr of *av.proc*.

        Each thread reads in chunks, appending to the corresponding head buffer
        until _DRAINER_CAP bytes have been captured, then continues reading and
        discarding to prevent pipe-buffer deadlock.
        """
        for pipe_attr, buf_attr, name in (
            ("stdout", "stdout_head", "stdout"),
            ("stderr", "stderr_head", "stderr"),
        ):
            pipe = getattr(av.proc, pipe_attr)
            if pipe is None:
                continue
            buf: bytearray = getattr(av, buf_attr)
            t = threading.Thread(
                target=self._pipe_drainer,
                args=(pipe, buf),
                daemon=True,
                name=f"serve-drainer-{av.ticket_identifier}-{name}",
            )
            t.start()

    def _pipe_drainer(self, pipe: Any, buf: bytearray) -> None:
        """Read *pipe* in chunks until EOF, appending to *buf* up to _DRAINER_CAP bytes.

        Buffer reads from the watchdog and the dead-proc path in _reconcile_serve
        are intentionally lock-free.  ``bytes(bytearray)`` is atomic under the GIL,
        so readers always see a consistent snapshot; at worst they see a slightly
        truncated capture if a read races with an append.  This is an accepted
        tradeoff — the comment may show partial output, which is fine for diagnostics.
        """
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                if len(buf) < _DRAINER_CAP:
                    buf.extend(chunk[: _DRAINER_CAP - len(buf)])
        except Exception:
            pass

    def _kill_active_serve(self) -> None:
        """Kill the active serve process and clear _active_serve (under _serve_lock).

        Sets intentional_kill before killing so the watchdog suppresses the
        failure comment.
        """
        with self._serve_lock:
            if self._active_serve is None:
                return
            av = self._active_serve
            self._active_serve = None

        if av.proc.returncode is None:
            logger.info("Killing active QA serve process")
            av.intentional_kill.set()
            try:
                av.proc.kill()
                av.proc.wait(timeout=5)
            except Exception:
                pass

    # ==================================================================
    # Task scheduling
    # ==================================================================

    def _schedule_task(self, ticket_id: str, target: Any, *args: Any) -> None:
        with self._task_lock:
            existing = self._active_tasks.get(ticket_id)
            if existing is not None and not existing.done():
                return
            future = self._executor.submit(self._task_wrapper, ticket_id, target, *args)
            self._active_tasks[ticket_id] = future

    def _task_wrapper(self, ticket_id: str, target: Any, *args: Any) -> None:
        try:
            target(*args)
        except Exception:
            logger.exception("Task for %s failed unexpectedly", ticket_id)
        finally:
            with self._task_lock:
                self._active_tasks.pop(ticket_id, None)
            with self._subprocess_lock:
                self._subprocesses.pop(ticket_id, None)
                self._cancelled.discard(ticket_id)

    # ==================================================================
    # Cancellation (B1 + S1)
    # ==================================================================

    def _cancel_ticket(self, ticket_id: str) -> None:
        with self._subprocess_lock:
            self._cancelled.add(ticket_id)
            proc = self._subprocesses.pop(ticket_id, None)
        if proc is not None and proc.returncode is None:
            logger.info("Cancelling subprocess for %s", ticket_id)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

        # Also kill the QA serve if this ticket owns it.
        with self._serve_lock:
            if (
                self._active_serve is not None
                and self._active_serve.ticket_id == ticket_id
            ):
                serve_av = self._active_serve
                self._active_serve = None
            else:
                serve_av = None
        if serve_av is not None and serve_av.proc.returncode is None:
            logger.info("Cancelling QA serve for %s", ticket_id)
            serve_av.intentional_kill.set()
            try:
                serve_av.proc.kill()
                serve_av.proc.wait(timeout=5)
            except Exception:
                pass

    def _is_cancelled(self, ticket_id: str) -> bool:
        with self._subprocess_lock:
            return ticket_id in self._cancelled

    def _register_subprocess(
        self, ticket_id: str, proc: subprocess.Popen[bytes]
    ) -> bool:
        """Register a Popen for cancellation.  Returns False if already cancelled (S1)."""
        with self._subprocess_lock:
            if ticket_id in self._cancelled:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                return False
            self._subprocesses[ticket_id] = proc
            return True

    # ==================================================================
    # Bot user id (S2)
    # ==================================================================

    def _get_bot_user_id(self) -> str | None:
        """Return bot user id or None on transient failure (caller must handle)."""
        if self._bot_user_id is not None:
            return self._bot_user_id
        try:
            uid = self._tracker.current_user_id()
            self._bot_user_id = uid
            return uid
        except Exception:
            logger.exception("Failed to get bot user id — transient")
            return None  # caller must skip

    # ==================================================================
    # New-ticket pipeline
    # ==================================================================

    def _new_ticket_pipeline(self, issue: Issue) -> None:
        tid = issue.id
        logger.info("New ticket pipeline starting for %s (%s)", tid, issue.identifier)
        if self._is_cancelled(tid):
            return

        # --- Resolve repository URL ---
        try:
            repo_url = self._tracker.repo_url_for(issue)
        except TrackerError as exc:
            logger.warning("Ticket %s has no resolvable repo: %s", tid, exc)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: {exc}",
                return_comment=True,
            )
            self._save_setup_error(tid, issue, "no_repo", err_comment)
            return

        if self._is_cancelled(tid):
            return

        # --- Save bootstrapping state EARLY (B2) ---
        # Branch is computed after loading project config; use "" as placeholder.
        ticket_state = TicketState(
            ticket_id=tid,
            ticket_identifier=issue.identifier,
            project_id=issue.project.id if issue.project else None,
            repo_url=repo_url,
            workspace_path="",  # not yet known
            branch="",  # will be updated after loading project config
            status=TicketStatus.bootstrapping,
        )
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        if self._is_cancelled(tid):
            return

        # --- Clone workspace ---
        try:
            workspace_path = clone_workspace(
                ticket_identifier=issue.identifier,
                repo_url=repo_url,
                workspace_root=str(self._workspace),
            )
        except (WorkspaceError, FileNotFoundError) as exc:
            logger.error("Workspace clone failed for %s: %s", tid, exc)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: Workspace clone failed:\n```\n{exc}\n```",
                return_comment=True,
            )
            self._save_setup_error(tid, issue, str(exc), err_comment)
            return

        # B2: check cancellation after clone returns.
        if self._is_cancelled(tid):
            return

        ticket_state.workspace_path = workspace_path
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # --- Load per-project config ---
        try:
            project_config = load_project_config(workspace_path)
        except ProjectConfigError as exc:
            logger.error("Project config invalid for %s: %s", tid, exc)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: Invalid project config:\n```\n{exc}\n```",
                return_comment=True,
            )
            self._save_setup_error(tid, issue, "project_config_invalid", err_comment)
            return

        # Compute effective auto_branch from project config, falling back to global.
        effective_auto_branch = (
            project_config.auto_branch
            if project_config.auto_branch is not None
            else self._config.auto_branch
        )

        # Compute branch name from effective auto_branch and update state.
        if effective_auto_branch:
            branch = issue.branch_name or f"symphony/{issue.identifier.lower()}"
        else:
            branch = ""
        ticket_state.branch = branch
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # --- Finalize workspace (B2: pass on_subprocess for setup script) ---
        try:
            finalize_workspace(
                workspace_path=workspace_path,
                ticket_identifier=issue.identifier,
                branch_name=issue.branch_name,
                sandbox_hide_paths=self._config.sandbox.hide_paths,
                on_subprocess=lambda proc: (self._register_subprocess(tid, proc), None)[
                    1
                ],
                sandbox_extra_rw_paths=self._config.sandbox.extra_rw_paths,
                auto_branch=effective_auto_branch,
            )
        except (WorkspaceError, FileNotFoundError) as exc:
            logger.error("Workspace finalization failed for %s: %s", tid, exc)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: Workspace preparation failed:\n```\n{exc}\n```",
                return_comment=True,
            )
            self._save_setup_error(tid, issue, str(exc), err_comment)
            return

        # B2: check cancellation after finalize returns.
        if self._is_cancelled(tid):
            return

        # --- Transition Linear to In Progress ---
        try:
            self._tracker.transition_to(tid, TransitionTarget.in_progress)
        except Exception:
            logger.exception(
                "Failed to transition %s to '%s'",
                tid,
                TransitionTarget.in_progress.value,
            )

        if self._is_cancelled(tid):
            return

        # --- Post metadata comment ---
        meta_comment: Comment | None = None
        meta_body = _build_metadata_comment(workspace_path)
        try:
            meta_comment = self._tracker.post_comment(tid, meta_body)
            ticket_state.metadata_comment_id = meta_comment.id
            with self._state_lock:
                self._state.upsert(ticket_state)
                self._state.save()
        except Exception:
            logger.exception("Failed to post metadata comment for %s", tid)

        if self._is_cancelled(tid):
            return

        # --- Fetch description + build prompt ---
        try:
            full_issue = self._tracker.get_issue(tid)
            description = full_issue.description
        except TrackerError:
            logger.exception("Failed to fetch issue %s for description", tid)
            description = None
        prompt = _build_initial_prompt(issue.title, description)

        if self._is_cancelled(tid):
            return

        # --- Run OpenCode (B3: pass hide_paths) ---
        ticket_state.status = TicketStatus.working
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # Compute effective turn timeout from project config, falling back to global.
        effective_turn_timeout = (
            project_config.turn_timeout_seconds or self._config.turn_timeout_seconds
        )

        try:
            session_id, final_message = run_initial(
                workspace_path=workspace_path,
                prompt=prompt,
                timeout_seconds=effective_turn_timeout,
                on_subprocess=lambda proc: (self._register_subprocess(tid, proc), None)[
                    1
                ],
                hide_paths=self._config.sandbox.hide_paths,
                extra_rw_paths=self._config.sandbox.extra_rw_paths,
            )
        except OpenCodeTimeout:
            logger.error("OpenCode turn timed out for %s", tid)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: The AI turn timed out after "
                f"{effective_turn_timeout}s.",
                return_comment=True,
            )
            with self._state_lock:
                ticket_state.status = TicketStatus.failed
                ticket_state.updated_at = _iso_now()
                if err_comment is not None:
                    ticket_state.last_seen_comment_id = err_comment.id  # B1
                ticket_state.session_id = None  # ensure no-session path on retry
                self._state.upsert(ticket_state)
                self._state.save()
            return
        except OpenCodeCancelled:
            logger.info("OpenCode turn cancelled for %s", tid)
            return
        except OpenCodeError as exc:
            logger.error("OpenCode failed for %s: %s", tid, exc)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: The AI turn failed:\n```\n{exc}\n```",
                return_comment=True,
            )
            with self._state_lock:
                ticket_state.status = TicketStatus.failed
                ticket_state.updated_at = _iso_now()
                if err_comment is not None:
                    ticket_state.last_seen_comment_id = err_comment.id  # B1
                ticket_state.session_id = None  # ensure no-session path on retry
                self._state.upsert(ticket_state)
                self._state.save()
            return

        if self._is_cancelled(tid):
            return

        # Fix 2: guard before edit_comment — a cancelled agent must not write to Linear.
        if self._is_cancelled(tid):
            logger.info("Ticket %s cancelled before metadata edit — skipping", tid)
            return

        # --- Edit metadata comment ---
        if meta_comment is not None:
            try:
                final_meta = _build_metadata_comment_final(workspace_path, session_id)
                self._tracker.edit_comment(meta_comment.id, final_meta)
            except Exception:
                logger.exception("Failed to edit metadata comment for %s", tid)

        ticket_state.session_id = session_id
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # Fix 2: guard before _post_final_message.
        if self._is_cancelled(tid):
            logger.info("Ticket %s cancelled before final message — skipping", tid)
            return

        # --- Post final message ---
        last_comment = self._post_final_message(tid, final_message)
        if last_comment is None:
            return  # state saved as failed inside _post_final_message
        ticket_state.last_seen_comment_id = last_comment.id

        # Guard: if cancelled between final message and transition (e.g. ticket moved
        # to QA state by a human), do not clobber the QA state with needs_input.
        if self._is_cancelled(tid):
            logger.info("Ticket %s cancelled before final transition — skipping", tid)
            return

        # --- Transition to Needs Input ---
        transition_ok = True
        try:
            self._tracker.transition_to(tid, TransitionTarget.needs_input)
        except Exception:
            logger.exception(
                "Failed to transition %s to '%s'",
                tid,
                TransitionTarget.needs_input.value,
            )
            transition_ok = False

        with self._state_lock:
            ticket_state.status = (
                TicketStatus.needs_input if transition_ok else TicketStatus.failed
            )
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()

        logger.info("New ticket pipeline complete for %s", tid)

    # ==================================================================
    # Resume pipeline
    # ==================================================================

    def _resume_pipeline(self, ticket_state: TicketState) -> None:
        tid = ticket_state.ticket_id
        logger.debug("Resume pipeline tick for %s", tid)

        bot_user_id = self._get_bot_user_id()
        if bot_user_id is None:  # S2: transient failure
            logger.warning("Cannot get bot user id for %s — skipping tick", tid)
            return

        try:
            new_comments = self._tracker.list_comments_since(
                tid,
                ticket_state.last_seen_comment_id,
            )
        except Exception:
            logger.exception("Failed to fetch comments for %s", tid)
            return

        human_comments = [c for c in new_comments if c.user_id != bot_user_id]
        if not human_comments:
            logger.debug("No new human comments on %s", tid)
            return

        if self._is_cancelled(tid):
            return

        logger.info(
            "Resume pipeline starting for %s (%d new human comment(s))",
            tid,
            len(human_comments),
        )
        message = _format_comments_message(human_comments)

        # Load per-project config BEFORE transitioning Linear (re-read on every
        # resume to pick up in-repo changes).  A malformed config aborts early so
        # the ticket doesn't flap between states.
        #
        # load_project_config now reads directly from origin/HEAD via git show,
        # so repo-side config fixes are picked up regardless of which branch the
        # workspace is on — no working-tree refresh needed.
        try:
            project_config = load_project_config(ticket_state.workspace_path)
        except ProjectConfigError as exc:
            logger.error("Project config invalid for %s on resume: %s", tid, exc)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: Invalid project config:\n```\n{exc}\n```",
                return_comment=True,
            )
            with self._state_lock:
                ticket_state.status = TicketStatus.failed
                ticket_state.setup_error = "project_config_invalid"
                ticket_state.updated_at = _iso_now()
                if err_comment is not None:
                    ticket_state.last_seen_comment_id = err_comment.id
                self._state.upsert(ticket_state)
                self._state.save()
            return

        if self._is_cancelled(tid):
            return

        try:
            self._tracker.transition_to(tid, TransitionTarget.in_progress)
        except Exception:
            logger.exception(
                "Failed to transition %s to '%s'",
                tid,
                TransitionTarget.in_progress.value,
            )

        with self._state_lock:
            ticket_state.status = TicketStatus.working
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()

        if self._is_cancelled(tid):
            return

        # Compute effective turn timeout from project config, falling back to global.
        effective_turn_timeout = (
            project_config.turn_timeout_seconds or self._config.turn_timeout_seconds
        )

        try:
            final_message = run_resume(
                workspace_path=ticket_state.workspace_path,
                session_id=ticket_state.session_id or "",
                message=message,
                timeout_seconds=effective_turn_timeout,
                on_subprocess=lambda proc: (self._register_subprocess(tid, proc), None)[
                    1
                ],
                hide_paths=self._config.sandbox.hide_paths,  # B3
                extra_rw_paths=self._config.sandbox.extra_rw_paths,
            )
        except OpenCodeTimeout:
            logger.error("OpenCode resume timed out for %s", tid)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: The AI turn timed out after "
                f"{effective_turn_timeout}s.",
                return_comment=True,
            )
            with self._state_lock:
                ticket_state.status = TicketStatus.failed
                ticket_state.updated_at = _iso_now()
                if err_comment is not None:
                    ticket_state.last_seen_comment_id = err_comment.id
                self._state.upsert(ticket_state)
                self._state.save()
            return
        except OpenCodeCancelled:
            logger.info("OpenCode resume cancelled for %s", tid)
            return
        except OpenCodeError as exc:
            logger.error("OpenCode resume failed for %s: %s", tid, exc)
            err_comment = self._post_comment_safe(
                tid,
                f"**Symphony error**: The AI turn failed:\n```\n{exc}\n```",
                return_comment=True,
            )
            with self._state_lock:
                ticket_state.status = TicketStatus.failed
                ticket_state.updated_at = _iso_now()
                if err_comment is not None:
                    ticket_state.last_seen_comment_id = err_comment.id
                self._state.upsert(ticket_state)
                self._state.save()
            return

        if self._is_cancelled(tid):
            return

        # Fix 2: guard before _post_final_message — cancelled agent must not write to Linear.
        if self._is_cancelled(tid):
            logger.info("Ticket %s cancelled before final message — skipping", tid)
            return

        last_comment = self._post_final_message(tid, final_message)
        if last_comment is None:
            return
        ticket_state.last_seen_comment_id = last_comment.id

        # Guard: if cancelled between final message and transition (e.g. ticket moved
        # to QA state by a human), do not clobber the QA state with needs_input.
        if self._is_cancelled(tid):
            logger.info("Ticket %s cancelled before final transition — skipping", tid)
            return

        transition_ok = True
        try:
            self._tracker.transition_to(tid, TransitionTarget.needs_input)
        except Exception:
            logger.exception(
                "Failed to transition %s to '%s'",
                tid,
                TransitionTarget.needs_input.value,
            )
            transition_ok = False

        with self._state_lock:
            ticket_state.status = (
                TicketStatus.needs_input if transition_ok else TicketStatus.failed
            )
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()

        logger.info("Resume pipeline complete for %s", tid)

    # ==================================================================
    # Shared helpers
    # ==================================================================

    def _post_comment_safe(
        self, tid: str, body: str, *, return_comment: bool = False
    ) -> Comment | None:
        try:
            comment = self._tracker.post_comment(tid, body)
            return comment if return_comment else None
        except Exception:
            logger.exception("Failed to post comment for %s", tid)
            return None

    def _post_final_message(self, tid: str, final_message: str) -> Comment | None:
        if self._is_cancelled(tid):
            return None
        try:
            return self._tracker.post_comment(
                tid, final_message if final_message else "_(No output from the AI.)_"
            )
        except Exception:
            logger.exception("Failed to post final message for %s", tid)
            with self._state_lock:
                ts = self._state.get(tid)
                if ts is not None:
                    ts.status = TicketStatus.failed
                    ts.updated_at = _iso_now()
                    self._state.upsert(ts)
                    self._state.save()
            return None

    def _save_setup_error(
        self,
        tid: str,
        issue: Issue,
        error_code: str,
        error_comment: Comment | None = None,
    ) -> None:
        """Save failed state with setup_error, using error comment id as baseline (S3).

        When a state entry already exists (e.g. clone succeeded but finalize
        failed), preserve its workspace_path, branch, project_id, repo_url, and
        session_id.  Only overwrite the setup-error-related fields.
        """
        existing = self._state.get(tid)
        if existing is not None:
            ts = existing.model_copy(
                update={
                    "status": TicketStatus.failed,
                    "setup_error": error_code,
                    "last_seen_comment_id": (
                        error_comment.id
                        if error_comment
                        else existing.last_seen_comment_id
                    ),
                    "updated_at": _iso_now(),
                }
            )
        else:
            if self._config.auto_branch:
                branch = issue.branch_name or f"symphony/{issue.identifier.lower()}"
            else:
                branch = ""
            ts = TicketState(
                ticket_id=tid,
                ticket_identifier=issue.identifier,
                project_id=issue.project.id if issue.project else None,
                repo_url="",
                workspace_path="",
                branch=branch,
                status=TicketStatus.failed,
                setup_error=error_code,
                last_seen_comment_id=error_comment.id if error_comment else None,
            )
        with self._state_lock:
            self._state.upsert(ts)
            self._state.save()

    def _has_new_human_comment(self, issue_id: str, last_seen: str | None) -> bool:
        bot_id = self._get_bot_user_id()
        if bot_id is None:  # S2: transient failure → skip
            return False
        try:
            comments = self._tracker.list_comments_since(issue_id, last_seen)
        except Exception:
            logger.exception("Failed to list comments for %s", issue_id)
            return False
        return any(c.user_id != bot_id for c in comments)

    # ==================================================================
    # Signal handling and shutdown
    # ==================================================================

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s – initiating shutdown", sig_name)
        self._shutdown.set()
        self._wake.set()

    def _shutdown_handler(self) -> None:
        self._wake.set()
        if self._webhook_server is not None:
            logger.info("Stopping webhook server")
            self._webhook_server.stop()
        logger.info("Shutting down – killing all subprocesses")
        with self._subprocess_lock:
            procs = list(self._subprocesses.items())
            self._subprocesses.clear()
            self._cancelled.update(tid for tid, _ in procs)

        for tid, proc in procs:
            if proc.returncode is None:
                logger.info("Killing subprocess for %s", tid)
                try:
                    proc.kill()
                except Exception:
                    pass

        # Kill the active QA serve process (if any).
        with self._serve_lock:
            active_serve = self._active_serve
            self._active_serve = None
        if active_serve is not None:
            if active_serve.proc.returncode is None:
                logger.info("Killing QA serve process on shutdown")
                active_serve.intentional_kill.set()
                try:
                    active_serve.proc.kill()
                except Exception:
                    pass

        deadline = time.monotonic() + _SHUTDOWN_GRACE_SECONDS
        while time.monotonic() < deadline:
            if all(p.returncode is not None for _, p in procs):
                break
            time.sleep(0.1)

        try:
            self._state.save()
            logger.info("State persisted on shutdown")
        except Exception:
            logger.exception("Failed to persist state on shutdown")

        self._executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self._task_lock:
                if not self._active_tasks:
                    break
            time.sleep(0.1)
        with self._task_lock:
            stuck = list(self._active_tasks.keys())
        if stuck:
            logger.warning("Shutdown: %d task(s) still running: %s", len(stuck), stuck)
        logger.info("symphony-lite shutdown complete")

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _fetch_triggered_issues(self) -> list[Issue]:
        try:
            return self._tracker.list_triggered_issues()
        except Exception:
            logger.exception("Failed to fetch triggered issues")
            return []

    def _is_still_triggered(self, issue: Issue) -> bool:
        return self._tracker.is_still_triggered(issue)
