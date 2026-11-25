"""ZHIXU-DES002 R1/R6: document migration, only new isolated databases this release."""
SQL='''
CREATE UNIQUE INDEX IF NOT EXISTS message_owner_conversation ON messages(id,owner_id,conversation_id);
CREATE UNIQUE INDEX IF NOT EXISTS message_owner ON messages(id,owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS request_owner_conversation ON generation_requests(id,owner_id,conversation_id);
-- Working supplement ZHIXU-DOCQA-CR-IDEMP-001; frozen BL002/003 unchanged.
CREATE TABLE IF NOT EXISTS idempotency_tombstones(owner_hash TEXT NOT NULL,scope_hash TEXT NOT NULL,key_hash TEXT NOT NULL,
 payload_hash TEXT NOT NULL,expires_at REAL NOT NULL,PRIMARY KEY(owner_hash,scope_hash,key_hash),
 CHECK(length(owner_hash)=64 AND length(scope_hash)=64 AND length(key_hash)=64 AND length(payload_hash)=64));
CREATE INDEX IF NOT EXISTS deleted_keys_expiry ON idempotency_tombstones(expires_at);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
 sha256 TEXT,name TEXT,mime TEXT,size INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1,parser_revision TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('security_check','parsing','indexing','ready','failed','cancelled','deleted')),
 object_key TEXT,page_count INTEGER,paragraph_count INTEGER,warnings_json TEXT NOT NULL DEFAULT '[]',deleted_at TEXT,created_at TEXT NOT NULL,
 UNIQUE(id,owner_id,conversation_id),UNIQUE(id,owner_id),UNIQUE(id,version),FOREIGN KEY(conversation_id,owner_id) REFERENCES conversations(id,owner_id),
 CHECK((deleted_at IS NULL AND name IS NOT NULL AND sha256 IS NOT NULL AND mime IS NOT NULL) OR (deleted_at IS NOT NULL AND name IS NULL AND sha256 IS NULL AND mime IS NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS live_file_hash ON files(owner_id,conversation_id,sha256) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS conversation_files(conversation_id TEXT NOT NULL,file_id TEXT NOT NULL,owner_id TEXT NOT NULL,version INTEGER NOT NULL,
 enabled INTEGER NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(conversation_id,file_id,version),
 FOREIGN KEY(conversation_id,owner_id) REFERENCES conversations(id,owner_id),FOREIGN KEY(file_id,owner_id,conversation_id) REFERENCES files(id,owner_id,conversation_id),FOREIGN KEY(file_id,version) REFERENCES files(id,version));
CREATE TABLE IF NOT EXISTS parse_jobs(id TEXT PRIMARY KEY,file_id TEXT NOT NULL REFERENCES files(id),owner_id TEXT NOT NULL REFERENCES users(id),
 attempt INTEGER NOT NULL,restart_recoveries INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
 phase TEXT NOT NULL,progress_completed INTEGER NOT NULL,progress_total INTEGER,error_code TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,lease_until REAL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(file_id,attempt),FOREIGN KEY(file_id,owner_id) REFERENCES files(id,owner_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_parse ON parse_jobs(file_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS upload_reservations(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,conversation_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
 declared_size INTEGER NOT NULL,received_size INTEGER NOT NULL,temp_key TEXT NOT NULL,expires_at REAL NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(owner_id,conversation_id,idempotency_key),FOREIGN KEY(conversation_id,owner_id) REFERENCES conversations(id,owner_id));
CREATE TABLE IF NOT EXISTS paragraphs(file_id TEXT NOT NULL,version INTEGER NOT NULL,paragraph_no INTEGER NOT NULL,page INTEGER,text TEXT NOT NULL,text_sha256 TEXT NOT NULL,
 PRIMARY KEY(file_id,version,paragraph_no),FOREIGN KEY(file_id,version) REFERENCES files(id,version));
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,file_id TEXT NOT NULL,owner_id TEXT NOT NULL REFERENCES users(id),version INTEGER NOT NULL,paragraph_no INTEGER NOT NULL,
 start INTEGER NOT NULL,end INTEGER NOT NULL,text TEXT NOT NULL,token_count INTEGER NOT NULL,
 UNIQUE(file_id,version,paragraph_no,start,end),FOREIGN KEY(file_id,owner_id) REFERENCES files(id,owner_id),FOREIGN KEY(file_id,version,paragraph_no) REFERENCES paragraphs(file_id,version,paragraph_no));
CREATE TABLE IF NOT EXISTS chunk_terms(chunk_id TEXT NOT NULL REFERENCES chunks(id),term TEXT NOT NULL,frequency INTEGER NOT NULL,PRIMARY KEY(chunk_id,term));
CREATE INDEX IF NOT EXISTS terms_lookup ON chunk_terms(term,chunk_id);
CREATE TABLE IF NOT EXISTS message_claims(message_id TEXT NOT NULL,conversation_id TEXT NOT NULL,owner_id TEXT NOT NULL,claim_index INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('fact','inference','no_answer')),text TEXT NOT NULL,content_start INTEGER NOT NULL,content_end INTEGER NOT NULL,
 PRIMARY KEY(message_id,claim_index),FOREIGN KEY(message_id,owner_id,conversation_id) REFERENCES messages(id,owner_id,conversation_id));
CREATE TABLE IF NOT EXISTS citations(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,message_id TEXT NOT NULL,request_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
 file_id TEXT NOT NULL,version INTEGER NOT NULL,chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,source_chunk_id TEXT NOT NULL,
 source_status TEXT NOT NULL CHECK(source_status IN ('available','disabled','deleted')),paragraph_no INTEGER NOT NULL,page INTEGER,start INTEGER NOT NULL,end INTEGER NOT NULL,quote TEXT NOT NULL,claim_index INTEGER NOT NULL,
 UNIQUE(message_id,claim_index,source_chunk_id,start,end),FOREIGN KEY(message_id,owner_id,conversation_id) REFERENCES messages(id,owner_id,conversation_id),
 FOREIGN KEY(request_id,owner_id,conversation_id) REFERENCES generation_requests(id,owner_id,conversation_id),FOREIGN KEY(message_id,claim_index) REFERENCES message_claims(message_id,claim_index),
 FOREIGN KEY(file_id,owner_id,conversation_id) REFERENCES files(id,owner_id,conversation_id),FOREIGN KEY(file_id,version) REFERENCES files(id,version));
CREATE TABLE IF NOT EXISTS message_source_dependencies(message_id TEXT NOT NULL,file_id TEXT NOT NULL,conversation_id TEXT NOT NULL,owner_id TEXT NOT NULL,
 PRIMARY KEY(message_id,file_id),FOREIGN KEY(message_id,owner_id,conversation_id) REFERENCES messages(id,owner_id,conversation_id),FOREIGN KEY(file_id,owner_id,conversation_id) REFERENCES files(id,owner_id,conversation_id));
CREATE TABLE IF NOT EXISTS feedback(owner_id TEXT NOT NULL REFERENCES users(id),message_id TEXT NOT NULL REFERENCES messages(id),rating TEXT NOT NULL CHECK(rating IN ('up','down')),reason TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(owner_id,message_id),FOREIGN KEY(message_id,owner_id) REFERENCES messages(id,owner_id));
CREATE TABLE IF NOT EXISTS deletion_jobs(id TEXT PRIMARY KEY,owner_id TEXT REFERENCES users(id) ON DELETE SET NULL,owner_ref_hash TEXT NOT NULL,
 resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,receipt_hash TEXT,expires_at REAL NOT NULL,object_keys_json TEXT NOT NULL,status TEXT NOT NULL,
 attempt INTEGER NOT NULL,last_error_code TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(owner_ref_hash,resource_type,resource_id));
CREATE TRIGGER IF NOT EXISTS message_parent_guard BEFORE INSERT ON messages WHEN
 (NEW.role='assistant' AND NOT EXISTS(SELECT 1 FROM messages p WHERE p.id=NEW.parent_user_message_id AND p.owner_id=NEW.owner_id AND p.conversation_id=NEW.conversation_id AND p.role='user'))
 OR (NEW.role='user' AND NEW.parent_user_message_id IS NOT NULL)
 BEGIN SELECT RAISE(ABORT,'message parent ownership'); END;
CREATE TRIGGER IF NOT EXISTS request_message_guard BEFORE INSERT ON generation_requests WHEN
 NOT EXISTS(SELECT 1 FROM messages u JOIN messages a ON a.id=NEW.assistant_message_id WHERE u.id=NEW.user_message_id AND u.owner_id=NEW.owner_id AND a.owner_id=NEW.owner_id
 AND u.conversation_id=NEW.conversation_id AND a.conversation_id=NEW.conversation_id AND u.role='user' AND a.role='assistant' AND a.parent_user_message_id=u.id)
 BEGIN SELECT RAISE(ABORT,'request message ownership'); END;
CREATE TRIGGER IF NOT EXISTS citation_request_message_guard BEFORE INSERT ON citations WHEN
 NOT EXISTS(SELECT 1 FROM generation_requests r WHERE r.id=NEW.request_id AND r.assistant_message_id=NEW.message_id)
 BEGIN SELECT RAISE(ABORT,'citation request message'); END;
'''


def migrate(db):
    db.conn.executescript(SQL)
    from .db import now
    db.run('INSERT OR IGNORE INTO schema_migrations VALUES(2,?)',(now(),))
    db.run('INSERT OR IGNORE INTO schema_migrations VALUES(3,?)',(now(),))
