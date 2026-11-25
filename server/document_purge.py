"""Dependency-order database purge; caller holds owner gate + one transaction."""
from .idempotency import retire_resource


def purge_conversation(db,cid,owner):
    mids=[r['id'] for r in db.all('SELECT id FROM messages WHERE conversation_id=? AND owner_id=?',(cid,owner))]
    rids=[r['id'] for r in db.all('SELECT id FROM generation_requests WHERE conversation_id=? AND owner_id=?',(cid,owner))]
    fids=[r['id'] for r in db.all('SELECT id FROM files WHERE conversation_id=? AND owner_id=?',(cid,owner))]
    for rid in rids:
        db.run('DELETE FROM generation_events WHERE request_id=?',(rid,))
    for mid in mids:
        for table in ('feedback','citations','message_claims','message_source_dependencies'):
            db.run('DELETE FROM '+table+' WHERE message_id=?',(mid,))
    db.run('DELETE FROM generation_requests WHERE conversation_id=? AND owner_id=?',(cid,owner))
    db.run("DELETE FROM messages WHERE conversation_id=? AND owner_id=? AND role='assistant'",(cid,owner))
    db.run('DELETE FROM messages WHERE conversation_id=? AND owner_id=?',(cid,owner))
    for fid in fids:
        db.run('DELETE FROM chunk_terms WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)',(fid,))
        db.run('DELETE FROM chunks WHERE file_id=?',(fid,))
        db.run('DELETE FROM paragraphs WHERE file_id=?',(fid,))
        db.run('DELETE FROM parse_jobs WHERE file_id=?',(fid,))
    db.run('DELETE FROM conversation_files WHERE conversation_id=? AND owner_id=?',(cid,owner))
    db.run('DELETE FROM upload_reservations WHERE conversation_id=? AND owner_id=?',(cid,owner))
    resources=[cid,*mids,*rids,*fids]
    for resource in resources:
        retire_resource(db,owner,resource)
    db.run('DELETE FROM files WHERE conversation_id=? AND owner_id=?',(cid,owner))
    db.run('DELETE FROM conversations WHERE id=? AND owner_id=?',(cid,owner))
