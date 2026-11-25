import asyncio
import json
import re
import time
from datetime import datetime,timezone,timedelta
from fastapi.responses import StreamingResponse
from .errors import DomainError


def events(request, service, rid, owner):
    row=service.owned(rid,owner)
    values=[request.query_params.get('after'),request.headers.get('last-event-id')]
    after=0
    for value in values:
        if value is not None:
            if not re.fullmatch(r'[0-9]{1,15}',value):
                raise DomainError('INVALID_EVENT_CURSOR',400,'事件游标无效')
            after=max(after,int(value))
    if after>row['last_seq']:
        raise DomainError('EVENT_CURSOR_AHEAD',422,'事件游标超前',details={'last_seq':row['last_seq']})
    expired=datetime.fromisoformat(row['created_at']) < datetime.now(timezone.utc)-timedelta(hours=24)
    if row['events_invalidated'] or expired and after<row['last_seq']:
        raise DomainError('EVENT_HISTORY_EXPIRED',410,'事件已过期，请重新获取请求快照')
    async def generate():
        nonlocal after
        heartbeat=time.monotonic()
        while True:
            current=service.owned(rid,owner)
            rows=service.db.all('SELECT * FROM generation_events WHERE request_id=? AND seq>? ORDER BY seq LIMIT 100',(rid,after))
            for event in rows:
                after=event['seq']
                data=dict(type=event['type'],request_id=rid,seq=after,payload=json.loads(event['payload_json']))
                yield f'id: {after}\nevent: {event["type"]}\ndata: {json.dumps(data,ensure_ascii=False)}\n\n'
            if current['status'] not in ('queued','running') and after>=current['last_seq']:
                return
            if time.monotonic()-heartbeat>=15:
                heartbeat=time.monotonic();yield ': heartbeat\n\n'
            await asyncio.sleep(.1)
    return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache,no-transform','X-Accel-Buffering':'no'})
