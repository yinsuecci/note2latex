import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, job TEXT, body TEXT)")

    def connect(self):
        return sqlite3.connect(self.db, timeout=30)

    def path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise KeyError("Invalid job ID")
        return self.root / "jobs" / job_id

    def create(self, title, template, pages):
        job_id = uuid.uuid4().hex
        root = self.path(job_id)
        (root / "sources").mkdir(parents=True)
        for i, data in enumerate(pages, 1):
            (root / "sources" / f"p{i:03d}.png").write_bytes(data)
        meta = dict(
            id=job_id, title=title, template=template, page_count=len(pages),
            status="queued", stage="queued", revision=None, warnings=[],
            error=None, cancel_requested=False, pending_edit=None,
            created_at=now(), updated_at=now(),
        )
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?,?)", (job_id, json.dumps(meta)))
        return meta

    def get(self, job_id):
        self.path(job_id)
        with self.connect() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Job not found")
        return json.loads(row[0])

    def update(self, job_id, *, allowed=None, expected_revision=None, **changes):
        self.path(job_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("Job not found")
            meta = json.loads(row[0])
            if expected_revision is not None and meta["revision"] != expected_revision:
                raise ValueError("Stale revision")
            if allowed is not None and meta["status"] not in allowed:
                raise ValueError("Job is busy or not in a resumable state")
            meta.update(changes, updated_at=now())
            db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(meta), job_id))
        return meta

    def list(self):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM jobs ORDER BY rowid DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def event(self, job_id, stage, message, **details):
        entry = dict(time=now(), stage=stage, message=message, **details)
        with self.connect() as db:
            db.execute("INSERT INTO events(job,body) VALUES (?,?)", (job_id, json.dumps(entry)))

    def events(self, job_id):
        self.get(job_id)
        with self.connect() as db:
            rows = db.execute("SELECT body FROM events WHERE job=? ORDER BY seq", (job_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recover(self):
        # A local single-worker server owns this store. Never leave restart-orphaned tasks running.
        with self.connect() as db:
            records = db.execute("SELECT body FROM jobs").fetchall()
        for row in records:
            meta = json.loads(row[0])
            if meta["status"] in {"running", "queued"}:
                self.update(meta["id"], status="interrupted", error="Process restarted; resume from checkpoints")

