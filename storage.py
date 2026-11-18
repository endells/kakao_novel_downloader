"""원자적 파일 저장과 표준 EPUB 3 생성. 외부 변환 프로그램이 필요 없습니다."""
import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape


def safe_name(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value)).strip(' .')
    return (value[:120] or 'untitled')


def atomic_bytes(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for part in iter(lambda: stream.read(65536), b''):
            h.update(part)
    return h.hexdigest()


def build_epub(folder, title, sid, chapters):
    """검증된 회차 레코드를 공식 목록 순번으로 정렬하여 합본을 만듭니다."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (safe_name(title) + '.epub')
    fd, temp = tempfile.mkstemp(prefix='.epub-', dir=str(folder))
    os.close(fd)
    try:
        manifest, spine, nav = [], [], []
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            z.writestr('META-INF/container.xml', '<?xml version="1.0"?>'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>')
            for ch in sorted(chapters, key=lambda x: x['number']):
                name = 'chapter-%s.xhtml' % int(ch['pid'])
                key = 'c%s' % int(ch['pid'])
                text = Path(ch['path']).read_text(encoding='utf-8')
                content = ''.join('<p>%s</p>' % escape(p).replace('\n', '<br/>')
                                  for p in text.split('\n\n'))
                z.writestr('OEBPS/' + name, '<?xml version="1.0" encoding="utf-8"?>'
                    '<html xmlns="http://www.w3.org/1999/xhtml" lang="ko"><head><title>'
                    + escape(ch['title']) + '</title></head><body><h1>' + escape(ch['title'])
                    + '</h1>' + content + '</body></html>')
                manifest.append('<item id="%s" href="%s" media-type="application/xhtml+xml"/>' % (key, name))
                spine.append('<itemref idref="%s"/>' % key)
                nav.append('<li><a href="%s">%s</a></li>' % (name, escape(ch['title'])))
            z.writestr('OEBPS/nav.xhtml', '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
                'lang="ko"><head><title>목차</title></head><body><nav epub:type="toc"><h1>목차</h1><ol>'
                + ''.join(nav) + '</ol></nav></body></html>')
            z.writestr('OEBPS/content.opf', '<?xml version="1.0" encoding="utf-8"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier id="book">kakao-novel:%s</dc:identifier><dc:title>%s</dc:title>'
                '<dc:language>ko</dc:language><meta property="dcterms:modified">%s</meta></metadata>'
                '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                '%s</manifest><spine>%s</spine></package>' %
                (int(sid), escape(title), datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                 ''.join(manifest), ''.join(spine)))
        with zipfile.ZipFile(temp) as check:
            if check.testzip() is not None:
                raise OSError('EPUB ZIP 검증 실패')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return str(path)
