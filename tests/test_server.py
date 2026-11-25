"""Tests for server.py. Run from the project folder:  python -m unittest discover -s tests -v"""
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

HOUR = 3600
C1 = 1   # the client every fresh database starts with


def ts(*args) -> float:
    """Local datetime -> epoch seconds."""
    return datetime(*args).timestamp()


def find_dst_day():
    """Return a date in the local time zone that is not 24 h long, or None."""
    d = date.today()
    for _ in range(800):
        length = server.local_midnight(d + timedelta(days=1)) - server.local_midnight(d)
        if length != 86400:
            return d, length
        d += timedelta(days=1)
    return None


class DbTestCase(unittest.TestCase):
    """Gives each test a fresh SQLite file with one client (id 1, 8 h/day)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = server.DB_PATH
        server.DB_PATH = os.path.join(self.tmp.name, "test.db")
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self._old_db
        self.tmp.cleanup()

    def summary(self, month, client_id=C1):
        with server.db() as con:
            return server.month_summary(con, month, client_id)

    def add(self, start, end, client_id=C1, activity=""):
        return server.api_add_entry({"client_id": client_id, "start": start, "end": end, "activity": activity})

    def new_client(self, name, daily_hours=8):
        return server.api_create_client({"name": name, "daily_hours": daily_hours})["id"]

    def insert_raw(self, start, end, client_id=C1, activity=""):
        """Insert without validation, e.g. to simulate old or corrupted data."""
        with server.db() as con:
            con.execute("INSERT INTO entries (client_id, start_ts, end_ts, activity) VALUES (?, ?, ?, ?)",
                        (client_id, start, end, activity))

    def assertApiError(self, status, fn, *args, **kwargs):
        with self.assertRaises(server.ApiError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.status, status, cm.exception.message)
        return cm.exception


# ------------------------------------------------------------------ pure logic
class SplitByDayTest(unittest.TestCase):
    def test_within_one_day(self):
        self.assertEqual(list(server.split_by_day(ts(2026, 11, 2, 9), ts(2026, 11, 2, 17))),
                         [(date(2026, 11, 2), 8 * HOUR)])

    def test_crosses_midnight(self):
        self.assertEqual(list(server.split_by_day(ts(2026, 11, 2, 22), ts(2026, 11, 3, 2))),
                         [(date(2026, 11, 2), 2 * HOUR), (date(2026, 11, 3), 2 * HOUR)])

    def test_empty_interval(self):
        self.assertEqual(list(server.split_by_day(ts(2026, 11, 2, 9), ts(2026, 11, 2, 9))), [])

    def test_daylight_saving_day_counts_real_hours(self):
        found = find_dst_day()
        if not found:
            self.skipTest("local time zone has no daylight-saving changes")
        d, length = found
        parts = list(server.split_by_day(server.local_midnight(d), server.local_midnight(d + timedelta(days=1))))
        self.assertEqual(parts, [(d, length)])


class DailyHoursTest(unittest.TestCase):
    HISTORY = [("1970-01-01", 8.0), ("2026-11-01", 4.0), ("2027-01-01", 6.0)]

    def test_picks_last_setting_on_or_before_day(self):
        self.assertEqual(server.daily_hours_for(self.HISTORY, date(2026, 10, 31)), 8.0)
        self.assertEqual(server.daily_hours_for(self.HISTORY, date(2026, 11, 1)), 4.0)
        self.assertEqual(server.daily_hours_for(self.HISTORY, date(2026, 12, 31)), 4.0)
        self.assertEqual(server.daily_hours_for(self.HISTORY, date(2027, 3, 1)), 6.0)

    def test_empty_history_means_no_target(self):
        self.assertEqual(server.daily_hours_for([], date(2026, 11, 2)), 0.0)


class ParseMonthTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(server.parse_month("2026-12"), (date(2026, 12, 1), date(2027, 1, 1)))
        self.assertEqual(server.parse_month("2026-2"), (date(2026, 2, 1), date(2026, 3, 1)))

    def test_invalid(self):
        for bad in ["", "2026", "2026-13", "2026-00", "abc", "2026-11-05", "9999-12"]:
            with self.subTest(bad=bad), self.assertRaises(server.ApiError):
                server.parse_month(bad)


# ------------------------------------------------------------------ database
class InitDbTest(DbTestCase):
    def test_fresh_database_has_one_client_with_default_hours(self):
        with server.db() as con:
            self.assertEqual(server.list_clients(con), [{"id": C1, "name": "Client 1", "archived": False}])
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], server.SCHEMA_VERSION)
        self.assertEqual(self.summary("2026-11")["daily_hours"], 8.0)

    def test_init_is_idempotent(self):
        server.init_db()
        with server.db() as con:
            self.assertEqual(len(server.list_clients(con)), 1)

    def test_newer_database_is_refused_and_left_alone(self):
        with server.db() as con:
            con.execute(f"PRAGMA user_version = {server.SCHEMA_VERSION + 1}")
        with self.assertRaises(RuntimeError):
            server.init_db()
        with server.db() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], server.SCHEMA_VERSION + 1)


class MigrationTest(unittest.TestCase):
    """A database from before clients existed is upgraded in place, with a backup copy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = server.DB_PATH
        server.DB_PATH = os.path.join(self.tmp.name, "timetracker.db")
        con = sqlite3.connect(server.DB_PATH)
        con.executescript("""
            CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts REAL NOT NULL,
                                  end_ts REAL, activity TEXT NOT NULL DEFAULT '');
            CREATE TABLE settings (effective_from TEXT PRIMARY KEY, daily_hours REAL NOT NULL);
            INSERT INTO settings VALUES ('1970-01-01', 8), ('2026-11-01', 4);
        """)
        con.execute("INSERT INTO entries (start_ts, end_ts, activity) VALUES (?, ?, 'old work')",
                    (ts(2026, 11, 2, 9), ts(2026, 11, 2, 13)))
        con.execute("INSERT INTO entries (start_ts, end_ts) VALUES (?, NULL)", (time.time() - 600,))
        con.commit()
        con.close()

    def tearDown(self):
        server.DB_PATH = self._old_db
        self.tmp.cleanup()

    def test_upgrade_keeps_everything_under_first_client(self):
        server.init_db()
        server.init_db()   # running again must change nothing
        with server.db() as con:
            self.assertEqual(server.list_clients(con), [{"id": 1, "name": "Client 1", "archived": False}])
            s = server.month_summary(con, "2026-11", 1)
            running = server.get_running(con)
        self.assertEqual(s["days"][1]["worked"], 4 * HOUR)        # Mon 2 Nov
        self.assertEqual(s["days"][1]["target"], 4 * HOUR)        # settings history kept
        self.assertEqual(s["entries"][0]["activity"], "old work")
        self.assertEqual(running["client_id"], 1)                 # a running timer keeps running

    def test_backup_copy_is_the_old_database(self):
        server.init_db()
        backup = sqlite3.connect(server.backup_path(server.DB_PATH))
        self.assertEqual(backup.execute("SELECT count(*) FROM entries").fetchone()[0], 2)
        self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
        backup.close()

    def test_failed_upgrade_changes_nothing(self):
        original = server.MIGRATE_1_TO_2
        server.MIGRATE_1_TO_2 = original + "\nSELECT * FROM no_such_table;"
        try:
            with self.assertRaises(sqlite3.OperationalError):
                server.init_db()
        finally:
            server.MIGRATE_1_TO_2 = original
        con = sqlite3.connect(server.DB_PATH)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("clients", tables)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)
        columns = [r[1] for r in con.execute("PRAGMA table_info(entries)")]
        self.assertNotIn("client_id", columns)
        self.assertEqual(con.execute("SELECT count(*), sum(end_ts IS NULL) FROM entries").fetchone(), (2, 1))
        self.assertEqual(con.execute("SELECT count(*) FROM settings").fetchone()[0], 2)
        con.close()

    def test_old_database_without_settings_gets_default_hours(self):
        with closing(sqlite3.connect(server.DB_PATH)) as con, con:
            con.execute("DELETE FROM settings")
        server.init_db()
        with server.db() as con:
            self.assertEqual(server.month_summary(con, "2026-11", 1)["daily_hours"], server.DEFAULT_DAILY_HOURS)


# ------------------------------------------------------------- month summary
class MonthSummaryTest(DbTestCase):
    def test_targets_follow_weekdays_and_settings_history(self):
        server.api_put_settings({"client_id": C1, "daily_hours": 6, "effective_from": "2026-11-16"})
        s = self.summary("2026-11")
        by_date = {d["date"]: d for d in s["days"]}
        self.assertEqual(by_date["2026-11-13"]["target"], 8 * HOUR)   # Friday, old setting
        self.assertEqual(by_date["2026-11-14"]["target"], 0)          # Saturday
        self.assertEqual(by_date["2026-11-16"]["target"], 6 * HOUR)   # Monday, new setting
        # November 2026: 10 workdays at 8 h before the 16th, 11 workdays at 6 h from the 16th.
        self.assertEqual(s["target"], (10 * 8 + 11 * 6) * HOUR)

    def test_entry_across_month_boundary_is_split(self):
        self.insert_raw(ts(2026, 10, 31, 22), ts(2026, 11, 1, 3))
        self.assertEqual(self.summary("2026-10")["worked"], 2 * HOUR)
        self.assertEqual(self.summary("2026-11")["worked"], 3 * HOUR)

    def test_weekend_work_counts_without_target(self):
        self.insert_raw(ts(2026, 11, 14, 10), ts(2026, 11, 14, 12))   # Saturday
        day = next(d for d in self.summary("2026-11")["days"] if d["date"] == "2026-11-14")
        self.assertEqual((day["worked"], day["target"]), (2 * HOUR, 0))

    def test_past_month_has_no_remaining_workdays(self):
        s = self.summary("2001-01")
        self.assertEqual(s["remaining_workdays"], 0)
        self.assertIsNone(s["per_remaining_workday"])
        self.assertEqual(s["missing"], s["target"])

    def test_running_entry_counts_until_now(self):
        server.api_toggle({"client_id": C1})
        with server.db() as con:
            con.execute("UPDATE entries SET start_ts = start_ts - 3600")
        s = self.summary(datetime.now().strftime("%Y-%m"))
        self.assertIsNotNone(s["running"])
        self.assertAlmostEqual(s["worked"], HOUR, delta=5)

    def test_very_long_entry_does_not_break_summary(self):
        # Regression: an entry ending in the year ~33000 used to make every summary fail.
        self.insert_raw(ts(2026, 1, 1), 1e12)
        s = self.summary("2026-11")
        self.assertEqual(s["worked"], server.local_midnight(date(2026, 12, 1)) - server.local_midnight(date(2026, 11, 1)))

    def test_unknown_client(self):
        self.assertApiError(404, self.summary, "2026-11", 999)


# ------------------------------------------------------------------- clients
class ClientsTest(DbTestCase):
    def test_create_rename_archive_restore(self):
        cid = self.new_client("  Acme  ", 6)
        self.assertEqual(server.api_patch_client(cid, {"name": "Acme Ltd"})["name"], "Acme Ltd")
        self.assertTrue(server.api_patch_client(cid, {"archived": True})["archived"])
        self.assertEqual([c["name"] for c in server.api_list_clients()], ["Client 1", "Acme Ltd"])   # archived last
        self.assertFalse(server.api_patch_client(cid, {"archived": False})["archived"])
        self.assertEqual(self.summary("2026-11", cid)["daily_hours"], 6.0)

    def test_invalid_names_and_duplicates(self):
        self.new_client("Acme")
        self.assertApiError(409, self.new_client, "acme")              # case-insensitive
        self.assertApiError(409, server.api_patch_client, C1, {"name": "ACME"})
        for bad in ["", "   ", "x" * 61, None, 5]:
            with self.subTest(bad=bad):
                self.assertApiError(400, server.api_create_client, {"name": bad})
        self.assertApiError(400, server.api_create_client, {"name": "B", "daily_hours": 0})
        self.assertApiError(400, server.api_patch_client, C1, {"archived": "yes"})
        self.assertApiError(404, server.api_patch_client, 999, {"name": "X"})

    def test_archived_client_cannot_get_new_time(self):
        cid = self.new_client("Old")
        server.api_patch_client(cid, {"archived": True})
        self.assertApiError(400, server.api_toggle, {"client_id": cid})
        self.assertApiError(400, self.add, ts(2026, 11, 2, 9), ts(2026, 11, 2, 10), cid)
        e = self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 10))
        self.assertApiError(400, server.api_patch_entry, e["id"], {"client_id": cid})

    def test_archived_clients_entries_can_still_be_corrected(self):
        cid = self.new_client("Old")
        e = self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 10), cid)
        server.api_patch_client(cid, {"archived": True})
        fixed = server.api_patch_entry(e["id"], {"client_id": cid, "activity": "fixed typo"})   # same client: fine
        self.assertEqual((fixed["client_id"], fixed["activity"]), (cid, "fixed typo"))
        self.assertEqual(server.api_patch_entry(e["id"], {"client_id": C1})["client_id"], C1)   # move out: fine

    def test_default_client_follows_list_order(self):
        z, a = self.new_client("Zeta"), self.new_client("Alpha")
        self.assertEqual(server.api_summary({})["client"]["id"], a)        # alphabetical, like the list
        for cid in (C1, z, a):
            server.api_patch_client(cid, {"archived": True})
        self.assertEqual(server.api_summary({})["client"]["id"], a)        # all archived: still works

    def test_cannot_archive_while_its_timer_runs(self):
        server.api_toggle({"client_id": C1})
        self.assertApiError(400, server.api_patch_client, C1, {"archived": True})

    def test_data_is_separate_per_client(self):
        b = self.new_client("B", 4)
        self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 12))             # client 1: 3 h
        self.add(ts(2026, 11, 2, 13), ts(2026, 11, 2, 18), b)         # client B: 5 h
        server.api_put_settings({"client_id": b, "daily_hours": 2, "effective_from": "2026-11-16"})
        s1, sb = self.summary("2026-11"), self.summary("2026-11", b)
        self.assertEqual((s1["worked"], sb["worked"]), (3 * HOUR, 5 * HOUR))
        self.assertEqual([e["client_id"] for e in sb["entries"]], [b])
        self.assertEqual(s1["target"], 21 * 8 * HOUR)                 # client 1 unaffected by B's settings
        self.assertEqual(sb["target"], (10 * 4 + 11 * 2) * HOUR)

    def test_time_cannot_overlap_across_clients(self):
        b = self.new_client("B")
        self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 12))
        err = self.assertApiError(409, self.add, ts(2026, 11, 2, 11), ts(2026, 11, 2, 13), b)
        self.assertIn("Client 1", err.message)
        self.add(ts(2026, 11, 2, 12), ts(2026, 11, 2, 13), b)         # touching is fine

    def test_move_entry_to_other_client(self):
        b = self.new_client("B")
        e = self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 12))
        self.assertEqual(server.api_patch_entry(e["id"], {"client_id": b})["client_id"], b)
        self.assertEqual(self.summary("2026-11")["worked"], 0)
        self.assertEqual(self.summary("2026-11", b)["worked"], 3 * HOUR)


# ------------------------------------------------------------------ endpoints
class AddEntryTest(DbTestCase):
    def test_add_and_reject_invalid(self):
        e = self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 17), activity="  work  ")
        self.assertEqual((e["activity"], e["client_id"]), ("work", C1))
        base = {"client_id": C1}
        cases = {
            "end before start": {"start": ts(2026, 11, 3, 9), "end": ts(2026, 11, 3, 8)},
            "NaN": {"start": float("nan"), "end": ts(2026, 11, 3, 9)},
            "infinity": {"start": ts(2026, 11, 3, 9), "end": float("inf")},
            "string": {"start": "9:00", "end": ts(2026, 11, 3, 9)},
            "bool": {"start": True, "end": ts(2026, 11, 3, 9)},
            "year 1990": {"start": ts(1990, 1, 1, 9), "end": ts(1990, 1, 1, 10)},
            "year 3000": {"start": ts(2026, 11, 3, 9), "end": 1e12},
            "too long": {"start": ts(2026, 11, 3, 9), "end": ts(2026, 11, 11, 9)},
            "activity too long": {"start": ts(2026, 11, 3, 9), "end": ts(2026, 11, 3, 10), "activity": "x" * 201},
            "no client": {"client_id": None, "start": ts(2026, 11, 3, 9), "end": ts(2026, 11, 3, 10)},
            "client as text": {"client_id": "1", "start": ts(2026, 11, 3, 9), "end": ts(2026, 11, 3, 10)},
            "huge client id": {"client_id": 2**70, "start": ts(2026, 11, 3, 9), "end": ts(2026, 11, 3, 10)},
            "huge integer": {"start": 10**400, "end": ts(2026, 11, 3, 9)},   # float() would overflow
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertApiError(400, server.api_add_entry, {**base, **body})
        self.assertApiError(404, self.add, ts(2026, 11, 3, 9), ts(2026, 11, 3, 10), 999)

    def test_overlap_rejected_but_touching_allowed(self):
        self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 12))
        self.add(ts(2026, 11, 2, 12), ts(2026, 11, 2, 13))   # touches
        self.assertApiError(409, self.add, ts(2026, 11, 2, 11), ts(2026, 11, 2, 14))
        self.assertApiError(409, self.add, ts(2026, 11, 2, 8), ts(2026, 11, 2, 18))


class ToggleTest(DbTestCase):
    def test_start_stop_cycle(self):
        r = server.api_toggle({"client_id": C1, "activity": "coding"})
        self.assertIsNone(r["stopped"])
        started = r["started"]
        self.assertIsNone(started["end"])
        r = server.api_toggle({"client_id": C1})
        self.assertIsNone(r["started"])
        self.assertEqual(r["stopped"]["id"], started["id"])
        self.assertEqual(r["stopped"]["activity"], "coding")   # empty activity keeps the old one
        self.assertNotEqual(server.api_toggle({"client_id": C1})["started"]["id"], started["id"])

    def test_stop_can_set_activity(self):
        server.api_toggle({"client_id": C1})
        self.assertEqual(server.api_toggle({"client_id": C1, "activity": "review"})["stopped"]["activity"], "review")

    def test_stop_without_client_stops_whatever_runs(self):
        server.api_toggle({"client_id": C1})
        self.assertIsNotNone(server.api_toggle({})["stopped"])
        self.assertApiError(400, server.api_toggle, {})               # nothing runs: a client is needed

    def test_switch_to_other_client(self):
        b = self.new_client("B")
        first = server.api_toggle({"client_id": C1, "activity": "for A"})["started"]
        r = server.api_toggle({"client_id": b, "activity": "for B"})
        self.assertEqual(r["stopped"]["id"], first["id"])
        self.assertEqual(r["stopped"]["activity"], "for A")           # the new text goes to the new entry
        self.assertEqual((r["started"]["client_id"], r["started"]["activity"]), (b, "for B"))
        self.assertEqual(r["started"]["start"], r["stopped"]["end"])  # seamless hand-over, no overlap
        with server.db() as con:
            self.assertEqual(server.get_running(con)["client_id"], b)

    def test_only_one_running_entry(self):
        server.api_toggle({"client_id": C1})
        with server.db() as con:
            self.assertApiError(409, server.check_overlap, con, time.time(), None)

    def test_click_from_outdated_page_is_refused(self):
        # Tab 1 shows A running; tab 2 switched to B. "Stop" in tab 1 must not start A again.
        b = self.new_client("B")
        a_run = server.api_toggle({"client_id": C1, "running_id": None})["started"]
        b_run = server.api_toggle({"client_id": b, "running_id": a_run["id"]})["started"]
        self.assertApiError(409, server.api_toggle, {"client_id": C1, "running_id": a_run["id"]})
        with server.db() as con:
            self.assertEqual(server.get_running(con)["id"], b_run["id"])   # unchanged

    def test_double_click_does_not_undo_itself(self):
        started = server.api_toggle({"client_id": C1, "running_id": None})["started"]
        self.assertApiError(409, server.api_toggle, {"client_id": C1, "running_id": None})
        with server.db() as con:
            self.assertEqual(server.get_running(con)["id"], started["id"])

    def test_failed_switch_keeps_old_timer_running(self):
        b = self.new_client("B")
        a_run = server.api_toggle({"client_id": C1})["started"]
        original = server.check_overlap

        def fail(*args):
            raise server.ApiError(409, "simulated overlap")

        server.check_overlap = fail   # make the start half of the switch fail after A was stopped
        try:
            self.assertApiError(409, server.api_toggle, {"client_id": b})
        finally:
            server.check_overlap = original
        with server.db() as con:
            run = server.get_running(con)
        self.assertEqual((run["id"], run["end_ts"]), (a_run["id"], None))

    def test_running_entry_can_move_to_other_client(self):
        b = self.new_client("B")
        run = server.api_toggle({"client_id": C1})["started"]
        moved = server.api_patch_entry(run["id"], {"client_id": b})
        self.assertEqual((moved["client_id"], moved["end"]), (b, None))


class PatchEntryTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.e1 = self.add(ts(2026, 11, 2, 9), ts(2026, 11, 2, 12))
        self.e2 = self.add(ts(2026, 11, 2, 13), ts(2026, 11, 2, 17))

    def test_partial_update_keeps_other_fields(self):
        e = server.api_patch_entry(self.e1["id"], {"activity": "meeting"})
        self.assertEqual((e["start"], e["end"], e["activity"]), (self.e1["start"], self.e1["end"], "meeting"))

    def test_moving_within_own_slot_is_not_an_overlap(self):
        e = server.api_patch_entry(self.e1["id"], {"end": ts(2026, 11, 2, 13)})
        self.assertEqual(e["end"], ts(2026, 11, 2, 13))

    def test_overlap_and_missing(self):
        self.assertApiError(409, server.api_patch_entry, self.e1["id"], {"end": ts(2026, 11, 2, 14)})
        self.assertApiError(404, server.api_patch_entry, 9999, {"activity": "x"})
        self.assertApiError(400, server.api_patch_entry, self.e1["id"], {"end": ts(2026, 11, 2, 8)})
        self.assertApiError(404, server.api_patch_entry, self.e1["id"], {"client_id": 999})

    def test_running_entry_rules(self):
        run = server.api_toggle({"client_id": C1})["started"]
        self.assertApiError(400, server.api_patch_entry, run["id"], {"end": time.time() + 60})
        self.assertApiError(400, server.api_patch_entry, run["id"], {"start": time.time() + 3600})
        moved = server.api_patch_entry(run["id"], {"start": time.time() - 600})
        self.assertIsNone(moved["end"])
        # Moving the start years back would create a huge entry on stop.
        self.assertApiError(400, server.api_patch_entry, run["id"], {"start": time.time() - 8 * 86400})

    def test_long_stopped_timer_can_still_be_edited(self):
        # A timer forgotten over a holiday and stopped after 8 days is longer than manual entries
        # may be; renaming it or moving it to another client must still work.
        b = self.new_client("B")
        with server.db() as con:
            eid = con.execute("INSERT INTO entries (client_id, start_ts, end_ts) VALUES (?, ?, ?)",
                              (C1, ts(2026, 10, 1, 9), ts(2026, 10, 9, 9))).lastrowid
        self.assertEqual(server.api_patch_entry(eid, {"activity": "holiday"})["activity"], "holiday")
        self.assertEqual(server.api_patch_entry(eid, {"client_id": b})["client_id"], b)
        self.assertApiError(400, server.api_patch_entry, eid, {"end": ts(2026, 10, 9, 10)})   # changing times is checked

    def test_delete(self):
        server.api_delete_entry(self.e1["id"])
        self.assertEqual([e["id"] for e in self.summary("2026-11")["entries"]], [self.e2["id"]])


class SettingsTest(DbTestCase):
    def put(self, **body):
        return server.api_put_settings({"client_id": C1, **body})

    def test_new_value_applies_from_date(self):
        self.put(daily_hours=6, effective_from="2026-12-01")
        self.assertEqual(self.summary("2026-12")["days"][0]["target"], 6 * HOUR)     # Tue 1 Dec
        self.assertEqual(self.summary("2026-11")["days"][1]["target"], 8 * HOUR)     # Mon 2 Nov unchanged

    def test_rejects_non_canonical_and_invalid_dates(self):
        # Regression: '20261201' used to be stored as-is and then ignored by string comparison.
        for bad in ["20261201", "2026-W49-1", "2026-02-30", "01.12.2026", 20261201]:
            with self.subTest(bad=bad):
                self.assertApiError(400, self.put, daily_hours=6, effective_from=bad)

    def test_rejects_bad_hours(self):
        for bad in [0, -1, 25, None, "8", float("nan"), True, 10**400]:
            with self.subTest(bad=bad):
                self.assertApiError(400, self.put, daily_hours=bad)

    def test_requires_existing_client(self):
        self.assertApiError(400, server.api_put_settings, {"daily_hours": 6})
        self.assertApiError(404, server.api_put_settings, {"client_id": 999, "daily_hours": 6})


# ------------------------------------------------------------------ HTTP layer
class HttpTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.srv = server.make_server(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None, raw=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"localhost:{self.port}"}
        if body is not None:
            raw = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        con.request(method, path, body=raw, headers=h)
        r = con.getresponse()
        data = r.read()
        con.close()
        return r, data

    def test_index_has_security_headers(self):
        r, data = self.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"<title>TimeTracker</title>", data)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_vendor_files_and_traversal(self):
        r, _ = self.request("GET", "/vendor/flatpickr/flatpickr.min.js")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/javascript"))
        for path in ["/vendor/../server.py", "/vendor/%2e%2e/server.py", "/vendor/missing.js", "/server.py"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 404)

    def test_foreign_host_is_rejected(self):   # DNS rebinding
        r, _ = self.request("GET", "/api/summary", headers={"Host": "evil.example:8765"})
        self.assertEqual(r.status, 403)

    def test_ipv6_localhost_host_is_accepted(self):
        r, _ = self.request("GET", "/", headers={"Host": f"[::1]:{self.port}"})
        self.assertEqual(r.status, 200)

    def test_foreign_origin_cannot_write(self):
        body = {"client_id": C1}
        for method, path in [("POST", "/api/toggle"), ("POST", "/api/clients"), ("PATCH", "/api/clients/1")]:
            with self.subTest(path=path):
                r, _ = self.request(method, path, {"name": "X", **body}, headers={"Origin": "https://evil.example"})
                self.assertEqual(r.status, 403)
        r, _ = self.request("POST", "/api/toggle", body, headers={"Origin": "null"})   # sandboxed iframe, file://
        self.assertEqual(r.status, 403)
        r, _ = self.request("POST", "/api/toggle", body, headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(r.status, 200)
        r, _ = self.request("POST", "/api/toggle", body)   # no Origin header (curl, scripts) is allowed
        self.assertEqual(r.status, 200)

    def test_missing_host_is_rejected(self):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        con.putrequest("GET", "/api/summary", skip_host=True)
        con.endheaders()
        r = con.getresponse()
        r.read()
        con.close()
        self.assertEqual(r.status, 403)

    def test_writes_require_json(self):   # CSRF: HTML forms cannot send application/json
        for method, path in [("POST", "/api/toggle"), ("DELETE", "/api/entries/1"), ("PUT", "/api/settings"),
                             ("POST", "/api/clients"), ("PATCH", "/api/clients/1")]:
            with self.subTest(method=method, path=path):
                r, _ = self.request(method, path, raw=b"a=b",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(r.status, 415)

    def test_bad_bodies(self):
        cases = [
            (b"{not json", {}, 400),
            (b'{"start": NaN, "end": 1}', {}, 400),
            (b"[1, 2]", {}, 400),
            (b"{}", {"Content-Length": "abc"}, 400),
            # Only the header: the server must refuse before reading the body.
            (None, {"Content-Length": str(server.MAX_BODY_BYTES + 1)}, 413),
        ]
        for raw, extra, status in cases:
            with self.subTest(raw=raw and raw[:20], extra=extra):
                h = {"Content-Type": "application/json", **extra}
                r, _ = self.request("POST", "/api/entries", raw=raw, headers=h)
                self.assertEqual(r.status, status)

    def test_api_round_trip(self):
        r, data = self.request("POST", "/api/clients", {"name": "Acme", "daily_hours": 6})
        self.assertEqual(r.status, 201)
        acme = json.loads(data)["id"]
        r, data = self.request("POST", "/api/entries", {"client_id": acme, "start": ts(2026, 11, 2, 9),
                                                        "end": ts(2026, 11, 2, 17), "activity": "work"})
        self.assertEqual(r.status, 201)
        eid = json.loads(data)["id"]
        r, data = self.request("GET", f"/api/summary?month=2026-11&client={acme}")
        s = json.loads(data)
        self.assertEqual((r.status, s["client"]["name"], s["worked"]), (200, "Acme", 8 * HOUR))
        self.assertEqual([c["name"] for c in s["clients"]], ["Acme", "Client 1"])
        r, data = self.request("GET", "/api/summary?month=2026-11")       # no client: first in the list
        self.assertEqual(json.loads(data)["client"]["id"], acme)
        self.assertEqual(self.request("PATCH", f"/api/clients/{acme}", {"name": "Acme Ltd"})[0].status, 200)
        r, data = self.request("GET", "/api/clients")
        self.assertEqual([c["name"] for c in json.loads(data)], ["Acme Ltd", "Client 1"])
        self.assertEqual(self.request("PATCH", f"/api/entries/{eid}", {"activity": "x"})[0].status, 200)
        self.assertEqual(self.request("DELETE", f"/api/entries/{eid}", {})[0].status, 200)

    def test_errors_are_json(self):
        for method, path, status in [("GET", "/api/summary?month=nope", 400), ("GET", "/api/summary?client=x", 400),
                                     ("GET", "/api/summary?client=999", 404), ("GET", "/api/nope", 404),
                                     ("GET", "/api/summary?client=%C2%B2", 400),        # '²' is a "digit"
                                     ("GET", "/api/summary?client=99999999999999999999", 400),
                                     ("PATCH", "/api/clients/%C2%B2", 404),
                                     ("PATCH", "/api/entries/99999999999999999999", 404),
                                     ("POST", "/api/summary", 404), ("DELETE", "/api/clients/1", 404),
                                     ("GET", "/nope", 404)]:
            with self.subTest(method=method, path=path):
                r, data = self.request(method, path, {} if method != "GET" else None)
                self.assertEqual(r.status, status)
                self.assertIn("error", json.loads(data))

    def test_unexpected_error_is_logged_not_leaked(self):
        original = server.api_summary
        server.api_summary = lambda q: 1 / 0
        try:
            with self.assertLogs("timetracker", "ERROR"):
                r, data = self.request("GET", "/api/summary")
        finally:
            server.api_summary = original
        self.assertEqual(r.status, 500)
        self.assertNotIn("division", json.loads(data)["error"])


if __name__ == "__main__":
    unittest.main()
