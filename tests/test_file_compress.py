import io
import os
import re
import time

from pdfs import needs_gs, text_pdf
from test_rtf import sample_rtf
from curator import filecompress as FC
from curator import pdfops


def _token(client):
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)


def _upload(c, data, name):
    tok = _token(c)
    r = c.post('/file_compress', data={'_csrf': tok, 'file': (io.BytesIO(data), name)}, content_type='multipart/form-data')
    return r, tok


def _sync(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.files, 'start', lambda fid: None)     # compute only when the test asks
    return app_module.files


def test_detect_format_and_safe_names():
    assert FC.detect_format(b'%PDF-1.7\n...') == 'pdf'
    assert FC.detect_format(b'\xef\xbb\xbf{\\rtf1 x}') == 'rtf'
    assert FC.detect_format(b'<?xml version="1.0"?>') is None
    assert FC.detect_format(b'PK\x03\x04') is None
    assert FC.safe_stem('C:\\Users\\me\\Scan "1".pdf') == 'Scan _1_'
    assert FC.safe_stem('../../etc/passwd') == 'passwd'
    assert FC.safe_stem('') == 'file'


def test_rejects_other_files_by_content(client, monkeypatch):
    _sync(monkeypatch)
    r, _ = _upload(client, b'<?xml version="1.0"?><x/>', 'renamed.pdf')
    assert r.headers['Location'].endswith('/?tab=file')
    assert 'Only PDF, RTF, TIFF and BMP files' in client.get('/?tab=file').get_data(as_text=True)


def test_rtf_compress_and_download(client, monkeypatch):
    files = _sync(monkeypatch)
    rtf = sample_rtf()
    r, tok = _upload(client, rtf, 'Letter.rtf')
    fid = r.headers['Location'].rsplit('/', 1)[1]
    assert 'Preparing the options' in client.get(f'/file_compress/{fid}').get_data(as_text=True)
    files.compute(fid)
    page = client.get(f'/file_compress/{fid}').get_data(as_text=True)
    assert 'Download compress pictures' in page and 'Text verified identical' in page
    d = client.get(f'/file_compress/{fid}/file/rtfpic?download=1')
    assert d.status_code == 200 and 'Letter_compressed.rtf' in d.headers['Content-Disposition']
    assert len(d.data) < len(rtf) and b'Dear Doctor' in d.data
    assert client.get(f'/file_compress/{fid}/file/original?download=1').data == rtf
    assert 'Letter.rtf' in client.get('/file_compress', follow_redirects=True).get_data(as_text=True)
    client.post(f'/file_compress/{fid}/delete', data={'_csrf': tok})
    assert client.get(f'/file_compress/{fid}').status_code == 404


@needs_gs
def test_pdf_options_and_text_download(client, monkeypatch):
    files = _sync(monkeypatch)
    pdf = text_pdf()
    r, tok = _upload(client, pdf, 'Report.pdf')
    fid = r.headers['Location'].rsplit('/', 1)[1]
    files.compute(fid)
    page = client.get(f'/file_compress/{fid}').get_data(as_text=True)
    assert 'Download strip images' in page and 'Download text only' in page
    strip = client.get(f'/file_compress/{fid}/file/strip?download=1')
    assert 'Report_no_images.pdf' in strip.headers['Content-Disposition']
    assert pdfops.verify_text(pdf, strip.data)['identical']
    initial = files.text_initial(fid, '')
    t = client.post(f'/file_compress/{fid}/text?download=1', data={'_csrf': tok, 'text': initial + '\nAdded line'})
    assert t.data.startswith(b'%PDF') and 'Report_text_only.pdf' in t.headers['Content-Disposition']
    assert t.headers['X-Edited-Words'] == '2'


def test_old_files_are_removed(client, monkeypatch):
    files = _sync(monkeypatch)
    r, _ = _upload(client, sample_rtf(), 'Old.rtf')
    fid = r.headers['Location'].rsplit('/', 1)[1]
    old = time.time() - (FC.KEEP_HOURS + 1) * 3600
    os.utime(files.dir(fid), (old, old))
    client.get('/')
    assert files.meta(fid) is None
    assert client.get(f'/file_compress/{fid}').status_code == 404
    assert client.get('/file_compress/not-an-id').status_code == 404


def test_start_page_tabs_and_progress_upload(client, monkeypatch):
    _sync(monkeypatch)
    home = client.get('/').get_data(as_text=True)
    assert 'data-home="xml" ' in home and 'data-home="file" hidden' in home          # XML exports by default
    assert 'data-home="xml" hidden' in client.get('/?tab=file').get_data(as_text=True)
    # the page's uploader asks for JSON instead of a redirect; the flash message waits for the next page
    tok = _token(client)
    r = client.post('/file_compress', data={'_csrf': tok, 'file': (io.BytesIO(sample_rtf()), 'L.rtf')},
                    content_type='multipart/form-data', headers={'X-Upload': '1'})
    assert r.status_code == 200 and r.get_json()['location'].startswith('/file_compress/')
    r = client.post('/file_compress', data={'_csrf': tok, 'file': (io.BytesIO(b'hello'), 'x.pdf')},
                    content_type='multipart/form-data', headers={'X-Upload': '1'})
    assert r.get_json()['location'] == '/?tab=file'
    assert 'Only PDF, RTF, TIFF and BMP files' in client.get('/?tab=file').get_data(as_text=True)
