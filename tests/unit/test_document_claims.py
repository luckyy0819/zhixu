import pytest
from server.errors import DomainError


def test_claim_requires_exact_unicode_quote_and_offsets():
    from server.document_claims import validate_claim
    source={'token':{'file_id':'f','version':1,'chunk_id':'c','paragraph_no':1,'page':2,'start':0,'end':13,'text':'培训预算为7,600元。'}}
    good={'kind':'fact','text':'预算7,600元。','sources':[{'source_token':'token','quote':'7,600','start':5,'end':10}]}
    claim,citations=validate_claim(good,source)
    assert citations[0]['quote']=='7,600'
    bad={**good,'sources':[{**good['sources'][0],'end':11}]}
    with pytest.raises(DomainError,match='CITATION_INVALID'):
        validate_claim(bad,source)
    assert validate_claim({'kind':'no_answer','text':'资料未记载日期。','sources':[]},source)[0]['kind']=='no_answer'
