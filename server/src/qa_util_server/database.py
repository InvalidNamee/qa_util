import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE,
 alias TEXT NOT NULL DEFAULT '',
 client_id TEXT, session_id TEXT, session_until REAL NOT NULL DEFAULT 0,
 last_seen REAL NOT NULL DEFAULT 0, client_status TEXT NOT NULL DEFAULT '离线',
 task_cursor INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, forum_url TEXT NOT NULL,
 question_target INTEGER NOT NULL CHECK(question_target > 0),
 answer_target INTEGER NOT NULL CHECK(answer_target > 0),
 account_answer_target INTEGER NOT NULL DEFAULT 0 CHECK(account_answer_target>=0 AND account_answer_target<=question_target),
 state TEXT NOT NULL DEFAULT 'draft', course_name TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
 task_id INTEGER NOT NULL REFERENCES tasks(id), account_id INTEGER NOT NULL REFERENCES accounts(id),
 question_count INTEGER NOT NULL DEFAULT 0, synced_at REAL,
 answer_count INTEGER, answer_synced_at REAL,
 PRIMARY KEY(task_id, account_id)
);
CREATE TABLE IF NOT EXISTS questions (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 owner_id INTEGER NOT NULL REFERENCES accounts(id), position INTEGER NOT NULL,
 text TEXT NOT NULL DEFAULT '', url TEXT, answer_count INTEGER NOT NULL DEFAULT 0,
 observed_at REAL NOT NULL DEFAULT 0,
 UNIQUE(task_id, owner_id, position), UNIQUE(task_id, url)
);
CREATE TABLE IF NOT EXISTS answer_drafts (
 id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL REFERENCES questions(id),
 text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready'
);
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 account_id INTEGER NOT NULL REFERENCES accounts(id), question_id INTEGER NOT NULL REFERENCES questions(id),
 kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'reserved', session_id TEXT NOT NULL,
 reserved_until REAL NOT NULL, body TEXT NOT NULL DEFAULT '',
 draft_id INTEGER REFERENCES answer_drafts(id), result TEXT, error TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_account ON operations(account_id)
 WHERE state IN ('reserved','submitting');
CREATE UNIQUE INDEX IF NOT EXISTS active_question ON operations(question_id)
 WHERE state IN ('reserved','submitting');
CREATE TABLE IF NOT EXISTS answer_attempts (
 question_id INTEGER NOT NULL REFERENCES questions(id), account_id INTEGER NOT NULL REFERENCES accounts(id),
 status TEXT NOT NULL, PRIMARY KEY(question_id, account_id)
);
CREATE TABLE IF NOT EXISTS generation_jobs (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL,
 owner_id INTEGER REFERENCES accounts(id), question_id INTEGER REFERENCES questions(id),
 state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS question_generation ON generation_jobs(task_id,owner_id)
 WHERE kind='questions' AND state IN ('pending','running','failed');
CREATE UNIQUE INDEX IF NOT EXISTS answer_generation ON generation_jobs(question_id)
 WHERE kind='answers' AND state IN ('pending','running','failed');
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        # 老任务的个人回答目标保留为 0，不追溯改变已启动任务的规则。
        with self.transaction() as db:
            additions = {
                "accounts": {"alias": "TEXT NOT NULL DEFAULT ''"},
                "tasks": {"account_answer_target": "INTEGER NOT NULL DEFAULT 0 CHECK(account_answer_target>=0 AND account_answer_target<=question_target)"},
                "members": {"answer_count": "INTEGER", "answer_synced_at": "REAL"},
            }
            for table, columns in additions.items():
                present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in present:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, parameters=()):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(sql, parameters)]
