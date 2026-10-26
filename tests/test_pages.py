"""Removing pages from a PDF (curator/pdfpages.py, the Pages screen for documents and for single files)."""
import base64
import io
import re

import pikepdf
from conftest import _zip_b64, bp_xml
from PIL import Image

from curator import documents as D
from curator import pdfpages


def _pdf(n=4):
    import img2pdf
    pages = []
    for i in range(n):
        b = io.BytesIO()
        Image.effect_noise((300, 400), 20 + i * 10).convert('L').save(b, 'JPEG', quality=70)
        pages.append(b.getvalue())
    return img2pdf.convert(pages)


def _pages(pdf):
    with pikepdf.open(io.BytesIO(pdf)) as d:
        return len(d.pages)


PDF4 = _pdf()


# ---- the module ------------------------------------------------------------------------------------------

def test_removing_pages_keeps_the_rest_and_frees_their_bytes():
    info = pdfpages.info(PDF4)
    assert info['pages'] == 4 and len(info['page_bytes']) == 4 and all(b > 1000 for b in info['page_bytes'])
    new, total = pdfpages.remove_pages(PDF4, [2, 3])
    assert total == 4 and _pages(new) == 2
    assert len(PDF4) - len(new) >= 0.9 * (info['page_bytes'][1] + info['page_bytes'][2])


def test_bookmarks_to_removed_pages_are_dropped():
    with pikepdf.open(io.BytesIO(PDF4)) as d:
        with d.open_outline() as o:
            o.root.extend([pikepdf.OutlineItem('first', 0), pikepdf.OutlineItem('third', 2)])
        b = io.BytesIO()
        d.save(b)
    new, _n = pdfpages.remove_pages(b.getvalue(), [3])
    with pikepdf.open(io.BytesIO(new)) as d, d.open_outline() as o:
        assert [i.title for i in o.root] == ['first']


def test_impossible_removals_are_refused():
    for pages, msg in (([], 'No pages'), ([5], 'there is no page 5'), ([1, 2, 3, 4], 'At least one page')):
        try:
            pdfpages.remove_pages(PDF4, pages)
            assert False, pages
        except pdfpages.PageError as e:
            assert msg in str(e)
    with pikepdf.open(io.BytesIO(PDF4)) as d:
        b = io.BytesIO()
        d.save(b, encryption=pikepdf.Encryption(owner='x', user='y'))
    try:
        pdfpages.info(b.getvalue())
        assert False
    except pdfpages.PageError as e:
        assert 'password' in str(e)


def test_signed_pdf_is_reported():
    with pikepdf.open(io.BytesIO(PDF4)) as d:
        d.Root.AcroForm = pikepdf.Dictionary(SigFlags=3, Fields=pikepdf.Array())
        b = io.BytesIO()
        d.save(b)
    assert pdfpages.info(b.getvalue())['signed'] and not pdfpages.info(PDF4)['signed']


# ---- a document in an export -----------------------------------------------------------------------------

def _token(client):
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)


def _export():
    s = bp_xml().decode('latin-1')
    i = s.index('<FileName>a.pdf</FileName><Content>') + len('<FileName>a.pdf</FileName><Content>')
    return (s[:i] + _zip_b64('scan.pdf', PDF4) + s[s.index('</Content>', i):]).encode('latin-1')


def test_pages_screen_removes_ticked_pages_and_restore_brings_them_back(client):
    tok = _token(client)
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(_export()), 'export.xml')},
                    content_type='multipart/form-data')
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    assert '✂ Pages…' in client.get(f'/w/{wid}/documents?key=bpdoc-7').get_data(as_text=True)
    page = client.get(f'/w/{wid}/doc/bpdoc-7/part/0/pages').get_data(as_text=True)
    assert page.count('name="page"') == 4 and 'Remove ticked pages' in page
    h = re.search(r'name="h" value="([0-9a-f]+)"', page).group(1)
    thumb = client.get(re.search(r'src="([^"]+\.png[^"]*)"', page).group(1).replace('&amp;', '&'))
    assert thumb.mimetype == 'image/png' and thumb.data[:4] == b'\x89PNG'

    assert 'changed after' in client.post(f'/w/{wid}/doc/bpdoc-7/part/0/pages/delete',
                                          data={'_csrf': tok, 'h': 'stale', 'page': ['2']},
                                          follow_redirects=True).get_data(as_text=True)
    r = client.post(f'/w/{wid}/doc/bpdoc-7/part/0/pages/delete', data={'_csrf': tok, 'h': h, 'page': ['2', '3']},
                    follow_redirects=True)
    assert 'Removed pages 2 and 3 of 4' in r.get_data(as_text=True)
    doc = client.ws.load(wid)
    p = D.record_map(doc)['bpdoc-7'].parts[0]
    assert _pages(D.part_bytes(doc, p)[1]) == 2 and doc.text(p.node.find('FileName')) == 'a.pdf'
    raw = base64.b64decode(re.sub(rb'\s+', b'', doc.inner_raw(p.payload)))
    assert raw[:4] == b'PK\x03\x04'                                     # still in its ZIP wrapper
    last = client.ws.changes(wid)[-1]
    assert last['action'] == 'pages' and last['removed'] == [2, 3] and last['of'] == 4
    assert 'pages 2 and 3 of 4 removed' in client.get(f'/w/{wid}/changes').get_data(as_text=True)
    assert client.ws.status_by_key(wid)['bpdoc-7'] == 'modified'
    assert client.get(f'/w/{wid}/changes.pdf').status_code == 200

    client.post(f'/w/{wid}/doc/bpdoc-7/revert', data={'_csrf': tok})
    doc = client.ws.load(wid)
    assert _pages(D.part_bytes(doc, D.record_map(doc)['bpdoc-7'].parts[0])[1]) == 4


def test_pages_screen_is_only_for_pdfs(client):
    tok = _token(client)
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(bp_xml()), 'export.xml')},
                    content_type='multipart/form-data')
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    assert client.get(f'/w/{wid}/doc/bpdoc-7/part/1/pages').status_code == 404       # an RTF part


# ---- a single file ---------------------------------------------------------------------------------------

def test_single_pdf_pages_removed_into_a_new_file(client):
    tok = _token(client)
    r = client.post('/file_compress', data={'_csrf': tok, 'file': (io.BytesIO(PDF4), 'letter.pdf')},
                    content_type='multipart/form-data')
    fid = r.headers['Location'].rsplit('/', 1)[1]
    assert '✂ Remove pages…' in client.get(f'/file_compress/{fid}').get_data(as_text=True)
    page = client.get(f'/file_compress/{fid}/pages').get_data(as_text=True)
    assert page.count('name="page"') == 4
    assert client.get(f'/file_compress/{fid}/pages/1.png').data[:4] == b'\x89PNG'
    r = client.post(f'/file_compress/{fid}/pages/delete', data={'_csrf': tok, 'page': ['1']})
    new_fid = r.headers['Location'].rsplit('/', 1)[1]
    assert new_fid != fid
    import app as A
    assert A.files.meta(new_fid)['name'] == 'letter_pages_removed.pdf'
    with open(A.files.original_path(new_fid), 'rb') as f:
        assert _pages(f.read()) == 3
    with open(A.files.original_path(fid), 'rb') as f:
        assert f.read() == PDF4                                         # the upload is kept as it was
