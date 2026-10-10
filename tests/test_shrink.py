import base64
import io
import re
import subprocess
import tempfile
import zipfile

import pytest

from conftest import bp_xml, md_xml, PDF
from pdfs import needs_gs, scan_pdf, text_pdf
from curator import documents as D
from curator import pdfops
from curator.xmlstore import XmlDoc


# ---- payload encoding (no external tools) ----------------------------------------------------------------

def test_encode_like_single_line_and_wrapped():
    data = bytes(range(256)) * 3
    assert pdfops.encode_like(b'QUJD', data) == base64.b64encode(data)
    wrapped = base64.b64encode(b'x' * 200)
    orig = b'\n'.join(wrapped[i:i + 72] for i in range(0, len(wrapped), 72)) + b'\n'
    out = pdfops.encode_like(orig, data)
    lines = out.split(b'\n')
    assert all(len(l) == 72 for l in lines[:-2]) and lines[-1] == b'' and b'\r' not in out
    assert base64.b64decode(out) == data
    crlf = pdfops.encode_like(orig.replace(b'\n', b'\r\n'), data)
    assert b'\r\n' in crlf and base64.b64decode(crlf) == data


def test_build_payload_keeps_zip_member_name():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('Scan 0001.pdf', PDF)
    inner = base64.b64encode(buf.getvalue())
    new = pdfops.build_payload('zip-b64', inner, b'%PDF-new')
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(new))) as z:
        assert z.namelist() == ['Scan 0001.pdf'] and z.read('Scan 0001.pdf') == b'%PDF-new'


# ---- PDF operations --------------------------------------------------------------------------------------

def _images(pdf):
    with tempfile.NamedTemporaryFile(suffix='.pdf') as f:
        f.write(pdf)
        f.flush()
        out = subprocess.run(['pdfimages', '-list', f.name], capture_output=True, text=True).stdout
    return len(out.splitlines()) - 2


@needs_gs
def test_analyse_kinds():
    a = pdfops.analyse(text_pdf())
    assert a['kind'] == 'text' and a['pages'] == 2
    assert a['page_info'][0]['images'] == 1
    s = pdfops.analyse(scan_pdf())
    assert s['kind'] == 'scan' and s['pages'] == 2 and s['max_image_ppi'] == 200


@needs_gs
def test_strip_keeps_text_and_drops_images():
    pdf = text_pdf()
    out = pdfops.strip_images(pdf)
    assert _images(out) == 0
    v = pdfops.verify_text(pdf, out)
    assert v['identical'] and v['words'] > 10


@needs_gs
def test_compress_scan_is_g4_and_smaller():
    pdf = scan_pdf()
    a = pdfops.analyse(pdf)
    out = pdfops.compress(pdf, a)
    assert pdfops.analyse(out)['pages'] == 2
    assert len(out) < len(pdf)
    with tempfile.NamedTemporaryFile(suffix='.pdf') as f:
        f.write(out)
        f.flush()
        listing = subprocess.run(['pdfimages', '-list', f.name], capture_output=True, text=True).stdout
    assert 'ccitt' in listing


@needs_gs
def test_compress_text_pdf_keeps_text():
    pdf = text_pdf()
    out = pdfops.compress(pdf, pdfops.analyse(pdf))
    assert pdfops.verify_text(pdf, out)['identical']


def test_strip_warnings():
    a = {'page_info': [{'page': 1, 'kind': 'text', 'image_coverage': 0.1},
                       {'page': 2, 'kind': 'image', 'image_coverage': 1.0},
                       {'page': 3, 'kind': 'text', 'image_coverage': 0.6}]}
    w = pdfops.strip_warnings(a)
    assert len(w) == 2 and w[0].startswith('Page 2') and w[1].startswith('Page 3')


# ---- web flow --------------------------------------------------------------------------------------------

def _token(client):
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)


def _upload(client, data, name='x.xml'):
    tok = _token(client)
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), name)}, content_type='multipart/form-data')
    return r.headers['Location'].split('/w/')[1].split('/')[0], tok


@pytest.fixture
def sync_client(client, monkeypatch):
    """Background jobs run synchronously and only when asked, so tests are deterministic."""
    import app as app_module
    monkeypatch.setattr(app_module.cands, 'start', lambda *a: None)
    monkeypatch.setattr(app_module.cands, 'start_precompute', lambda *a: None)
    client.cands = app_module.cands
    return client


def _bp_with(pdf):
    """The BP fixture with the first PDF of document 7 replaced by `pdf` (zipped and wrapped like BP does)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('a.pdf', pdf)
    b64 = base64.b64encode(buf.getvalue())
    wrapped = b'\n'.join(b64[i:i + 72] for i in range(0, len(b64), 72))
    data = bp_xml()
    start = data.index(b'<DocumentPage><PageNo>1</PageNo><DocType>PDF </DocType><FileName>a.pdf</FileName><Content>')
    cstart = data.index(b'<Content>', start) + len(b'<Content>')
    cend = data.index(b'</Content>', cstart)
    return data[:cstart] + wrapped + data[cend:]


@needs_gs
def test_shrink_strip_save_and_restore(sync_client):
    c = sync_client
    pdf = text_pdf()
    data = _bp_with(pdf)
    wid, tok = _upload(c, data)
    page = c.get(f'/w/{wid}/doc/bpdoc-7/part/0/shrink').get_data(as_text=True)
    assert 'Preparing the options' in page
    c.cands.compute(wid, 'bpdoc-7', 0)
    page = c.get(f'/w/{wid}/doc/bpdoc-7/part/0/shrink').get_data(as_text=True)
    assert 'Text verified identical' in page
    cid = re.search(r'name="cid" value="([^"]+)"', page).group(1)
    assert c.get(f'/w/{wid}/cand/{cid}/strip.pdf').data.startswith(b'%PDF')

    r = c.post(f'/w/{wid}/doc/bpdoc-7/part/0/shrink/save', data={
        '_csrf': tok, 'cid': cid, 'option': 'strip', 'date': '2020-01-03', 'provider': 'Dr A',
        'category': 'Letter', 'subject': 'Referral & reply'})
    assert r.status_code == 302 and '/documents' in r.headers['Location']

    work = c.get(f'/w/{wid}/download').data
    doc = XmlDoc(work)
    rec = D.record_map(doc)['bpdoc-7']
    name, new_pdf, fmt = D.part_bytes(doc, rec.parts[0])
    assert name == 'a.pdf' and fmt == 'pdf' and _images(new_pdf) == 0
    assert pdfops.verify_text(pdf, new_pdf)['identical']
    # the payload keeps BP's 72-column wrapping
    inner = doc.inner_raw(rec.parts[0].payload)
    assert inner.index(b'\n') == 72
    # everything else in the file is untouched: only the one payload differs
    assert work.replace(inner, b'') == data.replace(XmlDoc(data).inner_raw(D.record_map(XmlDoc(data))['bpdoc-7'].parts[0].payload), b'')

    log = c.get(f'/w/{wid}/changes?format=json').get_json()
    assert log[-1]['action'] == 'shrink' and log[-1]['option'] == 'strip' and log[-1]['new_bytes'] < log[-1]['bytes']
    assert 'shrunk' in c.get(f'/w/{wid}/documents?key=bpdoc-7').get_data(as_text=True)

    # the old candidate is now stale: saving it again is refused
    r = c.post(f'/w/{wid}/doc/bpdoc-7/part/0/shrink/save', data={'_csrf': tok, 'cid': cid, 'option': 'strip'})
    assert 'changed after the preview' in c.get(r.headers['Location']).get_data(as_text=True)

    c.post(f'/w/{wid}/doc/bpdoc-7/revert', data={'_csrf': tok})
    assert c.get(f'/w/{wid}/download').data == data


@needs_gs
def test_shrink_scan_on_md_needs_confirm_only_with_warnings(sync_client):
    c = sync_client
    pdf = scan_pdf()
    data = md_xml().replace(base64.b64encode(PDF), base64.b64encode(pdf), 1)
    wid, tok = _upload(c, data)
    c.cands.compute(wid, 'md-4', 0)
    page = c.get(f'/w/{wid}/doc/md-4/part/0/shrink').get_data(as_text=True)
    assert 'no text layer (it is a scan)' in page            # strip not offered for scans
    cid = re.search(r'name="cid" value="([^"]+)"', page).group(1)
    r = c.post(f'/w/{wid}/doc/md-4/part/0/shrink/save', data={'_csrf': tok, 'cid': cid, 'option': 'compress',
                                                             'subject': 'Scanned letter'})
    assert '/documents' in r.headers['Location']
    doc = XmlDoc(c.get(f'/w/{wid}/download').data)
    rec = D.record_map(doc)['md-4']
    assert rec.subject == 'Scanned letter'
    new_pdf = D.part_bytes(doc, rec.parts[0])[1]
    assert len(new_pdf) < len(pdf) and pdfops.analyse(new_pdf)['pages'] == 2
    assert b'\n' not in doc.inner_raw(rec.parts[0].payload)        # MedicalDirector: single-line base64
    item = doc.root.find('DOCUMENTHANDLING').findall('ITEM')[0]
    assert doc.child_text(item, 'SUBJECT') == 'Scanned letter'


@needs_gs
def test_original_tab_saves_details_only(sync_client):
    c = sync_client
    wid, tok = _upload(c, _bp_with(text_pdf()))
    c.cands.compute(wid, 'bpdoc-7', 0)
    page = c.get(f'/w/{wid}/doc/bpdoc-7/part/0/shrink').get_data(as_text=True)
    cid = re.search(r'name="cid" value="([^"]+)"', page).group(1)
    before = c.get(f'/w/{wid}/download').data
    c.post(f'/w/{wid}/doc/bpdoc-7/part/0/shrink/save', data={'_csrf': tok, 'cid': cid, 'option': 'original',
                                                            'subject': 'Renamed'})
    after = c.get(f'/w/{wid}/download').data
    assert after == before.replace(b'<SUBJECT>Referral &amp; reply</SUBJECT>', b'<SUBJECT>Renamed</SUBJECT>')
    assert c.get(f'/w/{wid}/changes?format=json').get_json()[-1]['action'] == 'metadata'


def test_build_payload_keeps_unzipped_bp_pages_unzipped():
    # Some BP exports store DocumentPage/Content as the plain file (no ZIP wrapper)
    inner = base64.b64encode(PDF)
    wrapped = b'\n'.join(inner[i:i + 72] for i in range(0, len(inner), 72))
    new = pdfops.build_payload('zip-b64', wrapped, b'%PDF-new-content')
    assert base64.b64decode(new) == b'%PDF-new-content'


@needs_gs
def test_compress_text_pdf_removes_colour_and_keeps_text():
    import pikepdf
    pdf = text_pdf()                                   # its picture is a colour (noise) image
    out = pdfops.compress(pdf, pdfops.analyse(pdf))
    assert pdfops.verify_text(pdf, out)['identical']
    with pikepdf.open(io.BytesIO(out)) as p:
        spaces = {str(o.get('/ColorSpace')) for o in p.objects
                  if isinstance(o, pikepdf.Stream) and o.get('/Subtype') == '/Image'}
    assert spaces == {'/DeviceGray'}


def test_bitonal_detection():
    from PIL import Image
    from curator.pdfops import _is_bitonal
    line_art = Image.new('L', (100, 100), 255)
    line_art.paste(0, (10, 10, 60, 20))
    assert _is_bitonal(line_art)
    assert not _is_bitonal(Image.linear_gradient('L').resize((100, 100)))
