#!/bin/sh
# Sourced by the container entrypoint. Only noVNC is published by Compose.

login_desktop_ready() {
    python - <<'PY'
from socket import create_connection
from urllib.request import ProxyHandler, build_opener

try:
    with create_connection(("127.0.0.1", 5900), timeout=0.5) as vnc:
        if not vnc.recv(12).startswith(b"RFB "):
            raise SystemExit(1)
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:6080/vnc.html", timeout=0.5) as response:
        if response.status != 200:
            raise SystemExit(1)
except OSError:
    raise SystemExit(1) from None
PY
}

start_login_desktop() {
    export DISPLAY=:99
    export NTPU_AI_BRIDGE_STORAGE="${NTPU_APP_DATA_DIR:?}/browser"
    LOGIN_DESKTOP_RUNTIME="${TMPDIR:-/tmp}/ntpu-login-desktop"
    (umask 077 && mkdir -p -- "$LOGIN_DESKTOP_RUNTIME") || return 1
    chmod 700 -- "$LOGIN_DESKTOP_RUNTIME" || return 1
    export XAUTHORITY="$LOGIN_DESKTOP_RUNTIME/Xauthority"
    LOGIN_DESKTOP_PIDS=''
    mkdir -p -- "$NTPU_AI_BRIDGE_STORAGE" || return 1
    chmod 700 -- "$NTPU_AI_BRIDGE_STORAGE" || return 1

    # X11 is an authenticated Unix socket. Chromium inherits only the local display.
    touch -- "$XAUTHORITY" || return 1
    cookie=$(mcookie) || return 1
    xauth -f "$XAUTHORITY" add "$DISPLAY" MIT-MAGIC-COOKIE-1 "$cookie" >/dev/null || return 1
    Xvfb "$DISPLAY" -screen 0 1440x1000x24 -nolisten tcp -auth "$XAUTHORITY" \
        >"$LOGIN_DESKTOP_RUNTIME/xvfb.log" 2>&1 &
    display_pid=$!
    LOGIN_DESKTOP_PIDS="$display_pid"
    ready=0
    for retry in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
            ready=1
            break
        fi
        kill -0 "$display_pid" 2>/dev/null || break
        sleep 0.25
    done
    if [ "$ready" -ne 1 ]; then
        printf '%s\n' 'Unable to start the AI login desktop.' >&2
        return 1
    fi

    # Rotate the desktop code on restart. Only the application user can read it.
    # VNC's authentication protocol uses the first eight characters.
    access_code=$(python -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8)))') || return 1
    x11vnc -storepasswd "$access_code" "$LOGIN_DESKTOP_RUNTIME/vnc.pass" >/dev/null || return 1
    install -m 600 /dev/null "$NTPU_APP_DATA_DIR/login-desktop-password" || return 1
    printf '%s\n' "$access_code" >"$NTPU_APP_DATA_DIR/login-desktop-password" || return 1
    unset access_code cookie

    openbox >"$LOGIN_DESKTOP_RUNTIME/openbox.log" 2>&1 &
    LOGIN_DESKTOP_PIDS="$LOGIN_DESKTOP_PIDS $!"
    x11vnc -display "$DISPLAY" -auth "$XAUTHORITY" -localhost -rfbport 5900 \
        -rfbauth "$LOGIN_DESKTOP_RUNTIME/vnc.pass" -forever -noxdamage -noremote -nosel \
        >"$LOGIN_DESKTOP_RUNTIME/x11vnc.log" 2>&1 &
    LOGIN_DESKTOP_PIDS="$LOGIN_DESKTOP_PIDS $!"
    websockify --web=/usr/share/novnc --heartbeat=30 0.0.0.0:6080 127.0.0.1:5900 \
        >"$LOGIN_DESKTOP_RUNTIME/novnc.log" 2>&1 &
    LOGIN_DESKTOP_PIDS="$LOGIN_DESKTOP_PIDS $!"
    for retry in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        for pid in $LOGIN_DESKTOP_PIDS; do
            if ! kill -0 "$pid" 2>/dev/null; then
                printf '%s\n' 'A process for the AI login desktop stopped.' >&2
                return 1
            fi
        done
        if login_desktop_ready; then
            return 0
        fi
        sleep 0.25
    done
    printf '%s\n' 'Unable to start the noVNC login connection.' >&2
    return 1
}

stop_login_desktop() {
    for pid in ${LOGIN_DESKTOP_PIDS:-}; do
        kill "$pid" 2>/dev/null || true
    done
    if [ -n "${LOGIN_DESKTOP_PIDS:-}" ]; then
        rm -f -- "$NTPU_APP_DATA_DIR/login-desktop-password"
    fi
}
