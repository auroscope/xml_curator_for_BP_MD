"""TIFF and BMP scans converted to PDF (curator/imageops.py, the shrink screen, Bulk and the single-file tab)."""
import base64
import io
import re
import zipfile

from conftest import _zip_b64, bp_xml, md_xml
from PIL import Image, ImageDraw

from curator import documents as D
from curator import imageops
from curator import shrink as S


def _page(mode, colour=False, size=(600, 800)):
    im = Image.new('RGB', size, (250, 248, 240))
    d = ImageDraw.Draw(im)
    for i in range(30):
        d.text((40, 40 + i * 24), f'Line {i}: a synthetic scanned letter.', fill=(10, 10, 10))
    if colour:
        d.ellipse((200, 300, 400, 500), fill=(210, 40, 40))
    return im.convert(mode)


def _tiff(*pages, **kw):
    b = io.BytesIO()
    pages[0].save(b, 'TIFF', save_all=True, append_images=list(pages[1:]), **kw)
    return b.getvalue()


def _bmp(page):
    b = io.BytesIO()
    page.save(b, 'BMP')
    return b.getvalue()


TIF = _tiff(_page('1'), _page('L'), _page('RGB', colour=True), dpi=(200, 200))
BMP = _bmp(_page('RGB'))


# ---- conversions -----------------------------------------------------------------------------------------

def test_multi_page_tiff_keeps_every_page_and_pixel():
    a = imageops.analyse(TIF, 'tif')
    assert a['pages'] == 3 and [p['kind'] for p in a['page_info']] == ['bw', 'bitonal', 'colour']
    assert a['colour'] and not a['all_bw'] and not a['dpi_guessed'] and a['page_info'][0]['dpi'] == 200
    pdf = imageops.lossless_pdf(TIF)
    assert pdf.startswith(b'%PDF-') and imageops.verify_lossless(TIF, pdf)
    import pikepdf
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 3
        w_pt = float(doc.pages[0].mediabox[2])
        assert abs(w_pt - 600 / 200 * 72) < 1                      # 3 inches wide on paper, as the TIFF says


def test_lossless_check_notices_changed_pixels():
    other = _tiff(_page('1'), _page('L'), _page('RGB'), dpi=(200, 200))     # last page without the red circle
    assert not imageops.verify_lossless(TIF, imageops.lossless_pdf(other))


def test_bmp_without_resolution_is_sized_as_a4_and_grey_rgb_stored_as_grey():
    a = imageops.analyse(BMP, 'bmp')
    assert a['dpi_guessed'] and a['page_info'][0]['dpi'] == max(100, round(800 / imageops.A4_LONG_IN))
    grey_rgb = _bmp(_page('L').convert('RGB'))
    pdf = imageops.lossless_pdf(grey_rgb)
    assert imageops.verify_lossless(grey_rgb, pdf) and b'/DeviceGray' in pdf


def test_photo_option_keeps_colour_and_unusual_modes_are_noted():
    pdf = imageops.photo_pdf(TIF)
    assert b'/DCTDecode' in pdf and b'/DeviceRGB' in pdf               # the colour page stays in colour
    rgba = io.BytesIO()
    _page('RGBA').save(rgba, 'TIFF')
    a = imageops.analyse(rgba.getvalue(), 'tif')
    assert a['notes'] == ['transparent areas were filled with white']


# ---- in an export: shrink, save as PDF, restore ----------------------------------------------------------

def _bp_with_tiff():
    s = bp_xml().decode('latin-1')
    s = s.replace('<DocType>PDF </DocType><FileName>c.pdf</FileName>', '<DocType>TIF </DocType><FileName>c.tif</FileName>', 1)
    i = s.index('<FileName>c.tif</FileName><Content>') + len('<FileName>c.tif</FileName><Content>')
    return (s[:i] + _zip_b64('scan.tif', TIF) + s[s.index('</Content>', i):]).encode('latin-1')


def _md_with_bmp():
    s = md_xml().decode('utf-8').replace('<FILENAME>z.PDF</FILENAME>', '<FILENAME>z.BMP</FILENAME>', 1)
    i = s.index('<FILENAME>z.BMP</FILENAME><BASE64_DATA>') + len('<FILENAME>z.BMP</FILENAME><BASE64_DATA>')
    return (s[:i] + base64.b64encode(BMP).decode() + s[s.index('</BASE64_DATA>', i):]).encode('utf-8')


def _token(client):
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)


def _open(client, tok, data, key):
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), 'export.xml')},
                    content_type='multipart/form-data')
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    import app as A
    cands = A.cands
    cands.compute(wid, key, 0)
    doc = client.ws.load(wid)
    rec = D.record_map(doc)[key]
    return wid, cands, S.cand_id(doc, rec, rec.parts[0])


def test_bp_tiff_becomes_a_pdf_with_its_type_and_names_changed(client):
    tok = _token(client)
    wid, cands, cid = _open(client, tok, _bp_with_tiff(), 'bpdoc-9')
    res = cands.state(wid, cid)['result']
    assert res['source'] == 'tif' and res['imgpdf']['verification'] == {'kind': 'pixels', 'identical': True}
    assert res['imgjpeg'].get('available') is not None and res['text'].get('status') in ('ready', 'error')
    page = client.get(f'/w/{wid}/doc/bpdoc-9/part/0/shrink').get_data(as_text=True)
    assert re.findall(r'data-tab="(\w+)"', page) == ['original', 'imgpdf', 'compress', 'imgjpeg', 'text']
    assert 'TIFF → PDF' in page and 'Exactly the same pixels' in page

    client.post(f'/w/{wid}/doc/bpdoc-9/part/0/shrink/save', data={'_csrf': tok, 'cid': cid, 'option': 'imgpdf'})
    doc = client.ws.load(wid)
    p = D.record_map(doc)['bpdoc-9'].parts[0]
    assert p.fmt == 'pdf'
    assert doc.text(p.node.find('DocType')) == 'PDF ' and doc.text(p.node.find('FileName')) == 'c.pdf'
    raw = base64.b64decode(re.sub(rb'\s+', b'', doc.inner_raw(p.payload)))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert z.namelist() == ['scan.pdf'] and z.read('scan.pdf').startswith(b'%PDF-')
    log = client.ws.changes(wid)[-1]
    assert log['option'] == 'imgpdf' and log['converted_from'] == 'tif'
    assert 'TIFF converted to PDF' in client.get(f'/w/{wid}/changes').get_data(as_text=True)

    # restoring the document brings the TIFF back, with its own type and names
    client.post(f'/w/{wid}/doc/bpdoc-9/revert', data={'_csrf': tok})
    doc = client.ws.load(wid)
    p = D.record_map(doc)['bpdoc-9'].parts[0]
    assert p.fmt == 'tif' and doc.text(p.node.find('DocType')) == 'TIF '
    assert D.part_bytes(doc, p)[1] == TIF


def test_md_bmp_becomes_a_pdf_and_its_file_name_keeps_its_case(client):
    tok = _token(client)
    wid, cands, cid = _open(client, tok, _md_with_bmp(), 'md-5')
    client.post(f'/w/{wid}/doc/md-5/part/0/shrink/save', data={'_csrf': tok, 'cid': cid, 'option': 'compress'})
    doc = client.ws.load(wid)
    p = D.record_map(doc)['md-5'].parts[0]
    assert p.fmt == 'pdf' and doc.text(p.node.find('FILENAME')) == 'z.PDF'
    assert D.part_bytes(doc, p)[1].startswith(b'%PDF-')


def test_bulk_offers_lossless_conversion_and_black_and_white_only_without_colour(client):
    tok = _token(client)
    wid, cands, _cid = _open(client, tok, _md_with_bmp(), 'md-5')
    import app as A
    plan = A._bulk_plan(wid, client.ws.load(wid))
    assert [e['rec'].key for e in plan['imgpdf']] == ['md-5'] and [e['rec'].key for e in plan['compress']] == ['md-5']
    r = client.post(f'/w/{wid}/bulk/shrink', data={'_csrf': tok, 'option': 'imgpdf'})
    assert r.status_code == 302
    doc = client.ws.load(wid)
    assert D.record_map(doc)['md-5'].parts[0].fmt == 'pdf'
    assert client.ws.changes(wid)[-1]['bulk'] and A._bulk_plan(wid, doc)['compress'] == []   # first applied wins

    wid2, _c, _i = _open(client, tok, _bp_with_tiff(), 'bpdoc-9')
    plan = A._bulk_plan(wid2, client.ws.load(wid2))
    assert 'bpdoc-9' not in [e['rec'].key for e in plan['compress']]                      # it has a colour page


def test_pdf_options_are_unchanged_for_pdfs():
    assert S.PDF_OPTIONS == ('strip', 'compress')
    assert D.pdf_name('Scan 1.TIFF') == 'Scan 1.PDF' and D.pdf_name('a.mtif') == 'a.pdf' and D.pdf_name('x') == 'x.pdf'


# ---- single file tab -------------------------------------------------------------------------------------

def test_single_tiff_file_is_converted_and_downloaded_as_pdf(client):
    tok = _token(client)
    r = client.post('/file_compress', data={'_csrf': tok, 'file': (io.BytesIO(TIF), 'old scan.tif')},
                    content_type='multipart/form-data')
    fid = r.headers['Location'].rsplit('/', 1)[1]
    import time
    import app as A
    for _ in range(300):                       # the upload started the conversion in the background
        st = A.files.state(fid, '')
        if st['status'] == 'done' and (st['result'].get('text') or {}).get('status') != 'running':
            break
        time.sleep(0.1)
    page = client.get(f'/file_compress/{fid}').get_data(as_text=True)
    assert re.findall(r'data-tab="(\w+)"', page) == ['original', 'imgpdf', 'compress', 'imgjpeg', 'text']
    r = client.get(f'/file_compress/{fid}/file/imgpdf?download=1')
    assert r.data.startswith(b'%PDF-') and 'old scan.pdf' in r.headers['Content-Disposition']
    assert client.get(f'/file_compress/{fid}/file/original').data.startswith(b'%PDF-')      # preview as PDF
    assert client.get(f'/file_compress/{fid}/file/original?download=1').data == TIF


def test_a_file_that_only_starts_like_a_bmp_is_refused(client):
    tok = _token(client)
    r = client.post('/file_compress', data={'_csrf': tok, 'file': (io.BytesIO(b'BM is not a bitmap'), 'x.bmp')},
                    content_type='multipart/form-data', follow_redirects=True)
    assert 'Only PDF, RTF, TIFF and BMP files' in r.get_data(as_text=True)
