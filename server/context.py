import json
from .errors import DomainError


def build_context(db, cfg, cid, owner, content, retry_user_id=None):
    rows=db.all('SELECT * FROM messages WHERE conversation_id=? AND owner_id=? ORDER BY created_at,id',(cid,owner))
    messages=[{'role':'system','content':'你是知序，一个诚实、清晰的中文助手。不要捏造材料、引用或已执行的动作。'}]
    ids=[]
    for row in rows:
        if row['role']!='user':
            continue
        if row['id']==retry_user_id:
            break
        messages.append({'role':'user','content':row['content']});ids.append(row['id'])
        answers=[a for a in rows if a['parent_user_message_id']==row['id'] and a['status']=='completed']
        if answers:
            answer=max(answers,key=lambda a:a['answer_version'])
            messages.append({'role':'assistant','content':answer['content']});ids.append(answer['id'])
    messages.append({'role':'user','content':content})
    estimated=len(json.dumps(messages,ensure_ascii=False,separators=(',',':')).encode())+512+cfg.output_tokens
    if estimated>cfg.context_tokens:
        raise DomainError('CONTEXT_LIMIT',413,'会话超过保守上下文预算，请新建对话',details={'limit':cfg.context_tokens,'actual':estimated,'unit':'estimated_tokens'})
    return messages,ids,{'estimated_tokens':estimated,'context_budget_tokens':cfg.context_tokens,'max_output_tokens':cfg.output_tokens,'estimator':'utf8_bytes_plus_512'}
