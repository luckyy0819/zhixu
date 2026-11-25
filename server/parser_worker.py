"""Private worker: launched -I -S only AFTER Job limits are assigned. No app/db/env imports."""
import json
import re
import stat
import sys
import zipfile
from pathlib import Path,PurePosixPath

MAX_TEXT=5242880
PHASE_FILE=None


def phase(value):
    if PHASE_FILE is not None:
        PHASE_FILE.write_text(value,encoding='ascii')


class ParseError(Exception):
    pass


def text_clean(text):
    text=text.removeprefix('\ufeff').replace('\r\n','\n').replace('\r','\n')
    if any(ord(c)<32 and c not in '\t\n' for c in text):
        raise ParseError('BINARY_TEXT')
    return text


def paragraphs_from(text,page=None):
    return [{'page':page,'text':part} for part in re.split(r'\n[ \t]*\n',text_clean(text)) if part.strip()]


def docx_paragraphs(source):
    from defusedxml.ElementTree import fromstring
    data={};total=0
    with zipfile.ZipFile(source) as archive:
        items=archive.infolist()
        if len(items)>2000:
            raise ParseError('EXPANSION_LIMIT')
        for info in items:
            name=info.filename;parts=PurePosixPath(name).parts
            if name in data or name.startswith('/') or '\\' in name or ':' in name or '..' in parts or stat.S_ISLNK(info.external_attr>>16):
                raise ParseError('UNSAFE_DOCUMENT')
            if info.flag_bits&1 or info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
                raise ParseError('UNSAFE_DOCUMENT')
            if info.file_size>20971520 or info.file_size>100*max(1,info.compress_size):
                raise ParseError('EXPANSION_LIMIT')
            lower=name.lower()
            if 'vbaproject' in lower or lower.startswith('word/embeddings/') or 'oleobject' in lower:
                raise ParseError('UNSAFE_DOCUMENT')
            content=bytearray()
            with archive.open(info) as stream:
                while part:=stream.read(65536):
                    content.extend(part);total+=len(part)
                    if len(content)>20971520 or total>104857600 or len(content)>100*max(1,info.compress_size):
                        raise ParseError('EXPANSION_LIMIT')
            data[name]=bytes(content)
            if name.endswith('.xml') or name.endswith('.rels'):
                tree=fromstring(content,forbid_dtd=True,forbid_entities=True,forbid_external=True)
                if name.endswith('.rels') and any(node.get('TargetMode')=='External' for node in tree.iter()):
                    raise ParseError('UNSAFE_DOCUMENT')
        if '[Content_Types].xml' not in data or 'word/document.xml' not in data:
            raise ParseError('CORRUPT_DOCUMENT')
        types=fromstring(data['[Content_Types].xml'],forbid_dtd=True)
        main='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
        if not any(n.get('PartName')=='/word/document.xml' and n.get('ContentType')==main for n in types):
            raise ParseError('MIME_MISMATCH')
        tree=fromstring(data['word/document.xml'],forbid_dtd=True)
    phase('parsing')
    ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    def content(node):
        return ''.join(n.text or '' if n.tag==ns+'t' else '\t' if n.tag==ns+'tab' else '\n' if n.tag in (ns+'br',ns+'cr') else '' for n in node.iter())
    result=[];body=tree.find(ns+'body')
    if body is None:
        raise ParseError('CORRUPT_DOCUMENT')
    for node in body:
        if node.tag==ns+'p':
            result+=paragraphs_from(content(node))
        elif node.tag==ns+'tbl':
            for row in node.findall(ns+'tr'):
                result+=paragraphs_from('\t'.join(content(cell) for cell in row.findall(ns+'tc')))
    return result,None,[]


def has_image_resource(page):
    # Classification needs object metadata only. page.images would decode image
    # pixels and pull in Pillow even for a legal empty page; OCR is out of scope.
    from pypdf.generic import DictionaryObject,ArrayObject,IndirectObject
    pending=[page.get('/Resources',{})];seen=set()
    while pending:
        value=pending.pop()
        if isinstance(value,IndirectObject):
            value=value.get_object()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if len(seen)>50000:
            raise ParseError('UNSAFE_DOCUMENT')
        if isinstance(value,DictionaryObject):
            if value.get('/Subtype')=='/Image':
                return True
            pending.extend(value.values())
        elif isinstance(value,ArrayObject):
            pending.extend(value)
    return False


def pdf_paragraphs(source):
    from pypdf import PdfReader
    from pypdf.generic import DictionaryObject,ArrayObject,IndirectObject
    reader=PdfReader(source,strict=True)
    if reader.is_encrypted:
        raise ParseError('ENCRYPTED_DOCUMENT')
    if len(reader.pages)>200:
        raise ParseError('PAGE_LIMIT')
    forbidden={'/JavaScript','/JS','/Launch','/OpenAction','/AA','/EmbeddedFiles','/RichMedia','/EF'}
    seen=set();pending=[reader.trailer]
    while pending:
        obj=pending.pop()
        if isinstance(obj,IndirectObject):
            obj=obj.get_object()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if len(seen)>50000:
            raise ParseError('UNSAFE_DOCUMENT')
        if isinstance(obj,DictionaryObject):
            if forbidden.intersection(obj.keys()) or obj.get('/S') in ('/JavaScript','/Launch'):
                raise ParseError('UNSAFE_DOCUMENT')
            pending.extend(obj.values())
        elif isinstance(obj,ArrayObject):
            pending.extend(obj)
    phase('parsing')
    result=[];empty=[];image_seen=False
    for number,page in enumerate(reader.pages,1):
        stream=page.get_contents()
        if stream is not None and len(stream.get_data())>20971520:
            raise ParseError('EXPANSION_LIMIT')
        text=page.extract_text() or ''
        parts=paragraphs_from(text,number)
        if not parts:
            empty.append(number)
            image_seen=image_seen or has_image_resource(page)
        result+=parts
        if sum(len(p['text'].encode('utf-8')) for p in result)>MAX_TEXT:
            raise ParseError('TEXT_LIMIT')
    if not result:
        raise ParseError('SCAN_ONLY' if image_seen else 'NO_EXTRACTABLE_TEXT')
    warnings=[{'code':'PARTIAL_TEXT','pages':empty,'message':'部分页未提取到文字，回答不覆盖这些页面'}] if empty else []
    return result,len(reader.pages),warnings


def parse(source,kind):
    if kind=='pdf':
        paragraphs,pages,warnings=pdf_paragraphs(source)
    elif kind=='docx':
        paragraphs,pages,warnings=docx_paragraphs(source)
    else:
        try:
            text=Path(source).read_bytes().decode('utf-8-sig',errors='strict')
        except UnicodeDecodeError:
            raise ParseError('TEXT_ENCODING')
        text=text_clean(text);phase('parsing')
        paragraphs,pages,warnings=paragraphs_from(text),None,[]
    if not paragraphs:
        raise ParseError('EMPTY_DOCUMENT')
    if len(paragraphs)>50000 or sum(len(p['text'].encode()) for p in paragraphs)>MAX_TEXT:
        raise ParseError('TEXT_LIMIT')
    for number,paragraph in enumerate(paragraphs,1):
        paragraph['paragraph_no']=number
    return {'paragraphs':paragraphs,'page_count':pages,'warnings':warnings,'parser_revision':'pypdf7.1.1-defusedxml0.7.1-r2'}


if __name__=='__main__':
    source,kind,target,deps=sys.argv[1:]
    PHASE_FILE=Path(target+'.phase')
    phase('security_check')
    sys.path.insert(0,deps)
    try:
        output={'data':parse(source,kind)}
    except MemoryError:
        output={'error':'PARSER_RESOURCE_LIMIT'}
    except ParseError as error:
        output={'error':str(error)}
    except Exception as error:
        output={'error':'UNSAFE_DOCUMENT' if type(error).__name__ in ('DTDForbidden','EntitiesForbidden','ExternalReferenceForbidden') else 'CORRUPT_DOCUMENT'}
    encoded=json.dumps(output,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if len(encoded)>16777216:
        encoded=b'{"error":"WORKER_OUTPUT_LIMIT"}'
    Path(target).write_bytes(encoded)
