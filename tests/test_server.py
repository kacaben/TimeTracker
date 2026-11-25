"""Tests for server.py. Run from the project folder:  python -m unittest discover -s tests -v"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

HOUR = 3600


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
    """Gives each test a fresh SQLite file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = server.DB_PATH
        server.DB_PATH = os.path.join(self.tmp.name, "test.db")
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self._old_db
        self.tmp.cleanup()

    def summary(self, month):
        with server.db() as con:
            return server.month_summary(con, month)

    def insert_raw(self, start, end, activity=""):
        """Insert without validation, e.g. to simulate old or corrupted data."""
        with server.db() as con:
            con.execute("INSERT INTO entries (start_ts, end_ts, activity) VALUES (?, ?, ?)", (start, end, activity))

    def assertApiError(self, status, fn, *args):
        with self.assertRaises(server.ApiError) as cm:
            fn(*args)
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


class ParseMonthTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(server.parse_month("2026-12"), (date(2026, 12, 1), date(2027, 1, 1)))
        self.assertEqual(server.parse_month("2026-2"), (date(2026, 2, 1), date(2026, 3, 1)))

    def test_invalid(self):
        for bad in ["", "2026", "2026-13", "2026-00", "abc", "2026-11-05", "9999-12"]:
            with self.subTest(bad=bad), self.assertRaises(server.ApiError):
                server.parse_month(bad)


# ------------------------------------------------------------- month summary
class MonthSummaryTest(DbTestCase):
    def test_targets_follow_weekdays_and_settings_history(self):
        server.api_put_settings({"daily_hours": 6, "effective_from": "2026-11-16"})
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
        server.api_toggle({})
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


# ------------------------------------------------------------------ endpoints
class AddEntryTest(DbTestCase):
    def test_add_and_reject_invalid(self):
        e = server.api_add_entry({"start": ts(2026, 11, 2, 9), "end": ts(2026, 11, 2, 17), "activity": "  work  "})
        self.assertEqual(e["activity"], "work")
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
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertApiError(400, server.api_add_entry, body)

    def test_overlap_rejected_but_touching_allowed(self):
        server.api_add_entry({"start": ts(2026, 11, 2, 9), "end": ts(2026, 11, 2, 12)})
        server.api_add_entry({"start": ts(2026, 11, 2, 12), "end": ts(2026, 11, 2, 13)})   # touches
        self.assertApiError(409, server.api_add_entry, {"start": ts(2026, 11, 2, 11), "end": ts(2026, 11, 2, 14)})
        self.assertApiError(409, server.api_add_entry, {"start": ts(2026, 11, 2, 8), "end": ts(2026, 11, 2, 18)})


class ToggleTest(DbTestCase):
    def test_start_stop_cycle(self):
        started = server.api_toggle({"activity": "coding"})
        self.assertIsNone(started["end"])
        stopped = server.api_toggle({})
        self.assertEqual(stopped["id"], started["id"])
        self.assertIsNotNone(stopped["end"])
        self.assertEqual(stopped["activity"], "coding")   # empty activity keeps the old one
        again = server.api_toggle({})
        self.assertNotEqual(again["id"], started["id"])

    def test_stop_can_set_activity(self):
        server.api_toggle({})
        self.assertEqual(server.api_toggle({"activity": "review"})["activity"], "review")

    def test_only_one_running_entry(self):
        server.api_toggle({})
        with server.db() as con:
            self.assertApiError(409, server.check_overlap, con, time.time(), None)


class PatchEntryTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.e1 = server.api_add_entry({"start": ts(2026, 11, 2, 9), "end": ts(2026, 11, 2, 12)})
        self.e2 = server.api_add_entry({"start": ts(2026, 11, 2, 13), "end": ts(2026, 11, 2, 17)})

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

    def test_running_entry_rules(self):
        run = server.api_toggle({})
        self.assertApiError(400, server.api_patch_entry, run["id"], {"end": time.time() + 60})
        self.assertApiError(400, server.api_patch_entry, run["id"], {"start": time.time() + 3600})
        moved = server.api_patch_entry(run["id"], {"start": time.time() - 600})
        self.assertIsNone(moved["end"])

    def test_delete(self):
        server.api_delete_entry(self.e1["id"])
        self.assertEqual([e["id"] for e in self.summary("2026-11")["entries"]], [self.e2["id"]])


class SettingsTest(DbTestCase):
    def test_new_value_applies_from_date(self):
        server.api_put_settings({"daily_hours": 6, "effective_from": "2026-12-01"})
        days = self.summary("2026-12")["days"]
        self.assertEqual(days[0]["target"], 6 * HOUR)    # Tue 1 Dec
        self.assertEqual(self.summary("2026-11")["days"][1]["target"], 8 * HOUR)   # Mon 2 Nov unchanged

    def test_rejects_non_canonical_and_invalid_dates(self):
        # Regression: '20261201' used to be stored as-is and then ignored by string comparison.
        for bad in ["20261201", "2026-W49-1", "2026-02-30", "01.12.2026", 20261201]:
            with self.subTest(bad=bad):
                self.assertApiError(400, server.api_put_settings, {"daily_hours": 6, "effective_from": bad})

    def test_rejects_bad_hours(self):
        for bad in [0, -1, 25, None, "8", float("nan")]:
            with self.subTest(bad=bad):
                self.assertApiError(400, server.api_put_settings, {"daily_hours": bad})


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
        r, _ = self.request("POST", "/api/toggle", {}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.request("POST", "/api/toggle", {}, headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(r.status, 200)

    def test_writes_require_json(self):   # CSRF: HTML forms cannot send application/json
        for method, path in [("POST", "/api/toggle"), ("DELETE", "/api/entries/1"), ("PUT", "/api/settings")]:
            with self.subTest(method=method):
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
        r, data = self.request("POST", "/api/entries",
                               {"start": ts(2026, 11, 2, 9), "end": ts(2026, 11, 2, 17), "activity": "work"})
        self.assertEqual(r.status, 201)
        eid = json.loads(data)["id"]
        r, data = self.request("GET", "/api/summary?month=2026-11")
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(data)["worked"], 8 * HOUR)
        self.assertEqual(self.request("PATCH", f"/api/entries/{eid}", {"activity": "x"})[0].status, 200)
        self.assertEqual(self.request("DELETE", f"/api/entries/{eid}", {})[0].status, 200)

    def test_errors_are_json(self):
        for method, path, status in [("GET", "/api/summary?month=nope", 400), ("GET", "/api/nope", 404),
                                     ("POST", "/api/summary", 404), ("GET", "/nope", 404)]:
            with self.subTest(path=path):
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
