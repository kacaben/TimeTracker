#!/usr/bin/env python3
"""TimeTracker: personal start/stop time tracker for several clients. Stdlib only (no pip install).

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
import sys
import threading
import time
import webbrowser
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TT_DB", os.path.join(HERE, "timetracker.db"))
LOG_PATH = os.environ.get("TT_LOG", os.path.join(HERE, "timetracker.log"))
PORT = int(os.environ.get("TT_PORT", "8765"))

SCHEMA_VERSION = 2                    # 1 (implicit, user_version 0): single client. 2: clients table.
DEFAULT_CLIENT = "Client 1"
DEFAULT_DAILY_HOURS = 8.0
FAR_FUTURE = 1e18                     # stands in for "no end yet" in SQL comparisons
MIN_TS = datetime(2000, 1, 1).timestamp()
MAX_TS = datetime(2100, 1, 1).timestamp()
MAX_ENTRY_SECONDS = 7 * 86400         # longer entries are almost certainly typos
MAX_ACTIVITY_LEN = 200
MAX_CLIENT_NAME_LEN = 60
MAX_BODY_BYTES = 64 * 1024
MAX_ID = 10**15                       # ids beyond this are invalid input (SQLite integers end at 2**63)
ID_RE = re.compile(r"[0-9]{1,15}")    # ASCII digits only: '²'.isdigit() is True but int('²') fails
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


SCHEMA = """
CREATE TABLE clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    start_ts REAL NOT NULL,
    end_ts REAL,                           -- NULL while the timer runs
    activity TEXT NOT NULL DEFAULT ''
);
CREATE TABLE settings (
    client_id INTEGER NOT NULL REFERENCES clients(id),
    effective_from TEXT NOT NULL,          -- YYYY-MM-DD
    daily_hours REAL NOT NULL,
    PRIMARY KEY (client_id, effective_from)
);
"""

# Version 1 had no clients: all entries and settings move to the first client.
MIGRATE_1_TO_2 = """
CREATE TABLE clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    archived INTEGER NOT NULL DEFAULT 0
);
INSERT INTO clients (id, name) VALUES (1, :name);
ALTER TABLE entries ADD COLUMN client_id INTEGER NOT NULL DEFAULT 1 REFERENCES clients(id);
CREATE TABLE settings_v2 (
    client_id INTEGER NOT NULL REFERENCES clients(id),
    effective_from TEXT NOT NULL,
    daily_hours REAL NOT NULL,
    PRIMARY KEY (client_id, effective_from)
);
INSERT INTO settings_v2 SELECT 1, effective_from, daily_hours FROM settings;
DROP TABLE settings;
ALTER TABLE settings_v2 RENAME TO settings;
"""


def backup_path(path: str) -> str:
    root, ext = os.path.splitext(path)
    return f"{root}.before-clients{ext or '.db'}"


def init_db() -> None:
    """Create the schema, or upgrade an older database in place (after saving a copy).

    Everything happens under one write lock (BEGIN IMMEDIATE): the version is read, the backup taken
    and the upgrade done while no other process can write, and an error rolls all of it back.
    sqlite3 does not open a transaction for CREATE/ALTER by itself, hence the explicit BEGIN.
    """
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{DB_PATH} was written by a newer TimeTracker (schema {version}, "
                               f"this code knows {SCHEMA_VERSION}). Update the code; the file was not changed.")
        has_entries = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'").fetchone()
        if version < SCHEMA_VERSION and has_entries:
            # A second connection may read while we hold the write lock, so the copy is consistent.
            with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(backup_path(DB_PATH))) as dst:
                src.backup(dst)
            for stmt in split_sql(MIGRATE_1_TO_2):
                con.execute(stmt, {"name": DEFAULT_CLIENT} if ":name" in stmt else {})
        elif version < SCHEMA_VERSION:
            for stmt in split_sql(SCHEMA):
                con.execute(stmt)
        con.execute("CREATE INDEX IF NOT EXISTS entries_by_client ON entries (client_id, start_ts)")
        if version != SCHEMA_VERSION:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if not con.execute("SELECT 1 FROM clients").fetchone():
            insert_client(con, DEFAULT_CLIENT, DEFAULT_DAILY_HOURS)
        # Every client needs a daily-hours value (an old database could have had no settings row).
        con.execute("INSERT INTO settings SELECT id, '1970-01-01', ? FROM clients "
                    "WHERE id NOT IN (SELECT client_id FROM settings)", (DEFAULT_DAILY_HOURS,))


def split_sql(script: str) -> list[str]:
    """Split a script into statements, so they run inside one transaction (executescript would commit).

    Only for the simple scripts in this file: no ';' or '--' inside string literals.
    Note: MIGRATE_1_TO_2 adds a column with REFERENCES and a default, which SQLite allows only
    while foreign keys are off (its default); db() must not turn them on.
    """
    lines = [ln.split("--")[0] for ln in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def insert_client(con: sqlite3.Connection, name: str, daily_hours: float) -> int:
    try:
        cid = con.execute("INSERT INTO clients (name) VALUES (?)", (name,)).lastrowid
    except sqlite3.IntegrityError:
        raise ApiError(409, "A client with this name already exists.") from None
    con.execute("INSERT INTO settings VALUES (?, '1970-01-01', ?)", (cid, daily_hours))
    return cid


def entry_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "client_id": r["client_id"], "start": r["start_ts"], "end": r["end_ts"],
            "activity": r["activity"]}


def client_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": r["name"], "archived": bool(r["archived"])}


def get_entry(con: sqlite3.Connection, eid: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()


def get_running(con: sqlite3.Connection) -> sqlite3.Row | None:
    """The one running entry, for any client."""
    return con.execute("SELECT * FROM entries WHERE end_ts IS NULL").fetchone()


def list_clients(con: sqlite3.Connection) -> list[dict]:
    return [client_json(r) for r in
            con.execute("SELECT * FROM clients ORDER BY archived, name COLLATE NOCASE")]


def check_overlap(con: sqlite3.Connection, start: float, end: float | None, exclude_id: int | None = None) -> None:
    """Reject overlaps across all clients: you cannot work for two at once. Touching entries are fine."""
    end_eff = FAR_FUTURE if end is None else end
    r = con.execute(
        "SELECT e.id, c.name FROM entries e JOIN clients c ON c.id = e.client_id "
        "WHERE e.id != ? AND e.start_ts < ? AND COALESCE(e.end_ts, ?) > ?",
        (exclude_id or -1, end_eff, FAR_FUTURE, start),
    ).fetchone()
    if r:
        raise ApiError(409, f"This entry overlaps another entry (client {r['name']}).")


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
    h = history[0][1] if history else 0.0
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


def month_summary(con: sqlite3.Connection, month: str, client_id: int) -> dict:
    """Everything the page shows for one client and month. 'running' is the timer of any client."""
    client = client_row(con, client_id)
    first, nxt_first = parse_month(month)
    y, m = first.year, first.month
    ms, me = local_midnight(first), local_midnight(nxt_first)
    now = time.time()
    today = datetime.fromtimestamp(now).date()

    history = [(r["effective_from"], r["daily_hours"]) for r in con.execute(
        "SELECT * FROM settings WHERE client_id=? ORDER BY effective_from", (client_id,))]

    rows = con.execute(
        "SELECT * FROM entries WHERE client_id=? AND start_ts < ? AND COALESCE(end_ts, ?) > ? ORDER BY start_ts",
        (client_id, me, FAR_FUTURE, ms),
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
        "client": client_json(client),
        "clients": list_clients(con),
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
def finite_float(v) -> float | None:
    """A JSON number as a finite float, else None (rejects bools, NaN, infinity and huge integers)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:   # an integer with hundreds of digits
        return None
    return f if math.isfinite(f) else None


def num(v, name: str) -> float:
    f = finite_float(v)
    if f is None:
        raise ApiError(400, f"{name} must be a number (epoch seconds)")
    return f


def timestamp(v, name: str) -> float:
    ts = num(v, name)
    if not MIN_TS <= ts <= MAX_TS:
        raise ApiError(400, f"{name} must be between the years 2000 and 2100.")
    return ts


def daily_hours_value(v) -> float:
    f = finite_float(v)
    if f is None or not 0 < f <= 24:
        raise ApiError(400, "daily_hours must be between 0 and 24.")
    return f


def activity_text(v) -> str:
    text = str(v or "").strip()
    if len(text) > MAX_ACTIVITY_LEN:
        raise ApiError(400, f"Activity must be at most {MAX_ACTIVITY_LEN} characters.")
    return text


def client_name(v) -> str:
    name = v.strip() if isinstance(v, str) else ""
    if not 1 <= len(name) <= MAX_CLIENT_NAME_LEN:
        raise ApiError(400, f"Client name must be 1 to {MAX_CLIENT_NAME_LEN} characters.")
    return name


def client_row(con: sqlite3.Connection, v, active: bool = False) -> sqlite3.Row:
    """Look up a client by id; with active=True, archived clients are refused."""
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= MAX_ID:
        raise ApiError(400, "client_id must be a client number.")
    r = con.execute("SELECT * FROM clients WHERE id=?", (v,)).fetchone()
    if not r:
        raise ApiError(404, "Client not found.")
    if active and r["archived"]:
        raise ApiError(400, f"Client {r['name']} is archived. Restore it first.")
    return r


def check_interval(start: float, end: float | None) -> None:
    if end is None:
        now = time.time()
        if start > now:
            raise ApiError(400, "A running entry cannot start in the future.")
        if now - start > MAX_ENTRY_SECONDS:
            raise ApiError(400, f"A running entry can start at most {MAX_ENTRY_SECONDS // 86400} days ago.")
        return
    if end <= start:
        raise ApiError(400, "End must be after start.")
    if end - start > MAX_ENTRY_SECONDS:
        raise ApiError(400, f"An entry can be at most {MAX_ENTRY_SECONDS // 86400} days long.")


# --------------------------------------------------------------- endpoints
def api_list_clients() -> list[dict]:
    with db() as con:
        return list_clients(con)


def api_create_client(body: dict) -> dict:
    name = client_name(body.get("name"))
    hours = daily_hours_value(body.get("daily_hours", DEFAULT_DAILY_HOURS))
    with WRITE_LOCK, db() as con:
        cid = insert_client(con, name, hours)
        return client_json(client_row(con, cid))


def api_patch_client(cid: int, body: dict) -> dict:
    """Rename and/or archive (archived: true) or restore (archived: false) a client."""
    with WRITE_LOCK, db() as con:
        r = client_row(con, cid)
        name = client_name(body["name"]) if "name" in body else r["name"]
        archived = r["archived"]
        if "archived" in body:
            if not isinstance(body["archived"], bool):
                raise ApiError(400, "archived must be true or false.")
            archived = int(body["archived"])
            run = get_running(con)
            if archived and run and run["client_id"] == cid:
                raise ApiError(400, "Stop the timer before archiving this client.")
        try:
            con.execute("UPDATE clients SET name=?, archived=? WHERE id=?", (name, archived, cid))
        except sqlite3.IntegrityError:
            raise ApiError(409, "A client with this name already exists.") from None
        return client_json(client_row(con, cid))


def api_toggle(body: dict) -> dict:
    """Start/stop button.

    - Nothing runs: start a timer for client_id.
    - The timer runs for client_id (or client_id is omitted): stop it; a non-empty activity is saved on it.
    - The timer runs for another client: stop that one and start one for client_id (switch).
    The page sends running_id: the running entry it showed (null if none). If the timer changed
    meanwhile (another tab, a double click), the click is refused with 409 instead of doing the
    opposite of what the button said.
    Returns {"stopped": entry or None, "started": entry or None}.
    """
    activity = activity_text(body.get("activity"))
    with WRITE_LOCK, db() as con:
        cid = client_row(con, body["client_id"])["id"] if body.get("client_id") is not None else None
        run = get_running(con)
        if "running_id" in body and body["running_id"] != (run["id"] if run else None):
            raise ApiError(409, "The timer was changed meanwhile (another tab?). The page is up to date now; click again.")
        now = time.time()
        stopped = started = None
        if run and cid in (None, run["client_id"]):
            if activity:
                con.execute("UPDATE entries SET activity=? WHERE id=?", (activity, run["id"]))
            stopped = stop_entry(con, run, now)
        else:
            if cid is None:
                raise ApiError(400, "client_id is required to start the timer.")
            client_row(con, cid, active=True)
            if run:
                stopped = stop_entry(con, run, now)
            check_overlap(con, now, None)
            started = con.execute("INSERT INTO entries (client_id, start_ts, activity) VALUES (?, ?, ?)",
                                  (cid, now, activity)).lastrowid
        return {"stopped": entry_json(get_entry(con, stopped)) if stopped else None,
                "started": entry_json(get_entry(con, started)) if started else None}


def stop_entry(con: sqlite3.Connection, run: sqlite3.Row, now: float) -> int:
    con.execute("UPDATE entries SET end_ts=? WHERE id=?", (max(now, run["start_ts"]), run["id"]))
    return run["id"]


def api_add_entry(body: dict) -> dict:
    start, end = timestamp(body.get("start"), "start"), timestamp(body.get("end"), "end")
    check_interval(start, end)
    activity = activity_text(body.get("activity"))
    with WRITE_LOCK, db() as con:
        cid = client_row(con, body.get("client_id"), active=True)["id"]
        check_overlap(con, start, end)
        eid = con.execute("INSERT INTO entries (client_id, start_ts, end_ts, activity) VALUES (?, ?, ?, ?)",
                          (cid, start, end, activity)).lastrowid
        return entry_json(get_entry(con, eid))


def api_patch_entry(eid: int, body: dict) -> dict:
    """Change start, end, activity and/or client. A running entry stays running (its end cannot be set here).

    Times are validated only when they change: a timer stopped after more than 7 days is
    still a real entry, and its activity or client can be edited without shortening it.
    """
    with WRITE_LOCK, db() as con:
        r = get_entry(con, eid)
        if not r:
            raise ApiError(404, "Entry not found.")
        start = timestamp(body["start"], "start") if "start" in body else r["start_ts"]
        if "end" in body and r["end_ts"] is None:
            raise ApiError(400, "Stop the timer before editing the end.")
        end = timestamp(body["end"], "end") if "end" in body else r["end_ts"]
        if "start" in body or "end" in body:
            check_interval(start, end)
            check_overlap(con, start, end, eid)
        activity = activity_text(body["activity"]) if "activity" in body else r["activity"]
        cid = r["client_id"]
        if "client_id" in body and body["client_id"] != cid:   # moving: the target must be active
            cid = client_row(con, body["client_id"], active=True)["id"]
        con.execute("UPDATE entries SET client_id=?, start_ts=?, end_ts=?, activity=? WHERE id=?",
                    (cid, start, end, activity, eid))
        return entry_json(get_entry(con, eid))


def api_delete_entry(eid: int) -> dict:
    with WRITE_LOCK, db() as con:
        con.execute("DELETE FROM entries WHERE id=?", (eid,))
    return {"ok": True}


def api_put_settings(body: dict) -> dict:
    """Set a client's daily hours from effective_from (default today) onward."""
    hours = daily_hours_value(body.get("daily_hours"))
    eff = body.get("effective_from") or datetime.now().date().isoformat()
    if not isinstance(eff, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", eff):
        raise ApiError(400, "effective_from must be YYYY-MM-DD.")
    try:
        # Store the canonical form: daily_hours_for compares these as strings.
        eff = date.fromisoformat(eff).isoformat()
    except ValueError:
        raise ApiError(400, "effective_from must be YYYY-MM-DD.") from None
    with WRITE_LOCK, db() as con:
        cid = client_row(con, body.get("client_id"))["id"]
        con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", (cid, eff, hours))
    return {"ok": True}


def api_summary(query: dict) -> dict:
    month = (query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
    client = (query.get("client") or [""])[0]
    with db() as con:
        if client:
            if not ID_RE.fullmatch(client):
                raise ApiError(400, "client must be a client number.")
            cid = int(client)
        else:  # default: the first client in the list (active ones come first)
            cid = list_clients(con)[0]["id"]
        return month_summary(con, month, cid)


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
        self._body_read = True
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
        has_id = len(parts) == 2 and ID_RE.fullmatch(parts[1]) is not None
        if parts == ["summary"] and method == "GET":
            return 200, api_summary(query)
        if parts == ["clients"] and method == "GET":
            return 200, api_list_clients()
        if parts == ["clients"] and method == "POST":
            return 201, api_create_client(self._read_json())
        if has_id and parts[0] == "clients" and method == "PATCH":
            return 200, api_patch_client(int(parts[1]), self._read_json())
        if parts == ["toggle"] and method == "POST":
            return 200, api_toggle(self._read_json())
        if parts == ["entries"] and method == "POST":
            return 201, api_add_entry(self._read_json())
        if has_id and parts[0] == "entries":
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
            self._discard_body()
            self._send(e.status, {"error": e.message})
        except Exception:  # noqa: BLE001 - last resort: log details, show a generic message
            log.exception("Unhandled error for %s %s", method, self.path)
            self._discard_body()
            self._send(500, {"error": f"Server error. Details are in {os.path.basename(LOG_PATH)}."})

    def _discard_body(self) -> None:
        """Read an unread request body before answering an error.

        Closing a socket with unread data makes Windows reset the connection, and the
        client then never sees the error response. Oversized bodies are not read.
        """
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= MAX_BODY_BYTES:
            self.rfile.read(n)

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
    # Take the port first: a second copy of the server stops here, before it touches the database.
    try:
        srv = make_server()
        init_db()
    except Exception:  # noqa: BLE001 - under pythonw.exe there is no console, so the log is the only trace
        log.exception("Server could not start")
        raise SystemExit(1)
    url = f"http://localhost:{PORT}"
    print(f"TimeTracker running on {url}  (Ctrl+C to stop)")
    if "--open" in sys.argv[1:]:   # start.bat: open the browser once the port is listening
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
