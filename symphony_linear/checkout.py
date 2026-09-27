"""Watch the git checkout the running package lives in.

When the daemon runs from a git checkout (an editable install or ``uv run``),
the process keeps executing the code it imported at startup.  This module
answers two read-only questions used to restart the daemon in place after the
checkout moves (``git pull``, ``jj new``, ``jj rebase``):

1. Did the checkout's HEAD move?  (:class:`CheckoutWatcher`)
2. Does the new checkout at least load its config?  (:func:`validate_checkout`)

The daemon itself never pulls, rebases, or syncs dependencies: detection
exists so a supervisor can re-exec the same command line.  ``validate_checkout``
runs before the re-exec so a checkout that cannot even load its config is not
allowed to replace a working daemon.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 10
_VALIDATE_TIMEOUT_SECONDS = 60
_OUTPUT_TAIL_CHARS = 2000


def _package_dir() -> Path:
    """Directory containing the running ``symphony_linear`` package."""
    return Path(__file__).resolve().parent


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _tail(text: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    return text[-limit:] if len(text) > limit else text


class CheckoutWatcher:
    """Tracks the HEAD of the checkout containing the running package.

    ``disabled`` is True when the package directory is not inside a git
    checkout (a wheel/site-packages install, or git unavailable).  In that
    case the daemon disables auto-restart for the process lifetime and
    ``read_head`` is never called again, so no poll-interval git subprocess
    runs for installs that can never restart.
    """

    def __init__(self, package_dir: Path | None = None) -> None:
        self._package_dir = (
            Path(package_dir) if package_dir is not None else _package_dir()
        )
        self.head: str | None = None
        self.disabled = False
        head = self.read_head()
        if head is None or not self._package_is_tracked():
            self.disabled = True
            logger.info(
                "Package is not a tracked file in a git checkout (or git is "
                "unavailable); auto-restart on checkout change is off."
            )
        else:
            self.head = head

    def read_head(self) -> str | None:
        """Return the checkout's current HEAD sha, or None if unavailable."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self._package_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        head = result.stdout.strip()
        return head or None

    def _package_is_tracked(self) -> bool:
        """Whether the package is part of the checkout, not a wheel in it.

        A non-editable wheel installed into a ``.venv`` inside the repository
        sits under the checkout, so ``rev-parse HEAD`` succeeds even though the
        running code is not the checkout's code.  Requiring ``__init__.py`` to
        be tracked distinguishes the two: the wheel's copy is not in the index.
        """
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._package_dir),
                    "ls-files",
                    "--error-unmatch",
                    "__init__.py",
                ],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0


def validate_checkout(workspace: Path) -> bool:
    """Validate the checkout by loading its config in a fresh interpreter.

    Runs ``python -m symphony_linear --validate-config --workspace <ws>`` and
    returns whether it exited 0.  A failure is logged with the output tail so
    the operator can see why the restart was skipped.
    """
    cmd = [
        sys.executable,
        "-m",
        "symphony_linear",
        "--validate-config",
        "--workspace",
        str(workspace),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_VALIDATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "Checkout validation timed out after %ds; keeping the running code:\n%s",
            _VALIDATE_TIMEOUT_SECONDS,
            _tail(_as_text(exc.stdout) + _as_text(exc.stderr)),
        )
        return False
    except OSError as exc:
        logger.error("Checkout validation could not run: %s", exc)
        return False
    if result.returncode != 0:
        logger.error(
            "Checkout validation failed (rc=%d); keeping the running code:\n%s",
            result.returncode,
            _tail((result.stdout or "") + (result.stderr or "")),
        )
        return False
    return True
