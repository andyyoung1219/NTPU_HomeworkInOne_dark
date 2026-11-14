#!/bin/sh
set -eu

data=${NTPU_APP_DATA_DIR:-/data}
if [ "$(id -u)" -eq 0 ]; then
    chown workbench:workbench "$data"
    if [ -S /var/run/docker.sock ]; then
        socket_gid=$(stat -c '%g' /var/run/docker.sock)
        if ! getent group "$socket_gid" >/dev/null; then
            groupadd -g "$socket_gid" workbench-docker
        fi
        socket_group=$(getent group "$socket_gid" | cut -d: -f1)
        usermod -aG "$socket_group" workbench
    fi
    exec gosu workbench /bin/sh "$0" "$@"
fi

# Resolve the built Runner once. The sandbox only accepts the immutable ID.
if [ -n "${NTPU_RUNNER_IMAGE_TAG:-}" ]; then
    if image=$(docker image inspect "$NTPU_RUNNER_IMAGE_TAG" --format '{{.Id}}' 2>/dev/null); then
        python -c 'import json, pathlib, sys; path = pathlib.Path(sys.argv[1]); temp = path.with_suffix(".tmp"); temp.write_text(json.dumps({"image_id": sys.argv[2]}), encoding="utf-8"); temp.replace(path)' "$data/runner.json" "$image"
    else
        echo "Runner image unavailable. Build the runner-image service and restart the app." >&2
    fi
fi

. /opt/workbench/scripts/docker-desktop.sh
app_pid=
cleanup() {
    if [ -n "$app_pid" ]; then
        kill -TERM "$app_pid" 2>/dev/null || true
        wait "$app_pid" || true
    fi
    stop_login_desktop
}
trap cleanup EXIT
trap 'exit 0' TERM INT
start_login_desktop
"$@" &
app_pid=$!
wait "$app_pid"
