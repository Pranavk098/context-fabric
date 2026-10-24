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
