import io
import re

from conftest import PDF, bp_xml, md_xml


def _token(client):
    html = client.get('/').get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', html).group(1)


def _upload(client, data, name):
    tok = _token(client)
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), name)},
                    content_type='multipart/form-data')
    assert r.status_code == 302
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    return wid, tok


def test_upload_view_delete_restore_download(client):
    data = bp_xml()
    wid, tok = _upload(client, data, 'Test Patient.xml')

    page = client.get(f'/w/{wid}/documents').get_data(as_text=True)
    assert 'Referral &amp; reply' in page and 'Testy Caf' in page

    r = client.get(f'/w/{wid}/doc/bpdoc-7/part/0')
    assert r.mimetype == 'application/pdf' and r.data == PDF
    r = client.get(f'/w/{wid}/doc/bpdoc-7/part/1')
    assert 'Hello' in r.get_data(as_text=True) and '<table' in r.get_data(as_text=True)
    r = client.get(f'/w/{wid}/doc/bpinv-8/part/0')
    assert 'Hb 140 &amp; ok' in r.get_data(as_text=True)

    r = client.post(f'/w/{wid}/documents/delete', data={'_csrf': tok, 'keys': ['bpdoc-7', 'bpcor-7']})
    assert r.status_code == 302
    page = client.get(f'/w/{wid}/documents').get_data(as_text=True)
    assert 'Deleted (2' in page
    assert client.get(f'/w/{wid}/doc/bpdoc-7/part/0').status_code == 404
    assert client.get(f'/w/{wid}/source/doc/bpdoc-7/part/0').data == PDF

    dl = client.get(f'/w/{wid}/download')
    from datetime import datetime
    assert dl.headers['Content-Disposition'].endswith(f'Test_Patient_curated_{datetime.now():%d%m%Y}.xml')
    assert b'<DOCUMENTID>7</DOCUMENTID>' not in dl.data and len(dl.data) < len(data)

    client.post(f'/w/{wid}/doc/bpdoc-7/revert', data={'_csrf': tok})
    client.post(f'/w/{wid}/doc/bpcor-7/revert', data={'_csrf': tok})
    assert client.get(f'/w/{wid}/download').data == data
    assert client.get(f'/w/{wid}/source').data == data

    log = client.get(f'/w/{wid}/changes?format=json').get_json()
    assert [c['action'] for c in log] == ['upload', 'delete', 'delete', 'revert', 'revert']


def test_md_export_parts(client):
    wid, tok = _upload(client, md_xml(), 'md.xml')
    page = client.get(f'/w/{wid}/documents?key=md-4&part=1').get_data(as_text=True)
    assert 'Part 2' in page and '2 parts' in page
    assert client.get(f'/w/{wid}/doc/md-4/part/0').data == PDF


def test_post_without_token_is_rejected(client):
    wid, _ = _upload(client, bp_xml(), 'x.xml')
    r = client.post(f'/w/{wid}/documents/delete', data={'keys': ['bpdoc-7']})
    assert r.status_code == 400
    assert client.get(f'/w/{wid}/doc/bpdoc-7/part/0').status_code == 200


def test_bad_workspace_id(client):
    assert client.get('/w/..%2F..%2Fetc/documents').status_code == 404
    assert client.get('/w/nope/documents').status_code == 404


def test_attached_html_is_sandboxed(client):
    import base64
    html_doc = b'<html><body><script>alert(1)</script>hi</body></html>'
    data = md_xml().replace(base64.b64encode(PDF), base64.b64encode(html_doc), 1)
    wid, _ = _upload(client, data, 'h.xml')
    r = client.get(f'/w/{wid}/doc/md-4/part/0')
    assert r.mimetype == 'text/html' and 'sandbox' in r.headers['Content-Security-Policy']


def test_metadata_save_log_and_restore(client):
    data = md_xml()
    wid, tok = _upload(client, data, 'md.xml')
    page = client.get(f'/w/{wid}/documents?key=md-4').get_data(as_text=True)
    assert 'Edit details' in page and 'Brain &amp; Spine' in page and 'document-handling record' in page
    r = client.post(f'/w/{wid}/doc/md-4/meta', data={
        '_csrf': tok, 'date': '2020-11-18', 'provider': 'Brain & Spine', 'to': 'Dr Zürich',
        'category': 'Medical', 'subject': 'New subject'})
    assert r.status_code == 302
    page = client.get(f'/w/{wid}/documents?key=md-4').get_data(as_text=True)
    assert 'New subject' in page and 'Restore original' in page and 'Saved subject.' in page
    log = client.get(f'/w/{wid}/changes?format=json').get_json()
    assert log[-1]['action'] == 'metadata'
    assert log[-1]['changes'] == {'subject': {'old': 'Discharge summary', 'new': 'New subject'}}
    csv_text = client.get(f'/w/{wid}/changes?format=csv').get_data(as_text=True)
    assert 'Subject: Discharge summary -> New subject' in csv_text
    client.post(f'/w/{wid}/doc/md-4/revert', data={'_csrf': tok})
    assert client.get(f'/w/{wid}/download').data == data


def test_metadata_bad_date_is_flashed(client):
    wid, tok = _upload(client, bp_xml(), 'bp.xml')
    client.post(f'/w/{wid}/doc/bpdoc-9/meta', data={'_csrf': tok, 'date': 'nonsense'})
    page = client.get(f'/w/{wid}/documents?key=bpdoc-9').get_data(as_text=True)
    assert 'Date must be a valid date.' in page
    assert client.get(f'/w/{wid}/download').data == bp_xml()


def test_start_page_lists_original_and_curated(client):
    wid, tok = _upload(client, bp_xml(), 'Test Patient.xml')
    page = client.get('/').get_data(as_text=True)
    assert 'Test_Patient.xml' in page and 'Test_Patient_curated.xml' in page and 'no changes yet' in page
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(bp_xml()), 'again.xml')},
                    content_type='multipart/form-data')
    assert f'/w/{wid}/' in r.headers['Location']
    assert 'already in the curator' in client.get(r.headers['Location']).get_data(as_text=True)
    assert client.get('/').get_data(as_text=True).count('<span class="f-name">Test_Patient_curated.xml</span>') == 1


def test_start_page_open_fresh_and_resume(client):
    data = bp_xml()
    wid, tok = _upload(client, data, 'Test Patient.xml')
    page = client.get('/').get_data(as_text=True)
    assert 'Open fresh' in page and '>Resume</a>' not in page      # nothing to resume yet
    client.post(f'/w/{wid}/documents/delete', data={'_csrf': tok, 'keys': ['bpdoc-7']})
    page = client.get('/').get_data(as_text=True)
    assert f'href="/w/{wid}/documents">Resume</a>' in page
    assert 'data-changes="1"' in page and 'data-copy="Test_Patient_curated_' in page
    r = client.post(f'/w/{wid}/reset', data={'_csrf': tok, 'fresh': '1', 'save_copy': '1'})
    assert 'Opened the original fresh' in client.get(r.headers['Location']).get_data(as_text=True)
    assert client.get(f'/w/{wid}/download').data == data
    page = client.get('/').get_data(as_text=True)
    assert 'Resume from this copy' in page and 'no changes yet' in page


def test_reset_can_save_a_copy_which_can_be_restored(client):
    data = bp_xml()
    wid, tok = _upload(client, data, 'Test Patient.xml')
    client.post(f'/w/{wid}/documents/delete', data={'_csrf': tok, 'keys': ['bpdoc-7', 'bpcor-7']})
    curated = client.get(f'/w/{wid}/download').data
    page = client.get(f'/w/{wid}/documents').get_data(as_text=True)
    assert 'all 2 changes' in page and 'Test_Patient_curated_' in page and 'name="save_copy" value="1" checked' in page

    r = client.post(f'/w/{wid}/reset', data={'_csrf': tok, 'save_copy': '1'})
    assert 'Saved the curated copy as Test_Patient_curated_' in client.get(r.headers['Location']).get_data(as_text=True)
    assert client.get(f'/w/{wid}/download').data == data                      # reset happened
    start = client.get('/').get_data(as_text=True)
    name = re.search(r'(Test_Patient_curated_\d{8}\.xml)', start).group(1)
    assert 'Saved copy' in start and '2 changes' in start and 'no changes yet' in start
    assert client.get(f'/w/{wid}/copies/{name}').data == curated               # the saved copy is the old curated copy

    client.post(f'/w/{wid}/copies/{name}/restore', data={'_csrf': tok})
    assert client.get(f'/w/{wid}/download').data == curated
    assert '2 documents/items changed' in client.get('/').get_data(as_text=True)   # its change list came back
    page = client.get(f'/w/{wid}/documents').get_data(as_text=True)
    assert 'Deleted (2' in page                                                 # deleted documents restorable again

    # a second copy the same day gets _2; deleting a copy removes it
    client.post(f'/w/{wid}/reset', data={'_csrf': tok, 'save_copy': '1'})
    start = client.get('/').get_data(as_text=True)
    assert name.replace('.xml', '_2.xml') in start
    client.post(f'/w/{wid}/copies/{name}/delete', data={'_csrf': tok})
    assert client.get(f'/w/{wid}/copies/{name}').status_code == 404


def test_reset_without_saving_and_bad_copy_names(client):
    wid, tok = _upload(client, bp_xml(), 'x.xml')
    client.post(f'/w/{wid}/documents/delete', data={'_csrf': tok, 'keys': ['bpdoc-7']})
    client.post(f'/w/{wid}/reset', data={'_csrf': tok})
    assert 'Saved copy' not in client.get('/').get_data(as_text=True)
    assert client.get(f'/w/{wid}/copies/..%2F..%2Fsecret_key').status_code == 404
    assert client.get(f'/w/{wid}/copies/x_curated_01012026.xml').status_code == 404


def test_unknown_formats_show_a_page_instead_of_downloading(client):
    import base64
    blob = b'\x00\x01binary-thing' * 20
    data = md_xml().replace(base64.b64encode(PDF), base64.b64encode(blob), 1).replace(b'x.PDF', b'x.BIN', 1)
    wid, _ = _upload(client, data, 'u.xml')
    r = client.get(f'/w/{wid}/doc/md-4/part/0')
    assert r.status_code == 200 and r.mimetype == 'text/html'
    assert 'Content-Disposition' not in r.headers or 'attachment' not in r.headers['Content-Disposition']
    assert "Browsers can&#39;t display" in r.get_data(as_text=True) or "Browsers can't display" in r.get_data(as_text=True)
    d = client.get(f'/w/{wid}/doc/md-4/part/0?download=1')
    assert 'attachment' in d.headers['Content-Disposition'] and d.data == blob


def test_outlook_msg_shown_as_email_on_local_sample(client):
    import os
    import pytest
    from curator import documents as D
    from curator.render import read_msg
    from curator.xmlstore import XmlDoc
    from test_core import REAL_DIR
    if os.environ.get('CURATOR_REAL_TESTS') != '1' or not REAL_DIR or not os.path.isdir(REAL_DIR):
        pytest.skip('set CURATOR_REAL_TESTS=1 to run on local sample exports')
    msgs = []
    for name in sorted(os.listdir(REAL_DIR)):
        if name.endswith('.xml'):
            doc = XmlDoc(open(os.path.join(REAL_DIR, name), 'rb').read())
            msgs += [D.part_bytes(doc, p)[1] for r in D.records(doc) for p in r.parts if p.fmt == 'msg']
    if not msgs:
        pytest.skip('no Outlook emails in the local sample exports')
    for data in msgs:
        m = read_msg(data)
        assert m['from'] and m['date'] and m['body']
