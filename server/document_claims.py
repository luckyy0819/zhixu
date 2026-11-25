import json
from .db import uid,now
from .document_errors import doc_error
from .model import model_error


def decode_claim(line):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    def constant(value):
        raise ValueError('nonstandard JSON constant')
    return json.loads(line,object_pairs_hook=unique,parse_constant=constant)


def validate_claim(value,sources):
    if not isinstance(value,dict) or set(value)!={'kind','text','sources'} or value['kind'] not in ('fact','inference','no_answer'):
        raise model_error('MODEL_FORMAT_ERROR')
    if not isinstance(value['text'],str) or not 1<=len(value['text'])<=2000 or not value['text'].strip() or not isinstance(value['sources'],list) or len(value['sources'])>8:
        raise model_error('MODEL_FORMAT_ERROR')
    if (value['kind']=='no_answer') != (len(value['sources'])==0):
        raise model_error('CITATION_INVALID')
    citations=[];seen=set()
    for source in value['sources']:
        if not isinstance(source,dict) or set(source)!={'source_token','quote','start','end'}:
            raise model_error('CITATION_INVALID')
        row=sources.get(source['source_token']) if isinstance(source['source_token'],str) else None
        start,end=source['start'],source['end'];quote=source['quote']
        if not row or type(start) is not int or type(end) is not int or not isinstance(quote,str) or not 1<=len(quote)<=300:
            raise model_error('CITATION_INVALID')
        if not row['start']<=start<end<=row['end'] or row['text'][start-row['start']:end-row['start']]!=quote:
            raise model_error('CITATION_INVALID')
        key=(row['chunk_id'],start,end)
        if key in seen:
            raise model_error('CITATION_INVALID')
        seen.add(key)
        citations.append(dict(file_id=row['file_id'],version=row['version'],chunk_id=row['chunk_id'],paragraph=row['paragraph_no'],page=row['page'],start=start,end=end,quote=quote,source_status='available'))
    return {'kind':value['kind'],'text':value['text']},citations


async def claim_stream(upstream,sources):
    buffer='';total=0;count=0
    async for part in upstream:
        total+=len(part.encode())
        if total>65536:
            raise model_error('MODEL_FORMAT_ERROR')
        buffer+=part
        while '\n' in buffer:
            line,buffer=buffer.split('\n',1)
            if not line.strip():
                continue
            if len(line.encode())>8192:
                raise model_error('MODEL_FORMAT_ERROR')
            try:
                value=decode_claim(line)
            except ValueError:
                raise model_error('MODEL_FORMAT_ERROR')
            yield validate_claim(value,sources);count+=1
        if len(buffer.encode())>8192:
            raise model_error('MODEL_FORMAT_ERROR')
    if buffer.strip():
        try:
            value=decode_claim(buffer)
        except ValueError:
            raise model_error('MODEL_FORMAT_ERROR')
        yield validate_claim(value,sources);count+=1
    if not count:
        raise model_error('MODEL_FORMAT_ERROR')


def claim_dtos(db,mid):
    return [{k:r[k] for k in ('claim_index','kind','text','content_start','content_end')} for r in db.all('SELECT * FROM message_claims WHERE message_id=? ORDER BY claim_index',(mid,))]


def citation_dtos(db,mid):
    rows=db.all('''SELECT c.*,f.deleted_at,cf.enabled FROM citations c JOIN files f ON c.file_id=f.id
 JOIN conversation_files cf ON cf.file_id=f.id AND cf.version=f.version WHERE c.message_id=? ORDER BY c.claim_index,c.id''',(mid,))
    result=[]
    for row in rows:
        dto={k:row[k] for k in ('id','message_id','claim_index','file_id','version','page','start','end')}
        deleted=bool(row['deleted_at'])
        dto.update(chunk_id=row['source_chunk_id'],paragraph=row['paragraph_no'],source_status='deleted' if deleted else 'available' if row['enabled'] else 'disabled',quote='' if deleted else row['quote'])
        result.append(dto)
    return result


def persist_claim(db,request,claim,citations):
    mid=request['assistant_message_id'];owner=request['owner_id'];cid=request['conversation_id'];rid=request['id']
    previous=db.one('SELECT content FROM messages WHERE id=?',(mid,))['content']
    index=db.one('SELECT count(*) AS n FROM message_claims WHERE message_id=?',(mid,))['n']
    append=('\n\n' if index else '')+claim['text'];start=len(previous)+(2 if index else 0)
    dto={**claim,'claim_index':index,'content_start':start,'content_end':start+len(claim['text'])}
    db.run('INSERT INTO message_claims VALUES(?,?,?,?,?,?,?,?)',(mid,cid,owner,index,claim['kind'],claim['text'],dto['content_start'],dto['content_end']))
    db.run('UPDATE messages SET content=content||?,updated_at=? WHERE id=?',(append,now(),mid))
    db.event(rid,'delta',{'append':append,'claim_index':index,'claim':dto})
    for c in citations:
        citation_id=uid()
        db.run('''INSERT INTO citations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (citation_id,owner,mid,rid,cid,c['file_id'],c['version'],c['chunk_id'],c['chunk_id'],'available',c['paragraph'],c['page'],c['start'],c['end'],c['quote'],index))
        db.event(rid,'citation',dict(id=citation_id,message_id=mid,claim_index=index,**c))
