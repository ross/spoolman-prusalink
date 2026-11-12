# Shared by script/demo and script/integration; source it, don't run it.
# It needs .venv, so call it after script/bootstrap.

# The Spoolman image CI tests. compose.example.yaml repeats this version, so
# change it there too. SPOOLMAN_IMAGE overrides it.
spoolman_image=${SPOOLMAN_IMAGE:-ghcr.io/donkie/spoolman:0.27.1}

# CONTAINER_ENGINE picks podman or docker (default: podman if it's installed).
container_engine=${CONTAINER_ENGINE:-$(command -v podman >/dev/null && echo podman || echo docker)}

# wait_for URL WHAT: poll URL until it answers with a success, or exit after
# 120 seconds saying WHAT didn't come up.
wait_for() {
    .venv/bin/python - "$1" "$2" <<'PY'
import sys
import time

import httpx

url, what = sys.argv[1:]
deadline = time.monotonic() + 120
while True:
    try:
        if httpx.get(url, timeout=2).is_success:
            break
    except httpx.HTTPError:
        pass
    if time.monotonic() > deadline:
        sys.exit(f"{what} didn't come up within 120 seconds")
    time.sleep(1)
PY
}
