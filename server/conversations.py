import base64
import hashlib
import json
import time
from uuid import UUID
from .db import now, uid
from .errors import DomainError


def valid_uuid(value):
    try:
        return str(UUID(value))
    except (ValueError,TypeError,AttributeError):
        raise DomainError('VALIDATION_ERROR')


def cursor_decode(value):
    if value is None:
        return None
    try:
        result=json.loads(base64.urlsafe_b64decode(value.encode()+b'='*(-len(value)%4)))
        if not isinstance(result,list) or len(result)!=2 or not all(isinstance(x,str) for x in result):
            raise ValueError()
        valid_uuid(result[1])
        return result
    except Exception:
        raise DomainError('INVALID_CURSOR',400,'分页游标无效，请重新加载列表')


def cursor_encode(row):
    return base64.urlsafe_b64encode(json.dumps([row['updated_at'],row['id']]).encode()).decode().rstrip('=')


class Conversations:
    def __init__(self, db):
        self.db=db

    def owned(self, cid, owner):
        valid_uuid(cid)
        row=self.db.one('SELECT * FROM conversations WHERE id=? AND owner_id=? AND deleted_at IS NULL',(cid,owner))
        if not row:
            raise DomainError('NOT_FOUND',404,'会话不存在或不可访问')
        return row

    def dto(self, row):
        active=self.db.one("SELECT id FROM generation_requests WHERE conversation_id=? AND owner_id=? AND status IN ('queued','running')",(row['id'],row['owner_id']))
        return {k:row[k] for k in ('id','title','revision','created_at','updated_at')} | {'active_request_id':active['id'] if active else None}

    def lookup_key(self, request, owner, body):
        key=valid_uuid(request.headers.get('idempotency-key'))
        route=request.method+' '+request.url.path
        hashed=hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.db.run('DELETE FROM idempotency_keys WHERE expires_at<?',(time.time(),))
        row=self.db.one('SELECT * FROM idempotency_keys WHERE owner_id=? AND route=? AND key=?',(owner,route,key))
        if row and row['payload_hash']!=hashed:
            raise DomainError('IDEMPOTENCY_CONFLICT',409,'同一请求编号对应的内容已改变')
        return row,(owner,route,key,hashed)

    def save_key(self, fields, kind, rid):
        self.db.run('INSERT INTO idempotency_keys VALUES(?,?,?,?,?,?,?,?)',(*fields,kind,rid,now(),time.time()+86400))

    def create(self, request, owner, data):
        title=data.title.strip()
        if not title:
            raise DomainError('VALIDATION_ERROR')
        with self.db.transaction():
            existing,key=self.lookup_key(request,owner,data.model_dump())
            if existing:
                return self.dto(self.owned(existing['resource_id'],owner))
            cid=uid(); t=now()
            self.db.run('INSERT INTO conversations VALUES(?,?,?,?,1,1,?,?,NULL)',(cid,owner,title,'default' if title=='新对话' else 'manual',t,t))
            self.save_key(key,'conversation',cid)
            return self.dto(self.owned(cid,owner))

    def listing(self, owner, q, cursor, limit):
        if not 1<=limit<=100 or len(q)>100:
            raise DomainError('VALIDATION_ERROR')
        anchor=cursor_decode(cursor)
        escaped=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        sql="SELECT * FROM conversations WHERE owner_id=? AND deleted_at IS NULL AND title LIKE ? ESCAPE '\\'"
        params=[owner,'%'+escaped+'%']
        if anchor:
            sql+=' AND (updated_at,id)<(?,?)';params+=anchor
        rows=self.db.all(sql+' ORDER BY updated_at DESC,id DESC LIMIT ?',(*params,limit+1))
        return dict(items=[self.dto(r) for r in rows[:limit]],next_cursor=cursor_encode(rows[limit-1]) if len(rows)>limit else None)

    def rename(self, cid, owner, data):
        row=self.owned(cid,owner)
        if row['revision']!=data.revision:
            raise DomainError('REVISION_CONFLICT',409,'标题已变更，请重新加载',details={'current':self.dto(row)})
        if not data.title.strip():
            raise DomainError('VALIDATION_ERROR')
        if row['title']!=data.title.strip():
            self.db.run("UPDATE conversations SET title=?,title_source='manual',revision=revision+1,updated_at=? WHERE id=? AND owner_id=?",(data.title.strip(),now(),cid,owner))
        return self.dto(self.owned(cid,owner))

    @staticmethod
    def message(row):
        return {k:row[k] for k in ('id','conversation_id','role','content','status','parent_user_message_id','answer_version','model','created_at')} | {'claims':[],'citations':[]}

    def messages(self, cid, owner, cursor, limit):
        self.owned(cid,owner)
        if not 1<=limit<=100:
            raise DomainError('VALIDATION_ERROR')
        anchor=cursor_decode(cursor)
        sql='SELECT * FROM messages WHERE conversation_id=? AND owner_id=?';params=[cid,owner]
        if anchor:
            sql+=' AND (created_at,id)>(?,?)';params+=anchor
        rows=self.db.all(sql+' ORDER BY created_at,id LIMIT ?',(*params,limit+1))
        next_cursor=None
        if len(rows)>limit:
            next_cursor=cursor_encode({**rows[limit-1],'updated_at':rows[limit-1]['created_at']})
        return dict(items=[self.message(r) for r in rows[:limit]],next_cursor=next_cursor)
