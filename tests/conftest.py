import base64
import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('CURATOR_OCR', 'tesseract')      # tests must not depend on a liteparse service

PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'
RTF = rb'{\rtf1\ansi Hello | A | B |\par World}'


def _zip_b64(name, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        # fixed timestamp: a ZIP records when it was made, which would make the fixture differ between calls
        z.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data, compress_type=zipfile.ZIP_DEFLATED)
    return base64.b64encode(buf.getvalue()).decode()


def bp_xml():
    """A small BPSEHRV2 export: ISO-8859-1, LF separators, a multi-page document, an attached investigation,
    an RTF-only investigation, outgoing correspondence, a self-closing field and a non-ASCII character."""
    pdf = _zip_b64('scan.pdf', PDF)
    rtf = _zip_b64('note.rtf', RTF)
    return ('<?xml version="1.0" encoding="ISO-8859-1"?>\n<BPSEHRV2><BPSVersion>1.13</BPSVersion>\n\n'
            '<Demographics>\n<Patient><FIRSTNAME>Testy   </FIRSTNAME><MIDDLENAME /><SURNAME>Caf\xe9</SURNAME><DOB>01/02/1950</DOB></Patient>\n</Demographics>'
            '<Investigations>\n'
            f'<Investigation><REPORTID>7</REPORTID><TESTNAME>CT Head</TESTNAME><REPORTDATE>05/06/2021</REPORTDATE><PROVIDERNAME>Xray Co</PROVIDERNAME><InvestigationPage><PageNo>1</PageNo><DocType>PDF </DocType><Content>{pdf}</Content></InvestigationPage></Investigation>\n'
            '<Investigation><REPORTID>8</REPORTID><TESTNAME>FBC</TESTNAME><REPORTDATE>01/01/2020</REPORTDATE><PROVIDERNAME>Path Lab</PROVIDERNAME><REPORTHEADER>{\\rtf1 Header}</REPORTHEADER><REPORTBODY>{\\rtf1 Hb 140 &amp; ok}</REPORTBODY></Investigation>\n'
            '</Investigations>\n<InvestigationValues>\n<Result><REPORTID>8</REPORTID><RESULTNAME>Hb</RESULTNAME></Result>\n</InvestigationValues>\n'
            '<CorrespondenceIn>\n'
            f'<Document><DOCUMENTID>7</DOCUMENTID><CORRESPONDENCEDATE>03/01/2020</CORRESPONDENCEDATE><CONTACTNAME>Dr A</CONTACTNAME><CATEGORY>Letter</CATEGORY><SUBJECT>Referral &amp; reply</SUBJECT><DocumentPage><PageNo>1</PageNo><DocType>PDF </DocType><FileName>a.pdf</FileName><Content>{pdf}</Content></DocumentPage><DocumentPage><PageNo>2</PageNo><DocType>RTF </DocType><FileName>b.rtf</FileName><Content>{rtf}</Content></DocumentPage></Document>\n'
            f'<Document><DOCUMENTID>9</DOCUMENTID><CORRESPONDENCEDATE>04/02/2021</CORRESPONDENCEDATE><CONTACTNAME>Dr B</CONTACTNAME><CATEGORY>Report</CATEGORY><SUBJECT /><DocumentPage><PageNo>1</PageNo><DocType>PDF </DocType><FileName>c.pdf</FileName><Content>{pdf}</Content></DocumentPage></Document>\n'
            '</CorrespondenceIn>\n<CorrespondenceOut>\n'
            '<Correspondence><RECORDID>7</RECORDID><CORRESPONDENCEDATE>02/02/2022</CORRESPONDENCEDATE><CONTACTNAME>Dr C</CONTACTNAME><SUBJECT>To specialist</SUBJECT><CONTENT>{\\rtf1 Dear Dr}</CONTENT></Correspondence>\n'
            '</CorrespondenceOut>\n</BPSEHRV2>\n').encode('iso-8859-1')


def md_xml():
    """A small MedicalDirector (<PATIENT>) export: UTF-8 with BOM, no whitespace between elements, a two-part document and the
    linked DOCUMENTHANDLING items."""
    pdf = base64.b64encode(PDF).decode()
    rtf = base64.b64encode(RTF).decode()

    def row(no, page, guid, fname, data, title='Discharge summary'):
        return (f'<DOCUMENT><DOC_NO>{no}</DOC_NO><PAGE_NO>{page}</PAGE_NO><DOCTORNAME>Dr Zürich</DOCTORNAME>'
                f'<DOCTITLE>{title}</DOCTITLE><DOCTYPE>Medical</DOCTYPE><DOCDATE>2020-11-18 00:00:00</DOCDATE>'
                f'<DOCDESC>DESC</DOCDESC><MD_GUID>{guid}</MD_GUID><FILENAME>{fname}</FILENAME>'
                f'<BASE64_DATA>{data}</BASE64_DATA></DOCUMENT>')

    def item(guid, subject, frm, to):
        return (f'<ITEM><DMS_MD_GUID>{guid}</DMS_MD_GUID><CATEGORY>2</CATEGORY><DOCUMENT_TYPE>Medical</DOCUMENT_TYPE>'
                f'<DOCUMENT_DATETIME>2020-11-18 00:00:00</DOCUMENT_DATETIME><SUBJECT>{subject}</SUBJECT>'
                f'<DOCUMENT_FROM>{frm}</DOCUMENT_FROM><DOCUMENT_TO>{to}</DOCUMENT_TO></ITEM>')

    return ('﻿<?xml version="1.0" encoding="utf-8"?><PATIENT VERSION="3.2" SCHEMA="1374">'
            '<DEMOGRAPHICS><FIRSTNAME>Testy</FIRSTNAME><SURNAME>Persön</SURNAME><DOB>1950-02-01 00:00:00</DOB></DEMOGRAPHICS>'
            '<DOCUMENTHANDLING>' + item('G1', 'Discharge summary', 'Brain \\T\\ Spine', 'DR Z\u00dcRICH')
            + '<ITEM><DMS_MD_GUID></DMS_MD_GUID><CATEGORY>1</CATEGORY></ITEM>'
            + item('G2', 'Letter', '', 'Someone Else') + '</DOCUMENTHANDLING>'
            '<DOCUMENTS2>' + row(4, 1, 'G1', 'x.PDF', pdf) + row(4, 2, 'G1', 'y.RTF', rtf) + row(5, 1, 'G2', 'z.PDF', pdf, 'Letter')
            + '</DOCUMENTS2></PATIENT>').encode('utf-8')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CURATOR_DATA', str(tmp_path / 'data'))
    import importlib
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as c:
        c.ws = app_module.ws
        yield c
