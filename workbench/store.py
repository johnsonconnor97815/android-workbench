"""Small durable transactions. All access is serialized by Service.lock."""

import json
import sqlite3
import time
from .common import encode


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, name TEXT NOT NULL, project TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
                request_key TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL,
                spec TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                started REAL, finished REAL, priority INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, generation TEXT,
                cancel INTEGER NOT NULL DEFAULT 0, parent TEXT, reason TEXT,
                result TEXT, worker TEXT, UNIQUE(owner,project,request_key));
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, time REAL NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resources(
                key TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outputs(path TEXT PRIMARY KEY, job TEXT NOT NULL);
        """)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row and row[0] != "1":
            raise RuntimeError(
                "Unsupported database schema; refusing to migrate live state"
            )
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema','1')")

    def rows(self, query, args=()):
        return [dict(row) for row in self.db.execute(query, args)]

    def job(self, identifier):
        rows = self.rows("SELECT * FROM jobs WHERE id=?", (identifier,))
        if not rows:
            raise ValueError("Unknown job")
        return self.decode(rows[0])

    @staticmethod
    def decode(row):
        for field in ("spec", "result", "worker"):
            if row[field] is not None:
                row[field] = json.loads(row[field])
        return row

    def jobs(self):
        return [
            self.decode(row)
            for row in self.rows("SELECT * FROM jobs ORDER BY created,id")
        ]

    def update(self, identifier, **values):
        values["updated"] = time.time()
        for key in ("spec", "result", "worker"):
            if key in values and values[key] is not None:
                values[key] = encode(values[key])
        self.db.execute(
            "UPDATE jobs SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
            [*values.values(), identifier],
        )

    def event(self, job, kind, data):
        self.db.execute(
            "INSERT INTO events(job,time,kind,data) VALUES(?,?,?,?)",
            (job, time.time(), kind, encode(data)),
        )

    def health(self, key, status, detail):
        self.db.execute(
            "INSERT INTO resources VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET status=excluded.status,detail=excluded.detail",
            (key, status, encode(detail)),
        )
