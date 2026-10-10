import base64
import io
import re
import struct

import pytest
from PIL import Image

from conftest import PDF, bp_xml, md_xml
from curator import documents as D
from curator import rtfops as R
from curator.xmlstore import XmlDoc


def _noise_png(w, h):
    import random
    rnd = random.Random(1)
    im = Image.new('RGB', (w, h))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return buf.getvalue()


def _dib(w, h):
    """An uncompressed 24-bit DIB (BITMAPINFOHEADER + pixels), as Word stores \\dibitmap pictures."""
    row = (w * 3 + 3) // 4 * 4
    header = struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, row * h, 3780, 3780, 0, 0)
    return header + bytes([200, 30, 30] * w + [0] * (row - w * 3)) * h


def sample_rtf():
    banner = _noise_png(1200, 150).hex()
    logo = _dib(300, 300).hex()
    return ('{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}{\\*\\themedata ' + '0' * 4000 + '}'
            '{\\*\\rsidtbl \\rsid123\\rsid456}\n'
            '{\\*\\shppict{\\pict\\pngblip\\picw1200\\pich150\\picwgoal4536\\pichgoal567 ' + banner + '}}'
            '{\\nonshppict{\\pict\\wmetafile8\\picwgoal4536\\pichgoal567 0100090000}}\\par\n'
            'Dear Doctor, thank you for seeing this patient. \\par\n'
            '{\\pict\\dibitmap0\\wbmbitspixel24\\picw300\\pich300\\picwgoal1701\\pichgoal1701 ' + logo + '}\\par\n'
            'Kind regards {\\b Dr Test}\\par}').encode('latin-1')


def test_compress_pictures_keeps_text_and_displayed_size():
    rtf = sample_rtf()
    new, notes = R.compress_pictures(rtf)
    assert len(new) < len(rtf) * 0.3
    assert R.verify_text(rtf, new)['identical']
    pics = R.pictures(new)
    banner = next(p for p in pics if p.goal_w == 4536)
    assert (banner.goal_w, banner.goal_h) == (4536, 567)                 # still shown 8 x 1 cm
    assert banner.image().size[0] <= 480                                  # 8 cm at 150 dpi = 472 px
    logo = next(p for p in pics if p.goal_w == 1701)
    assert logo.kind in ('pngblip', 'jpegblip') and logo.image().size[0] <= 180   # 3 cm at 150 dpi = 177 px
    assert b'\\themedata' not in new and b'\\rsidtbl' not in new
    assert any('kept' in n for n in notes)                                # the WMF fallback is kept
    assert b'Dear Doctor' in new and b'{\\b Dr Test}' in new


def test_remove_pictures_removes_wrappers_and_keeps_text():
    rtf = sample_rtf()
    new, _ = R.remove_pictures(rtf)
    assert b'\\pict' not in new and b'\\shppict' not in new and b'\\nonshppict' not in new
    assert R.verify_text(rtf, new)['identical'] and len(new) < 1000


def test_bin_picture_data_and_braces_in_hex_are_handled():
    png = _noise_png(400, 60)
    rtf = (b'{\\rtf1 A {\\pict\\pngblip\\picwgoal2268\\pichgoal340\\bin' + str(len(png)).encode() + b' ' + png +
           b'} B\\par}')
    pics = R.pictures(rtf)
    assert len(pics) == 1 and pics[0].data == png
    new, _ = R.compress_pictures(rtf)
    assert R.verify_text(rtf, new)['identical'] and len(new) < len(rtf)


# ---- web ---------------------------------------------------------------------------------------------------

def _token(client):
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get('/').get_data(as_text=True)).group(1)


@pytest.fixture
def sync_client(client, monkeypatch):
    import app as app_module
    for name in ('start', 'start_precompute', 'start_all'):
        monkeypatch.setattr(app_module.cands, name, lambda *a: None)
    client.cands = app_module.cands
    return client


def _upload(c, data, name='r.xml'):
    tok = _token(c)
    r = c.post('/upload', data={'_csrf': tok, 'file': (io.BytesIO(data), name)}, content_type='multipart/form-data')
    return r.headers['Location'].split('/w/')[1].split('/')[0], tok


def test_bp_letter_rtf_compress_save_and_restore(sync_client):
    c = sync_client
    rtf = sample_rtf().decode('latin-1')
    data = bp_xml().replace(b'<CONTENT>{\\rtf1 Dear Dr}</CONTENT>', ('<CONTENT>' + rtf + '</CONTENT>').encode('iso-8859-1'))
    wid, tok = _upload(c, data)
    assert '🗜' in c.get(f'/w/{wid}/documents?key=bpcor-7').get_data(as_text=True)
    c.cands.compute(wid, 'bpcor-7', 0)
    page = c.get(f'/w/{wid}/doc/bpcor-7/part/0/shrink').get_data(as_text=True)
    assert 'Compress pictures' in page and 'Remove pictures' in page and 'Text identical to the original' in page
    cid = re.search(r'name="cid" value="([^"]+)"', page).group(1)
    c.post(f'/w/{wid}/doc/bpcor-7/part/0/shrink/save', data={'_csrf': tok, 'cid': cid, 'option': 'rtfpic',
                                                            'date': '2022-02-02', 'to': 'Dr C', 'subject': 'To specialist'})
    work = c.get(f'/w/{wid}/download').data
    doc = XmlDoc(work)                                                    # still well-formed
    new_rtf = D.part_bytes(doc, D.record_map(doc)['bpcor-7'].parts[0])[1]
    assert len(new_rtf) < len(rtf) * 0.3 and R.verify_text(rtf.encode('latin-1'), new_rtf)['identical']
    assert len(work) < len(data)
    c.post(f'/w/{wid}/doc/bpcor-7/revert', data={'_csrf': tok})
    assert c.get(f'/w/{wid}/download').data == data


def test_md_rtf_document_in_bulk(sync_client):
    c = sync_client
    rtf = sample_rtf()
    data = md_xml().replace(base64.b64encode(b'{\\rtf1\\ansi Hello | A | B |\\par World}'), base64.b64encode(rtf), 1)
    wid, tok = _upload(c, data)
    c.cands.compute(wid, 'md-4', 1)
    p = c.get(f'/w/{wid}/bulk/progress').get_json()
    assert p['rtfpic']['count'] == 1 and p['rtfpic']['saving'] > 0
    c.post(f'/w/{wid}/bulk/shrink', data={'_csrf': tok, 'option': 'rtfpic'})
    doc = XmlDoc(c.get(f'/w/{wid}/download').data)
    part = D.record_map(doc)['md-4'].parts[1]
    assert R.verify_text(rtf, D.part_bytes(doc, part)[1])['identical']
    assert b'\n' not in doc.inner_raw(part.payload)                     # MedicalDirector single-line base64 kept
    assert c.get(f'/w/{wid}/changes?format=json').get_json()[-1]['option'] == 'rtfpic'
