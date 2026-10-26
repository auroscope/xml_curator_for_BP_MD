import io
import re

from conftest import bp_xml, md_xml
from curator import clinical as C
from curator import documents as D
from curator.xmlstore import XmlDoc, apply_edits


def bp_clinical():
    block = ('<Reactions>\n<Reaction><RECORDID>1</RECORDID><ITEMNAME>Penicillin</ITEMNAME><REACTION>Rash</REACTION></Reaction>\n</Reactions>\n'
             '<CurrentRx>\n'
             '<Medication><RECORDID>11</RECORDID><DRUGNAME>Metformin 500mg</DRUGNAME><RXSTATUS>1</RXSTATUS><DOSE>1</DOSE>'
             '<INSTRUCTIONS>twice daily</INSTRUCTIONS><FIRSTDATE>01/02/2020</FIRSTDATE><LASTDATE>01/02/2024</LASTDATE></Medication>\n'
             '<Medication><RECORDID>12</RECORDID><DRUGNAME>Codeine</DRUGNAME><RXSTATUS>0</RXSTATUS><DELETIONREASON>Nausea</DELETIONREASON>'
             '<DELETIONDATE>03/03/2021</DELETIONDATE></Medication>\n</CurrentRx>\n'
             '<PastHistory>\n<Condition><RECORDID>21</RECORDID><YEAR>2015</YEAR><ITEMTEXT>Type 2 diabetes</ITEMTEXT><DETAILS /></Condition>\n'
             '<Condition><RECORDID>22</RECORDID><YEAR>0</YEAR><ITEMTEXT>Asthma</ITEMTEXT><DETAILS>childhood</DETAILS></Condition>\n</PastHistory>\n'
             '<Observations>\n'
             '<Observation><RECORDID>31</RECORDID><OBSDATE>01/01/2022</OBSDATE><DATANAME>Systolic    </DATANAME><DATAVALUE>130</DATAVALUE></Observation>\n'
             '<Observation><RECORDID>32</RECORDID><OBSDATE>01/01/2022</OBSDATE><DATANAME>Diastolic   </DATANAME><DATAVALUE>80</DATAVALUE></Observation>\n'
             '<Observation><RECORDID>33</RECORDID><OBSDATE>01/06/2022</OBSDATE><DATANAME>Systolic    </DATANAME><DATAVALUE>124</DATAVALUE></Observation>\n'
             '</Observations>\n'
             '<Visit><VISITDATE>05/05/2022</VISITDATE><DRNAME>Dr Who</DRNAME><VISITNOTES>{\\rtf1 Cough for 3 days.}</VISITNOTES></Visit>\n')
    data = bp_xml().replace(b'<CorrespondenceIn>', block.encode('iso-8859-1') + b'<CorrespondenceIn>', 1)
    # two charted results for haemoglobin (LOINC 718-7)
    return data.replace(b'<Result><REPORTID>8</REPORTID><RESULTNAME>Hb</RESULTNAME></Result>\n',
                        b'<Result><RECORDID>41</RECORDID><REPORTID>8</REPORTID><REPORTDATE>01/01/2020</REPORTDATE><LOINCCODE>718-7</LOINCCODE>'
                        b'<RESULTNAME>Hb</RESULTNAME><RESULTVALUE>140</RESULTVALUE><UNITS>g/L</UNITS><RANGE>115-165</RANGE><ABNORMALFLAG /></Result>\n'
                        b'<Result><RECORDID>42</RECORDID><REPORTID>8</REPORTID><REPORTDATE>01/01/2021</REPORTDATE><LOINCCODE>718-7</LOINCCODE>'
                        b'<RESULTNAME>Haemoglobin</RESULTNAME><RESULTVALUE>110</RESULTVALUE><UNITS>g/L</UNITS><RANGE>115-165</RANGE><ABNORMALFLAG>L</ABNORMALFLAG></Result>\n')


def md_clinical():
    block = ('<REACTIONS><ALLERGY><ITEM>SULFONAMIDES</ITEM><TYPE></TYPE><SEVERITY>0</SEVERITY><ADR>True</ADR></ALLERGY></REACTIONS>'
             '<ADDITIONAL_DEMOGRAPHICS><ALLERGIES>Nuts</ALLERGIES><WARNINGS></WARNINGS></ADDITIONAL_DEMOGRAPHICS>'
             '<RX><RXITEM><DRUG_NO>7</DRUG_NO><DOSE>one</DOSE><FREQUENCY>daily</FREQUENCY><SPECIAL></SPECIAL>'
             '<FIRSTDATE>2021-06-11</FIRSTDATE><LASTDATE>2026-04-20</LASTDATE><CEASED_DATE></CEASED_DATE></RXITEM></RX>'
             '<SCRIPTS><SCRIPTITEM><DRUG_NO>7</DRUG_NO><P1>Atorvastatin 20mg</P1></SCRIPTITEM></SCRIPTS>'
             '<PROGRESS><VISIT><DR>Dr Z</DR><REASON>Check-up</REASON><VISITDATE>2021-03-19 00:00:00</VISITDATE>'
             '<NOTES>{\\rtf1\\ansi Well.}</NOTES></VISIT></PROGRESS>'
             '<PMH><PMHITEM><YEAR>2010</YEAR><CONDITION>Hypertension</CONDITION><COMMENT></COMMENT><ACTIVE>True</ACTIVE></PMHITEM></PMH>'
             '<MEASURES><MEASUREMENT><TYPE>SYSTOLIC</TYPE><VALUE>130</VALUE><MEAS_DATE>2021-03-19 15:26:08</MEAS_DATE></MEASUREMENT>'
             '<MEASUREMENT><TYPE>SYSTOLIC</TYPE><VALUE>135</VALUE><MEAS_DATE>2021-09-19 15:26:08</MEAS_DATE></MEASUREMENT></MEASURES>'
             '<IX><RESULT><PATHOLOGY_ID>99</PATHOLOGY_ID><LABNAME>Test Lab</LABNAME><TESTNAME>FBC</TESTNAME>'
             '<REPORTDATE>2021-06-12</REPORTDATE><REPORTHEAD>Start patient: TEST</REPORTHEAD>'
             '<REPORTTEXT>{\\rtf1\\ansi Hb 140 g/L}</REPORTTEXT><DMS_MD_GUID></DMS_MD_GUID></RESULT></IX>'
             '<ATOM><RESULT><PATHOLOGY_ID>99</PATHOLOGY_ID><RESULTDATE>2021-06-12</RESULTDATE><LOINC>718-7</LOINC>'
             '<TESTNAME>Haemoglobin</TESTNAME><RESULT>140</RESULT><UNITS>g/L</UNITS><RANGE>115-165</RANGE></RESULT></ATOM>')
    data = md_xml().replace('</DEMOGRAPHICS>'.encode(), ('</DEMOGRAPHICS>' + block).encode('utf-8'), 1)
    item = ('<ITEM><PATHOLOGY_ID>99</PATHOLOGY_ID><DMS_MD_GUID></DMS_MD_GUID><CATEGORY>1</CATEGORY>'
            '<DOCUMENT_TYPE>Pathology</DOCUMENT_TYPE><DOCUMENT_DATETIME>2021-06-12 00:00:00</DOCUMENT_DATETIME>'
            '<SUBJECT>FBC</SUBJECT></ITEM>')
    return data.replace(b'</DOCUMENTHANDLING>', item.encode() + b'</DOCUMENTHANDLING>', 1)


def _secs(doc):
    return {s.id: s for s in C.sections(doc)}


def test_bp_sections():
    s = _secs(XmlDoc(bp_clinical()))
    assert [i.values['item'] for i in s['alg'].items] == ['Penicillin']
    assert [i.values['condition'] for i in s['pmh'].items] == ['Type 2 diabetes', 'Asthma']
    assert s['pmh'].items[1].values['year'] == ''
    assert s['rx'].items[0].values['drug'] == 'Metformin 500mg' and s['rx'].items[0].values['type'] == 'Regular'
    assert s['rxp'].items[0].values['reason'] == '(ceased: Nausea)' and s['rxp'].items[0].values['stopped'] == '03/03/2021'
    assert [i.values['value'] for i in s['obs'].items] == ['124', '130', '80']
    assert s['vis'].items[0].values['notes'] == 'Cough for 3 days.'
    assert [i.flag for i in s['res'].items] == ['L', '']


def test_md_sections_and_demographics():
    doc = XmlDoc(md_clinical())
    s = _secs(doc)
    assert s['alg'].items[0].values['reaction'] == 'Adverse reaction' and 'Nuts' in s['alg'].note
    assert s['rx'].items[0].values['drug'] == 'Atorvastatin 20mg'
    assert s['vis'].items[0].values['notes'] == 'Reason: Check-up\nWell.'
    assert s['res'].items[0].values['loinc'] == '718-7'
    assert C.demographics(doc)['Date of birth'] == '01/02/1950'


def test_charts_combine_blood_pressure_and_lab_series():
    charts = {c['title']: c for c in C.charts(XmlDoc(bp_clinical()))}
    bp = charts['Blood pressure']
    assert bp['labels'] == ['01/01/2022', '01/06/2022']
    assert [d['label'] for d in bp['datasets']] == ['Systolic', 'Diastolic']
    assert bp['datasets'][0]['data'] == [130.0, 124.0] and bp['datasets'][1]['data'] == [80.0, None]
    hb = charts['Hb']                                   # shortest name of the LOINC group
    assert hb['code'] == '718-7' and hb['datasets'][0]['data'] == [140.0, 110.0] and hb['range'] == [115.0, 165.0]
    md = {c['title']: c for c in C.charts(XmlDoc(md_clinical()))}
    assert md['Blood pressure']['datasets'][0]['data'] == [130.0, 135.0] and 'Haemoglobin' in md


def test_item_keys_match_between_copies_and_restore_is_byte_identical():
    src_data = bp_clinical()
    src = XmlDoc(src_data)
    keys = list(C.item_map(src))
    assert len(keys) == len(set(keys)) and all(re.match(r'^c[a-z]+-[0-9a-f]{12}', k) for k in keys)
    edits = [src.edit_remove(n) for k in keys for n in C.item_map(src)[k].nodes]
    work = XmlDoc(apply_edits(src_data, edits))
    assert all(len(s.items) == 0 for s in C.sections(work))
    for k in reversed(keys):
        e, rec = D.revert_edits(work, src, k, C.item_map)
        assert rec is not None
        work = XmlDoc(apply_edits(work.data, e))
    assert work.data == src_data


def test_md_pathology_report_is_a_document_with_its_handling_item():
    src_data = md_clinical()
    src = XmlDoc(src_data)
    rec = D.record_map(src)['mdix-99']
    assert rec.section == 'reports' and rec.subject == 'FBC' and rec.provider == 'Test Lab' and rec.date == '12/06/2021'
    assert [n.tag for n in rec.nodes] == ['RESULT', 'ITEM']
    assert [n for n, _ in D.editable_fields(rec)] == ['date', 'provider', 'subject']
    edits, _ = D.delete_edits(src, ['mdix-99'])
    work = XmlDoc(apply_edits(src_data, edits))
    assert 'mdix-99' not in D.record_map(work)
    assert [work.child_text(i, 'PATHOLOGY_ID') for i in work.root.find('DOCUMENTHANDLING').findall('ITEM')] == ['', '', '']
    assert len(work.root.find('ATOM').findall('RESULT')) == 1        # its atomised values stay
    e, _ = D.revert_edits(work, src, 'mdix-99')
    assert apply_edits(work.data, e) == src_data


# ---- web -------------------------------------------------------------------------------------------------

def _upload(client, data, name='x.xml'):
    tok = re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)
    r = client.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), name)}, content_type='multipart/form-data')
    return r.headers['Location'].split('/w/')[1].split('/')[0], tok


def test_summary_delete_and_restore(client):
    data = bp_clinical()
    wid, tok = _upload(client, data)
    page = client.get(f'/w/{wid}/summary').get_data(as_text=True)
    for text in ('Penicillin', 'Metformin 500mg', 'Type 2 diabetes', 'Cough for 3 days.', 'Outgoing letters', 'To specialist'):
        assert text in page
    doc = XmlDoc(data)
    pen = next(i.key for i in C.item_map(doc).values() if i.subject == 'Penicillin')
    r = client.post(f'/w/{wid}/items/delete', data={'_csrf': tok, 'keys': [pen, 'bpcor-7']})
    assert r.status_code == 302
    page = client.get(f'/w/{wid}/summary').get_data(as_text=True)
    assert 'Deleted (1)' in page and 'Penicillin' in page        # listed for restore
    work = XmlDoc(client.get(f'/w/{wid}/download').data)
    assert not _secs(work)['alg'].items and 'bpcor-7' not in D.record_map(work)
    log = client.get(f'/w/{wid}/changes?format=json').get_json()
    assert sorted((c['action'], c['subject']) for c in log[1:]) == [('delete', 'Penicillin'), ('delete', 'To specialist')]
    for k in (pen, 'bpcor-7'):
        client.post(f'/w/{wid}/doc/{k}/revert', data={'_csrf': tok, 'back': 'summary'})
    assert client.get(f'/w/{wid}/download').data == data


def test_charts_page(client):
    wid, _ = _upload(client, md_clinical())
    page = client.get(f'/w/{wid}/charts').get_data(as_text=True)
    assert '/static/vendor/chart.umd.js' in page and 'cdn.' not in page and 'Blood pressure' in page and 'LOINC 718-7' in page
    report = client.get(f'/w/{wid}/doc/mdix-99/part/0').get_data(as_text=True)
    assert 'Start patient: TEST' in report and 'Hb 140 g/L' in report


def test_summary_shows_the_space_each_section_takes(client):
    import io as _io
    import re as _re
    from conftest import bp_xml
    tok = _re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)
    r = client.post('/upload', data={'_csrf': tok, 'file': (_io.BytesIO(bp_xml()), 'x.xml')},
                    content_type='multipart/form-data')
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    page = client.get(f'/w/{wid}/summary').get_data(as_text=True)
    assert _re.search(r'Outgoing letters <span class="muted">1 · \d+ B</span>', page)
    assert _re.search(r'Documents <span class="muted">3 · \d+ KB</span>', page)    # 2 documents + 1 attached report
    assert 'Everything else' in page and 'Whole file' in page

    import app as A
    from curator import clinical as Cl
    from curator import documents as Dm
    doc = client.ws.load(wid)
    groups = {sec.id: sec.items for sec in Cl.sections(doc)}
    recs = Dm.records(doc)
    for name in ('reports', 'letters', 'documents'):
        groups[name] = [r for r in recs if r.section == name]
    sizes = A._section_sizes(doc, groups)
    parts = {k: v for k, v in sizes.items() if k != 'other_parts'}
    assert sum(parts.values()) == len(doc.data)                        # they add up to the whole file
    assert sizes['letters'] == sum(n.end - n.start for r in groups['letters'] for n in r.nodes)


# ---- Other data (BP sections not summarised elsewhere) ---------------------------------------------------

def _bp_with_other_data():
    from conftest import bp_xml
    extra = ('<Prescriptions>\n'
             '<Prescription><SCRIPTID>11</SCRIPTID><SCRIPTDATE>01/02/2015</SCRIPTDATE><AUTHORITY>0</AUTHORITY></Prescription>\n'
             '<Prescription><SCRIPTID>12</SCRIPTID><SCRIPTDATE>01/03/2019</SCRIPTDATE></Prescription>\n'
             '</Prescriptions>\n<ScriptItems>\n'
             '<ScriptItem><SCRIPTID>11</SCRIPTID><PRODUCTNAME>Testamol</PRODUCTNAME><STRENGTH>500mg</STRENGTH><DOSE>1</DOSE>'
             '<FREQUENCY>twice a day</FREQUENCY><QUANTITY>20</QUANTITY><REPEATS>2</REPEATS></ScriptItem>\n'
             '<ScriptItem><SCRIPTID>12</SCRIPTID><PRODUCTNAME>Examplin</PRODUCTNAME><QUANTITY>30</QUANTITY><REPEATS>0</REPEATS></ScriptItem>\n'
             '</ScriptItems>\n<Parameters>\n'
             '<Parameter><DATADATE>01/02/2015</DATADATE><DATANAME>UAGlucose</DATANAME><DATAVALUE></DATAVALUE><DATACODE>0</DATACODE></Parameter>\n'
             '<Parameter><DATADATE>01/02/2015</DATADATE><DATANAME>UApH</DATANAME><DATAVALUE /><DATACODE>0</DATACODE></Parameter>\n'
             '<Parameter><DATADATE>01/02/2015</DATADATE><DATANAME>Hs</DATANAME><DATAVALUE>12</DATAVALUE></Parameter>\n'
             '</Parameters>\n<ReasonForVisit>\n<Reason><VISITID>1</VISITID><REASON>Check-up</REASON></Reason>\n</ReasonForVisit>\n')
    return bp_xml().replace(b'<CorrespondenceIn>', extra.encode('latin-1') + b'<CorrespondenceIn>', 1)


def _upload_other(client):
    import io as _io
    import re as _re
    tok = _re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)
    r = client.post('/upload', data={'_csrf': tok, 'file': (_io.BytesIO(_bp_with_other_data()), 'x.xml')},
                    content_type='multipart/form-data')
    return r.headers['Location'].split('/w/')[1].split('/')[0], tok


def test_other_data_is_listed_and_a_script_goes_with_its_items(client):
    from curator import clinical as Cl
    wid, tok = _upload_other(client)
    doc = client.ws.load(wid)
    secs = {s.id: s for s in Cl.sections(doc)}
    assert {'scr', 'par', 'rfv'} <= set(secs) and 'req' not in secs               # empty parts are not shown
    scr = secs['scr'].items
    assert [i.values['drug'] for i in scr] == ['Examplin', 'Testamol 500mg']        # newest first
    old = next(i for i in scr if i.values['drug'] == 'Testamol 500mg')
    assert len(old.nodes) == 2 and 'PRODUCTNAME: Testamol' in old.values['_hover']
    assert old.values['dose'] == '1 twice a day' and old.values['qty'] == '20' and old.values['rpt'] == '2'
    assert 'AUTHORITY' not in old.values['_hover']                                # defaults are left out of the hover

    page = client.get(f'/w/{wid}/summary').get_data(as_text=True)
    assert 'Other data' in page and 'Prescriptions written (script history)' in page
    frag = client.get(f'/w/{wid}/summary/section/scr').get_data(as_text=True)
    assert frag.count('name="keys"') == 2 and 'PRODUCTNAME: Testamol' in frag and 'data-tick-before' in frag

    client.post(f'/w/{wid}/items/delete', data={'_csrf': tok, 'keys': [old.key]})
    data = client.ws.load(wid).data
    assert b'Testamol' not in data and b'<SCRIPTID>11</SCRIPTID>' not in data and b'Examplin' in data
    client.post(f'/w/{wid}/doc/{old.key}/revert', data={'_csrf': tok, 'back': 'summary'})
    data = client.ws.load(wid).data
    assert b'Testamol' in data and data.count(b'<SCRIPTID>11</SCRIPTID>') == 2


def test_visit_measurements_are_listed_only_with_empty_entries_greyed(client):
    from curator import clinical as Cl
    wid, tok = _upload_other(client)
    par = {s.id: s for s in Cl.sections(client.ws.load(wid))}['par']
    assert par.readonly and [i.values['value'] for i in par.items] == ['(no value)', '(no value)', '12']
    frag = client.get(f'/w/{wid}/summary/section/par').get_data(as_text=True)
    assert 'name="keys"' not in frag and frag.count('empty-row') == 2
    client.post(f'/w/{wid}/items/delete', data={'_csrf': tok, 'keys': [i.key for i in par.items]})
    assert client.ws.load(wid).data.count(b'<Parameter>') == 3                     # refused even if posted


def test_reasons_for_visit_are_listed_only(client):
    from curator import clinical as Cl
    wid, tok = _upload_other(client)
    rfv = {s.id: s for s in Cl.sections(client.ws.load(wid))}['rfv']
    assert rfv.readonly and 'name="keys"' not in client.get(f'/w/{wid}/summary/section/rfv').get_data(as_text=True)
    client.post(f'/w/{wid}/items/delete', data={'_csrf': tok, 'keys': [rfv.items[0].key]})
    assert b'Check-up' in client.ws.load(wid).data                                 # refused even if posted
