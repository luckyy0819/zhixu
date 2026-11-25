import asyncio
import httpx
import pytest
from server.config import Settings
from server.errors import DomainError


def test_real_stream_decoder_contract(tmp_path):
    from server.model import stream_model
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='test')
    async def run(raw):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=raw))) as client:
            return [x async for x in stream_model(cfg,[{'role':'user','content':'hello'}],client)]
    raw=': comment\n\ndata: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\ndata: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    assert asyncio.run(run(raw)) == ['你好']
    with pytest.raises(DomainError,match='OUTPUT_LIMIT'):
        asyncio.run(run(raw.replace('"stop"','"length"')))
    with pytest.raises(DomainError,match='UPSTREAM_INCOMPLETE'):
        asyncio.run(run(raw.replace('data: [DONE]\n\n','')))
    with pytest.raises(DomainError,match='MODEL_FORMAT_ERROR'):
        asyncio.run(run('data: {invalid}\n\n'))
