import json
from .errors import DomainError


def build_context(db, cfg, cid, owner, content, retry_user_id=None, metadata=False):
    rows=db.all('SELECT * FROM messages WHERE conversation_id=? AND owner_id=? ORDER BY created_at,id',(cid,owner))
    messages=[{'role':'system','content':'你是知序，一个诚实、清晰的中文助手。不要捏造材料、引用或已执行的动作。'}]
    ids=[];excluded=[];dependencies=set()
    for row in rows:
        if row['role']!='user':
            continue
        if row['id']==retry_user_id:
            break
        messages.append({'role':'user','content':row['content']});ids.append(row['id'])
        answers=[a for a in rows if a['parent_user_message_id']==row['id'] and a['status']=='completed']
        if answers:
            answer=max(answers,key=lambda a:a['answer_version'])
            sources=db.all('''SELECT d.file_id,f.deleted_at,cf.enabled FROM message_source_dependencies d JOIN files f ON f.id=d.file_id
 JOIN conversation_files cf ON cf.file_id=f.id AND cf.version=f.version WHERE d.message_id=? AND d.owner_id=?''',(answer['id'],owner))
            unavailable=[s for s in sources if s['deleted_at'] or not s['enabled']]
            if unavailable:
                excluded.append({'message_id':answer['id'],'reason':'source_deleted' if any(s['deleted_at'] for s in unavailable) else 'source_disabled'})
                continue
            dependencies.update(s['file_id'] for s in sources)
            messages.append({'role':'assistant','content':answer['content']});ids.append(answer['id'])
    messages.append({'role':'user','content':content})
    estimated=len(json.dumps(messages,ensure_ascii=False,separators=(',',':')).encode())+512+cfg.output_tokens
    if estimated>cfg.context_tokens:
        raise DomainError('CONTEXT_LIMIT',413,'会话超过保守上下文预算，请新建对话',details={'limit':cfg.context_tokens,'actual':estimated,'unit':'estimated_tokens'})
    result=(messages,ids,{'estimated_tokens':estimated,'context_budget_tokens':cfg.context_tokens,'max_output_tokens':cfg.output_tokens,'estimator':'utf8_bytes_plus_512'})
    return (*result,excluded,sorted(dependencies)) if metadata else result
