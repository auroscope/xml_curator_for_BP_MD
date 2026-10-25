"""Health check and repair of uploads that are not well-formed XML (curator/xmlhealth.py and the /health pages)."""
import io
import os
import re

from conftest import bp_xml, md_xml

from curator import documents as D
from curator.xmlhealth import check
from curator.xmlstore import XmlDoc


def _kinds(r):
    return [(k['tier'], k['kind'], k['count']) for k in r['repairs']]


# ---- the checks ------------------------------------------------------------------------------------------

def test_well_formed_file_needs_no_repair():
    r = check(bp_xml())
    assert r['problem'] is None and r['repairs'] == [] and r['repaired'] == bp_xml()


def test_safe_text_repairs():
    r = check(b'<BPSEHRV2><S>Smith & Jones&nbsp;BP < 140 &amp; ok &#160; &lt;</S></BPSEHRV2>')
    assert _kinds(r) == [('safe', 'ampersand', 1), ('safe', 'entity', 1), ('safe', 'less-than', 1)]
    assert not r['needs_guess'] and r['root'] == 'BPSEHRV2'
    assert r['repaired'] == b'<BPSEHRV2><S>Smith &amp; Jones&#160;BP &lt; 140 &amp; ok &#160; &lt;</S></BPSEHRV2>'


def test_text_repairs_leave_cdata_and_comments_alone():
    data = b'<BPSEHRV2><!-- a & b < c --><S><![CDATA[a & b < c]]></S><T>x & y</T></BPSEHRV2>'
    r = check(data)
    assert r['repaired'] == data.replace(b'x & y', b'x &amp; y')


def test_invalid_utf8_is_read_as_windows_1252():
    r = check(b'<?xml version="1.0" encoding="UTF-8"?><PATIENT><S>caf\xe9 \x93q\x94</S></PATIENT>')
    assert _kinds(r) == [('safe', 'encoding', 3)]
    assert XmlDoc(r['repaired']).child_text(XmlDoc(r['repaired']).root, 'S') == 'café “q”'


def test_truncated_bp_file_drops_the_half_written_record_and_closes_the_sections():
    data = bp_xml()
    cut = data.index(b'<Document><DOCUMENTID>9') + 120
    r = check(data[:cut])
    assert _kinds(r) == [('safe', 'truncated', 1)] and not r['needs_guess']
    ex = r['repairs'][0]['examples'][0]
    assert ex['record'].startswith('<Document>') and ex['closed'] == ['CorrespondenceIn', 'BPSEHRV2']
    doc = XmlDoc(r['repaired'])
    keys = {rec.key for rec in D.records(doc)}
    assert 'bpdoc-7' in keys and 'bpdoc-9' not in keys
    assert r['repaired'].endswith(b'</CorrespondenceIn>\n</BPSEHRV2>\n')
    assert r['repaired'][:cut - 200] == data[:cut - 200]        # everything before the lost record is untouched


def test_truncated_inside_a_tag_and_inside_an_end_tag():
    data = md_xml()
    for cut in (data.index(b'<DOC_NO>5') + 3,                                 # inside a start tag
                data.index(b'</DOCUMENTS2>') + 5):                          # inside an end tag
        r = check(data[:cut])
        assert r['repaired'] is not None and r['remaining'] is None, cut
        assert XmlDoc(r['repaired']).root.tag == 'PATIENT'


def test_unclosed_leaf_is_closed_after_its_text():
    good = bp_xml()
    broken = good.replace(b'</CONTACTNAME>', b'', 1)
    r = check(broken)
    assert _kinds(r) == [('guess', 'unclosed', 1)] and r['needs_guess']
    assert r['repaired'] == good


def test_unclosed_element_without_text_is_closed_before_its_parent():
    r = check(b'<BPSEHRV2><S><D><A>x</A></S></BPSEHRV2>')
    assert r['repaired'] == b'<BPSEHRV2><S><D><A>x</A></D></S></BPSEHRV2>'
    assert r['repairs'][0]['examples'][0]['element'] == 'D'


def test_stray_end_tag_and_trailing_content_are_removed():
    good = bp_xml()
    r = check(good.replace(b'</Demographics>', b'</Bogus></Demographics>', 1))
    assert _kinds(r) == [('guess', 'stray', 1)] and r['repaired'] == good
    r = check(good + b'<BPSEHRV2><Demo')
    assert _kinds(r) == [('guess', 'trailing', 1)] and r['repaired'] == good


def test_unrepairable_files_are_reported_with_their_place():
    r = check(b'<BPSEHRV2>\n<S a="1" a="2"/>\n</BPSEHRV2>')
    assert r['repaired'] is None and r['remaining']['message'] == 'a tag has the same attribute twice'
    assert r['remaining']['line'] == 2 and 'a="2"' in r['remaining']['after']
    r = check(b'   ')
    assert r['repaired'] is None and r['remaining']['message'] == 'the file is empty'


# ---- upload, report, repair ------------------------------------------------------------------------------

def _token(client):
    html = client.get('/').get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', html).group(1)


def _upload(client, tok, data, name):
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), name)},
                    content_type='multipart/form-data')
    assert r.status_code == 302
    return r.headers['Location']


def test_truncated_upload_gets_a_report_and_opens_after_repair(client):
    tok = _token(client)
    data = bp_xml()
    broken = data[:data.index(b'<Document><DOCUMENTID>9') + 120]
    loc = _upload(client, tok, broken, 'Cut Off.xml')
    assert loc.startswith('/health/')
    hid = loc.rsplit('/', 1)[1]
    page = client.get(loc).get_data(as_text=True)
    assert 'not well-formed XML' in page and 'It can be repaired safely' in page and 'accept_guess' not in page
    assert 'Cut_Off.xml' in client.get('/').get_data(as_text=True)                      # listed on the start page
    assert client.get(f'/health/{hid}/file').data == broken

    r = client.post(f'/health/{hid}/repair', data={'_csrf': tok})
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    ws = client.ws
    assert ws.held(hid) is None and ws.held_list() == []
    assert open(ws.source_path(wid), 'rb').read() == open(ws.working_path(wid), 'rb').read() != broken
    assert client.get(f'/w/{wid}/unrepaired').data == broken
    assert client.get(f'/w/{wid}/documents').status_code == 200
    log = client.get(f'/w/{wid}/changes?format=json').get_json()
    assert [e['action'] for e in log] == ['upload', 'repair'] and log[0]['repaired'] and log[0]['bytes'] == len(broken)
    assert 'repaired on upload' in client.get('/').get_data(as_text=True)
    assert client.get(f'/w/{wid}/changes.pdf').status_code == 200

    # the same broken file again opens the repaired workspace
    assert _upload(client, tok, broken, 'again.xml').startswith(f'/w/{wid}/')

    # reset goes back to the repaired copy, not to the broken upload
    ws.reset(wid)
    assert open(ws.working_path(wid), 'rb').read() == open(ws.source_path(wid), 'rb').read()


def test_best_guess_repair_needs_confirmation(client):
    tok = _token(client)
    loc = _upload(client, tok, md_xml().replace(b'</DOCTITLE>', b'', 1), 'guess.xml')
    hid = loc.rsplit('/', 1)[1]
    page = client.get(loc).get_data(as_text=True)
    assert 'partly by best guess' in page and 'Best-guess repairs can be wrong' in page and 'accept_guess' in page
    r = client.post(f'/health/{hid}/repair', data={'_csrf': tok})
    assert r.headers['Location'].endswith(f'/health/{hid}') and client.ws.held(hid)
    r = client.post(f'/health/{hid}/repair', data={'_csrf': tok, 'accept_guess': '1'})
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    assert open(client.ws.working_path(wid), 'rb').read() == md_xml()
    log = client.get(f'/w/{wid}/changes').get_data(as_text=True)
    assert 'best guess' in log


def test_unrepairable_upload_is_reported_and_can_be_removed(client):
    tok = _token(client)
    loc = _upload(client, tok, b'<BPSEHRV2 a="1" a="2"></BPSEHRV2>', 'dup.xml')
    hid = loc.rsplit('/', 1)[1]
    page = client.get(loc).get_data(as_text=True)
    assert 'cannot be repaired automatically' in page and 'same attribute twice' in page
    assert client.post(f'/health/{hid}/repair', data={'_csrf': tok}).status_code == 400
    client.post(f'/health/{hid}/discard', data={'_csrf': tok})
    assert client.ws.held_list() == [] and client.get(loc).status_code == 404
    assert os.listdir(os.path.join(client.ws.data_dir, 'sources')) == []


def test_repaired_xml_that_is_not_an_export_cannot_be_opened(client):
    tok = _token(client)
    loc = _upload(client, tok, b'<rss><channel>a & b</channel></rss>', 'feed.xml')
    page = client.get(loc).get_data(as_text=True)
    assert 'cannot be repaired automatically' in page and 'top element is &lt;rss&gt;' in page


# ---- tags that lost a delimiter --------------------------------------------------------------------------

def test_tag_that_lost_its_closing_angle_bracket_before_its_text():
    good = md_xml().replace(b'</DEMOGRAPHICS>', b'</DEMOGRAPHICS><RX><RXITEM><FIRSTDATE>2020-01-02</FIRSTDATE>'
                                               b'<LASTDATE>2020-03-04</LASTDATE><CEASED_DATE></CEASED_DATE></RXITEM></RX>')
    r = check(good.replace(b'<LASTDATE>', b'<LASTDATE', 1))
    assert _kinds(r) == [('safe', 'delimiter', 1)] and not r['needs_guess'] and r['repaired'] == good
    ex = r['repairs'][0]['examples'][0]
    assert ex['element'] == 'LASTDATE' and ex['missing'] == '>'


def _each_delimiter(data):
    """Every copy of data with one '<', '>' or (in an end tag) '/' removed, as (description, copy)."""
    for m in re.finditer(rb'<(/?)([A-Za-z_][\w.:-]*)[^<>]*?(/?)>', data):
        if m.group(3):
            continue                       # '<X />' without its '<' is still well-formed (stray text)
        cuts = {'<': m.start(), '>': m.end() - 1}
        if m.group(1):
            cuts['/'] = m.start() + 1
        for d, i in cuts.items():
            yield f'{m.group(0)[:40]!r} without {d}', d, data[:i] + data[i + 1:]


def test_every_lost_angle_bracket_is_repaired_exactly():
    for data in (bp_xml(), md_xml()):
        for what, d, broken in _each_delimiter(data):
            if d == '/':
                continue
            r = check(broken)
            assert r['repaired'] == data and not r['needs_guess'], what


def test_lost_slash_in_an_end_tag_is_a_best_guess():
    for data in (bp_xml(), md_xml()):
        for what, d, broken in _each_delimiter(data):
            if d != '/':
                continue
            r = check(broken)
            assert r['repaired'] is not None, what
            if r['repaired'] != data:
                assert r['needs_guess'], what          # never a different result from safe repairs alone


def test_records_nested_in_a_section_of_the_same_name_are_not_mistaken_for_a_lost_slash():
    # BP: <ClinicalDetails><ClinicalDetails>…</ClinicalDetails></ClinicalDetails> is a section holding a record.
    good = (b'<BPSEHRV2>\n<ClinicalDetails>\n<ClinicalDetails><A>1</A><B>2</B></ClinicalDetails>\n'
            b'<ClinicalDetails><A>3</A><B>4</B></ClinicalDetails>\n</ClinicalDetails>\n</BPSEHRV2>\n')
    r = check(good.replace(b'</B></ClinicalDetails>\n<Clin', b'</B>\n<Clin', 1))     # first record not closed
    assert [k['kind'] for k in r['repairs']] == ['unclosed'] and r['repaired'] is not None
    assert XmlDoc(r['repaired']).root.find('ClinicalDetails') is not None


# ---- a cut-off file is not repaired without all of the patient's demographics ----------------------------

def test_cut_off_in_or_before_the_demographics_is_not_repaired():
    for data, section in ((bp_xml(), b'Demographics'), (md_xml(), b'DEMOGRAPHICS')):
        start, end = data.index(b'<' + section + b'>'), data.index(b'</' + section + b'>')
        for cut, where in ((start, 'before'), (start + 40, 'part-way through'), (end + 3, 'part-way through')):
            r = check(data[:cut])
            assert r['repaired'] is None, (section, cut)
            assert r['remaining']['reason'] == 'demographics'
            assert r['remaining']['message'].startswith(f"the file stops {where} the patient's demographics")
        r = check(data[:end + len(section) + 3 + 10])             # just after them: repaired
        assert r['repaired'] is not None and _kinds(r) == [('safe', 'truncated', 1)]


def test_refused_repair_says_why_on_the_report(client):
    tok = _token(client)
    data = bp_xml()
    loc = _upload(client, tok, data[:data.index(b'<SURNAME>')], 'cut.xml')
    page = client.get(loc).get_data(as_text=True)
    assert 'cannot be repaired automatically' in page and 'Not repaired:' in page
    assert 'part-way through the patient&#39;s demographics' in page and 'Export the patient again' in page
    assert 'Repair and open' not in page
