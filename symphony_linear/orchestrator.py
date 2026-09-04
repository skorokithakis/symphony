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

from symphony_linear import omp, opencode, pi
from symphony_linear.agent_runner import AgentCancelled, AgentError, AgentTimeout
from symphony_linear.attachments import process_attachments
from symphony_linear.config import AppConfig
from symphony_linear.linear import (
    Comment,
    Issue,
)
from symphony_linear.project_config import (
    ProjectConfigError,
    load_project_config,
)
from symphony_linear.state import SessionRecord, StateManager, TicketState, TicketStatus
from symphony_linear.tracker import (
    Tracker,
    TrackerError,
    TrackerNotFoundError,
    TrackerTransientError,
    TransitionTarget,
    is_bot_comment,
    model_for_issue,
)
from symphony_linear.webhook import WebhookServer
from symphony_linear.workspace import (
    ServeScriptMissing,
    WorkspaceError,
    clone_workspace,
    compute_workspace_path,
    dirty_summary,
    ensure_attachments_dir,
    ensure_dir_map,
    ensure_tmp_dir,
    finalize_workspace,
    remove,
    start_serve,
)

logger = logging.getLogger(__name__)

# Module-level aliases for the OpenCode adapter.  They exist only so the
# long-standing `symphony_linear.orchestrator.run_initial` patch target keeps
# working for the existing tests; OMP is reached through `omp.<name>` instead.
# Both go through _agent_callables — neither alias carries any meaning beyond
# being a stable patch seam.
run_initial = opencode.run_initial
run_resume = opencode.run_resume

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SHUTDOWN_GRACE_SECONDS = 5
_MAX_INTERRUPTED_TURNS = 3
_RESTART_NOTICE_BODY = (
    "**Symphony**: Restarted before setup completed. "
    "Picking this ticket up again on the next poll."
)
# Posted (once) when a human comment lands while a turn is genuinely
# running.  The em dash and apostrophes are deliberate; keep verbatim.
_IGNORED_COMMENT_BODY = (
    "**I did not read this — I'm mid-turn.**\n\n"
    "I'll post my reply when this turn finishes. Comment again after that "
    "and I'll pick it up."
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
        "other way to talk to them. Only your last message is posted. Anything you "
        "say between tool calls is dropped, so put the whole answer in your final "
        "message — do not spread it across the turn.\n\n---\n\n"
        f"# {title}\n\n{desc}"
    )


def _format_comments_message(comments: list[Comment]) -> str:
    parts: list[str] = []
    for c in comments:
        author = c.user_id or "unknown"
        parts.append(f"[{author} at {c.created_at}]\n{c.body}")
    return "\n\n".join(parts)


def _agent_callables(agent: str) -> tuple[Any, Any]:
    """Return the initial and resume callables for a configured agent."""
    if agent == "opencode":
        return run_initial, run_resume
    if agent == "omp":
        return omp.run_initial, omp.run_resume
    if agent == "pi":
        return pi.run_initial, pi.run_resume
    raise ValueError(f"Unsupported coding agent: {agent!r}")


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

        # Per-turn input markers (guarded by _task_lock).  Key present means
        # "this turn's input is fixed"; the value is the id of the newest
        # comment on the ticket at that moment (None when there were none).
        # _warn_ignored_comments measures "pending" from this marker so it
        # never accuses the comment that started the turn, and never touches
        # last_seen_comment_id mid-turn (which would leave restart recovery
        # nothing to replay).  In-memory by design: per-turn, and on restart
        # the map is empty, nothing warns, recovery behaves as today.
        self._turn_input_marker: dict[str, str | None] = {}

        # Active QA serve process (in-memory only; not persisted).
        self._active_serve: _ActiveServe | None = None
        self._serve_lock = threading.Lock()

        # Serialises upsert+save pairs.
        self._state_lock = threading.Lock()

        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._tick_lock = threading.Lock()

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
        # Only bootstrapping entries are handled at startup: their workspaces
        # may be half-prepared, so the entry is dropped and the ticket is
        # picked up as a fresh one on the first tick.  Working tickets are
        # left alone — run() ticks immediately after this, and tick step 4
        # re-runs interrupted turns there, where the issue list, the QA skip,
        # and step-3 cleanup are all available.
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
                            "restart",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to edit metadata comment %s during recovery of %s",
                            ticket_state.metadata_comment_id,
                            ticket_state.ticket_id,
                        )
                self._state.remove(ticket_state.ticket_id)
        self._state.save()
        logger.info("Startup recovery complete")

    def _rerun_interrupted_turn(self, ticket_state: TicketState, issue: Issue) -> None:
        """Re-run a turn interrupted by a restart or pipeline-thread death.

        Called from tick step 4 for tickets stuck in ``working``.  With a
        session id and pending human comments the interrupted resume is
        re-run; without a session id the initial turn restarts (carrying any
        pending comments into the prompt); with a session id but nothing
        pending there is nothing to replay, so the ticket is parked in
        needs_input.  After ``_MAX_INTERRUPTED_TURNS`` consecutive
        interruptions the daemon gives up and parks the ticket.  The re-run
        branches do not transition the tracker themselves: both pipelines
        transition to In Progress.
        """
        tid = ticket_state.ticket_id
        if self._is_cancelled(tid):
            return

        if ticket_state.interrupted_turns >= _MAX_INTERRUPTED_TURNS:
            self._park_interrupted_turn(
                ticket_state,
                (
                    "**Symphony**: I re-ran the last turn "
                    f"{_MAX_INTERRUPTED_TURNS} times and it kept getting "
                    "interrupted, so I stopped. Reply to continue, or "
                    f"{self._tracker.human_trigger_description()} to stop."
                ),
            )
            return

        try:
            pending = self._fetch_pending_human_comments(
                tid, ticket_state.last_seen_comment_id
            )
        except Exception:
            # A flaky fetch must not be mistaken for "nothing pending":
            # parking the ticket would advance last_seen past the
            # unconsumed human comment.  Leave the ticket working and let
            # the next tick retry.
            logger.warning(
                "Recovery: failed to fetch pending comments for %s — "
                "leaving the ticket working",
                tid,
                exc_info=True,
            )
            return

        if (
            ticket_state.session_id is not None
            and not self._session_matches_current_agent(ticket_state.agent)
        ):
            logger.info(
                "Recovery: discarding %s session for %s; configured agent is %s",
                ticket_state.agent or "opencode",
                tid,
                self._config.agent,
            )
            self._drop_stale_session(ticket_state)

        if ticket_state.session_id is not None and pending is None:
            self._park_interrupted_turn(
                ticket_state,
                (
                    "**Symphony**: Daemon restarted while I was working on "
                    "this, but I have nothing to re-run. Reply to continue, "
                    "or "
                    f"{self._tracker.human_trigger_description()} to stop."
                ),
            )
            return

        # A real re-run: count it and persist before starting, so a death
        # during the re-run itself is attributed to the interruption streak.
        with self._state_lock:
            ticket_state.interrupted_turns += 1
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()
        attempt = ticket_state.interrupted_turns

        if self._is_cancelled(tid):
            return

        if ticket_state.session_id is not None:
            self._post_comment_safe(
                tid,
                (
                    "**Symphony**: Restarted — re-running the last turn "
                    f"(attempt {attempt} of {_MAX_INTERRUPTED_TURNS}), "
                    "no reply needed."
                ),
                kind="restart",
            )
            # pending is not None here (the nothing-to-replay branch returned
            # above); replay_message being set is what makes the resume a
            # recovery re-run rather than a fresh human-input turn.
            self._resume_pipeline(
                ticket_state,
                model_for_issue(issue, self._config.models),
                replay_message=pending,
            )
        else:
            self._post_comment_safe(
                tid,
                (
                    "**Symphony**: Restarted before the first turn finished — "
                    f"starting it again (attempt {attempt} of "
                    f"{_MAX_INTERRUPTED_TURNS}), no reply needed."
                ),
                kind="restart",
            )
            self._new_ticket_pipeline(issue, pending, recovering=True)
        logger.info("Recovery: re-running interrupted turn for %s", tid)

    def _park_interrupted_turn(self, ticket_state: TicketState, message: str) -> None:
        """Post *message*, move the ticket to needs_input, and baseline comments.

        Used by the give-up and nothing-to-replay branches.  The
        ``interrupted_turns`` counter is deliberately left untouched: give-up
        does not reset it (a stale-comment replay stays capped), and the
        nothing-to-replay branch never had anything to count.
        """
        tid = ticket_state.ticket_id
        if self._is_cancelled(tid):
            return
        comment = self._post_comment_safe(
            tid, message, return_comment=True, kind="restart"
        )
        if self._is_cancelled(tid):
            return
        try:
            self._tracker.transition_to(tid, TransitionTarget.needs_input)
        except Exception:
            logger.exception(
                "Failed to transition %s to '%s' during restart recovery",
                tid,
                TransitionTarget.needs_input.value,
            )
        with self._state_lock:
            # Advance last_seen best-effort so the triggering comment is not
            # re-seen as new on the next tick (a replay would reset the
            # interruption counter and could loop forever).  When the comment
            # post fails, always try the baseline; never clobber a good value
            # with a failed baseline (None).
            if comment is not None:
                ticket_state.last_seen_comment_id = comment.id
            else:
                baseline = self._baseline_comment_id(tid)
                if baseline is not None:
                    ticket_state.last_seen_comment_id = baseline
            ticket_state.status = TicketStatus.needs_input
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()
        logger.info("Recovery: %s parked in needs_input", tid)

    # ==================================================================
    # Tick
    # ==================================================================

    def _tick(self) -> None:
        with self._tick_lock:
            logger.debug("Poll tick starting")
            issues = self._fetch_triggered_issues()

            # Tickets that already had a pipeline scheduled this tick.  Step 4
            # must not schedule a second pipeline for the same ticket in the
            # same tick: _schedule_task dedup only skips in-flight tasks, so a
            # fast pipeline could otherwise run twice and post two comments.
            scheduled_this_tick: set[str] = set()

            # --- Step 2: new tickets + setup-error retries ---
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
                        pending = self._pending_human_comments(
                            issue.id, existing.last_seen_comment_id
                        )
                        if pending is not None:
                            logger.info(
                                "User commented on setup-error %s – retrying",
                                issue.id,
                            )
                            with self._state_lock:
                                existing.setup_error = None
                                existing.updated_at = _iso_now()
                                self._state.upsert(existing)
                                self._state.save()
                            # Route to resume if the existing state already has a
                            # session (e.g. ProjectConfigError fired during a resume
                            # turn).  Otherwise start a fresh initial pipeline,
                            # carrying the pending comment into the prompt.
                            if existing.session_id:
                                self._schedule_task(
                                    issue.id,
                                    self._resume_pipeline,
                                    existing,
                                    model_for_issue(issue, self._config.models),
                                )
                            else:
                                self._schedule_task(
                                    issue.id,
                                    self._new_ticket_pipeline,
                                    issue,
                                    pending,
                                )
                            scheduled_this_tick.add(issue.id)
                    continue  # known ticket

                # Genuinely new ticket.
                self._schedule_task(issue.id, self._new_ticket_pipeline, issue)

            # --- Step 3: cleanup tickets that are no longer triggered ---
            # Build a lookup from the trigger list. Tickets that appear there
            # already meet the configured trigger condition, are in an active state,
            # and are not archived (Linear excludes archived by default), so they
            # are still triggered and we can skip the per-ticket get_issue call.
            issues_by_id = {i.id: i for i in issues}

            for ticket_state in list(self._state.tickets):
                tid = ticket_state.ticket_id

                if tid in issues_by_id:
                    continue

                # Derive the path from the identifier, not the state entry:
                # the entry may carry an empty or stale workspace_path (e.g.
                # while bootstrapping).  The dirtiness check inspects the
                # repo directory; remove() deletes the containing ticket dir.
                workspace_path = compute_workspace_path(
                    ticket_state.ticket_identifier, str(self._workspace)
                )

                try:
                    current = self._tracker.get_issue(tid)
                except TrackerNotFoundError:
                    # Ticket deletion is a deliberate act — clean up
                    # unconditionally, dirty or not.
                    logger.info("Ticket %s not found — cleaning up", tid)
                    self._cancel_ticket(tid)
                    identifier = ticket_state.ticket_identifier
                    self._state.remove(tid)
                    self._state.remove_session(tid)
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

                summary = dirty_summary(workspace_path)
                if summary is not None:
                    if ticket_state.cleanup_refused_state is None:
                        # First time cleanup fires on a dirty workspace: refuse.
                        # Keep the state entry (and the in-flight turn's
                        # ticket_state.status) and ask the human to decide.
                        logger.info(
                            "Ticket %s no longer triggered but workspace is dirty — "
                            "refusing cleanup",
                            tid,
                        )
                        needs_input_name = self._tracker.transition_name_for(
                            TransitionTarget.needs_input
                        )
                        # Transition first, so the comment can report what
                        # actually happened.
                        transition_ok = True
                        try:
                            self._tracker.transition_to(
                                tid, TransitionTarget.needs_input
                            )
                        except Exception:
                            logger.exception(
                                "Failed to transition %s to '%s' during "
                                "dirty-workspace refusal",
                                tid,
                                TransitionTarget.needs_input.value,
                            )
                            transition_ok = False
                        moved_line = (
                            f"I moved the ticket back to **{needs_input_name}**."
                            if transition_ok
                            else f"I could not move the ticket back to "
                            f"**{needs_input_name}** — please move it yourself."
                        )
                        self._post_comment_safe(
                            tid,
                            (
                                "**Workspace not clean — I did not delete it.**\n\n"
                                "This ticket has work that only exists in its "
                                "workspace:\n\n"
                                f"{summary}\n\n"
                                f"{moved_line} "
                                "Commit and push the work, or ask me to do it. "
                                "If you move the ticket out again, I will delete "
                                "the workspace."
                            ),
                            kind="cleanup",
                        )
                        # Remember which state we left the ticket in: the
                        # needs-input state if the transition worked, otherwise
                        # wherever it already was.  A later tick deletes only
                        # when the ticket has moved away from that state.
                        ticket_state.cleanup_refused_state = (
                            needs_input_name if transition_ok else current.state
                        )
                        ticket_state.updated_at = _iso_now()
                        with self._state_lock:
                            self._state.upsert(ticket_state)
                            self._state.save()
                        continue
                    if current.state == ticket_state.cleanup_refused_state:
                        # The ticket is exactly where we left it after the
                        # refusal: either the human never moved it again (the
                        # trigger went away for another reason — label removed,
                        # archived), or the refusal transition itself failed and
                        # the human has not acted.  Stop tracking but keep the
                        # dirty directory, and tell the human where it is.
                        logger.info(
                            "Ticket %s no longer triggered, workspace still dirty, "
                            "still in %s — dropping state, keeping workspace",
                            tid,
                            current.state,
                        )
                        self._post_comment_safe(
                            tid,
                            (
                                "**Stopped tracking this ticket.**\n\n"
                                "It is no longer triggered, but its workspace "
                                "holds work that exists nowhere else, so I kept "
                                "the directory:\n\n"
                                f"`{workspace_path}`\n\n"
                                "Trigger the ticket again and I will pick up "
                                "that workspace where it is. Nothing deletes it "
                                "on its own."
                            ),
                            kind="cleanup",
                        )
                        self._cancel_ticket(tid)
                        if ticket_state.session_id is not None:
                            record = SessionRecord(
                                session_id=ticket_state.session_id,
                                agent=ticket_state.agent or "opencode",
                                last_seen_comment_id=ticket_state.last_seen_comment_id,
                            )
                            self._state.set_session(tid, record)
                        self._state.remove(tid)
                        self._state.save()
                        continue
                    # The human moved the ticket out again — tell them what the
                    # workspace held before the full cleanup (including rmtree)
                    # deletes it.
                    self._post_comment_safe(
                        tid,
                        (
                            "**Workspace deleted.**\n\n"
                            "You moved the ticket out again, so I deleted the "
                            "workspace. It still held:\n\n"
                            f"{summary}\n\n"
                            "Those changes are gone."
                        ),
                        kind="cleanup",
                    )

                logger.info(
                    "Ticket %s no longer triggered (state=%s labels=%s archived=%s) — cleaning up",
                    tid,
                    current.state,
                    current.labels,
                    current.archived_at is not None,
                )
                self._cancel_ticket(tid)
                identifier = ticket_state.ticket_identifier

                # Snapshot session into persistent mapping before removing state.
                if ticket_state.session_id is not None:
                    record = SessionRecord(
                        session_id=ticket_state.session_id,
                        agent=ticket_state.agent or "opencode",
                        last_seen_comment_id=ticket_state.last_seen_comment_id,
                    )
                    self._state.set_session(tid, record)

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

                # Step 2 already scheduled a pipeline for this ticket this tick
                # (e.g. a setup-error retry): skip it so no ticket can get two
                # pipelines scheduled in one tick.
                if tid in scheduled_this_tick:
                    continue

                # _resume_pipeline handles its own early-return when there are no new
                # human comments, so QA tickets naturally fall through here — only
                # tickets with actual new human comments will get an agent turn.
                # _reconcile_serve on the next tick kills the serve when the ticket
                # leaves QA.  Recovery of a working ticket, however, is unconditional
                # (no comment gating), so untriggered and cleanup-refused tickets are
                # left alone.  A QA ticket is left alone only while its task is in
                # flight; a stale QA entry is parked in needs_input, where a human
                # comment can reach it again.

                if st == TicketStatus.failed and ticket_state.setup_error is not None:
                    continue
                if st == TicketStatus.working:
                    fetched = issues_by_id.get(tid)
                    if fetched is None:
                        # Not in the trigger list: step 3 handles cleanup (or a
                        # transient fetch failure resolves on the next tick).  Do
                        # not schedule a re-run for a ticket we may be dropping.
                        logger.debug(
                            "Skipping recovery for working ticket %s: not triggered",
                            tid,
                        )
                        continue
                    if ticket_state.cleanup_refused_state is not None:
                        # Step-3 cleanup was refused for a dirty workspace; leave
                        # the entry alone until the human moves the ticket.
                        logger.debug(
                            "Skipping recovery for working ticket %s: cleanup refused",
                            tid,
                        )
                        continue
                    if self._tracker.is_in_qa(fetched):
                        if self._is_task_in_flight(tid):
                            logger.debug(
                                "Skipping recovery for working QA ticket %s: task in flight",
                                tid,
                            )
                            continue

                        repaired = False
                        with self._state_lock:
                            state_entry = self._state.get(tid)
                            if (
                                state_entry is not None
                                and state_entry.status == TicketStatus.working
                            ):
                                # Without this write, the ticket stays working forever and
                                # tick step 4 never reaches the needs_input/failed resume
                                # branch for new human comments.
                                state_entry.status = TicketStatus.needs_input
                                state_entry.updated_at = _iso_now()
                                self._state.upsert(state_entry)
                                self._state.save()
                                repaired = True
                        if repaired:
                            logger.info(
                                "Repaired stale working QA ticket %s to needs_input",
                                tid,
                            )
                        else:
                            logger.debug(
                                "Skipping recovery for working QA ticket %s", tid
                            )
                        continue
                    if self._is_task_in_flight(tid):
                        # A turn is genuinely running right now (as opposed to a
                        # ticket stuck in working because a turn was interrupted):
                        # the running turn will not consume a mid-turn comment, so
                        # acknowledge it explicitly instead of discarding it in
                        # silence.  _rerun_interrupted_turn is scheduled only when
                        # no task is in flight — that path replays pending comments
                        # as its input and must not have them warned away.
                        self._warn_ignored_comments(ticket_state)
                        continue
                    self._schedule_task(
                        tid, self._rerun_interrupted_turn, ticket_state, fetched
                    )
                elif st in (TicketStatus.needs_input, TicketStatus.failed):
                    tracked_issue = issues_by_id.get(tid)
                    if ticket_state.session_id:
                        # Resolve the effective model override (issue label,
                        # else the issue's project label) from the freshly
                        # fetched issue.  When the ticket is not in the trigger
                        # list this tick there is no Issue at hand — pass None
                        # rather than making an extra tracker API call.
                        model = (
                            model_for_issue(tracked_issue, self._config.models)
                            if tracked_issue is not None
                            else None
                        )
                        self._schedule_task(
                            tid, self._resume_pipeline, ticket_state, model
                        )
                    else:
                        # No session and no setup_error (failed + setup_error is
                        # skipped above): run the full initial turn so the agent
                        # gets the ticket title/description instead of resuming
                        # an empty session.  Gate on a new human comment, the
                        # same gate _resume_pipeline applies, so a turn never
                        # starts out of nowhere.  Tickets not in the trigger
                        # list (e.g. cleanup-refused ones) are left alone.
                        if tracked_issue is not None:
                            pending = self._pending_human_comments(
                                tid, ticket_state.last_seen_comment_id
                            )
                            if pending is not None:
                                self._schedule_task(
                                    tid,
                                    self._new_ticket_pipeline,
                                    tracked_issue,
                                    pending,
                                )

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
                    self._tracker.post_comment(av.ticket_id, body, "qa")
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
                    "qa",
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

            made_resumable = False
            with self._state_lock:
                state_entry = self._state.get(winner_id)
                if state_entry is not None and state_entry.status in (
                    TicketStatus.working,
                    TicketStatus.bootstrapping,
                ):
                    # Without this write the ticket stays working forever and
                    # tick step 4 never reaches the needs_input/failed resume
                    # branch that reads new human comments.  The write lives
                    # here rather than in the pipelines' AgentCancelled
                    # handler because that handler also runs on daemon
                    # shutdown (where needs_input would disarm restart
                    # recovery) and on ticket cleanup (where it would
                    # resurrect a removed entry).  get() returns the live
                    # entry, so no upsert is needed.
                    state_entry.status = TicketStatus.needs_input
                    state_entry.updated_at = _iso_now()
                    self._state.save()
                    made_resumable = True
            if made_resumable:
                self._post_comment_safe(
                    winner_id,
                    (
                        "**Symphony**: The running turn was stopped because this ticket "
                        "entered QA. A reply on this ticket will continue the work."
                    ),
                    kind="qa",
                )

        logger.info(
            "Starting QA serve for %s (workspace=%s)", winner.identifier, workspace_path
        )
        try:
            # The serve-reconcile path runs without prepare(), so ensure the
            # per-ticket tmp directory exists before launching the sandbox
            # (bwrap --bind is fatal when the source dir is missing).
            tmp_path = ensure_tmp_dir(winner.identifier, str(self._workspace))
            dir_map = ensure_dir_map(
                self._config.sandbox.dir_map, winner.identifier, str(self._workspace)
            )
            proc = start_serve(
                workspace_path=workspace_path,
                hide_paths=self._config.sandbox.hide_paths,
                extra_rw_paths=self._config.sandbox.extra_rw_paths,
                dir_map=dir_map,
                tmp_path=tmp_path,
            )
        except (ServeScriptMissing, WorkspaceError, FileNotFoundError) as exc:
            logger.error("Failed to start QA serve for %s: %s", winner.identifier, exc)
            try:
                self._tracker.transition_to(winner_id, TransitionTarget.needs_input)
            except TrackerError:
                logger.exception(
                    "Failed to transition QA winner %s to needs_input after serve start failure",
                    winner_id,
                )
                return
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
            self._post_comment_safe(av.ticket_id, body, kind="qa")
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

    def _is_task_in_flight(self, ticket_id: str) -> bool:
        """Return True if a task for *ticket_id* is currently running.

        Mirrors the ``_active_tasks`` lookup :meth:`_schedule_task` uses,
        under the same lock.  This is the only reliable signal that a turn
        is genuinely running right now: ``TicketStatus.working`` also
        covers turns interrupted by a daemon restart or a dead pipeline
        thread, and those must not warn (recovery replays pending comments
        as input).
        """
        with self._task_lock:
            existing = self._active_tasks.get(ticket_id)
            return existing is not None and not existing.done()

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
                self._turn_input_marker.pop(ticket_id, None)
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
    # New-ticket pipeline
    # ==================================================================

    def _new_ticket_pipeline(
        self,
        issue: Issue,
        extra_context: str | None = None,
        *,
        recovering: bool = False,
    ) -> None:
        """Run a full initial turn for *issue*.

        *extra_context* is optional pre-formatted text (e.g. pending human
        comments that triggered a rerun) appended to the initial prompt.
        *recovering* marks a re-run of an interrupted first turn: the
        ``interrupted_turns`` streak is carried over instead of starting
        fresh.
        """
        tid = issue.id
        logger.info("New ticket pipeline starting for %s (%s)", tid, issue.identifier)
        if self._is_cancelled(tid):
            return

        # Model override for the primary agent, resolved from the freshly
        # fetched issue: a ``Model:`` label on the issue itself, else one on
        # its project as a project-wide default.  Nothing is persisted:
        # changing or removing either label takes effect on the next turn.
        model = model_for_issue(issue, self._config.models)

        # --- Resolve repository URL ---
        try:
            repo_url = self._tracker.repo_url_for(issue)
        except TrackerError as exc:
            logger.warning("Ticket %s has no resolvable repo: %s", tid, exc)
            self._transition_failed_to_needs_input(tid)
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
        # Preserve the previous last_seen_comment_id: this pipeline also runs on
        # the retry route (failed-no-session after a user comment).  Wiping it
        # here would make the next _pending_human_comments call return the whole
        # comment history again and re-run the initial pipeline every tick.
        previous_state = self._state.get(tid)
        ticket_state = TicketState(
            ticket_id=tid,
            ticket_identifier=issue.identifier,
            project_id=issue.project.id if issue.project else None,
            repo_url=repo_url,
            workspace_path="",  # not yet known
            branch="",  # will be updated after loading project config
            last_seen_comment_id=(
                previous_state.last_seen_comment_id if previous_state else None
            ),
            # This fresh state would wipe the interruption streak; carry it
            # over when the pipeline is a recovery re-run, else start clean.
            interrupted_turns=(
                previous_state.interrupted_turns
                if previous_state is not None and recovering
                else 0
            ),
            status=TicketStatus.bootstrapping,
        )
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        if self._is_cancelled(tid):
            return

        # --- Clone workspace ---
        try:
            workspace_path, recovered = clone_workspace(
                ticket_identifier=issue.identifier,
                repo_url=repo_url,
                workspace_root=str(self._workspace),
            )
        except (WorkspaceError, FileNotFoundError) as exc:
            logger.error("Workspace clone failed for %s: %s", tid, exc)
            self._transition_failed_to_needs_input(tid)
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

        # Notify the user when the workspace had to be nuked and re-cloned.
        if recovered:
            self._post_comment_safe(
                tid,
                "Workspace was reset due to stale git state and rebuilt from origin.",
                kind="workspace",
            )

        ticket_state.workspace_path = workspace_path
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # --- Load per-project config ---
        try:
            project_config = load_project_config(workspace_path)
        except ProjectConfigError as exc:
            logger.error("Project config invalid for %s: %s", tid, exc)
            self._transition_failed_to_needs_input(tid)
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
            # Ensure the per-ticket tmp directory exists before the setup
            # script runs inside the sandbox (bwrap --bind is fatal when the
            # source dir is missing).
            tmp_path = ensure_tmp_dir(issue.identifier, str(self._workspace))
            dir_map = ensure_dir_map(
                self._config.sandbox.dir_map, issue.identifier, str(self._workspace)
            )
            finalize_workspace(
                workspace_path=workspace_path,
                ticket_identifier=issue.identifier,
                branch_name=issue.branch_name,
                sandbox_hide_paths=self._config.sandbox.hide_paths,
                on_subprocess=lambda proc: (self._register_subprocess(tid, proc), None)[
                    1
                ],
                sandbox_extra_rw_paths=self._config.sandbox.extra_rw_paths,
                sandbox_dir_map=dir_map,
                auto_branch=effective_auto_branch,
                tmp_path=tmp_path,
            )
        except (WorkspaceError, FileNotFoundError) as exc:
            logger.error("Workspace finalization failed for %s: %s", tid, exc)
            self._transition_failed_to_needs_input(tid)
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
            meta_comment = self._tracker.post_comment(tid, meta_body, "workspace")
            ticket_state.metadata_comment_id = meta_comment.id
            with self._state_lock:
                self._state.upsert(ticket_state)
                self._state.save()
        except Exception:
            logger.exception("Failed to post metadata comment for %s", tid)

        if self._is_cancelled(tid):
            return

        # --- Rehydrate from session snapshot (if any) ---
        session_record = self._state.get_session(tid)
        if session_record is not None and not self._session_matches_current_agent(
            session_record.agent
        ):
            logger.info(
                "Discarding %s session snapshot for %s; configured agent is %s",
                session_record.agent or "opencode",
                tid,
                self._config.agent,
            )
            self._drop_stale_session(ticket_state)
            session_record = None
        if session_record is not None:
            logger.info(
                "Rehydrating session for %s from persistent snapshot (session=%s)",
                tid,
                session_record.session_id,
            )
            ticket_state.session_id = session_record.session_id
            ticket_state.agent = self._config.agent
            ticket_state.last_seen_comment_id = session_record.last_seen_comment_id
            ticket_state.status = TicketStatus.needs_input
            ticket_state.updated_at = _iso_now()
            with self._state_lock:
                self._state.upsert(ticket_state)
                self._state.save()

            # If the human already commented while the ticket was untriggered,
            # don't bounce the tracker back to needs_input: the next tick's
            # _resume_pipeline picks up the pending comment (state stays
            # needs_input, so step 4 schedules it) and transitions the tracker
            # to in_progress itself.  _pending_human_comments swallows tracker
            # failures, so an error falls through to the needs_input path below.
            if (
                self._pending_human_comments(tid, session_record.last_seen_comment_id)
                is not None
            ):
                self._post_comment_safe(
                    tid,
                    "Workspace restored — resuming previous session with your comment.",
                    kind="workspace",
                )
                logger.info("New ticket pipeline rehydrated for %s", tid)
                return

            # Transition the tracker to needs_input.
            try:
                self._tracker.transition_to(tid, TransitionTarget.needs_input)
            except Exception:
                logger.exception(
                    "Failed to transition %s to '%s' during rehydrate",
                    tid,
                    TransitionTarget.needs_input.value,
                )

            self._post_comment_safe(
                tid,
                "Workspace restored — previous session will resume on your next comment.",
                kind="workspace",
            )
            logger.info("New ticket pipeline rehydrated for %s", tid)
            return

        # --- Fetch description + build prompt ---
        try:
            full_issue = self._tracker.get_issue(tid)
            description = full_issue.description
        except TrackerError:
            logger.exception("Failed to fetch issue %s for description", tid)
            description = None

        # --- Process attachments ---
        # ensure_attachments_dir applies the path-containment security check
        # before creating the directory.
        host_attachments_dir = ensure_attachments_dir(
            ticket_state.ticket_identifier, str(self._workspace)
        )
        dir_map = ensure_dir_map(
            self._config.sandbox.dir_map,
            ticket_state.ticket_identifier,
            str(self._workspace),
        )
        try:
            result = process_attachments(
                description or "",
                tracker=self._tracker,
                host_attachments_dir=host_attachments_dir,
                existing_count=ticket_state.attachment_count,
            )
        except Exception:
            logger.exception("Attachment processing failed for %s", tid)
            result = None

        if result is None:
            # Plumbing failure — proceed with original description and no files.
            files: list[str] = []
            rewritten_description = description
        else:
            rewritten_description = result.rewritten_body
            files = result.file_paths

            if result.skipped:
                skipped_lines = "\n".join(
                    f"- {url}: {reason}" for url, reason in result.skipped
                )
                self._post_comment_safe(
                    tid,
                    f"**Symphony**: skipped attachments\n\n{skipped_lines}",
                    kind="attachments",
                )

            # Bump attachment_count to the next available index, ensuring
            # indices consumed by skipped downloads are never reused.
            ticket_state.attachment_count = max(
                ticket_state.attachment_count, result.next_index
            )
            with self._state_lock:
                self._state.upsert(ticket_state)
                self._state.save()

        prompt = _build_initial_prompt(issue.title, rewritten_description)
        # NOTE: extra_context (pending human comments) is appended as plain
        # text.  Attachment processing above only ran against the ticket
        # description, so any attachments in the continuation comments are
        # not downloaded or passed to run_initial here.  Accepted limitation.
        if extra_context:
            prompt += f"\n\n---\n\n{extra_context}"

        if self._is_cancelled(tid):
            return

        # --- Run agent (B3: pass hide_paths) ---
        ticket_state.status = TicketStatus.working
        # Agent takes a turn — re-arm the dirty-workspace guard.
        ticket_state.cleanup_refused_state = None
        if not recovering:
            # A turn started from genuinely new input ends any interruption
            # streak.
            ticket_state.interrupted_turns = 0
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # The turn's input is now fixed: record the newest comment id so
        # _warn_ignored_comments only flags comments arriving after this
        # point, never the comments this turn is consuming.  A failed
        # baseline fetch (None) is accepted imprecision: the turn then warns
        # about pre-existing comments once, and the marker advances past
        # them on that warning.
        marker = self._baseline_comment_id(tid)
        with self._task_lock:
            self._turn_input_marker[tid] = marker

        # Compute effective turn timeout from project config, falling back to global.
        effective_turn_timeout = (
            project_config.turn_timeout_seconds or self._config.turn_timeout_seconds
        )

        try:
            agent_run_initial, _ = _agent_callables(self._config.agent)
            session_id, final_message, _context_tokens = agent_run_initial(
                workspace_path=workspace_path,
                prompt=prompt,
                timeout_seconds=effective_turn_timeout,
                idle_timeout_seconds=self._config.turn_idle_timeout_seconds,
                on_subprocess=lambda proc: (self._register_subprocess(tid, proc), None)[
                    1
                ],
                hide_paths=self._config.sandbox.hide_paths,
                extra_rw_paths=self._config.sandbox.extra_rw_paths,
                attachments_path=host_attachments_dir,
                dir_map=dir_map,
                tmp_path=tmp_path,
                files=files,
                model=model,
            )
        except AgentTimeout as exc:
            logger.error("Agent turn timed out for %s", tid)
            body = f"**Symphony error**: The AI turn timed out ({exc.reason})."
            if exc.partial_message:
                body += (
                    f"\n\nPartial output before the timeout:\n\n---\n\n"
                    f"{exc.partial_message}"
                )
            self._transition_failed_to_needs_input(tid)
            err_comment = self._post_comment_safe(
                tid,
                body,
                return_comment=True,
            )
            with self._state_lock:
                ticket_state.status = TicketStatus.failed
                ticket_state.updated_at = _iso_now()
                if err_comment is not None:
                    ticket_state.last_seen_comment_id = err_comment.id  # B1
                ticket_state.session_id = exc.session_id  # salvaged session, if any
                ticket_state.agent = (
                    self._config.agent if exc.session_id is not None else None
                )
                self._state.upsert(ticket_state)
                self._state.save()
            return
        except AgentCancelled as exc:
            logger.info("Agent turn cancelled for %s", tid)
            self._salvage_cancelled_session(tid, exc.session_id)
            return
        except AgentError as exc:
            logger.error("Agent failed for %s: %s", tid, exc)
            self._transition_failed_to_needs_input(tid)
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
                ticket_state.agent = None
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
                self._tracker.edit_comment(meta_comment.id, final_meta, "workspace")
            except Exception:
                logger.exception("Failed to edit metadata comment for %s", tid)

        ticket_state.session_id = session_id
        ticket_state.agent = self._config.agent
        with self._state_lock:
            self._state.upsert(ticket_state)
            self._state.save()

        # Fix 2: guard before _post_final_message.
        if self._is_cancelled(tid):
            logger.info("Ticket %s cancelled before final message — skipping", tid)
            return

        # --- Post final message ---
        last_comment = self._post_final_message(
            tid, final_message, _context_tokens, model
        )
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

    def _resume_pipeline(
        self,
        ticket_state: TicketState,
        model: str | None = None,
        *,
        replay_message: str | None = None,
    ) -> None:
        """Resume an existing agent session with new human comments.

        *model* is the per-issue model override for the primary agent,
        resolved by the caller from the freshly fetched issue's labels
        (``None`` when no override applies); it is passed through to
        ``opencode run --model`` and recorded in the final-comment footer.

        When *replay_message* is provided, the pipeline is a recovery re-run
        of an interrupted turn: the comment fetch and the new-comment gate
        are skipped entirely and *replay_message* is fed to the model as-is
        (the caller already fetched and formatted it), and the
        ``interrupted_turns`` streak is preserved instead of reset.  Without
        it, the pipeline is a normal turn started by a genuine human comment.
        """
        tid = ticket_state.ticket_id
        logger.debug("Resume pipeline tick for %s", tid)

        if replay_message is None:
            try:
                new_comments = self._tracker.list_comments_since(
                    tid,
                    ticket_state.last_seen_comment_id,
                )
            except Exception:
                logger.exception("Failed to fetch comments for %s", tid)
                return

            human_comments = [c for c in new_comments if not is_bot_comment(c.body)]
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
        else:
            message = replay_message

        if (
            ticket_state.session_id is not None
            and not self._session_matches_current_agent(ticket_state.agent)
        ):
            logger.info(
                "Discarding %s session for %s; configured agent is %s",
                ticket_state.agent or "opencode",
                tid,
                self._config.agent,
            )
            self._drop_stale_session(ticket_state)
            try:
                issue = self._tracker.get_issue(tid)
            except TrackerError:
                logger.exception("Failed to fetch issue %s after agent switch", tid)
                return
            self._new_ticket_pipeline(
                issue,
                message,
                recovering=replay_message is not None,
            )
            return

        # --- Process attachments ---
        # ensure_attachments_dir applies the path-containment security check
        # before creating the directory.
        host_attachments_dir = ensure_attachments_dir(
            ticket_state.ticket_identifier, str(self._workspace)
        )
        # Ensure the per-ticket tmp directory exists before the resumed turn
        # runs inside the sandbox (bwrap --bind is fatal when the source dir
        # is missing).
        tmp_path = ensure_tmp_dir(ticket_state.ticket_identifier, str(self._workspace))
        dir_map = ensure_dir_map(
            self._config.sandbox.dir_map,
            ticket_state.ticket_identifier,
            str(self._workspace),
        )
        try:
            result = process_attachments(
                message,
                tracker=self._tracker,
                host_attachments_dir=host_attachments_dir,
                existing_count=ticket_state.attachment_count,
            )
        except Exception:
            logger.exception("Attachment processing failed for %s", tid)
            result = None

        if result is None:
            files_attach: list[str] = []
            rewritten_message = message
        else:
            rewritten_message = result.rewritten_body
            files_attach = result.file_paths

            if result.skipped:
                skipped_lines = "\n".join(
                    f"- {url}: {reason}" for url, reason in result.skipped
                )
                self._post_comment_safe(
                    tid,
                    f"**Symphony**: skipped attachments\n\n{skipped_lines}",
                    kind="attachments",
                )

            # Bump attachment_count to the next available index, ensuring
            # indices consumed by skipped downloads are never reused.
            ticket_state.attachment_count = max(
                ticket_state.attachment_count, result.next_index
            )
            with self._state_lock:
                self._state.upsert(ticket_state)
                self._state.save()

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
            self._transition_failed_to_needs_input(tid)
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
                elif ticket_state.last_seen_comment_id is None:
                    ticket_state.last_seen_comment_id = self._baseline_comment_id(tid)
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
            # Agent takes a turn — re-arm the dirty-workspace guard.
            ticket_state.cleanup_refused_state = None
            if replay_message is None:
                # A turn started from genuinely new input ends any
                # interruption streak.
                ticket_state.interrupted_turns = 0
            ticket_state.updated_at = _iso_now()
            self._state.upsert(ticket_state)
            self._state.save()

        # The turn's input is now fixed: record the newest comment id so
        # _warn_ignored_comments only flags comments arriving after this
        # point, never the comment(s) this turn is consuming.
        marker: str | None
        if replay_message is None:
            # new_comments is the unfiltered fetch from above; its last
            # element is the newest comment on the ticket at fetch time.
            marker = new_comments[-1].id
        else:
            # The caller only passed the formatted text, so fetch the
            # newest comment id fresh (None if the fetch fails).
            marker = self._baseline_comment_id(tid)
        with self._task_lock:
            self._turn_input_marker[tid] = marker

        if self._is_cancelled(tid):
            return

        # Compute effective turn timeout from project config, falling back to global.
        effective_turn_timeout = (
            project_config.turn_timeout_seconds or self._config.turn_timeout_seconds
        )

        try:
            _, agent_run_resume = _agent_callables(self._config.agent)
            final_message, _context_tokens = agent_run_resume(
                workspace_path=ticket_state.workspace_path,
                session_id=ticket_state.session_id,
                message=rewritten_message,
                timeout_seconds=effective_turn_timeout,
                idle_timeout_seconds=self._config.turn_idle_timeout_seconds,
                on_subprocess=lambda proc: (self._register_subprocess(tid, proc), None)[
                    1
                ],
                hide_paths=self._config.sandbox.hide_paths,  # B3
                extra_rw_paths=self._config.sandbox.extra_rw_paths,
                attachments_path=host_attachments_dir,
                dir_map=dir_map,
                tmp_path=tmp_path,
                files=files_attach,
                model=model,
            )
        except AgentTimeout as exc:
            logger.error("Agent resume timed out for %s", tid)
            body = f"**Symphony error**: The AI turn timed out ({exc.reason})."
            if exc.partial_message:
                body += (
                    f"\n\nPartial output before the timeout:\n\n---\n\n"
                    f"{exc.partial_message}"
                )
            self._transition_failed_to_needs_input(tid)
            err_comment = self._post_comment_safe(
                tid,
                body,
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
        except AgentCancelled as exc:
            logger.info("Agent resume cancelled for %s", tid)
            self._salvage_cancelled_session(tid, exc.session_id)
            return
        except AgentError as exc:
            logger.error("Agent resume failed for %s: %s", tid, exc)
            self._transition_failed_to_needs_input(tid)
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

        last_comment = self._post_final_message(
            tid, final_message, _context_tokens, model
        )
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

    def _salvage_cancelled_session(self, tid: str, session_id: str | None) -> None:
        """Keep a killed turn's session id so the next turn can resume it.

        Writes only session_id/agent, never status: the QA path in
        _reconcile_serve is the other writer of this entry.  The entry is
        mutated in place and never upserted — step-3 cleanup removes entries
        without holding _state_lock, and upsert appends an absent id, so an
        upsert here could resurrect a ticket whose workspace is already gone.
        """
        if session_id is None:
            return

        with self._state_lock:
            state_entry = self._state.get(tid)
            if state_entry is None or state_entry.session_id is not None:
                return

            state_entry.session_id = session_id
            state_entry.agent = self._config.agent
            state_entry.updated_at = _iso_now()
            self._state.save()
            logger.info("Saved cancelled agent session for %s", tid)

    def _session_matches_current_agent(self, agent: str | None) -> bool:
        """Whether a recorded session can be handed to the configured agent.

        Session IDs are adapter-specific; an untagged legacy session is OpenCode.
        """
        return (agent or "opencode") == self._config.agent

    def _drop_stale_session(self, ticket_state: TicketState) -> None:
        """Forget an incompatible live session and its durable snapshot."""
        ticket_state.session_id = None
        ticket_state.agent = None
        ticket_state.updated_at = _iso_now()
        with self._state_lock:
            self._state.remove_session(ticket_state.ticket_id)
            self._state.upsert(ticket_state)
            self._state.save()

    def _transition_failed_to_needs_input(self, tid: str) -> None:
        """Best-effort: move a failed ticket to the tracker's Needs Input state.

        Called on every failure path right before the error comment is posted,
        so a failed ticket never sits in the tracker's In Progress state with
        no work in flight.  The internal ``TicketStatus.failed`` is unchanged
        and keeps driving the retry routes; only the tracker state moves.

        Skipped when the ticket is cancelled: a human may have moved it to QA
        mid-turn, and a failure path must not drag it back.  Never raises.
        """
        if self._is_cancelled(tid):
            logger.info(
                "Ticket %s cancelled — skipping failure transition to '%s'",
                tid,
                TransitionTarget.needs_input.value,
            )
            return
        try:
            self._tracker.transition_to(tid, TransitionTarget.needs_input)
        except Exception:
            logger.exception(
                "Failed to transition %s to '%s'",
                tid,
                TransitionTarget.needs_input.value,
            )

    def _post_comment_safe(
        self, tid: str, body: str, *, return_comment: bool = False, kind: str = "error"
    ) -> Comment | None:
        try:
            comment = self._tracker.post_comment(tid, body, kind)
            return comment if return_comment else None
        except Exception:
            logger.exception("Failed to post comment for %s", tid)
            return None

    def _post_final_message(
        self,
        tid: str,
        final_message: str,
        context_tokens: int | None = None,
        model: str | None = None,
    ) -> Comment | None:
        if self._is_cancelled(tid):
            return None
        body = final_message if final_message else "_(No output from the AI.)_"
        if context_tokens is not None:
            kind = f"context: {context_tokens:,} tokens"
        else:
            kind = "final"
        if model:
            # Middle dot (U+00B7) separator, the same one the footer already
            # uses — keeps is_bot_comment detection working.
            kind += f" · model: {model}"
        try:
            return self._tracker.post_comment(tid, body, kind)
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
        # Determine last_seen_comment_id baseline.
        last_seen: str | None
        if error_comment is not None:
            last_seen = error_comment.id
        elif existing is not None and existing.last_seen_comment_id is not None:
            last_seen = existing.last_seen_comment_id
        else:
            last_seen = self._baseline_comment_id(tid)

        if existing is not None:
            ts = existing.model_copy(
                update={
                    "status": TicketStatus.failed,
                    "setup_error": error_code,
                    "last_seen_comment_id": last_seen,
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
                last_seen_comment_id=last_seen,
            )
        with self._state_lock:
            self._state.upsert(ts)
            self._state.save()

    def _baseline_comment_id(self, tid: str) -> str | None:
        """Return the id of the most recent comment, or None if none exist or the call fails."""
        try:
            comments = self._tracker.list_comments_since(tid, None)
            if comments:
                return comments[-1].id
        except Exception:
            logger.warning(
                "Cannot fetch comments to baseline last_seen_comment_id for %s", tid
            )
        return None

    def _fetch_pending_human_comments(
        self, issue_id: str, last_seen: str | None
    ) -> str | None:
        """Return formatted new human comments since *last_seen*, or None.

        Bot comments (``is_bot_comment``) are filtered out.  Returns None
        when there are no new human comments; tracker failures propagate, so
        callers that must distinguish "nothing pending" from "fetch failed"
        (e.g. restart recovery) can catch and act accordingly.
        """
        comments = self._tracker.list_comments_since(issue_id, last_seen)
        human_comments = [c for c in comments if not is_bot_comment(c.body)]
        if not human_comments:
            return None
        return _format_comments_message(human_comments)

    def _pending_human_comments(
        self, issue_id: str, last_seen: str | None
    ) -> str | None:
        """Return formatted new human comments since *last_seen*, or None.

        Swallowing wrapper over :meth:`_fetch_pending_human_comments`:
        returns None both when there are no new human comments and when the
        tracker call fails, so callers can gate a turn on the result
        directly.
        """
        try:
            return self._fetch_pending_human_comments(issue_id, last_seen)
        except Exception:
            logger.exception("Failed to list comments for %s", issue_id)
            return None

    def _warn_ignored_comments(self, ticket_state: TicketState) -> None:
        """Tell the human their mid-turn comment was not read (once per comment).

        Called from tick step 4 while a task for a ``working`` ticket is in
        flight; such comments are never consumed by the running turn, so
        without the notice they would be discarded in silence.  Advances
        the turn marker past the newest human comment to keep later ticks
        quiet.

        Read-only with respect to persisted state: never touches
        ``last_seen_comment_id``, the anchor restart recovery replays from.

        "Pending" is measured from the per-turn ``_turn_input_marker``, never
        ``last_seen_comment_id``; an absent key means the input is not yet
        fixed — stay silent (presence gate, not an error).
        """
        tid = ticket_state.ticket_id
        with self._task_lock:
            if tid not in self._turn_input_marker:
                # The turn is in flight but its input is not fixed yet: a
                # pending comment cannot be told apart from the triggering
                # one, so stay silent.
                return
            since = self._turn_input_marker[tid]
        try:
            comments = self._tracker.list_comments_since(tid, since)
        except Exception:
            logger.exception("Failed to list comments for %s", tid)
            return
        if not self._is_task_in_flight(tid):
            # The turn finished (or was cancelled) while the fetch blocked:
            # it posted its final reply and advanced last_seen, or is being
            # torn down — either way a notice claiming to be mid-turn now
            # would be confusing, so stay silent.
            return
        human_comments = [c for c in comments if not is_bot_comment(c.body)]
        if not human_comments:
            return
        self._post_comment_safe(tid, _IGNORED_COMMENT_BODY, kind="ignored")
        with self._task_lock:
            # Advance the turn marker past the warned comment too, so the
            # next tick does not warn again about the same input.  Only when
            # the entry is still there: if the task finished while the
            # warning was posting, _task_wrapper popped it, and re-adding a
            # stale value would mislead the next turn's presence-gate.
            if tid in self._turn_input_marker:
                self._turn_input_marker[tid] = human_comments[-1].id

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
        except TrackerTransientError as exc:
            logger.warning("Failed to fetch triggered issues (transient): %s", exc)
            return []
        except Exception:
            logger.exception("Failed to fetch triggered issues")
            return []

    def _is_still_triggered(self, issue: Issue) -> bool:
        return self._tracker.is_still_triggered(issue)
