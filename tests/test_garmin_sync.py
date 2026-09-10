"""Tests for the parts of garmin_sync that have branches worth getting wrong.

WHAT IS AND IS NOT TESTED, stated so nobody assumes coverage they do not have.

Tested: the pure logic — environment handling, state round-tripping, the
"which activities are new" filter, category mapping, and the shape of the log
records. These are where a silent wrong answer is possible: the filter in
particular decides whether a ride is created or skipped, and getting it wrong
in the permissive direction creates a duplicate trail for every past activity.

NOT tested: the Garmin and PocketBase HTTP calls. Mocking `requests` here would
assert that this file calls the functions this file calls — a green test that
measures nothing, which this estate has enough of. Those paths are covered by
running the job against the real services and reading what it did.
"""

import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REQUIRED = {
    "GARMIN_EMAIL": "rider@example.invalid",
    "GARMIN_PASSWORD": "pw",
    "WANDERER_EMAIL": "rider@example.invalid",
    "WANDERER_PASSWORD": "pw",
}


def load(monkeypatch, tmp_path, **extra):
    """Import the module fresh with a controlled environment.

    Config is read at import time, so every test needs its own import rather
    than a shared module object.
    """
    for k, v in {**REQUIRED, **extra}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    sys.modules.pop("garmin_sync", None)
    return importlib.import_module("garmin_sync")


# --------------------------------------------------------------- environment


def test_a_missing_credential_exits_nonzero_rather_than_running_partially(
    monkeypatch, tmp_path
):
    """A CronJob's alert IS its exit code. Exiting 0 here would be silent."""
    with pytest.raises(SystemExit) as e:
        load(monkeypatch, tmp_path, GARMIN_EMAIL=None)
    assert e.value.code == 1


def test_an_empty_credential_is_treated_as_missing_not_as_a_password(
    monkeypatch, tmp_path
):
    """An unset secret often arrives as "" — authenticating with it looks like
    a rejected password rather than a configuration fault."""
    with pytest.raises(SystemExit) as e:
        load(monkeypatch, tmp_path, WANDERER_PASSWORD="")
    assert e.value.code == 1


def test_wanderer_url_defaults_to_the_in_cluster_service(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    assert m.WANDERER_URL == "http://wanderer-db:8090"


def test_wanderer_url_is_overridable_so_the_image_is_not_cluster_specific(
    monkeypatch, tmp_path
):
    m = load(monkeypatch, tmp_path, WANDERER_URL="http://localhost:8090")
    assert m.WANDERER_URL == "http://localhost:8090"


def test_state_and_tokens_live_under_state_dir_so_a_pvc_can_hold_them(
    monkeypatch, tmp_path
):
    m = load(monkeypatch, tmp_path)
    assert m.STATE_FILE.parent == tmp_path
    assert m.GARMIN_TOKEN_DIR.parent == tmp_path


# --------------------------------------------------------------------- state


def test_missing_state_file_reads_as_empty_rather_than_crashing(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    assert m.load_state() == {"synced_ids": []}


def test_state_round_trips(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    m.save_state({"synced_ids": ["1", "2"]})
    assert m.load_state()["synced_ids"] == ["1", "2"]


def test_save_state_creates_its_directory_on_a_fresh_volume(monkeypatch, tmp_path):
    """A newly provisioned PVC is empty. Without the mkdir the very first run
    fails on the write, AFTER it has already created the trails."""
    nested = tmp_path / "not-yet"
    m = load(monkeypatch, nested)
    m.save_state({"synced_ids": ["1"]})
    assert m.load_state()["synced_ids"] == ["1"]


# ------------------------------------------------------- the "is it new" filter
#
# This is the one that creates duplicates if it goes wrong in the permissive
# direction, so it is tested from both sides.


def _filter(m, activities, synced_ids):
    """Call the REAL function.

    This helper used to re-implement the filter expression. Mutation testing
    caught it: flipping `>=` to `>` in garmin_sync.py left all 20 tests green,
    because they were exercising this copy. Never restate the logic under test.
    """
    return m.select_new_activities(activities, synced_ids, m.START_DATE)


ACTIVITIES = [
    {"activityId": 111, "startTimeLocal": "2026-08-01 09:00:00"},
    {"activityId": 222, "startTimeLocal": "2026-09-01 09:00:00"},
]


def test_an_already_synced_activity_is_not_resynced(monkeypatch, tmp_path):
    """THE duplicate-trail guard. Seeded state exists for exactly this."""
    m = load(monkeypatch, tmp_path)
    assert _filter(m, ACTIVITIES, {"111", "222"}) == []


def test_activity_ids_are_compared_as_STRINGS_not_ints(monkeypatch, tmp_path):
    """Garmin returns ints; state.json stores strings. Comparing an int against
    a set of strings never matches, so EVERY activity looks new and every one
    is duplicated — silently, on a run that reports success."""
    m = load(monkeypatch, tmp_path)
    assert _filter(m, ACTIVITIES, {111, 222}) == ACTIVITIES  # the bug's shape
    assert _filter(m, ACTIVITIES, {"111", "222"}) == []  # the correct call


def test_an_unsynced_activity_is_selected(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    assert _filter(m, ACTIVITIES, {"111"}) == [ACTIVITIES[1]]


def test_start_date_excludes_older_activities(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path, GARMIN_SYNC_START_DATE="2026-08-15")
    assert _filter(m, ACTIVITIES, set()) == [ACTIVITIES[1]]


def test_an_activity_exactly_on_the_start_date_is_included(monkeypatch, tmp_path):
    """Boundary: `>=`, not `>`. Off by one here silently drops a day."""
    m = load(monkeypatch, tmp_path, GARMIN_SYNC_START_DATE="2026-08-01")
    assert _filter(m, ACTIVITIES, set()) == ACTIVITIES


def test_no_start_date_means_no_date_filtering(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    assert m.START_DATE is None
    assert _filter(m, ACTIVITIES, set()) == ACTIVITIES


# ------------------------------------------------------------------ categories


def test_known_activity_types_map_to_categories(monkeypatch, tmp_path):
    m = load(monkeypatch, tmp_path)
    assert m.CATEGORY_MAP["hiking"] == "Hiking"
    assert m.CATEGORY_MAP["mountain_biking"] == "Biking"


def test_an_unknown_type_maps_to_empty_and_is_not_an_error(monkeypatch, tmp_path):
    """A new Garmin type must not stop the sync — the trail is created without
    a category and the run logs a warning."""
    m = load(monkeypatch, tmp_path)
    assert m.CATEGORY_MAP.get("side_by_side_utv", "") == ""


# ---------------------------------------------------------------------- logging


def test_every_log_line_is_one_parseable_json_object(monkeypatch, tmp_path, capsys):
    m = load(monkeypatch, tmp_path)
    m.log("hello", garmin_id="123")
    line = capsys.readouterr().out.strip()
    assert json.loads(line)["msg"] == "hello"


def test_log_records_carry_the_fields_a_query_needs(monkeypatch, tmp_path, capsys):
    m = load(monkeypatch, tmp_path)
    m.log("hello")
    rec = json.loads(capsys.readouterr().out.strip())
    for field in ("ts", "level", "msg", "run_id", "service"):
        assert field in rec, f"log record is missing {field}"


def test_a_run_id_correlates_lines_from_one_run(monkeypatch, tmp_path, capsys):
    """Several CronJob runs interleave in one stream. Without this, an error
    cannot be tied to the run that produced it."""
    m = load(monkeypatch, tmp_path)
    m.log("one")
    m.log("two")
    ids = {
        json.loads(l)["run_id"] for l in capsys.readouterr().out.strip().splitlines()
    }
    assert len(ids) == 1


def test_non_serialisable_values_do_not_crash_the_logger(monkeypatch, tmp_path, capsys):
    """Logging must never be the thing that fails a run. `default=str` is why."""
    m = load(monkeypatch, tmp_path)
    m.log("boom", error=ValueError("nope"), when=date(2026, 9, 9))
    assert json.loads(capsys.readouterr().out.strip())["when"] == "2026-09-09"
