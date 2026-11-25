#!/usr/bin/env python3
"""TimeTracker: personal start/stop time tracker. Stdlib only (no pip install).

Run:  python server.py   ->  http://localhost:8765
Data: timetracker.db (SQLite) next to this file. Override with TT_DB / TT_PORT.
"""
from __future__ import annotations

import calendar
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TT_DB", os.path.join(HERE, "timetracker.db"))
LOG_PATH = os.environ.get("TT_LOG", os.path.join(HERE, "timetracker.log"))
PORT = int(os.environ.get("TT_PORT", "8765"))

FAR_FUTURE = 1e18                     # stands in for "no end yet" in SQL comparisons
MIN_TS = datetime(2000, 1, 1).timestamp()
MAX_TS = datetime(2100, 1, 1).timestamp()
MAX_ENTRY_SECONDS = 7 * 86400         # longer entries are almost certainly typos
MAX_ACTIVITY_LEN = 200
MAX_BODY_BYTES = 64 * 1024
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
WRITE_LOCK = threading.Lock()

log = logging.getLogger("timetracker")


class ApiError(Exception):
    """An error that is shown to the user with the given HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- database
@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success (roll back on error) and always close it."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts REAL NOT NULL,
                end_ts REAL,
                activity TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS settings (
                effective_from TEXT PRIMARY KEY,   -- YYYY-MM-DD
                daily_hours REAL NOT NULL
            );
            """
        )
        if not con.execute("SELECT 1 FROM settings").fetchone():
            con.execute("INSERT INTO settings VALUES ('1970-01-01', 8)")


def entry_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "start": r["start_ts"], "end": r["end_ts"], "activity": r["activity"]}


def get_entry(con: sqlite3.Connection, eid: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()


def get_running(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM entries WHERE end_ts IS NULL").fetchone()


def check_overlap(con: sqlite3.Connection, start: float, end: float | None, exclude_id: int | None = None) -> None:
    """Reject overlaps: they would double count time. Touching entries are fine."""
    end_eff = FAR_FUTURE if end is None else end
    r = con.execute(
        "SELECT id FROM entries WHERE id != ? AND start_ts < ? AND COALESCE(end_ts, ?) > ?",
        (exclude_id or -1, end_eff, FAR_FUTURE, start),
    ).fetchone()
    if r:
        raise ApiError(409, "This entry overlaps another entry.")


# ------------------------------------------------------------------- logic
def local_midnight(d: date) -> float:
    return datetime.combine(d, datetime.min.time()).timestamp()


def split_by_day(start: float, end: float) -> Iterator[tuple[date, float]]:
    """Yield (local date, seconds) for each local day the interval touches.

    Uses real local midnights, so days with a daylight-saving change are 23 or 25 hours.
    """
    cur = start
    while cur < end:
        d = datetime.fromtimestamp(cur).date()
        seg_end = min(end, local_midnight(d + timedelta(days=1)))
        yield d, seg_end - cur
        cur = seg_end


def daily_hours_for(history: list[tuple[str, float]], d: date) -> float:
    """history: (effective_from 'YYYY-MM-DD', hours) sorted ascending; the last one on or before d wins."""
    h = history[0][1]
    day = d.isoformat()
    for eff, hours in history:
        if eff <= day:
            h = hours
    return h


def parse_month(month: str) -> tuple[date, date]:
    """'YYYY-MM' -> (first day of month, first day of next month)."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", month or "")
    try:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, 1), date(y + (mo == 12), mo % 12 + 1, 1)
    except (AttributeError, ValueError):
        raise ApiError(400, "month must be YYYY-MM") from None


def month_summary(con: sqlite3.Connection, month: str) -> dict:
    first, nxt_first = parse_month(month)
    y, m = first.year, first.month
    ms, me = local_midnight(first), local_midnight(nxt_first)
    now = time.time()
    today = datetime.fromtimestamp(now).date()

    history = [(r["effective_from"], r["daily_hours"]) for r in
               con.execute("SELECT * FROM settings ORDER BY effective_from")]

    rows = con.execute(
        "SELECT * FROM entries WHERE start_ts < ? AND COALESCE(end_ts, ?) > ? ORDER BY start_ts",
        (me, FAR_FUTURE, ms),
    ).fetchall()
    worked: dict[date, float] = {}
    for r in rows:
        end = r["end_ts"] if r["end_ts"] is not None else now
        # Clip to the month so long entries cost at most one month of work.
        for d, sec in split_by_day(max(r["start_ts"], ms), min(end, me)):
            worked[d] = worked.get(d, 0) + sec

    days = []
    target_total = 0.0
    remaining_workdays = 0
    for i in range(1, calendar.monthrange(y, m)[1] + 1):
        d = date(y, m, i)
        is_workday = d.weekday() < 5  # holidays count as workdays
        target = daily_hours_for(history, d) * 3600 if is_workday else 0
        target_total += target
        if is_workday and d >= today:
            remaining_workdays += 1
        days.append({"date": d.isoformat(), "weekday": d.weekday(), "worked": worked.get(d, 0),
                     "target": target})

    worked_total = sum(worked.values())
    missing = target_total - worked_total
    running = get_running(con)
    return {
        "month": f"{y:04d}-{m:02d}",
        "today": today.isoformat(),
        "server_time": now,
        "worked": worked_total,
        "target": target_total,
        "missing": missing,  # negative = overtime
        "remaining_workdays": remaining_workdays,
        "per_remaining_workday": (missing / remaining_workdays) if missing > 0 and remaining_workdays else None,
        "days": days,
        "entries": [entry_json(r) for r in rows],
        "running": entry_json(running) if running else None,
        "daily_hours": daily_hours_for(history, today),
        "settings_history": [{"effective_from": e, "daily_hours": h} for e, h in history],
    }


# --------------------------------------------------------------- validation
def num(v, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ApiError(400, f"{name} must be a number (epoch seconds)")
    return float(v)


def timestamp(v, name: str) -> float:
    ts = num(v, name)
    if not MIN_TS <= ts <= MAX_TS:
        raise ApiError(400, f"{name} must be between the years 2000 and 2100.")
    return ts


def activity_text(v) -> str:
    text = str(v or "").strip()
    if len(text) > MAX_ACTIVITY_LEN:
        raise ApiError(400, f"Activity must be at most {MAX_ACTIVITY_LEN} characters.")
    return text


def check_interval(start: float, end: float | None) -> None:
    if end is None:
        if start > time.time():
            raise ApiError(400, "A running entry cannot start in the future.")
        return
    if end <= start:
        raise ApiError(400, "End must be after start.")
    if end - start > MAX_ENTRY_SECONDS:
        raise ApiError(400, f"An entry can be at most {MAX_ENTRY_SECONDS // 86400} days long.")


# --------------------------------------------------------------- endpoints
def api_toggle(body: dict) -> dict:
    """Stop the running entry, or start a new one if nothing runs."""
    activity = activity_text(body.get("activity"))
    with WRITE_LOCK, db() as con:
        run = get_running(con)
        now = time.time()
        if run:
            if activity:
                con.execute("UPDATE entries SET activity=? WHERE id=?", (activity, run["id"]))
            con.execute("UPDATE entries SET end_ts=? WHERE id=?", (max(now, run["start_ts"]), run["id"]))
            eid = run["id"]
        else:
            check_overlap(con, now, None)
            eid = con.execute("INSERT INTO entries (start_ts, activity) VALUES (?, ?)",
                              (now, activity)).lastrowid
        return entry_json(get_entry(con, eid))


def api_add_entry(body: dict) -> dict:
    start, end = timestamp(body.get("start"), "start"), timestamp(body.get("end"), "end")
    check_interval(start, end)
    activity = activity_text(body.get("activity"))
    with WRITE_LOCK, db() as con:
        check_overlap(con, start, end)
        eid = con.execute("INSERT INTO entries (start_ts, end_ts, activity) VALUES (?, ?, ?)",
                          (start, end, activity)).lastrowid
        return entry_json(get_entry(con, eid))


def api_patch_entry(eid: int, body: dict) -> dict:
    """Change start, end and/or activity. A running entry stays running (its end cannot be set here)."""
    with WRITE_LOCK, db() as con:
        r = get_entry(con, eid)
        if not r:
            raise ApiError(404, "Entry not found.")
        start = timestamp(body["start"], "start") if "start" in body else r["start_ts"]
        if "end" in body and r["end_ts"] is None:
            raise ApiError(400, "Stop the timer before editing the end.")
        end = timestamp(body["end"], "end") if "end" in body else r["end_ts"]
        check_interval(start, end)
        check_overlap(con, start, end, eid)
        activity = activity_text(body["activity"]) if "activity" in body else r["activity"]
        con.execute("UPDATE entries SET start_ts=?, end_ts=?, activity=? WHERE id=?",
                    (start, end, activity, eid))
        return entry_json(get_entry(con, eid))


def api_delete_entry(eid: int) -> dict:
    with WRITE_LOCK, db() as con:
        con.execute("DELETE FROM entries WHERE id=?", (eid,))
    return {"ok": True}


def api_put_settings(body: dict) -> dict:
    hours = num(body.get("daily_hours"), "daily_hours")
    if not 0 < hours <= 24:
        raise ApiError(400, "daily_hours must be between 0 and 24.")
    eff = body.get("effective_from") or datetime.now().date().isoformat()
    if not isinstance(eff, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", eff):
        raise ApiError(400, "effective_from must be YYYY-MM-DD.")
    try:
        # Store the canonical form: daily_hours_for compares these as strings.
        eff = date.fromisoformat(eff).isoformat()
    except ValueError:
        raise ApiError(400, "effective_from must be YYYY-MM-DD.") from None
    with WRITE_LOCK, db() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (eff, hours))
    return {"ok": True}


def api_summary(query: dict) -> dict:
    month = (query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
    with db() as con:
        return month_summary(con, month)


# ------------------------------------------------------------------ server
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css"}


def host_name(header: str | None) -> str:
    """'localhost:8765' -> 'localhost', '[::1]:8765' -> '::1'."""
    h = (header or "").strip()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    return h.rsplit(":", 1)[0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console quiet; errors go to the log file
        pass

    def _send(self, status: int, payload, ctype: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # no clickjacking via iframes
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _check_request_origin(self, method: str) -> None:
        # DNS-rebinding guard: only answer to localhost names.
        if host_name(self.headers.get("Host")) not in LOCAL_HOSTS:
            raise ApiError(403, "Forbidden host.")
        # Defense in depth against cross-site writes (the JSON requirement below already blocks them).
        origin = self.headers.get("Origin")
        if method != "GET" and origin and host_name(urlparse(origin).netloc) not in LOCAL_HOSTS:
            raise ApiError(403, "Forbidden origin.")

    def _read_json(self) -> dict:
        # Requiring JSON blocks cross-site form posts (CSRF) from other pages.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError(415, "Content-Type must be application/json.")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Invalid Content-Length.") from None
        if n < 0:
            raise ApiError(400, "Invalid Content-Length.")
        if n > MAX_BODY_BYTES:
            raise ApiError(413, "Request body too large.")
        try:
            body = json.loads(self.rfile.read(n) or b"{}", parse_constant=_reject_constant)
        except ValueError:
            raise ApiError(400, "Invalid JSON.") from None
        if not isinstance(body, dict):
            raise ApiError(400, "Body must be a JSON object.")
        return body

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        root = os.path.realpath(os.path.join(HERE, "vendor"))
        full = os.path.realpath(os.path.join(HERE, path.lstrip("/")))
        try:
            inside = os.path.commonpath([root, full]) == root
        except ValueError:  # different drives on Windows
            inside = False
        if not inside or not os.path.isfile(full):
            raise ApiError(404, "Not found.")
        with open(full, "rb") as f:
            self._send(200, f.read(), STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def _route(self, method: str, path: str, query: dict):
        """Return (status, payload) for an /api/ request."""
        parts = path.strip("/").split("/")[1:]  # drop 'api'
        if parts == ["summary"] and method == "GET":
            return 200, api_summary(query)
        if parts == ["toggle"] and method == "POST":
            return 200, api_toggle(self._read_json())
        if parts == ["entries"] and method == "POST":
            return 201, api_add_entry(self._read_json())
        if len(parts) == 2 and parts[0] == "entries" and parts[1].isdigit():
            if method == "PATCH":
                return 200, api_patch_entry(int(parts[1]), self._read_json())
            if method == "DELETE":
                self._read_json()  # enforces the JSON content type (CSRF guard)
                return 200, api_delete_entry(int(parts[1]))
        if parts == ["settings"] and method == "PUT":
            return 200, api_put_settings(self._read_json())
        raise ApiError(404, "Not found.")

    def _handle(self, method: str) -> None:
        try:
            self._check_request_origin(method)
            url = urlparse(self.path)
            if method == "GET" and (url.path in ("/", "/index.html") or url.path.startswith("/vendor/")):
                return self._static(url.path)
            if not url.path.startswith("/api/"):
                raise ApiError(404, "Not found.")
            self._send(*self._route(method, url.path, parse_qs(url.query)))
        except ApiError as e:
            self._send(e.status, {"error": e.message})
        except Exception:  # noqa: BLE001 - last resort: log details, show a generic message
            log.exception("Unhandled error for %s %s", method, self.path)
            self._send(500, {"error": "Server error. Details are in timetracker.log."})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")  # json.loads accepts NaN/Infinity by default


def make_server(port: int = PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)  # localhost only


if __name__ == "__main__":
    logging.basicConfig(filename=LOG_PATH, level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    srv = make_server()
    print(f"TimeTracker running on http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
