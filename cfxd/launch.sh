#!/bin/sh
# Hook wrapper: find uv even when Claude Code was started from a Dock icon, Start menu or
# IDE (those don't read shell profiles, so uv's install dir is often missing from PATH),
# then run cfxd/launch.py. If uv really is missing, say so once instead of failing silently.
# Always exits 0: a hook must never break the session.
#
#   launch.sh session-start
#   launch.sh forward PreCompact

here=$(dirname "$0")

to_posix() {
    # Git Bash on Windows needs C:\Users\x as /c/Users/x; harmless elsewhere.
    if [ -n "$1" ] && command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$1" 2>/dev/null || printf '%s' "$1"
    else
        printf '%s' "$1"
    fi
}

# The state dir must match launch.py's resolve_state_dir so the cached interpreter
# path is read from where session-start writes it.
if [ -n "$CFX_HOME" ]; then
    state_dir="$(to_posix "$CFX_HOME")"
else
    state_dir="$(to_posix "${USERPROFILE:-$HOME}")/.cfx/daemon"
fi

# forward (the hot path) skips `uv run` startup when session-start has cached the
# resolved interpreter, so it runs before the PATH loop below (which only exists to
# find uv). Any problem with the cache falls through to uv, so a stale or missing
# cache can never break a hook.
if [ "$1" = "forward" ]; then
    py_file="$state_dir/python"
    if [ -f "$py_file" ]; then
        read -r py < "$py_file"
        py="$(to_posix "$py")"
        if [ -n "$py" ] && [ -f "$py" ] && [ -x "$py" ]; then
            "$py" "$here/launch.py" "$@"
            exit 0
        fi
    fi
fi

for d in \
    "$HOME/.local/bin" \
    "$XDG_BIN_HOME" \
    "$HOME/.cargo/bin" \
    "$(to_posix "$USERPROFILE")/.local/bin" \
    "$(to_posix "$LOCALAPPDATA")/Microsoft/WinGet/Links" \
    "$(to_posix "$USERPROFILE")/scoop/shims" \
    /opt/homebrew/bin \
    /usr/local/bin; do
    case "$d" in
        /*) ;;
        *) continue ;;  # skip empty or relative dirs (never the cwd)
    esac
    if [ -d "$d" ]; then
        PATH="${PATH:+$PATH:}$d"
    fi
done
export PATH

if ! command -v uv >/dev/null 2>&1; then
    if [ "$1" = "session-start" ]; then
        printf '%s\n' '{"systemMessage":"Context Fabric is installed but could not find uv. Install it from https://docs.astral.sh/uv/ and restart Claude Code."}'
    else
        printf '%s\n' '{}'
    fi
    exit 0
fi

uv run --no-config --no-project --quiet --python '>=3.12' "$here/launch.py" "$@"
exit 0
