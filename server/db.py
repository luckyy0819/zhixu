import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from uuid import uuid4


_last_timestamp = datetime.min.replace(tzinfo=timezone.utc)


def now():
    # Windows wall-clock resolution can give user and assistant the same timestamp.
    # Preserve causal order for the contract's (created_at,id) pagination.
    global _last_timestamp
    current = datetime.now(timezone.utc)
    _last_timestamp = max(current, _last_timestamp + timedelta(microseconds=1))
    return _last_timestamp.isoformat()


def uid():
    return str(uuid4())


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None,check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA secure_delete=ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username_norm TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL,salt TEXT NOT NULL,scrypt_n INTEGER NOT NULL,scrypt_r INTEGER NOT NULL,scrypt_p INTEGER NOT NULL,created_at TEXT NOT NULL,deleted_at TEXT);
CREATE TABLE IF NOT EXISTS auth_sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),token_hash TEXT NOT NULL UNIQUE,
 csrf_hash TEXT NOT NULL,created_at TEXT NOT NULL,expires_at REAL NOT NULL,last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preauth_nonces(token_hash TEXT PRIMARY KEY,csrf_hash TEXT NOT NULL,expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),title TEXT NOT NULL,
 title_source TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,access_epoch INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT,UNIQUE(id,owner_id));
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,owner_id TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,status TEXT NOT NULL,
 parent_user_message_id TEXT REFERENCES messages(id),answer_version INTEGER,model TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(parent_user_message_id,answer_version),UNIQUE(id,owner_id),FOREIGN KEY(conversation_id,owner_id) REFERENCES conversations(id,owner_id));
CREATE TABLE IF NOT EXISTS generation_requests(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,conversation_id TEXT NOT NULL,user_message_id TEXT NOT NULL,
 assistant_message_id TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled','interrupted')),
 mode TEXT NOT NULL,intent TEXT NOT NULL,model TEXT NOT NULL,scope_json TEXT NOT NULL DEFAULT '[]',scope_exclusions_json TEXT NOT NULL DEFAULT '[]',
 history_ids_json TEXT NOT NULL,budget_json TEXT NOT NULL,coverage_json TEXT,source_token_map_json TEXT NOT NULL DEFAULT '{}',
 last_seq INTEGER NOT NULL DEFAULT 0,events_invalidated INTEGER NOT NULL DEFAULT 0,error_code TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,
 dependency_file_ids_json TEXT NOT NULL DEFAULT '[]',snapshot_epoch INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 FOREIGN KEY(conversation_id,owner_id) REFERENCES conversations(id,owner_id),FOREIGN KEY(user_message_id,owner_id) REFERENCES messages(id,owner_id),
 FOREIGN KEY(assistant_message_id,owner_id) REFERENCES messages(id,owner_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_generation ON generation_requests(conversation_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS generation_events(request_id TEXT NOT NULL REFERENCES generation_requests(id),seq INTEGER NOT NULL,
 type TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(request_id,seq));
CREATE TABLE IF NOT EXISTS idempotency_keys(owner_id TEXT NOT NULL REFERENCES users(id),route TEXT NOT NULL,key TEXT NOT NULL,
 payload_hash TEXT NOT NULL,resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,created_at TEXT NOT NULL,expires_at REAL NOT NULL,PRIMARY KEY(owner_id,route,key));
''')
        self.conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)',(now(),))
        from .document_schema import migrate
        migrate(self)

    def one(self, sql, args=()):
        row = self.conn.execute(sql,args).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql,args).fetchall()]

    def run(self, sql, args=()):
        return self.conn.execute(sql,args)

    @contextmanager
    def transaction(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def event(self, rid, kind, payload):
        row = self.one('SELECT last_seq FROM generation_requests WHERE id=?',(rid,))
        seq = row['last_seq'] + 1
        if kind in ('done','cancelled','error'):
            payload = {**payload,'last_seq':seq}
        self.run('INSERT INTO generation_events VALUES(?,?,?,?,?)',(rid,seq,kind,json.dumps(payload,ensure_ascii=False),now()))
        self.run('UPDATE generation_requests SET last_seq=?,updated_at=? WHERE id=?',(seq,now(),rid))
        return seq
