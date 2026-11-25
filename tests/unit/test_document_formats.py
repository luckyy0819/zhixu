import asyncio
import zipfile
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
from server.process_limits import parse_in_job


def make_pdf(path):
    writer=PdfWriter();page=writer.add_blank_page(600,800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 14 Tf 40 740 Td (Training budget: 7,600 yuan.) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream)
    with path.open('wb') as output:
        writer.write(output)


def make_chinese_pdf(path):
    from pypdf.generic import ArrayObject,TextStringObject,NumberObject
    writer=PdfWriter();page=writer.add_blank_page(600,800)
    cid=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/CIDFontType0'),NameObject('/BaseFont'):NameObject('/STSong-Light'),
        NameObject('/CIDSystemInfo'):DictionaryObject({NameObject('/Registry'):TextStringObject('Adobe'),NameObject('/Ordering'):TextStringObject('GB1'),NameObject('/Supplement'):NumberObject(2)})})
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type0'),NameObject('/BaseFont'):NameObject('/STSong-Light'),NameObject('/Encoding'):NameObject('/UniGB-UCS2-H'),NameObject('/DescendantFonts'):ArrayObject([writer._add_object(cid)])})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 14 Tf 40 740 Td <'+'培训预算7,600元。'.encode('utf-16-be').hex().encode()+b'> Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream)
    with path.open('wb') as output:
        writer.write(output)


def make_docx(path):
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>培训预算7,600元。</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')


def test_four_formats_in_real_limited_processes(tmp_path):
    for kind in ('pdf','docx','txt','md'):
        source=tmp_path/('sample.'+kind)
        if kind=='pdf':
            make_pdf(source)
        elif kind=='docx':
            make_docx(source)
        else:
            source.write_text('培训预算7,600元。\n\n合成文本。',encoding='utf-8')
        result=asyncio.run(parse_in_job(source,kind,tmp_path/(kind+'.json')))
        assert '7,600' in result['paragraphs'][0]['text']
        assert result['paragraphs'][0]['page']==(1 if kind=='pdf' else None)
        if kind=='docx':
            assert result['paragraphs'][1]['text']=='项目\t金额'
