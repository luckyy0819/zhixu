"""ZHIXU-DOCQA-CR-IDEMP-001: expiring irreversible deletion-key tombstones.

No route, key, owner/resource ID, response, filename or body is retained here.
Access/actor checks remain at the normal entry point, BEFORE this lookup.
"""
import hashlib
import json
import time
from .errors import DomainError


def digest_parts(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def owner_hash(owner):
    return digest_parts('owner',owner)


def identifiers(owner,route,key):
    return owner_hash(owner),digest_parts('scope',owner,route),digest_parts('key',owner,route,key)


def cleanup(db):
    db.run('DELETE FROM idempotency_tombstones WHERE expires_at<=?',(time.time(),))


def check_deleted(db,owner,route,key,payload_hash):
    cleanup(db)
    row=db.one('SELECT payload_hash FROM idempotency_tombstones WHERE owner_hash=? AND scope_hash=? AND key_hash=?',identifiers(owner,route,key))
    if row:
        if row['payload_hash']!=payload_hash:
            raise DomainError('IDEMPOTENCY_CONFLICT',409,'同一请求编号对应的内容已改变')
        raise DomainError('RESOURCE_DELETED',410,'原请求的资源已删除，不会重建或返回原内容')


def retire_resource(db,owner,resource_id):
    # Caller holds the owner revocation gate and the SAME deletion transaction.
    # This is conversion, not a response cache. Never extend an existing expiry.
    for row in db.all('SELECT route,key,payload_hash,expires_at FROM idempotency_keys WHERE owner_id=? AND resource_id=?',(owner,resource_id)):
        if row['expires_at']>time.time():
            db.run('INSERT OR IGNORE INTO idempotency_tombstones VALUES(?,?,?,?,?)',(*identifiers(owner,row['route'],row['key']),row['payload_hash'],row['expires_at']))
    db.run('DELETE FROM idempotency_keys WHERE owner_id=? AND resource_id=?',(owner,resource_id))


def forget_owner(db,owner):
    db.run('DELETE FROM idempotency_tombstones WHERE owner_hash=?',(owner_hash(owner),))
