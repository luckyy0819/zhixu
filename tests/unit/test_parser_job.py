"""ZHIXU-TASK005 W4: actual Windows job, no in-process fallback."""
import asyncio
import json
from pathlib import Path


def test_actual_suspended_job_parses_utf8(tmp_path):
    from server.process_limits import parse_in_job
    source=tmp_path/'source';source.write_text('培训预算7,600元。\n\n仅供合成测试。',encoding='utf-8')
    result=asyncio.run(parse_in_job(source,'txt',tmp_path/'parsed.json'))
    assert result['paragraphs'][0]['text']=='培训预算7,600元。'
    assert result['paragraphs'][0]['page'] is None
    assert result['isolation']['assigned_before_resume'] is True
    assert result['isolation']['memory_bytes']==536870912
    assert result['isolation']['active_process_limit']==1


def test_invalid_utf8_fails_without_fake_success(tmp_path):
    from server.process_limits import parse_in_job
    from server.errors import DomainError
    import pytest
    source=tmp_path/'source';source.write_bytes(bytes([255,254,0]))
    with pytest.raises(DomainError,match='TEXT_ENCODING'):
        asyncio.run(parse_in_job(source,'txt',tmp_path/'parsed.json'))
