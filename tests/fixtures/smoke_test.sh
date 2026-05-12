#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke-test fixture for the bwrap sandbox.
#
# Validates:
#   1. Workspace is read-write.
#   2. Hidden/masked paths are inaccessible or empty.
#   3. Outbound network works (DNS or HTTP).
#   4. The Docker socket is not accessible.
#   5. ``sudo`` fails inside the sandbox.
#   6. System binaries (e.g. /usr/bin/git) are readable.
#   7. Writing outside the workspace is denied.
#
# Environment variables accepted (set by the test harness):
#   SMOKE_WORKSPACE   – path to the workspace directory   (required)
#   SMOKE_HIDE_PATHS  – colon-separated list of paths to check (optional)
#
# Exit: 0 on success, non-zero on the first failure.
# ---------------------------------------------------------------------------
set -euo pipefail

WORKSPACE="${SMOKE_WORKSPACE:-}"
HIDE_PATHS="${SMOKE_HIDE_PATHS:-}"

if [[ -z "$WORKSPACE" ]]; then
    echo "FAIL: SMOKE_WORKSPACE is not set" >&2
    exit 1
fi

echo "=== Sandbox Smoke Test ==="
echo "Workspace: $WORKSPACE"
echo ""

# ------------------------------------------------------------------
# 1. Workspace is read-write
# ------------------------------------------------------------------
echo "--- 1. Workspace is read-write ---"
TEST_FILE="$WORKSPACE/.smoke_test_write"
if echo "smoke-test-ok" > "$TEST_FILE" 2>/dev/null; then
    if [[ "$(cat "$TEST_FILE")" == "smoke-test-ok" ]]; then
        echo "PASS: workspace is writable"
        rm -f "$TEST_FILE"
    else
        echo "FAIL: could not read back written file" >&2
        exit 1
    fi
else
    echo "FAIL: workspace is not writable" >&2
    exit 1
fi

# ------------------------------------------------------------------
# 2. Hidden paths are masked
# ------------------------------------------------------------------
echo "--- 2. Hidden paths masked ---"
if [[ -n "$HIDE_PATHS" ]]; then
    IFS=':' read -r -a HIDDEN <<< "$HIDE_PATHS"
    for hp in "${HIDDEN[@]}"; do
        # Skip empty entries
        [[ -z "$hp" ]] && continue
        if [[ -d "$hp" ]]; then
            # Directory exists (as a tmpfs mount).  It must be empty.
            # shellcheck disable=SC2012
            count=$(ls -A "$hp" 2>/dev/null | wc -l)
            if [[ "$count" -eq 0 ]]; then
                echo "PASS: $hp is empty (masked)"
            else
                echo "FAIL: $hp is accessible and contains $count entries" >&2
                ls -la "$hp" >&2
                exit 1
            fi
        elif [[ -f "$hp" || -S "$hp" || -c "$hp" ]]; then
            # File, socket, or character device (e.g. /dev/null overlay).
            if [[ -c "$hp" ]]; then
                echo "PASS: $hp is a character device (masked with /dev/null)"
            else
                echo "FAIL: $hp exists as $(stat -c %F "$hp") but should be masked" >&2
                exit 1
            fi
        else
            # Doesn't exist at all — effectively masked.
            echo "PASS: $hp does not exist (masked)"
        fi
    done
else
    echo "SKIP: no hide paths specified"
fi

# ------------------------------------------------------------------
# 3. System binaries readable
# ------------------------------------------------------------------
echo "--- 3. System binaries readable ---"
if [[ -x /usr/bin/git ]]; then
    echo "PASS: /usr/bin/git is readable and executable"
else
    echo "FAIL: /usr/bin/git is not accessible" >&2
    exit 1
fi

# ------------------------------------------------------------------
# 4. Writing outside workspace is denied
# ------------------------------------------------------------------
echo "--- 4. Writing outside workspace denied ---"
if echo "should-fail" > /etc/.smoke_test_write 2>/dev/null; then
    echo "FAIL: was able to write to /etc" >&2
    rm -f /etc/.smoke_test_write
    exit 1
else
    echo "PASS: writing to /etc is denied (read-only filesystem)"
fi

# Also try writing to home (should fail if not explicitly bound)
if [[ ! -w "$HOME" ]]; then
    echo "PASS: home directory is not writable"
else
    # Home might be writable if it's part of the workspace or explicitly
    # bound.  This is a soft check — just log it.
    echo "INFO: home directory ($HOME) is writable (may be expected)"
fi

# ------------------------------------------------------------------
# 5. Network works
# ------------------------------------------------------------------
echo "--- 5. Network connectivity ---"
NET_OK=0
if command -v getent &>/dev/null; then
    if getent hosts google.com &>/dev/null; then
        echo "PASS: DNS resolution works (getent)"
        NET_OK=1
    fi
fi
if [[ $NET_OK -eq 0 ]] && command -v nslookup &>/dev/null; then
    if nslookup google.com &>/dev/null; then
        echo "PASS: DNS resolution works (nslookup)"
        NET_OK=1
    fi
fi
if [[ $NET_OK -eq 0 ]] && command -v curl &>/dev/null; then
    if curl -sSf --max-time 10 -o /dev/null https://httpbin.org/ip 2>/dev/null; then
        echo "PASS: HTTP connectivity works (curl)"
        NET_OK=1
    fi
fi
if [[ $NET_OK -eq 0 ]] && command -v python3 &>/dev/null; then
    if python3 -c "import urllib.request; urllib.request.urlopen('https://httpbin.org/ip', timeout=10)" 2>/dev/null; then
        echo "PASS: HTTP connectivity works (python3)"
        NET_OK=1
    fi
fi
if [[ $NET_OK -eq 0 ]]; then
    echo "FAIL: no network connectivity detected" >&2
    exit 1
fi

# ------------------------------------------------------------------
# 6. Docker socket not accessible
# ------------------------------------------------------------------
echo "--- 6. Docker socket not accessible ---"
# Check both common locations
DOCKER_SOCK_FOUND=0
for sock in /var/run/docker.sock /run/docker.sock; do
    if [[ -S "$sock" ]]; then
        echo "FAIL: $sock is accessible as a socket" >&2
        DOCKER_SOCK_FOUND=1
    elif [[ -e "$sock" ]]; then
        # It exists but isn't a socket (e.g. masked as /dev/null).
        echo "PASS: $sock is masked (exists as $(stat -c %F "$sock" 2>/dev/null || echo 'unknown'))"
    else
        echo "PASS: $sock does not exist"
    fi
done
if [[ $DOCKER_SOCK_FOUND -eq 1 ]]; then
    exit 1
fi

# ------------------------------------------------------------------
# 7. sudo fails
# ------------------------------------------------------------------
echo "--- 7. sudo fails ---"
if command -v sudo &>/dev/null; then
    if sudo -n true 2>/dev/null; then
        echo "FAIL: sudo succeeded (passwordless)" >&2
        exit 1
    else
        echo "PASS: sudo failed (as expected)"
    fi
else
    echo "PASS: sudo not available in sandbox"
fi

echo ""
echo "=== All smoke tests passed ==="
exit 0
