"""Daemon orchestrator: poll loop, per-ticket lifecycle, concurrency, error handling."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony_lite.config import AppConfig
from symphony_lite.linear import (
    Comment,
    Issue,
    LinearClient,
    LinearError,
    LinearNotFoundError,
)
from symphony_lite.opencode import (
    OpenCodeCancelled,
    OpenCodeError,
    OpenCodeTimeout,
    run_initial,
    run_resume,
)
from symphony_lite.state import StateManager, TicketState, TicketStatus
from symphony_lite.workspace import WorkspaceError, prepare, remove

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_LINEAR_STATES = {"Done", "Cancelled", "Canceled", "Duplicate"}
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


def _find_repo_link(project: Any, linear: LinearClient) -> str | None:
    if project is None or project.id is None:
        return None
    fp = linear.get_project(project.id)
    for link in fp.links:
        if link.label.strip().lower() == "repo":
            return link.url
    return None


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
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    def __init__(self, config: AppConfig, state: StateManager, linear: LinearClient, workspace: Path) -> None:
        self._config = config
        self._state = state
        self._linear = linear
        self._workspace = workspace

        self._executor = ThreadPoolExecutor(max_workers=5)

        # Subprocess tracking + cancellation flags (guarded by _subprocess_lock).
        self._subprocesses: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()
        self._subprocess_lock = threading.Lock()

        # Active task guard.
        self._active_tasks: dict[str, Future[None]] = {}
        self._task_lock = threading.Lock()

        # Serialises upsert+save pairs.
        self._state_lock = threading.Lock()

        self._shutdown = threading.Event()
        self._bot_user_id: str | None = None

    # ==================================================================
    # Public API
    # ==================================================================

    def run(self) -> None:
        self._install_signal_handlers()
        logger.info("symphony-lite daemon starting (poll interval=%ds)",
                     self._config.poll_interval_seconds)
        self._recover_state()
        try:
            while not self._shutdown.is_set():
                try:
                    self._tick()
                except Exception:
                    logger.exception("Unhandled error during poll tick")
                self._shutdown.wait(timeout=self._config.poll_interval_seconds)
        finally:
            self._shutdown_handler()

    # ==================================================================
    # Startup recovery
    # ==================================================================

    def _recover_state(self) -> None:
        for ticket_state in list(self._state.tickets):
            if ticket_state.status == TicketStatus.bootstrapping:
                logger.info("Recovery: dropping bootstrapping %s", ticket_state.ticket_id)
                if ticket_state.metadata_comment_id:
                    # A metadata comment was already posted; edit it rather
                    # than leave it looking like a normal run.
                    try:
                        self._linear.edit_comment(
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
                logger.info("Recovery: found orphaned working %s", ticket_state.ticket_id)
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
                "Reply to continue the conversation, or remove the `"
                f"{self._config.linear.trigger_label}` label to stop."
            )
            try:
                self._linear.post_comment(tid, recovery_msg)
            except LinearError:
                logger.exception("Failed to post recovery comment for %s", tid)
                return
        if self._is_cancelled(tid):
            return
        try:
            self._linear.transition_to_state(tid, self._config.linear.needs_input_state)
        except LinearError:
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
        logger.debug("Poll tick starting")
        issues = self._fetch_triggered_issues()
        known_ids = {t.ticket_id for t in self._state.tickets}

        # --- Step 2: new tickets + setup-error / failed-no-session retries ---
        for issue in issues:
            existing = self._state.get(issue.id)
            if existing is not None:
                # Retry setup-error tickets if user commented.
                if existing.setup_error is not None:
                    if self._has_new_human_comment(issue.id, existing.last_seen_comment_id):
                        logger.info("User commented on setup-error %s – retrying", issue.id)
                        with self._state_lock:
                            existing.setup_error = None
                            existing.updated_at = _iso_now()
                            self._state.upsert(existing)
                            self._state.save()
                        self._schedule_task(issue.id, self._new_ticket_pipeline, issue)
                # Retry failed-no-session tickets if user commented (B1).
                elif (existing.status == TicketStatus.failed
                      and existing.session_id is None
                      and existing.setup_error is None):
                    if self._has_new_human_comment(issue.id, existing.last_seen_comment_id):
                        logger.info("User commented on failed-no-session %s – retrying initial", issue.id)
                        self._schedule_task(issue.id, self._new_ticket_pipeline, issue)
                continue  # known ticket

            # Genuinely new ticket.
            self._schedule_task(issue.id, self._new_ticket_pipeline, issue)

        # --- Step 3: label removal / terminal cleanup ---
        for ticket_state in list(self._state.tickets):
            tid = ticket_state.ticket_id
            matching = [i for i in issues if i.id == tid]

            if not matching:
                try:
                    current = self._linear.get_issue(tid)
                except LinearNotFoundError:
                    logger.warning("Ticket %s not found — removing", tid)
                    self._cancel_ticket(tid)
                    self._state.remove(tid)
                    self._state.save()
                    continue
                except LinearError:
                    logger.exception("Linear error fetching %s — skipping cleanup", tid)
                    continue

                label_present = self._config.linear.trigger_label in current.labels
                if not label_present:
                    logger.info("Label removed from %s – cleaning up", tid)
                    self._cancel_ticket(tid)
                    self._state.remove(tid)
                    self._state.save()
                    continue
                if current.state in _TERMINAL_LINEAR_STATES:
                    logger.info("Ticket %s terminal (%s) – cleaning up", tid, current.state)
                    self._cancel_ticket(tid)
                    identifier = ticket_state.ticket_identifier
                    self._state.remove(tid)
                    try:
                        remove(identifier, str(self._workspace))
                    except Exception:
                        logger.exception("Failed to remove workspace for %s", tid)
                    self._state.save()
                    continue
            else:
                current = matching[0]
                if current.state in _TERMINAL_LINEAR_STATES:
                    logger.info("Ticket %s terminal (%s) – cleaning up", tid, current.state)
                    self._cancel_ticket(tid)
                    identifier = ticket_state.ticket_identifier
                    self._state.remove(tid)
                    try:
                        remove(identifier, str(self._workspace))
                    except Exception:
                        logger.exception("Failed to remove workspace for %s", tid)
                    self._state.save()
                    continue

        # --- Step 4: per-status tasks ---
        for ticket_state in self._state.tickets:
            tid = ticket_state.ticket_id
            st = ticket_state.status

            if st == TicketStatus.failed and ticket_state.setup_error is not None:
                continue
            if st == TicketStatus.working:
                self._schedule_task(tid, self._recover_working_ticket, ticket_state)
            elif st == TicketStatus.needs_input:
                self._schedule_task(tid, self._resume_pipeline, ticket_state)
            elif st == TicketStatus.failed:
                if ticket_state.session_id:
                    self._schedule_task(tid, self._resume_pipeline, ticket_state)
                # no-session + no setup_error: handled in step 2 (gated on new comment)

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

    def _is_cancelled(self, ticket_id: str) -> bool:
        with self._subprocess_lock:
            return ticket_id in self._cancelled

    def _register_subprocess(self, ticket_id: str, proc: subprocess.Popen[bytes]) -> bool:
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
            uid = self._linear.current_user_id()
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

        # --- Check project + Repo link ---
        if issue.project is None or issue.project.id is None:
            logger.warning("Ticket %s has no project", tid)
            err_comment = self._post_comment_safe(tid,
                "**Symphony error**: No project linked to this ticket.",
                return_comment=True)
            self._save_setup_error(tid, issue, "no_project", err_comment)
            return

        repo_url = _find_repo_link(issue.project, self._linear)
        if repo_url is None:
            logger.warning("Ticket %s has no Repo link", tid)
            err_comment = self._post_comment_safe(tid,
                "**Symphony error**: No `Repo` link found on the project. "
                "Add one and re-trigger.",
                return_comment=True)
            self._save_setup_error(tid, issue, "no_repo_link", err_comment)
            return

        if self._is_cancelled(tid):
            return

        # --- Save bootstrapping state EARLY (B2) ---
        branch = issue.branch_name or f"symphony/{issue.identifier.lower()}"
        ticket_state = TicketState(
            ticket_id=tid,
            ticket_identifier=issue.identifier,
            project_id=issue.project.id if issue.project else None,
            repo_url=repo_url,
            workspace_path="",  # not yet known
            branch=branch,
            status=TicketStatus.bootstrapping,
        )
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        if self._is_cancelled(tid):
            return

        # --- Prepare workspace (B2: pass on_subprocess for setup script) ---
        try:
            workspace_path = prepare(
                ticket_identifier=issue.identifier,
                repo_url=repo_url,
                branch_name=issue.branch_name,
                workspace_root=str(self._workspace),
                sandbox_hide_paths=self._config.sandbox.hide_paths,
                on_subprocess=lambda proc: self._register_subprocess(tid, proc),
            )
        except (WorkspaceError, FileNotFoundError) as exc:
            logger.error("Workspace preparation failed for %s: %s", tid, exc)
            err_comment = self._post_comment_safe(tid,
                f"**Symphony error**: Workspace preparation failed:\n```\n{exc}\n```",
                return_comment=True)
            self._save_setup_error(tid, issue, str(exc), err_comment)
            return

        # B2: check cancellation after prepare returns.
        if self._is_cancelled(tid):
            return

        ticket_state.workspace_path = workspace_path
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # --- Transition Linear to In Progress ---
        try:
            self._linear.transition_to_state(tid, self._config.linear.in_progress_state)
        except Exception:
            logger.exception("Failed to transition %s to '%s'",
                             tid, self._config.linear.in_progress_state)

        if self._is_cancelled(tid):
            return

        # --- Post metadata comment ---
        meta_comment: Comment | None = None
        meta_body = _build_metadata_comment(workspace_path)
        try:
            meta_comment = self._linear.post_comment(tid, meta_body)
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
            full_issue = self._linear.get_issue(tid)
            description = full_issue.description
        except LinearError:
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

        try:
            session_id, final_message = run_initial(
                workspace_path=workspace_path,
                prompt=prompt,
                model=self._config.opencode.model,
                timeout_seconds=self._config.turn_timeout_seconds,
                on_subprocess=lambda proc: self._register_subprocess(tid, proc),
                hide_paths=self._config.sandbox.hide_paths,
            )
        except OpenCodeTimeout:
            logger.error("OpenCode turn timed out for %s", tid)
            err_comment = self._post_comment_safe(tid,
                f"**Symphony error**: The AI turn timed out after "
                f"{self._config.turn_timeout_seconds}s.",
                return_comment=True)
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
            err_comment = self._post_comment_safe(tid,
                f"**Symphony error**: The AI turn failed:\n```\n{exc}\n```",
                return_comment=True)
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

        # --- Edit metadata comment ---
        if meta_comment is not None:
            try:
                final_meta = _build_metadata_comment_final(workspace_path, session_id)
                self._linear.edit_comment(meta_comment.id, final_meta)
            except Exception:
                logger.exception("Failed to edit metadata comment for %s", tid)

        ticket_state.session_id = session_id
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        if self._is_cancelled(tid):
            return

        # --- Post final message ---
        last_comment = self._post_final_message(tid, final_message)
        if last_comment is None:
            return  # state saved as failed inside _post_final_message
        ticket_state.last_seen_comment_id = last_comment.id

        # --- Transition to Needs Input ---
        transition_ok = True
        try:
            self._linear.transition_to_state(tid, self._config.linear.needs_input_state)
        except Exception:
            logger.exception("Failed to transition %s to '%s'",
                             tid, self._config.linear.needs_input_state)
            transition_ok = False

        with self._state_lock:
            ticket_state.status = TicketStatus.needs_input if transition_ok else TicketStatus.failed
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
            new_comments = self._linear.list_comments_since(
                tid, ticket_state.last_seen_comment_id,
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

        logger.info("Resume pipeline starting for %s (%d new human comment(s))",
                    tid, len(human_comments))
        message = _format_comments_message(human_comments)

        try:
            self._linear.transition_to_state(tid, self._config.linear.in_progress_state)
        except Exception:
            logger.exception("Failed to transition %s to '%s'",
                             tid, self._config.linear.in_progress_state)

        with self._state_lock:
            ticket_state.status = TicketStatus.working
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()

        if self._is_cancelled(tid):
            return

        try:
            final_message = run_resume(
                workspace_path=ticket_state.workspace_path,
                session_id=ticket_state.session_id or "",
                message=message,
                timeout_seconds=self._config.turn_timeout_seconds,
                on_subprocess=lambda proc: self._register_subprocess(tid, proc),
                hide_paths=self._config.sandbox.hide_paths,  # B3
            )
        except OpenCodeTimeout:
            logger.error("OpenCode resume timed out for %s", tid)
            err_comment = self._post_comment_safe(tid,
                f"**Symphony error**: The AI turn timed out after "
                f"{self._config.turn_timeout_seconds}s.",
                return_comment=True)
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
            err_comment = self._post_comment_safe(tid,
                f"**Symphony error**: The AI turn failed:\n```\n{exc}\n```",
                return_comment=True)
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

        last_comment = self._post_final_message(tid, final_message)
        if last_comment is None:
            return
        ticket_state.last_seen_comment_id = last_comment.id

        transition_ok = True
        try:
            self._linear.transition_to_state(tid, self._config.linear.needs_input_state)
        except Exception:
            logger.exception("Failed to transition %s to '%s'",
                             tid, self._config.linear.needs_input_state)
            transition_ok = False

        with self._state_lock:
            ticket_state.status = TicketStatus.needs_input if transition_ok else TicketStatus.failed
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()

        logger.info("Resume pipeline complete for %s", tid)

    # ==================================================================
    # Shared helpers
    # ==================================================================

    def _post_comment_safe(self, tid: str, body: str, *, return_comment: bool = False) -> Comment | None:
        try:
            comment = self._linear.post_comment(tid, body)
            return comment if return_comment else None
        except Exception:
            logger.exception("Failed to post comment for %s", tid)
            return None

    def _post_final_message(self, tid: str, final_message: str) -> Comment | None:
        if self._is_cancelled(tid):
            return None
        try:
            return self._linear.post_comment(
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
        self, tid: str, issue: Issue, error_code: str,
        error_comment: Comment | None = None,
    ) -> None:
        """Save failed state with setup_error, using error comment id as baseline (S3)."""
        branch = issue.branch_name or f"symphony/{issue.identifier.lower()}"
        ts = TicketState(
            ticket_id=tid,
            ticket_identifier=issue.identifier,
            project_id=issue.project.id if issue.project else None,
            repo_url="", workspace_path="", branch=branch,
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
            comments = self._linear.list_comments_since(issue_id, last_seen)
        except Exception:
            logger.exception("Failed to list comments for %s", issue_id)
            return False
        return any(c.user_id != bot_id for c in comments)

    def _get_issue_safe(self, issue_id: str) -> Issue | None:
        try:
            return self._linear.get_issue(issue_id)
        except Exception:
            logger.exception("Failed to fetch issue %s", issue_id)
            return None

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

    def _shutdown_handler(self) -> None:
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
            return self._linear.list_triggered_issues(
                label=self._config.linear.trigger_label,
                active_states=[
                    self._config.linear.in_progress_state,
                    self._config.linear.needs_input_state,
                ],
            )
        except Exception:
            logger.exception("Failed to fetch triggered issues")
            return []
