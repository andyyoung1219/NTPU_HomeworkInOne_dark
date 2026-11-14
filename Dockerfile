# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.7.21 AS uv
FROM docker.io/library/node:24-bookworm-slim AS node
FROM docker.io/library/docker:29-cli AS docker-cli

FROM docker.io/library/python:3.12-slim-bookworm AS build
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /opt/workbench
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY ntpu-ai-bridge/package*.json ntpu-ai-bridge/
RUN cd ntpu-ai-bridge \
    && node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --omit=dev --ignore-scripts --no-audit --no-fund

COPY docs/sources.lock.json docs/font.lock.json docs/
RUN python - <<'PY'
import json
import subprocess
from pathlib import Path

source = json.loads(Path("docs/sources.lock.json").read_text())["sources"]["NTPUOne"]
checkout = Path("/tmp/ntpu-upstream")
checkout.mkdir()
subprocess.run(["git", "init", "-q", str(checkout)], check=True)
subprocess.run(["git", "-C", str(checkout), "fetch", "--depth=1", source["url"], source["commit"]], check=True)
commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "FETCH_HEAD"], text=True).strip()
if commit != source["commit"]:
    raise SystemExit("NTPUOne commit mismatch")
target = Path(source["path"]) / "scripts/academic_probe.py"
target.parent.mkdir(parents=True)
target.write_bytes(subprocess.check_output(["git", "-C", str(checkout), "show", f"{commit}:scripts/academic_probe.py"]))
PY

COPY backend backend/
COPY scripts/setup_font.py scripts/
RUN .venv/bin/python scripts/setup_font.py

FROM docker.io/library/python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/workbench \
    PATH=/opt/workbench/.venv/bin:$PATH \
    NTPU_APP_DATA_DIR=/data NTPU_BIND_HOST=0.0.0.0 NTPU_EPHEMERAL_VAULT=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /opt/workbench

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates chromium fonts-noto-cjk gosu libgl1 libglib2.0-0 libgomp1 novnc openbox \
        util-linux websockify x11-utils x11vnc xauth xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 workbench \
    && useradd --uid 1000 --gid 1000 --create-home workbench \
    && install -d -m 0700 -o workbench -g workbench /data

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=build /opt/workbench/.venv .venv/
COPY --from=build /opt/workbench/.upstream .upstream/
COPY --from=build /opt/workbench/.local/fonts .local/fonts/
COPY --from=build /opt/workbench/backend backend/
COPY --from=build /opt/workbench/ntpu-ai-bridge/node_modules ntpu-ai-bridge/node_modules/
COPY ntpu-ai-bridge/src ntpu-ai-bridge/src/
COPY ntpu-ai-bridge/vendor ntpu-ai-bridge/vendor/
COPY frontend/static frontend/static/
COPY skills skills/
COPY LICENSES LICENSES/
COPY THIRD_PARTY_NOTICES.md ./
COPY scripts/docker-entrypoint.sh scripts/docker-desktop.sh scripts/
RUN sed -i 's/\r$//' scripts/docker-entrypoint.sh scripts/docker-desktop.sh

EXPOSE 8766 6080
ENTRYPOINT ["/bin/sh", "/opt/workbench/scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "backend.main"]
