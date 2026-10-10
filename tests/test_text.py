import base64
import io
import re

import pytest

from conftest import md_xml, PDF
from pdfs import LETTER, needs_gs, needs_tesseract, text_pdf, text_scan_pdf
from curator import documents as D
from curator import ocr, pdfops, textpdf
from curator.xmlstore import XmlDoc


# ---- reflow ----------------------------------------------------------------------------------------------

def test_reflow_joins_prose_keeps_short_lines_lists_and_tables():
    layout = (
        '    Dr A Smith\n'
        '    12 Main Street\n'
        '\n'
        '    Thank you for referring this patient with recurrent headaches over the\n'
        '    past three months. Examination was unremarkable and the findings were\n'
        '    normal.\n'
        '\n'
        '    Results:\n'
        '    Haemoglobin      140      115-165\n'
        '    Platelets        250      150-400\n'
        '\n'
        '    Plan:\n'
        '    1. Review in six weeks with a headache diary and repeat bloods then\n'
        '    2. MRI if symptoms persist\n')
    out = textpdf.reflow(layout)
    assert out.split('\n\n')[0] == 'Dr A Smith\n12 Main Street'
    assert ('Thank you for referring this patient with recurrent headaches over the past three months. '
            'Examination was unremarkable and the findings were normal.') in out
    assert 'Results:\nHaemoglobin      140      115-165\nPlatelets        250      150-400' in out
    assert 'Plan:\n1. Review in six weeks with a headache diary and repeat bloods then\n2. MRI' in out


def test_reflow_rejoins_hyphenated_words():
    out = textpdf.reflow('This line is long enough to be prose and it ends with a hyphen-\n'
                         'ated word that continues on the next line of the paragraph here.')
    assert 'hyphen-ated' in out


# ---- building / verifying --------------------------------------------------------------------------------

@needs_gs
def test_build_text_pdf_contains_exactly_the_text():
    text = ('Dear Doctor,\n\nµg/L 5 · temperature 38.2°C · café\n\n'
            'Haemoglobin      140      115-165\nPlatelets        250      150-400\n\n' + 'Long paragraph. ' * 400)
    footer = 'Text transcribed by OCR (Tesseract 5) from the original scanned document (1.0 MB, 2 pages) on 01/01/2026.'
    pdf, drawn = textpdf.build_text_pdf(text, footer, title='Letter')
    v = textpdf.verify_text_pdf(text, pdf, drawn)
    assert v['identical'], v
    assert pdfops.analyse(pdf)['pages'] >= 2 and len(pdf) < 60_000
    assert sum(1 for d in drawn['strings'] if d.startswith('Page ')) == pdfops.analyse(pdf)['pages']
    assert drawn['fonts']['Sans'] == 'Helvetica'                      # µ ° é · are all in the Western set


@needs_gs
def test_verify_detects_a_difference():
    pdf, drawn = textpdf.build_text_pdf('one two three', 'footer')
    v = textpdf.verify_text_pdf('one two four', pdf, drawn)
    assert not v['identical'] and v['missing'] == ['four'] and v['added'] == ['three']


@needs_gs
def test_wrapping_and_end_of_line_hyphens_still_verify():
    text = ('Page 1 of 2\n-2-\nRE: Referral\n\n' + 'See https://example.org/results/view?code=0a1b2c3d4e5f60718293a4 and '
            '01/02/1960 (e.g., post-operative review.\u201d) ' * 12)
    pdf, drawn = textpdf.build_text_pdf(text, 'footer')
    assert textpdf.verify_text_pdf(text, pdf, drawn)['identical']


@needs_gs
def test_symbol_bullets_become_real_bullets_and_base_fonts_are_used():
    text = textpdf.clean_text('\uf0b7 first item\n\uf0d8 second item')
    assert text == '\u2022 first item\n\u2022 second item'
    pdf, drawn = textpdf.build_text_pdf(text, 'footer')
    assert drawn['fonts']['Sans'] == 'Helvetica' and len(pdf) < 6000
    assert textpdf.verify_text_pdf(text, pdf, drawn)['identical']


@needs_gs
def test_unsupported_glyph_fails_verification():
    pdf, drawn = textpdf.build_text_pdf('Arabic \u0628 and a private \ue000 glyph', 'footer')
    v = textpdf.verify_text_pdf('Arabic \u0628 and a private \ue000 glyph', pdf, drawn)
    assert not v['identical'] and '\ue000' in v['unsupported']


@needs_gs
def test_hidden_text_layer_is_searchable_and_small():
    scan = text_scan_pdf()
    compressed = pdfops.compress(scan, pdfops.analyse(scan))
    pages = [{'page': 1, 'width': 595.44, 'height': 842.04,
              'words': [{'text': 'Haemoglobin', 'x': 54, 'y': 200, 'w': 90, 'h': 12},
                        {'text': 'Platelets', 'x': 54, 'y': 220, 'w': 70, 'h': 12}]}]
    layered = textpdf.add_hidden_text(compressed, pages)
    text = ''.join(textpdf.text_layer_pages(layered))
    assert 'Haemoglobin' in text and 'Platelets' in text
    assert len(layered) - len(compressed) < 4000


# ---- OCR ---------------------------------------------------------------------------------------------------

@needs_gs
@needs_tesseract
def test_tesseract_reads_a_scan():
    res = ocr.ocr_pdf(text_scan_pdf())
    assert res['engine'].startswith('Tesseract')
    page = res['pages'][0]
    got = set(pdfops.words(page['text']))
    expected = set(pdfops.words(' '.join(LETTER)))
    assert len(got & expected) / len(expected) > 0.9
    assert re.search(r'Haemoglobin {3,}140 {3,}115-165', page['text'])     # table spacing kept
    assert page['words'] and all(w['w'] > 0 and w['h'] > 0 for w in page['words'])


def test_engine_choice(monkeypatch):
    monkeypatch.setattr(ocr, 'ENGINE', 'auto')
    monkeypatch.setattr(ocr, 'liteparse_health', lambda: (True, '9.9'))
    assert ocr.choose_engine() == ('liteparse', 'liteparse 9.9')
    monkeypatch.setattr(ocr, 'liteparse_health', lambda: (False, ''))
    monkeypatch.setattr(ocr, 'tesseract_version', lambda: '5.5.0')
    assert ocr.choose_engine() == ('tesseract', 'Tesseract 5.5.0')
    monkeypatch.setattr(ocr, 'ENGINE', 'liteparse')
    with pytest.raises(ocr.OcrError):
        ocr.choose_engine()


# ---- web flow --------------------------------------------------------------------------------------------

def _token(client):
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)


@pytest.fixture
def sync_client(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.cands, 'start', lambda *a: None)
    monkeypatch.setattr(app_module.cands, 'start_precompute', lambda *a: None)
    client.cands = app_module.cands
    return client


@needs_gs
@needs_tesseract
def test_text_only_save_from_ocr(sync_client):
    c = sync_client
    data = md_xml().replace(base64.b64encode(PDF), base64.b64encode(text_scan_pdf()), 1)
    tok = _token(c)
    r = c.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), 'scan.xml')}, content_type='multipart/form-data')
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    c.cands.compute(wid, 'md-4', 0)
    page = c.get(f'/w/{wid}/doc/md-4/part/0/shrink').get_data(as_text=True)
    assert 'Text read by OCR (Tesseract' in page and 'id="text-box"' in page
    assert 'invisible OCR text layer' in page                       # compressed scan got the hidden layer
    cid = re.search(r'name="cid" value="([^"]+)"', page).group(1)
    initial = c.cands.text_initial(wid, cid)
    assert 'Haemoglobin' in initial

    edited = initial.replace('three months', 'four months')
    prev = c.post(f'/w/{wid}/doc/md-4/part/0/shrink/text-preview', data={'_csrf': tok, 'text': edited})
    assert prev.mimetype == 'application/pdf' and prev.headers['X-Text-Identical'] == '1'
    assert prev.headers['X-Edited-Words'] == '1'

    r = c.post(f'/w/{wid}/doc/md-4/part/0/shrink/save', data={'_csrf': tok, 'cid': cid, 'option': 'text', 'text': edited})
    assert '/documents' in r.headers['Location']
    doc = XmlDoc(c.get(f'/w/{wid}/download').data)
    new_pdf = D.part_bytes(doc, D.record_map(doc)['md-4'].parts[0])[1]
    text = ''.join(textpdf.text_layer_pages(new_pdf))
    assert 'four months' in text and 'three months' not in text
    assert 'Text transcribed by OCR (Tesseract' in text and 'reviewed and edited' in text
    log = c.get(f'/w/{wid}/changes?format=json').get_json()[-1]
    assert log['option'] == 'text' and log['edited_words'] == 1 and log['engine'].startswith('Tesseract')
    assert '1 word(s) edited' in c.get(f'/w/{wid}/changes?format=csv').get_data(as_text=True)


@needs_gs
def test_text_only_for_text_layer_pdf_needs_no_ocr(sync_client, monkeypatch):
    c = sync_client
    monkeypatch.setattr(ocr, 'ocr_pdf', lambda *a, **k: pytest.fail('OCR must not run for a text PDF'))
    data = md_xml().replace(base64.b64encode(PDF), base64.b64encode(text_pdf()), 1)
    tok = _token(c)
    r = c.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), 't.xml')}, content_type='multipart/form-data')
    wid = r.headers['Location'].split('/w/')[1].split('/')[0]
    c.cands.compute(wid, 'md-4', 0)
    page = c.get(f'/w/{wid}/doc/md-4/part/0/shrink').get_data(as_text=True)
    assert "Text taken from the PDF&#39;s own text layer" in page or "Text taken from the PDF's own text layer" in page


@needs_gs
def test_invisible_formatting_characters_are_dropped():
    text = textpdf.clean_text('Patient­Information and zero​width﻿')
    assert text == 'PatientInformation and zerowidth'
    pdf, drawn = textpdf.build_text_pdf('Patient­Information', 'footer')
    assert textpdf.verify_text_pdf('Patient­Information', pdf, drawn)['identical']
