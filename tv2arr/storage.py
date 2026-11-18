import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RIGA = ZoneInfo("Europe/Riga")
DEFAULTS = {
    "url": "", "api_key": "", "connection": "Not tested",
    "interval": 6, "mode": "manual", "limit": 5, "is4k": False, "user_id": None,
    "lookup_revision": 0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    baseline INTEGER NOT NULL,
    original_title TEXT NOT NULL DEFAULT '',
    year INTEGER,
    poster TEXT NOT NULL DEFAULT '',
    matched_poster TEXT,
    enriched INTEGER NOT NULL DEFAULT 0,
    candidates TEXT NOT NULL DEFAULT '[]',
    match_state TEXT NOT NULL DEFAULT 'new',
    match_checked_at INTEGER,
    tmdb_id INTEGER,
    matched_title TEXT,
    matched_original_title TEXT NOT NULL DEFAULT '',
    language TEXT,
    auto_match INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS history (
    tmdb_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    seerr_id INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    at INTEGER NOT NULL,
    message TEXT NOT NULL,
    success INTEGER NOT NULL
);
"""


def now_stamp():
    return int(datetime.now(timezone.utc).timestamp())


def retention_cutoff(now):
    local = datetime.fromtimestamp(now, RIGA)
    try:
        prior = local.replace(year=local.year - 1)
    except ValueError:  # February 29 in a leap year
        prior = local.replace(year=local.year - 1, day=28)
    return int(prior.timestamp())


class Storage:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(listings)")}
            if "matched_original_title" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN matched_original_title TEXT NOT NULL DEFAULT ''")
            if "matched_poster" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN matched_poster TEXT")
            if "match_checked_at" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN match_checked_at INTEGER")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).fetchall()

    def write(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def config(self, key, default=None):
        rows = self.rows("SELECT value FROM config WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_config(self, key, value):
        self.write(
            "INSERT INTO config VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def settings(self):
        return DEFAULTS | self.config("settings", {})

    def movie(self, listing_id):
        rows = self.rows("SELECT * FROM listings WHERE id=?", (listing_id,))
        return rows[0] if rows else None

    def observe(self, source, movies, now):
        with self.connect() as db:
            db.execute("UPDATE listings SET present=0 WHERE source=?", (source,))
            baseline = not db.execute("SELECT 1 FROM listings WHERE source=? LIMIT 1", (source,)).fetchone()
            old_count = db.execute("SELECT count(*) FROM listings WHERE source=?", (source,)).fetchone()[0]
            db.executemany(
                """INSERT INTO listings(source, source_id, title, url, first_seen, last_seen, baseline)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, source_id) DO UPDATE SET
                       last_seen=excluded.last_seen, present=1, url=excluded.url, title=excluded.title""",
                [(source, sid, title, url, now, now, baseline) for sid, title, url in movies],
            )
            total = db.execute("SELECT count(*) FROM listings WHERE source=?", (source,)).fetchone()[0]
        return total - old_count, baseline

    def save_match(self, listing_id, movie, auto=False):
        from .seerr import LANGUAGES, poster_url

        language = movie.get("originalLanguage") or ""
        status = "ready" if language in LANGUAGES else "excluded" if language else "review"
        self.write(
            """UPDATE listings SET tmdb_id=?, matched_title=?, matched_original_title=?,
               language=?, match_state=?, auto_match=?, matched_poster=?
               WHERE id=?""",
            (movie["id"], movie["title"], movie.get("originalTitle") or "", language, status, auto,
             poster_url(movie), listing_id),
        )
        return status

    def event(self, kind, message, success=True):
        self.write(
            "INSERT INTO events(kind, at, message, success) VALUES (?, ?, ?, ?)",
            (kind, now_stamp(), message, success),
        )

    def maintain(self, now, scheduled):
        with self.connect() as db:
            count = db.execute(
                "DELETE FROM history WHERE recorded_at <= ?", (retention_cutoff(now),)
            ).rowcount
            db.execute("DELETE FROM events WHERE at <= ?", (retention_cutoff(now),))
            db.execute(
                "INSERT INTO events(kind, at, message, success) VALUES ('Maintenance', ?, ?, 1)",
                (now, f"Removed {count} expired movie history entries."),
            )
            db.execute(
                "INSERT INTO config VALUES ('maintenance', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(scheduled),),
            )
        return count
