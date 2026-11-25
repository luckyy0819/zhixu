import hashlib
import re
import unicodedata
from collections import Counter
from .db import uid
from .document_errors import doc_error


def terms(text):
    value=unicodedata.normalize('NFKC',text).casefold();out=[]
    for token in re.findall(r'[\u3400-\u9fff]+|[a-z0-9]+(?:[.,][0-9]+)*(?:%)?',value):
        if re.fullmatch(r'[\u3400-\u9fff]+',token):
            out.extend(token);out.extend(token[i:i+2] for i in range(len(token)-1))
        else:
            out.append(token)
    return Counter(out)


def publish_index(db,file,result):
    chunks=[];records=0
    for paragraph in result['paragraphs']:
        text=paragraph['text'];number=paragraph['paragraph_no']
        db.run('INSERT INTO paragraphs VALUES(?,?,?,?,?,?)',(file['id'],1,number,paragraph['page'],text,hashlib.sha256(text.encode()).hexdigest()))
        for start in range(0,len(text),700):
            end=min(len(text),start+800);part=text[start:end];words=terms(part)
            if len(words)>2400:
                raise doc_error('INDEX_LIMIT')
            records+=len(words)
            if records>2000000 or len(chunks)>=20000:
                raise doc_error('INDEX_LIMIT')
            chunk_id=uid();chunks.append(chunk_id)
            db.run('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)',(chunk_id,file['id'],file['owner_id'],1,number,start,end,part,len(part.encode())))
            db.conn.executemany('INSERT INTO chunk_terms VALUES(?,?,?)',[(chunk_id,term,count) for term,count in words.items()])
            if end==len(text):
                break
    return len(chunks)
