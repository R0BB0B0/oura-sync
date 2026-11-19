#!/usr/bin/env python3
"""Pull your own Oura Ring data from the Oura API v2 into a local SQLite database.

Commands:
    python oura_sync.py auth                 # one-off OAuth2 login (opens browser)
    python oura_sync.py sync                 # backfill on first run, incremental after
    python oura_sync.py sync --full          # re-pull everything from the earliest date
    python oura_sync.py export [--out DIR]   # write one CSV per table
"""

import argparse
import csv
import json
import os
import secrets
import sqlite3
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".oura_tokens.json"
DEFAULT_DB = HERE / "oura.db"

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection/"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = "personal daily heartrate workout tag session spo2"

# Oura launched in 2015; nothing older than this can exist.
BACKFILL_FLOOR = date(2015, 1, 1)
DOC_CHUNK_DAYS = 365       # window size for date-based endpoints
HR_CHUNK_DAYS = 30         # heartrate accepts at most ~30 days per request
DOC_OVERLAP_DAYS = 7       # re-pull recent days: Oura revises scores after the fact
HR_OVERLAP_HOURS = 24

# endpoint name -> field holding the record's day
DOC_ENDPOINTS = {
    "daily_sleep": "day",
    "sleep": "day",
    "daily_readiness": "day",
    "daily_activity": "day",
    "daily_stress": "day",
    "daily_spo2": "day",
    "workout": "day",
    "enhanced_tag": "start_day",
}
HEARTRATE = "heartrate"
ALL_TABLES = list(DOC_ENDPOINTS) + [HEARTRATE]


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- tokens

def client_creds():
    load_dotenv(HERE / ".env")
    cid, secret = os.getenv("OURA_CLIENT_ID"), os.getenv("OURA_CLIENT_SECRET")
    if not cid or not secret:
        die("OURA_CLIENT_ID and OURA_CLIENT_SECRET must be set in .env")
    return cid, secret


def load_tokens():
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def save_tokens(tokens):
    """Write atomically so a crash mid-write can't lose the (single-use) refresh token."""
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, TOKEN_FILE)


def token_request(data, previous=None):
    cid, secret = client_creds()
    r = requests.post(
        TOKEN_URL,
        data={**data, "client_id": cid, "client_secret": secret},
        timeout=30,
    )
    if not r.ok:
        hint = " Run `python oura_sync.py auth` again." if data.get("grant_type") == "refresh_token" else ""
        die(f"token request failed ({r.status_code}): {r.text}.{hint}")
    tokens = r.json()
    if "refresh_token" not in tokens and previous:
        tokens["refresh_token"] = previous["refresh_token"]
    tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 0))
    save_tokens(tokens)  # save immediately: the old refresh token is now dead
    return tokens


def cmd_auth(_args):
    cid, _ = client_creds()
    state = secrets.token_urlsafe(24)
    url = AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            result.update(params)
            ok = "code" in params and params.get("state") == state
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Authorised. You can close this tab." if ok else "Authorisation failed. Check the terminal."
            self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *a):
            pass

    host, port = urlparse(REDIRECT_URI).hostname, urlparse(REDIRECT_URI).port
    try:
        server = HTTPServer((host, port), Handler)
    except OSError as e:
        die(f"could not listen on {host}:{port}: {e}")
    server.timeout = 1

    print("Opening your browser to authorise with Oura. If it doesn't open, visit:\n")
    print(url + "\n")
    webbrowser.open(url)

    deadline = time.time() + 300
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if not result:
        die("timed out waiting for the OAuth callback")
    if "error" in result:
        die(f"Oura returned an error: {result['error']} {result.get('error_description', '')}")
    if result.get("state") != state:
        die("state mismatch in OAuth callback; aborting")

    token_request({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
    })
    print(f"Tokens saved to {TOKEN_FILE.name}.")


# --------------------------------------------------------------------------- API client

class ApiError(Exception):
    pass


class Oura:
    def __init__(self):
        self.tokens = load_tokens()
        if not self.tokens:
            die("no tokens found; run `python oura_sync.py auth` first")
        self.session = requests.Session()

    def refresh(self):
        self.tokens = token_request(
            {"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"]},
            previous=self.tokens,
        )

    def access_token(self):
        if time.time() > self.tokens.get("expires_at", 0) - 120:
            self.refresh()
        return self.tokens["access_token"]

    def get(self, endpoint, params):
        refreshed = False
        for attempt in range(8):
            r = self.session.get(
                API_BASE + endpoint,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token()}"},
                timeout=60,
            )
            if r.status_code == 401 and not refreshed:
                self.refresh()
                refreshed = True
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After") or min(300, 5 * 2 ** attempt))
                print(f"  {endpoint}: HTTP {r.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            if not r.ok:
                raise ApiError(f"HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise ApiError("too many retries")

    def paginate(self, endpoint, params):
        next_token = None
        while True:
            p = dict(params)
            if next_token:
                p["next_token"] = next_token
            body = self.get(endpoint, p)
            yield from body.get("data", [])
            next_token = body.get("next_token")
            if not next_token:
                return


# --------------------------------------------------------------------------- database

def open_db(path):
    db = sqlite3.connect(path)
    for name in DOC_ENDPOINTS:
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {name} ("
            "id TEXT PRIMARY KEY, day TEXT, data TEXT NOT NULL, fetched_at TEXT)"
        )
        db.execute(f"CREATE INDEX IF NOT EXISTS {name}_day ON {name}(day)")
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {HEARTRATE} ("
        "timestamp TEXT NOT NULL, source TEXT NOT NULL, bpm INTEGER, "
        "PRIMARY KEY (timestamp, source))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS sync_state ("
        "endpoint TEXT PRIMARY KEY, synced_through TEXT NOT NULL)"
    )
    db.commit()
    return db


def get_state(db, endpoint):
    row = db.execute("SELECT synced_through FROM sync_state WHERE endpoint=?", (endpoint,)).fetchone()
    return row[0] if row else None


def set_state(db, endpoint, value):
    db.execute(
        "INSERT INTO sync_state(endpoint, synced_through) VALUES(?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET synced_through=excluded.synced_through",
        (endpoint, value),
    )


# --------------------------------------------------------------------------- sync

def sync_docs(api, db, name, day_field, start, end):
    """Fetch [start, end] in chunks, upserting by id. Chunks overlap by a day so it
    doesn't matter whether Oura treats end_date as inclusive or exclusive."""
    total = 0
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=DOC_CHUNK_DAYS), end)
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (rec["id"], rec.get(day_field), json.dumps(rec), now)
            for rec in api.paginate(name, {"start_date": cur.isoformat(), "end_date": chunk_end.isoformat()})
        ]
        db.executemany(
            f"INSERT INTO {name}(id, day, data, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET day=excluded.day, data=excluded.data, fetched_at=excluded.fetched_at",
            rows,
        )
        # record progress per chunk so an interrupted backfill resumes where it stopped
        set_state(db, name, min(chunk_end, date.today()).isoformat())
        db.commit()
        total += len(rows)
        cur = chunk_end
    return total


def sync_heartrate(api, db, start, end):
    total = 0
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=HR_CHUNK_DAYS), end)
        rows = [
            (rec["timestamp"], rec.get("source", ""), rec.get("bpm"))
            for rec in api.paginate(HEARTRATE, {
                "start_datetime": cur.isoformat(timespec="seconds"),
                "end_datetime": chunk_end.isoformat(timespec="seconds"),
            })
        ]
        db.executemany(
            f"INSERT INTO {HEARTRATE}(timestamp, source, bpm) VALUES(?,?,?) "
            "ON CONFLICT(timestamp, source) DO UPDATE SET bpm=excluded.bpm",
            rows,
        )
        set_state(db, HEARTRATE, chunk_end.isoformat(timespec="seconds"))
        db.commit()
        total += len(rows)
        cur = chunk_end
    return total


def earliest_day(db):
    days = [
        db.execute(f"SELECT MIN(day) FROM {name} WHERE day IS NOT NULL").fetchone()[0]
        for name in DOC_ENDPOINTS
    ]
    days = [d for d in days if d]
    return date.fromisoformat(min(days)) if days else None


def cmd_sync(args):
    api = Oura()
    db = open_db(args.db)
    since = date.fromisoformat(args.since) if args.since else None
    end = date.today() + timedelta(days=1)

    for name, day_field in DOC_ENDPOINTS.items():
        last = None if args.full else get_state(db, name)
        if last:
            start = date.fromisoformat(last) - timedelta(days=DOC_OVERLAP_DAYS)
            mode = "incremental"
        else:
            start = since or BACKFILL_FLOOR
            mode = "backfill"
        print(f"{name}: {mode} from {start}")
        try:
            n = sync_docs(api, db, name, day_field, start, end)
            print(f"  {n} records")
        except ApiError as e:
            print(f"  skipped: {e}")

    # Heartrate is dense, so backfill only from the earliest day any other endpoint returned.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    last = None if args.full else get_state(db, HEARTRATE)
    if last:
        hr_start = datetime.fromisoformat(last) - timedelta(hours=HR_OVERLAP_HOURS)
        mode = "incremental"
    else:
        first = since or earliest_day(db) or (date.today() - timedelta(days=30))
        hr_start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
        mode = "backfill"
    print(f"{HEARTRATE}: {mode} from {hr_start.isoformat()}")
    try:
        n = sync_heartrate(api, db, hr_start, now)
        print(f"  {n} samples")
    except ApiError as e:
        print(f"  skipped: {e}")

    db.close()


# --------------------------------------------------------------------------- export

def flatten(obj, prefix=""):
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = json.dumps(v)
        else:
            out[key] = v
    return out


def cmd_export(args):
    if not Path(args.db).exists():
        die(f"{args.db} not found; run `sync` first")
    db = open_db(args.db)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in DOC_ENDPOINTS:
        rows = [flatten(json.loads(d)) for (d,) in db.execute(f"SELECT data FROM {name} ORDER BY day, id")]
        columns = []
        seen = set()
        for row in rows:
            for c in row:
                if c not in seen:
                    seen.add(c)
                    columns.append(c)
        # put the identifying columns first
        lead = [c for c in ("id", DOC_ENDPOINTS[name]) if c in seen]
        columns = lead + [c for c in columns if c not in lead]
        path = out_dir / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(rows)
        print(f"{path}: {len(rows)} rows")

    path = out_dir / f"{HEARTRATE}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "source", "bpm"])
        n = 0
        for row in db.execute(f"SELECT timestamp, source, bpm FROM {HEARTRATE} ORDER BY timestamp"):
            w.writerow(row)
            n += 1
    print(f"{path}: {n} rows")
    db.close()


# --------------------------------------------------------------------------- CLI

def main():
    p = argparse.ArgumentParser(description="Sync Oura Ring data to SQLite.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: oura.db next to this script)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="authorise with Oura (OAuth2) and save tokens")

    s = sub.add_parser("sync", help="fetch data (backfill first time, incremental after)")
    s.add_argument("--full", action="store_true", help="ignore saved progress and re-pull everything")
    s.add_argument("--since", help="backfill start date YYYY-MM-DD (default: earliest available)")

    e = sub.add_parser("export", help="export each table to CSV")
    e.add_argument("--out", default=str(HERE / "exports"), help="output directory (default: ./exports)")

    args = p.parse_args()
    {"auth": cmd_auth, "sync": cmd_sync, "export": cmd_export}[args.command](args)


if __name__ == "__main__":
    main()
