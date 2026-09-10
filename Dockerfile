# Garmin Connect -> Wanderer sync, as a container so it can run IN the cluster.
#
# WHY THIS REPO EXISTS. This ran as a systemd timer on the docker host, talking
# to PocketBase on `localhost:8090`. When Wanderer moved to Kubernetes that
# address stopped existing, and PocketBase became a ClusterIP Service —
# reachable only from inside the cluster.
#
# The alternative was to publish PocketBase's API so a host process could keep
# reaching it. That trades a real reduction in attack surface for a little
# saved work. Running the sync as a cluster peer instead means PocketBase
# gains NO new exposure at all.
#
# Base pinned by TAG AND DIGEST: the tag is what a human and Dependabot read,
# the digest is what actually gets pulled, so the build is reproducible even if
# the tag is moved.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
# Digest read from the registry on 2026-09-09, not transcribed from memory.
# Verify before changing it:
#   docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
#
# The host venv happened to run a different minor version — whatever the distro
# shipped, not a requirement. 3.13-slim is the deliberate choice; the script
# uses nothing version-specific.

# Installed as root into the system site-packages, then dropped. Nothing is
# installed at RUNTIME: a runtime `pip install` needs a writable filesystem and
# egress to PyPI, and produces a different artifact on every single run.
COPY requirements.txt /tmp/requirements.txt
RUN set -eu; \
    pip install --no-cache-dir -r /tmp/requirements.txt; \
    rm -f /tmp/requirements.txt; \
    python -c "import garminconnect, requests; print('imports OK')"

COPY garmin_sync.py /app/garmin_sync.py

# 65532 is the conventional nonroot UID. Match it in the pod's securityContext
# and fsGroup so the PVC this writes to is owned correctly.
USER 65532:65532

# No CMD arguments to restate: the CronJob does not override this, so upstream
# behaviour cannot go stale behind a hand-copied command line.
ENTRYPOINT ["python", "/app/garmin_sync.py"]
