"""Parser code unit tests on safe generated fixtures, separate from Job end-to-end tests."""
import json,runpy,sys,zipfile
import pytest
from pypdf import PdfWriter
from server import parser_worker as worker
from test_document_formats import make_pdf,make_docx

def test_worker_entry_and_safe_format_limits(tmp_path,monkeypatch):
    text=tmp_path/'unit.txt';text.write_text('\ufeff甲\r\n\r\n预算7600',encoding='utf-8')
    result=worker.parse(text,'txt');assert len(result['paragraphs'])==2
    assert result['paragraphs'][0]['text']=='甲'
    for kind,maker in [('pdf',make_pdf),('docx',make_docx)]:
        p=tmp_path/('safe.'+kind);maker(p);result=worker.parse(p,kind);assert result['paragraphs'][0]['paragraph_no']==1
    output=tmp_path/'result.json';monkeypatch.setattr(sys,'argv',['parser_worker',str(text),'txt',str(output),str(tmp_path)])
    runpy.run_module('server.parser_worker',run_name='__main__')
    assert json.loads(output.read_text(encoding='utf-8'))['data']['paragraphs'][0]['text']=='甲'
    text.write_bytes(b'\xff');runpy.run_module('server.parser_worker',run_name='__main__');assert json.loads(output.read_text())['error']=='TEXT_ENCODING'
    for content,code in [(b'','EMPTY_DOCUMENT'),(b'abc\0','BINARY_TEXT'),(b'a'*5242881,'TEXT_LIMIT')]:
        text.write_bytes(content)
        with pytest.raises(worker.ParseError,match=code):worker.parse(text,'md')
    p=tmp_path/'blank.pdf';writer=PdfWriter();writer.add_blank_page(100,100)
    with p.open('wb') as out:writer.write(out)
    with pytest.raises(worker.ParseError,match='NO_EXTRACTABLE_TEXT'):worker.parse(p,'pdf')
    writer.encrypt('unit-password')
    with p.open('wb') as out:writer.write(out)
    with pytest.raises(worker.ParseError,match='ENCRYPTED_DOCUMENT'):worker.parse(p,'pdf')
    writer=PdfWriter()
    for _ in range(201):writer.add_blank_page(100,100)
    with p.open('wb') as out:writer.write(out)
    with pytest.raises(worker.ParseError,match='PAGE_LIMIT'):worker.parse(p,'pdf')
    broken=tmp_path/'empty.docx'
    with zipfile.ZipFile(broken,'w') as z:z.writestr('notice.txt','ordinary incomplete archive')
    with pytest.raises(worker.ParseError,match='CORRUPT_DOCUMENT'):worker.parse(broken,'docx')

def test_partial_pdf_pages_warning_is_explicit(tmp_path):
    from pypdf import PdfReader
    p=tmp_path/'partial.pdf';make_pdf(p);r=PdfReader(p);writer=PdfWriter();writer.add_page(r.pages[0]);writer.add_blank_page(100,100)
    with p.open('wb') as out:writer.write(out)
    result=worker.parse(p,'pdf');assert result['page_count']==2 and result['warnings'][0]['pages']==[2]
