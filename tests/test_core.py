import os
import shutil
import tempfile

import pytest

from conftest import PDF, bp_xml, md_xml
from curator import documents as D
from curator.workspace import Workspaces
from curator.xmlstore import XmlDoc, apply_edits

REAL_DIR = os.environ.get('CURATOR_SAMPLES_DIR', '')     # folder of real exports, for local tests only


# ---- xmlstore --------------------------------------------------------------------------------------------

def test_offsets_and_text():
    doc = XmlDoc(bp_xml())
    assert doc.encoding == 'iso-8859-1'
    p = doc.root.find('Demographics').find('Patient')
    assert doc.child_text(p, 'SURNAME') == 'Caf\xe9'
    assert doc.child_text(p, 'FIRSTNAME', strip=False) == 'Testy   '
    mid = p.find('MIDDLENAME')
    assert mid.self_closing and doc.raw(mid) == b'<MIDDLENAME />' and doc.text(mid) == ''
    subj = doc.root.find('CorrespondenceIn').find('Document').find('SUBJECT')
    assert doc.text(subj) == 'Referral & reply'


def test_bom_offsets_utf8():
    doc = XmlDoc(md_xml())
    assert doc.encoding == 'utf-8'
    demo = doc.root.find('DEMOGRAPHICS')
    assert doc.raw(demo).startswith(b'<DEMOGRAPHICS>') and doc.raw(demo).endswith(b'</DEMOGRAPHICS>')
    assert doc.child_text(demo, 'SURNAME') == 'Persön'


def test_set_text_keeps_everything_else():
    data = bp_xml()
    doc = XmlDoc(data)
    p = doc.root.find('Demographics').find('Patient')
    new = apply_edits(data, [doc.edit_set_text(p.find('MIDDLENAME'), 'J & K – \xe9')])
    doc2 = XmlDoc(new)
    p2 = doc2.root.find('Demographics').find('Patient')
    assert doc2.child_text(p2, 'MIDDLENAME') == 'J & K – \xe9'
    # en dash is not in ISO-8859-1, so it must be written as a character reference, é as a byte
    assert b'<MIDDLENAME>J &amp; K &#8211; \xe9</MIDDLENAME>' in new
    before, after = data.split(b'<MIDDLENAME />')
    assert new.startswith(before) and new.endswith(after)


def test_control_characters_do_not_break_parsing():
    data = b'<?xml version="1.0"?><R><A>x\x07y</A></R>'
    doc = XmlDoc(data)
    assert doc.text(doc.root.find('A')) == 'x\x07y'


def test_overlapping_edits_rejected():
    with pytest.raises(ValueError):
        apply_edits(b'0123456789', [(1, 5, b''), (3, 6, b'')])


# ---- documents -------------------------------------------------------------------------------------------

def test_bp_records():
    doc = XmlDoc(bp_xml())
    recs = D.record_map(doc)
    assert set(recs) == {'bpdoc-7', 'bpdoc-9', 'bpinv-7', 'bpinv-8', 'bpcor-7'}   # id 7 in three tables
    d7 = recs['bpdoc-7']
    assert [p.fmt for p in d7.parts] == ['pdf', 'rtf'] and d7.subject == 'Referral & reply'
    assert d7.date == '03/01/2020' and d7.date_sort == '2020-01-03'
    assert recs['bpinv-7'].section == 'documents' and recs['bpinv-8'].section == 'reports'
    assert recs['bpinv-8'].category == 'Investigation (has results)'
    assert recs['bpcor-7'].section == 'letters'
    name, data, fmt = D.part_bytes(doc, d7.parts[0])
    assert (name, data, fmt) == ('a.pdf', PDF, 'pdf')
    assert D.patient_summary(doc) == {'name': 'Testy Caf\xe9', 'dob': '01/02/1950'}


def test_md_records_group_parts_and_link_items():
    doc = XmlDoc(md_xml())
    recs = D.record_map(doc)
    assert set(recs) == {'md-4', 'md-5'}
    r4 = recs['md-4']
    assert [p.fmt for p in r4.parts] == ['pdf', 'rtf']
    assert [n.tag for n in r4.nodes] == ['DOCUMENT', 'DOCUMENT', 'ITEM']
    assert r4.date == '18/11/2020' and r4.category == 'Medical' and r4.to == 'Dr Zürich'
    assert D.part_bytes(doc, r4.parts[0])[1] == PDF


@pytest.mark.parametrize('make', [bp_xml, md_xml])
def test_delete_then_revert_is_byte_identical(make):
    src_data = make()
    src = XmlDoc(src_data)
    keys = [r.key for r in D.records(src)]
    edits, removed = D.delete_edits(src, keys)
    assert len(removed) == len(keys)
    trimmed = apply_edits(src_data, edits)
    work = XmlDoc(trimmed)
    assert D.records(work) == []
    # restore in an arbitrary order; the result must match the source exactly
    for key in reversed(keys):
        edits, rec = D.revert_edits(work, src, key)
        assert rec is not None
        work = XmlDoc(apply_edits(work.data, edits))
    assert work.data == src_data


def test_md_delete_keeps_unlinked_items():
    src = XmlDoc(md_xml())
    edits, _ = D.delete_edits(src, ['md-4'])
    work = XmlDoc(apply_edits(src.data, edits))
    items = work.root.find('DOCUMENTHANDLING').findall('ITEM')
    assert [work.child_text(i, 'DMS_MD_GUID') for i in items] == ['', 'G2']
    assert set(D.record_map(work)) == {'md-5'}


# ---- workspace -------------------------------------------------------------------------------------------

def test_workspace_source_is_read_only_and_untouched(tmp_path):
    import io
    ws = Workspaces(str(tmp_path))
    data = bp_xml()
    wid, existed = ws.create(io.BytesIO(data), 'Some Patient.xml')
    assert not existed
    src = ws.source_path(wid)
    assert not os.access(src, os.W_OK) or os.geteuid() == 0
    assert oct(os.stat(src).st_mode & 0o777) == '0o444'
    ws.apply(wid, lambda doc: D.delete_edits(doc, ['bpdoc-7']))
    assert open(src, 'rb').read() == data
    assert open(ws.working_path(wid), 'rb').read() != data
    ws.reset(wid)
    assert open(ws.working_path(wid), 'rb').read() == data


def test_uploading_the_same_file_again_reuses_its_workspace(tmp_path):
    import io
    ws = Workspaces(str(tmp_path))
    wid, existed = ws.create(io.BytesIO(bp_xml()), 'A.xml')
    ws.apply(wid, lambda doc: D.delete_edits(doc, ['bpdoc-7']))
    ws.log(wid, 'delete', 'bpdoc-7', {})
    again, existed = ws.create(io.BytesIO(bp_xml()), 'renamed copy.xml')
    assert existed and again == wid
    assert len(ws.list()) == 1 and os.listdir(tmp_path / 'sources') == [wid]
    other, existed = ws.create(io.BytesIO(md_xml()), 'A.xml')            # same name, different file
    assert not existed and other != wid
    listing = {w['wid']: w for w in ws.list()}
    assert listing[wid]['curated_name'] == 'A_curated.xml' and listing[wid]['changes'] == 1
    ws.reset(wid)
    assert {w['wid']: w for w in ws.list()}[wid]['changes'] == 0      # a reset leaves nothing changed


def test_workspace_rejects_non_xml(tmp_path):
    import io
    from curator.workspace import WorkspaceError
    ws = Workspaces(str(tmp_path))
    with pytest.raises(WorkspaceError):
        ws.create(io.BytesIO(b'not xml'), 'x.xml')
    assert os.listdir(tmp_path / 'sources') == []


# ---- real exports (local only; skipped when the sample files are absent) ---------------------------------

def _real_files():
    # Slow (minutes on large exports); run with CURATOR_REAL_TESTS=1 CURATOR_SAMPLES_DIR=<folder>
    if os.environ.get('CURATOR_REAL_TESTS') != '1' or not REAL_DIR or not os.path.isdir(REAL_DIR):
        return []
    return [os.path.join(REAL_DIR, f) for f in sorted(os.listdir(REAL_DIR)) if f.endswith('.xml')]


@pytest.mark.parametrize('path', _real_files() or [pytest.param(None, marks=pytest.mark.skip('set CURATOR_REAL_TESTS=1 to run on local sample exports'))])
def test_real_export_roundtrip(path):
    with open(path, 'rb') as f:
        src_data = f.read()
    src = XmlDoc(src_data)
    recs = D.records(src)
    for rec in recs:
        for part in rec.parts:
            if part.encoding in ('zip-b64', 'b64'):
                D.part_bytes(src, part)                 # every payload decodes
    keys = [r.key for r in recs]
    edits, removed = D.delete_edits(src, keys)
    work = XmlDoc(apply_edits(src_data, edits))
    assert D.records(work) == []
    for key in keys:
        e, _ = D.revert_edits(work, src, key)
        work = XmlDoc(apply_edits(work.data, e))
    assert work.data == src_data


# ---- metadata --------------------------------------------------------------------------------------------

def _apply(doc, rec_key, values):
    rec = D.record_map(doc)[rec_key]
    edits, changed = D.metadata_edits(doc, rec, values)
    return XmlDoc(apply_edits(doc.data, edits)), changed, edits


def test_bp_metadata_edit_changes_only_those_fields():
    src = XmlDoc(bp_xml())
    rec = D.record_map(src)['bpdoc-9']
    assert [n for n, _ in D.editable_fields(rec)] == ['date', 'provider', 'category', 'subject']
    vals = D.current_values(rec)
    vals.update(date='2021-02-14', subject='Echo <report> & notes', category='Cardiology')
    work, changed, edits = _apply(src, 'bpdoc-9', vals)
    assert set(changed) == {'date', 'subject', 'category'} and len(edits) == 3
    r = D.record_map(work)['bpdoc-9']
    assert (r.date, r.subject, r.category, r.provider) == ('14/02/2021', 'Echo <report> & notes', 'Cardiology', 'Dr B')
    assert b'<SUBJECT>Echo &lt;report&gt; &amp; notes</SUBJECT>' in work.data       # was <SUBJECT />
    assert b'<CORRESPONDENCEDATE>14/02/2021</CORRESPONDENCEDATE>' in work.data
    # nothing else moved: undoing the three splices by restoring the record gives the source back
    e, _ = D.revert_edits(work, src, 'bpdoc-9')
    assert apply_edits(work.data, e) == src.data


def test_unchanged_values_write_nothing():
    src = XmlDoc(md_xml())
    rec = D.record_map(src)['md-4']
    _, changed, edits = _apply(src, 'md-4', D.current_values(rec))
    assert changed == {} and edits == []


def test_md_provider_is_sender_and_to_is_addressee():
    rec = D.record_map(XmlDoc(md_xml()))['md-4']
    assert rec.provider == 'Brain & Spine'                 # HL7 \T\ shown as &
    assert rec.to == 'Dr Zürich'
    assert [n for n, _ in D.editable_fields(rec)] == ['date', 'provider', 'to', 'category', 'subject']


def test_md_metadata_updates_every_part_and_the_handling_item():
    src = XmlDoc(md_xml())
    vals = dict(date='2021-01-05', provider='Sleep Clinic', to='Dr New', category='Respiratory', subject='Sleep study')
    work, changed, _ = _apply(src, 'md-4', vals)
    assert set(changed) == set(vals)
    rows = [r for r in work.root.find('DOCUMENTS2').findall('DOCUMENT') if work.child_text(r, 'DOC_NO') == '4']
    assert len(rows) == 2
    for row in rows:
        assert work.child_text(row, 'DOCDATE') == '2021-01-05 00:00:00'
        assert work.child_text(row, 'DOCTITLE') == 'Sleep study'
        assert work.child_text(row, 'DOCTYPE') == 'Respiratory'
        assert work.child_text(row, 'DOCTORNAME') == 'Dr New'
    item = work.root.find('DOCUMENTHANDLING').findall('ITEM')[0]
    assert work.child_text(item, 'DOCUMENT_DATETIME') == '2021-01-05 00:00:00'
    assert work.child_text(item, 'SUBJECT') == 'Sleep study'
    assert work.child_text(item, 'DOCUMENT_TYPE') == 'Respiratory'
    assert work.child_text(item, 'DOCUMENT_FROM') == 'Sleep Clinic'
    assert work.child_text(item, 'DOCUMENT_TO') == 'Dr New'          # was the same name in other case: synced
    r = D.record_map(work)['md-4']
    assert (r.provider, r.to, r.subject, r.date) == ('Sleep Clinic', 'Dr New', 'Sleep study', '05/01/2021')
    # the other document is untouched
    assert D.record_map(work)['md-5'] == D.record_map(src)['md-5'] or \
        work.raw(work.root.find('DOCUMENTS2').findall('DOCUMENT')[2]) == src.raw(src.root.find('DOCUMENTS2').findall('DOCUMENT')[2])


def test_md_out_of_sync_copy_is_left_alone():
    src = XmlDoc(md_xml())
    work, changed, _ = _apply(src, 'md-5', {'to': 'Dr New'})
    item = work.root.find('DOCUMENTHANDLING').findall('ITEM')[2]
    assert work.child_text(item, 'DOCUMENT_TO') == 'Someone Else'
    row = work.root.find('DOCUMENTS2').findall('DOCUMENT')[2]
    assert work.child_text(row, 'DOCTORNAME') == 'Dr New'


def test_md_date_keeps_time_part():
    data = md_xml().replace(b'<DOCDATE>2020-11-18 00:00:00</DOCDATE><DOCDESC>DESC</DOCDESC><MD_GUID>G2',
                            b'<DOCDATE>2020-11-18 09:30:00</DOCDATE><DOCDESC>DESC</DOCDESC><MD_GUID>G2')
    work, _, _ = _apply(XmlDoc(data), 'md-5', {'date': '2022-03-04'})
    row = work.root.find('DOCUMENTS2').findall('DOCUMENT')[2]
    assert work.child_text(row, 'DOCDATE') == '2022-03-04 09:30:00'


def test_invalid_date_rejected():
    src = XmlDoc(bp_xml())
    with pytest.raises(D.MetadataError):
        _apply(src, 'bpdoc-9', {'date': '14/02/2021'})
    with pytest.raises(D.MetadataError):
        _apply(src, 'bpdoc-9', {'date': ''})


def test_letters_edit_recipient():
    src = XmlDoc(bp_xml())
    rec = D.record_map(src)['bpcor-7']
    assert rec.to == 'Dr C' and rec.provider == ''
    assert [n for n, _ in D.editable_fields(rec)] == ['date', 'to', 'subject']
    work, _, _ = _apply(src, 'bpcor-7', {'to': 'Dr D'})
    assert D.record_map(work)['bpcor-7'].to == 'Dr D'
