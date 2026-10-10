import base64
import io
import re

import pytest

from conftest import PDF, md_xml
from pdfs import needs_gs, scan_pdf, text_pdf
from curator import documents as D
from curator.xmlstore import XmlDoc


def _md_with(pdf4, pdf5):
    """The MedicalDirector fixture with document 4 part 1 and document 5 replaced by real PDFs."""
    data = md_xml()
    b64 = base64.b64encode(PDF)
    first = data.index(b64)
    second = data.index(b64, first + len(b64))
    return data[:first] + base64.b64encode(pdf4) + data[first + len(b64):second] + base64.b64encode(pdf5) + data[second + len(b64):]


def test_duplicates_ignore_tiny_content_and_need_every_part_identical():
    # md-4 (PDF + RTF) and md-5 (PDF) share the same PDF, but it is under 64 bytes: never a duplicate
    assert D.duplicate_groups(XmlDoc(md_xml())) == []
    # with a real-sized shared PDF they are still different documents, because md-4 also has an RTF part
    big = b'%PDF-1.4\n' + b'x' * 500
    assert D.duplicate_groups(XmlDoc(md_xml().replace(base64.b64encode(PDF), base64.b64encode(big)))) == []


def test_duplicate_records_detected():
    big = b'%PDF-1.4\n' + b'y' * 500
    data = md_xml().replace(base64.b64encode(PDF), base64.b64encode(big))
    # make document 5 an exact copy of document 4 part 1 by giving it the same single part
    doc = XmlDoc(data)
    rows = doc.root.find('DOCUMENTS2').findall('DOCUMENT')
    dup_row = doc.raw(rows[2]).replace(b'<DOC_NO>5</DOC_NO>', b'<DOC_NO>6</DOC_NO>').replace(b'G2', b'G3')
    data = data.replace(doc.raw(rows[2]), doc.raw(rows[2]) + dup_row)
    doc = XmlDoc(data)
    assert {k: v.key for k, v in D.duplicate_of(doc).items()} == {'md-6': 'md-5'}


def _token(client):
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)


@pytest.fixture
def sync_client(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.cands, 'start', lambda *a: None)
    monkeypatch.setattr(app_module.cands, 'start_precompute', lambda *a: None)
    monkeypatch.setattr(app_module.cands, 'start_all', lambda *a: None)
    client.cands = app_module.cands
    return client


def _upload(c, data):
    tok = _token(c)
    r = c.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), 'q.xml')}, content_type='multipart/form-data')
    return r.headers['Location'].split('/w/')[1].split('/')[0], tok


@needs_gs
def test_queue_navigation_and_save_and_next(sync_client):
    c = sync_client
    scan, text = scan_pdf(), text_pdf()
    assert len(scan) > len(text)
    wid, tok = _upload(c, _md_with(text, scan))
    for key in ('md-4', 'md-5'):
        c.cands.compute(wid, key, 0)
    page = c.get(f'/w/{wid}/doc/md-5/part/0/shrink').get_data(as_text=True)      # the scan is the largest
    assert 'Document 1 of 2' in page and '2 not shrunk' in page and 'Save &amp; next' in page
    cid = re.search(r'name="cid" value="([^"]+)"', page).group(1)
    r = c.post(f'/w/{wid}/doc/md-5/part/0/shrink/save?then=next', data={'_csrf': tok, 'cid': cid, 'option': 'compress'})
    assert r.headers['Location'].endswith('/doc/md-4/part/0/shrink')
    page = c.get(r.headers['Location']).get_data(as_text=True)
    assert 'Document 2 of 2' in page and '1 not shrunk' in page


@needs_gs
def test_bulk_page_apply_strip_and_compress(sync_client):
    c = sync_client
    data = _md_with(text_pdf(), scan_pdf())
    wid, tok = _upload(c, data)
    page = c.get(f'/w/{wid}/bulk').get_data(as_text=True)
    assert 'Preparing the options' in page and 'Strip images from <span data-btn-label>the 0 ready' in page
    c.cands.compute(wid, 'md-4', 0)
    p = c.get(f'/w/{wid}/bulk/progress').get_json()            # the cards' figures grow as PDFs become ready
    assert p['pending'] == 1 and p['strip']['count'] == 1 and p['strip']['saving'] > 0 and p['compress']['count'] == 0
    c.cands.compute(wid, 'md-5', 0)
    p = c.get(f'/w/{wid}/bulk/progress').get_json()
    assert p['pending'] == 0 and p['compress']['count'] == 1
    page = c.get(f'/w/{wid}/bulk').get_data(as_text=True)
    assert 'Strip images from <span data-btn-label>all 1</span>' in page and 'Compress <span data-btn-label>all 1</span> scans' in page
    # compressing scans in bulk needs an explicit acceptance
    r = c.post(f'/w/{wid}/bulk/shrink', data={'_csrf': tok, 'option': 'compress'})
    assert 'Tick the box' in c.get(r.headers['Location']).get_data(as_text=True)
    c.post(f'/w/{wid}/bulk/shrink', data={'_csrf': tok, 'option': 'compress', 'accept': '1'})
    c.post(f'/w/{wid}/bulk/shrink', data={'_csrf': tok, 'option': 'strip'})
    log = c.get(f'/w/{wid}/changes?format=json').get_json()
    assert sorted((e['key'], e['option'], e.get('bulk')) for e in log if e['action'] == 'shrink') == \
        [('md-4', 'strip', True), ('md-5', 'compress', True)]
    page = c.get(f'/w/{wid}/bulk').get_data(as_text=True)
    assert '2 PDF(s) already shrunk' in page
    assert '(bulk)' in c.get(f'/w/{wid}/changes?format=csv').get_data(as_text=True)
    for key in ('md-4', 'md-5'):
        c.post(f'/w/{wid}/doc/{key}/revert', data={'_csrf': tok})
    assert c.get(f'/w/{wid}/download').data == data


def test_bulk_page_deletes_ticked_duplicates(sync_client):
    c = sync_client
    big = b'%PDF-1.4\n' + b'z' * 500
    data = md_xml().replace(base64.b64encode(PDF), base64.b64encode(big))
    doc = XmlDoc(data)
    row = doc.raw(doc.root.find('DOCUMENTS2').findall('DOCUMENT')[2])
    data = data.replace(row, row + row.replace(b'<DOC_NO>5</DOC_NO>', b'<DOC_NO>6</DOC_NO>').replace(b'G2', b'G3'))
    wid, tok = _upload(c, data)
    page = c.get(f'/w/{wid}/bulk').get_data(as_text=True)
    assert 'value="md-6" checked' in page and 'keep' in page
    assert 'badge-dup' in c.get(f'/w/{wid}/documents').get_data(as_text=True)
    r = c.post(f'/w/{wid}/items/delete', data={'_csrf': tok, 'keys': ['md-6'], 'back': 'bulk'})
    assert r.headers['Location'].endswith(f'/w/{wid}/bulk')
    assert 'md-6' not in D.record_map(XmlDoc(c.get(f'/w/{wid}/download').data))
    assert 'No duplicate documents' in c.get(f'/w/{wid}/bulk').get_data(as_text=True)


@needs_gs
def test_prepare_all_retries_pdfs_another_job_was_holding(client, monkeypatch, tmp_path):
    import os
    import time
    import app as app_module
    c = client
    cands = app_module.cands
    monkeypatch.setattr(cands, 'start_precompute', lambda *a: None)
    wid, _tok = _upload(c, _md_with(text_pdf(), scan_pdf()))
    # another job holds the text PDF's candidate for a moment
    doc = app_module.ws.load(wid)
    rec = D.record_map(doc)['md-4']
    from curator import shrink as S
    d = cands.dir(wid, S.cand_id(doc, rec, rec.parts[0]))
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, '.claim'), 'w').close()
    monkeypatch.setattr(S.time, 'sleep', lambda s: os.unlink(os.path.join(d, '.claim')) if os.path.exists(os.path.join(d, '.claim')) else None)
    cands.start_all(wid)
    deadline = time.time() + 120
    while cands.all_running(wid) and time.time() < deadline:
        time.sleep(0.2)
    assert c.get(f'/w/{wid}/bulk/progress').get_json()['pending'] == 0


@needs_gs
def test_recheck_redoes_failed_and_interrupted_preparations(sync_client):
    import os
    c = sync_client
    wid, tok = _upload(c, _md_with(text_pdf(), scan_pdf()))
    for key in ('md-4', 'md-5'):
        c.cands.compute(wid, key, 0)
    from curator import shrink as S
    import app as app_module
    work = app_module.ws.load(wid)
    rec = D.record_map(work)['md-4']
    d = c.cands.dir(wid, S.cand_id(work, rec, rec.parts[0]))
    # simulate a failure, and a claim left by a process that no longer exists
    os.unlink(os.path.join(d, 'result.json'))
    open(os.path.join(d, 'error.txt'), 'w').write('BadZipFile: test')
    open(os.path.join(d, '.claim'), 'w').write('999999 0')
    assert not c.cands._claimed(d)                                   # dead process: not treated as busy
    r = c.post(f'/w/{wid}/bulk/recheck', data={'_csrf': tok})
    assert '2 failed or interrupted' in c.get(r.headers['Location']).get_data(as_text=True)
    assert not os.path.exists(os.path.join(d, 'error.txt')) and not os.path.exists(os.path.join(d, '.claim'))
    c.cands.compute(wid, 'md-4', 0)
    assert c.cands.state(wid, S.cand_id(work, rec, rec.parts[0]))['status'] == 'done'
    # recalculate all discards every prepared option
    c.post(f'/w/{wid}/bulk/recheck', data={'_csrf': tok, 'scope': 'all'})
    assert c.cands.done_ids(wid) == set()
    assert '↻ Re-check' in c.get(f'/w/{wid}/bulk').get_data(as_text=True)


@needs_gs
def test_delete_from_shrink_screen_moves_to_next(sync_client):
    c = sync_client
    data = _md_with(text_pdf(), scan_pdf())
    wid, tok = _upload(c, data)
    page = c.get(f'/w/{wid}/doc/md-5/part/0/shrink').get_data(as_text=True)     # largest first: md-5 then md-4
    assert 'Delete document' in page and 'id="shrink-delete-form"' in page and 'inbox entry' in page
    r = c.post(f'/w/{wid}/doc/md-5/part/0/shrink/delete', data={'_csrf': tok})
    assert r.headers['Location'].endswith('/doc/md-4/part/0/shrink')
    work = XmlDoc(c.get(f'/w/{wid}/download').data)
    assert 'md-5' not in D.record_map(work)
    assert c.get(f'/w/{wid}/changes?format=json').get_json()[-1]['action'] == 'delete'
    r = c.post(f'/w/{wid}/doc/md-4/part/0/shrink/delete', data={'_csrf': tok})    # last one: back to the list
    assert r.headers['Location'].endswith(f'/w/{wid}/documents')
    for key in ('md-5', 'md-4'):
        c.post(f'/w/{wid}/doc/{key}/revert', data={'_csrf': tok})
    assert c.get(f'/w/{wid}/download').data == data


@needs_gs
def test_options_that_are_not_smaller_are_not_offered(sync_client):
    from curator import pdfops
    c = sync_client
    compact = pdfops.compress(scan_pdf(), pdfops.analyse(scan_pdf()))      # already black-and-white G4
    wid, tok = _upload(c, _md_with(text_pdf(), compact))
    c.cands.compute(wid, 'md-5', 0)
    page = c.get(f'/w/{wid}/doc/md-5/part/0/shrink').get_data(as_text=True)
    assert 'Not smaller than the original' in page and 'so it is not offered' in page
    c.cands.compute(wid, 'md-4', 0)
    p = c.get(f'/w/{wid}/bulk/progress').get_json()
    assert p['compress']['count'] == 0 and p['nothing'] >= 1
    assert 'already as small as any option' in c.get(f'/w/{wid}/bulk').get_data(as_text=True)


def test_rule_also_applies_to_options_prepared_earlier():
    from curator.shrink import withdraw_unhelpful
    old = {'original': {'xml_bytes': 30000}, 'kind': 'scan',
           'compress': {'available': True, 'xml_bytes': 43000, 'warnings': ['This is not smaller than the original.']},
           'text': {'available': True, 'xml_bytes': 4000, 'warnings': []}}
    r = withdraw_unhelpful(old)
    assert r['compress']['available'] is False and r['compress']['not_smaller'] and '42 KB vs 29 KB' in r['compress']['reason']
    assert r['text']['available'] is True
