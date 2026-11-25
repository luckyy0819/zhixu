import asyncio,json
import pytest
from server.document_claims import claim_stream,validate_claim
from server.errors import DomainError

def collect(parts):
    async def source():
        for part in parts:yield part
    async def run():return [c async for c in claim_stream(source(),{})]
    return asyncio.run(run())

def test_claim_stream_requires_complete_valid_bounded_records():
    value={'kind':'no_answer','text':'资料不足','sources':[]};line=json.dumps(value,ensure_ascii=False)
    assert len(collect(['\n',line[:5],line[5:]+'\n',line]))==2
    for parts in [[],['{}'],['not json\n'],['not json'],[' '*8193],['"'+'x'*8192+'"\n'],['x'*65537]]:
        with pytest.raises(DomainError):collect(parts)
    for value in [[],{'kind':'unknown','text':'x','sources':[]},{'kind':'no_answer','text':'','sources':[]},{'kind':'fact','text':'x','sources':[]},{'kind':'fact','text':'x','sources':[{}]}]:
        with pytest.raises(DomainError):validate_claim(value,{})
