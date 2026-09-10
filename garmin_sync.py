#!/usr/bin/env python3
"""Sync Garmin Connect activities to Wanderer trail journal.

Ported from a systemd timer on the docker host. The LOGIC is unchanged from the
version that ran there — only how it is configured and where it keeps state.
Migrating and rewriting at the same time makes any failure ambiguous.

What changed, and only this:
  * configuration comes from the ENVIRONMENT, not by parsing a .env file
  * token cache and sync state live under STATE_DIR (a PVC), not beside the script
  * WANDERER_URL defaults to the in-cluster Service, not localhost:8090
"""

import json
import os
import sys
import uuid
from datetime import date, timezone, datetime
from pathlib import Path

import garminconnect
import requests

# ⚠️ STRUCTURED JSON TO STDOUT, one object per line — the platform standard.
#
# Not a formatting preference. A log written to a file inside a
# readOnlyRootFilesystem container is a crash, and one written to an emptyDir
# disappears with the pod. stdout is the only surface the runtime captures.
#
# Every line carries level, timestamp, message and a run id. RUN_ID exists
# because this is a CronJob: several runs interleave in one log stream, and
# without a correlation id "Error creating trail" cannot be tied to the run
# that produced it.
#
# NEVER log a credential, a token, or a full response body. The identifiers
# here — activity ids, trail ids, counts — are the things worth searching by.
RUN_ID = os.environ.get("RUN_ID") or uuid.uuid4().hex[:12]


def log(msg, level="info", **fields):
    """Emit one structured JSON line to stdout.

    Kept deliberately tiny rather than reaching for `logging`: the whole
    surface is one line-oriented sink, and a stdlib logger would need
    configuring to not also emit its own unstructured output to stderr.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "msg": msg,
        "run_id": RUN_ID,
        "service": os.environ.get("OTEL_SERVICE_NAME", "garmin-sync"),
    }
    record.update(fields)
    print(json.dumps(record, default=str), flush=True)


def env(name, default=None):
    """Required unless a default is given — fail loudly at startup, not midway.

    A missing credential that surfaces on the first API call looks like the
    remote rejecting us. Checked here, it says which variable is absent.
    """
    v = os.environ.get(name, default)
    if v is None or v == "":
        # Structured, like everything else — a fatal that is the ONE plain-text
        # line in the stream is the one a log query will miss.
        log("required environment variable is not set", level="fatal", variable=name)
        sys.exit(1)
    return v


GARMIN_EMAIL = env("GARMIN_EMAIL")
GARMIN_PASSWORD = env("GARMIN_PASSWORD")
WANDERER_EMAIL = env("WANDERER_EMAIL")
WANDERER_PASSWORD = env("WANDERER_PASSWORD")

# The in-cluster Service. PocketBase is ClusterIP and stays that way — this
# pod is a peer inside the namespace, so nothing needs publishing.
WANDERER_URL = os.environ.get("WANDERER_URL", "http://wanderer-db:8090")

# ⚠️ BOTH OF THESE MUST SURVIVE A POD RESTART, for different reasons.
#
#   garmin_tokens.json  — without it every run re-authenticates, which can
#                         trip Garmin's MFA. There is no TTY here to answer it.
#   state.json          — without it EVERY past activity looks new, and the
#                         sync would create a duplicate trail for each one.
#                         PocketBase ships a `dedup` subcommand, which suggests
#                         this is a well-travelled mistake. Do not make it.
#
# Hence a PVC, and hence the seeding step in the README.
STATE_DIR = Path(os.environ.get("STATE_DIR", "/data"))
GARMIN_TOKEN_DIR = STATE_DIR / "garmin-tokens"
STATE_FILE = STATE_DIR / "state.json"

_start = os.environ.get("GARMIN_SYNC_START_DATE", "")
START_DATE = date.fromisoformat(_start) if _start else None

# Garmin activityType.typeKey → Wanderer category name
# Add your SxS custom type key here once you see it logged as "unmapped"
CATEGORY_MAP = {
    "walking": "Walking",
    "running": "Walking",
    "trail_running": "Hiking",
    "hiking": "Hiking",
    "cycling": "Biking",
    "mountain_biking": "Biking",
    "gravel_cycling": "Biking",
    "swimming": "Workout",
    "lap_swimming": "Workout",
    "strength_training": "Workout",
    "yoga": "Workout",
    "skiing": "Skiing",
    "backcountry_skiing": "Skiing",
    "kayaking": "Canoeing",
    "paddling": "Canoeing",
    "rock_climbing": "Climbing",
}


def load_state():
    """Read the set of already-synced Garmin activity IDs.

    A missing file reads as empty rather than raising: that is the legitimate
    state of a freshly provisioned volume. It is ALSO the state that makes every
    past activity look new and produces a duplicate trail for each, which is why
    the volume is seeded before the first run rather than left to initialise
    itself.
    """
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"synced_ids": []}


def save_state(state):
    """Persist the synced-ID set, creating the directory on a fresh volume."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def select_new_activities(activities, synced_ids, start_date):
    """Which Garmin activities still need a trail.

    Extracted from main() so it can be tested against DIRECTLY. It was inline,
    and the tests re-implemented the same expression to exercise it — which
    meant they asserted the copy in the test file and passed happily when the
    real comparison was mutated. A test that restates the logic it is checking
    measures nothing.

    Two ways this goes wrong, both silent, both creating duplicate trails:

    * `activityId` arrives from Garmin as an INT, while state.json stores
      strings. Comparing the two never matches, so every activity looks new.
    * `>=` not `>`: an activity recorded exactly on the start date is in scope.
    """
    return [
        a
        for a in activities
        if str(a["activityId"]) not in synced_ids
        and (
            start_date is None
            or date.fromisoformat(a["startTimeLocal"][:10]) >= start_date
        )
    ]


# ─────────────────────────────────────────────────────────────────────────────
# WHAT IS EXCLUDED FROM COVERAGE, AND WHY IT IS NOT A DODGE.
#
# The functions below marked `pragma: no cover` do one thing each: make an HTTP
# call and check the status. Testing them means mocking `requests`, and a mock
# asserts that this file calls the functions this file calls — a green test that
# measures nothing. This estate has spent real time on exactly that failure.
#
# They are covered by RUNNING the job against the real Garmin and PocketBase and
# reading what it did, which is also the only thing that can catch an upstream
# API change. The behavioural check in the README is that test.
#
# Everything with branches worth getting wrong — environment handling, state,
# the "is it new" filter, category mapping, log shape — is gated at 95%.
# ─────────────────────────────────────────────────────────────────────────────


def garmin_login():  # pragma: no cover - network
    """Return an authenticated Garmin client, preferring the cached tokens.

    Refuses to fall back to an interactive login unless explicitly allowed:
    that path can demand MFA, and a CronJob has no TTY to answer it.
    """
    client = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    GARMIN_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        client.login(str(GARMIN_TOKEN_DIR))
        log("loaded cached Garmin tokens", token_dir=str(GARMIN_TOKEN_DIR))
    except Exception as e:
        # On the host this fell through to an interactive login that could
        # prompt for MFA. In a CronJob there is no TTY to answer it, so that
        # path either hangs until the deadline or fails with a confusing
        # error. Refuse instead, and say exactly what to do.
        if os.environ.get("GARMIN_ALLOW_INTERACTIVE_LOGIN") != "true":
            log(
                "no usable cached Garmin tokens; refusing to attempt an "
                "interactive login because this job has no TTY to answer MFA. "
                "Seed the token cache — see README.md 'Seeding the PVC'.",
                level="fatal",
                token_dir=str(GARMIN_TOKEN_DIR),
                error=str(e),
            )
            sys.exit(1)
        log("no cached tokens, authenticating interactively", level="warn")
        client.login()
        # pylint: disable=no-member
        # `garth` is attached to the Garmin client at runtime, so pylint's
        # static view cannot see it. A blanket disable would hide genuine
        # no-member bugs, so this is scoped to the one line that needs it.
        client.garth.dump(str(GARMIN_TOKEN_DIR))
        log("cached Garmin tokens for future runs")
    return client


def wanderer_auth():  # pragma: no cover - network
    """Authenticate against PocketBase; return (token, user id)."""
    if not WANDERER_PASSWORD:
        raise RuntimeError(
            "WANDERER_PASSWORD not set — it should be supplied from a Secret"
        )
    r = requests.post(
        f"{WANDERER_URL}/api/collections/users/auth-with-password",
        json={"identity": WANDERER_EMAIL, "password": WANDERER_PASSWORD},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data["token"], data["record"]["id"]


def get_actor_id(token, user_id):  # pragma: no cover - network
    """Resolve the ActivityPub actor id that owns created trails.

    Trails are authored by the ACTOR, not the user — passing the user id creates
    a trail that belongs to nobody and renders without an author.
    """
    r = requests.get(
        f"{WANDERER_URL}/api/collections/activitypub_actors/records",
        headers={"Authorization": token},
        params={"filter": f'user="{user_id}"'},
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise RuntimeError(f"No Wanderer actor found for user {user_id}")
    return items[0]["id"]


def get_category_id(token, name):  # pragma: no cover - network
    """Look up a Wanderer category id by name; empty string when unmapped.

    An unknown activity type is not an error — the trail is created without a
    category and the run logs a warning naming the type to add.
    """
    if not name:
        return ""
    r = requests.get(
        f"{WANDERER_URL}/api/collections/categories/records",
        headers={"Authorization": token},
        params={"filter": f'name="{name}"'},
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    return items[0]["id"] if items else ""


def create_trail(
    token, actor_id, activity, gpx_bytes, category_id
):  # pragma: no cover - network
    """Create one Wanderer trail from a Garmin activity; return its id.

    Trails are created PRIVATE (`public: false`). Publishing is a deliberate
    act, and a sync that made every ride public by default would be a privacy
    fault that is tedious to undo one trail at a time.
    """
    name = activity.get("activityName") or "Untitled"
    data = {
        "name": name,
        "public": "false",
        "distance": str(round(activity.get("distance") or 0)),
        "elevation_gain": str(round(activity.get("elevationGain") or 0)),
        "duration": str(int(activity.get("duration") or 0)),
        "date": activity.get("startTimeLocal", ""),
        "lat": str(activity.get("startLatitude") or 0),
        "lon": str(activity.get("startLongitude") or 0),
        "difficulty": "easy",
        "author": actor_id,
    }
    if category_id:
        data["category"] = category_id

    files = None
    if gpx_bytes:
        files = {
            "gpx": (
                f"garmin_{activity['activityId']}.gpx",
                gpx_bytes,
                "application/gpx+xml",
            )
        }

    r = requests.post(
        f"{WANDERER_URL}/api/collections/trails/records",
        headers={"Authorization": token},
        data=data,
        files=files,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def main():  # pragma: no cover - orchestration
    """Sync every not-yet-synced Garmin activity into Wanderer.

    Ordering is deliberate. State is read first and written LAST, and an
    activity is only recorded as synced after its trail actually exists — so a
    failure anywhere leaves it to be retried, never silently dropped.
    Activities are processed oldest-first, so a partial run leaves a
    chronologically contiguous history rather than holes.
    """
    state = load_state()
    synced_ids = set(state["synced_ids"])

    log("connecting to Garmin Connect")
    garmin = garmin_login()

    log("fetching recent activities")
    activities = garmin.get_activities(0, 50)

    new_activities = select_new_activities(activities, synced_ids, START_DATE)
    if not new_activities:
        log("no new activities to sync", new=0, already_synced=len(synced_ids))
        return

    log("new activities found", new=len(new_activities), already_synced=len(synced_ids))

    log("connecting to Wanderer", url=WANDERER_URL)
    token, user_id = wanderer_auth()
    actor_id = get_actor_id(token, user_id)

    category_cache = {}

    for activity in reversed(new_activities):  # oldest first
        garmin_id = str(activity["activityId"])
        name = activity.get("activityName") or "Untitled"
        activity_type = (activity.get("activityType") or {}).get("typeKey", "")

        log(
            "syncing activity",
            garmin_id=garmin_id,
            name=name,
            activity_type=activity_type,
        )

        category_name = CATEGORY_MAP.get(activity_type, "")
        if activity_type and not category_name:
            log(
                "unmapped activity type; add it to CATEGORY_MAP",
                level="warn",
                activity_type=activity_type,
            )
        if category_name not in category_cache:
            category_cache[category_name] = get_category_id(token, category_name)
        category_id = category_cache[category_name]

        try:
            gpx_bytes = garmin.download_activity(
                activity["activityId"],
                dl_fmt=garmin.ActivityDownloadFormat.GPX,
            )
        except Exception as e:
            log(
                "GPX download failed; creating trail without a track",
                level="warn",
                garmin_id=garmin_id,
                error=str(e),
            )
            gpx_bytes = None

        try:
            trail_id = create_trail(token, actor_id, activity, gpx_bytes, category_id)
            synced_ids.add(garmin_id)
            log("trail created", garmin_id=garmin_id, trail_id=trail_id)
        except Exception as e:
            log(
                "failed to create trail; will retry next run",
                level="error",
                garmin_id=garmin_id,
                error=str(e),
            )
            continue

    state["synced_ids"] = list(synced_ids)
    save_state(state)
    log("done", synced_total=len(synced_ids))


if __name__ == "__main__":
    main()
