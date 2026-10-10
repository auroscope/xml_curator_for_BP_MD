"""Small real PDFs for tests, generated on the fly (no patient data)."""
import io
import shutil
import subprocess
import tempfile

import pytest
from PIL import Image, ImageDraw, ImageFont

needs_gs = pytest.mark.skipif(not shutil.which('gs') or not shutil.which('pdftotext'),
                              reason='Ghostscript and poppler-utils are required')

_PS = """%!PS
/Helvetica findfont 12 scalefont setfont
72 760 moveto (Pathology report for testing only) show
72 740 moveto (Haemoglobin 140 g/L   Range 115-165) show
72 720 moveto (Platelets 250 x10^9/L  Range 150-400) show
/s 30000 string def 0 1 29999 { s exch rand 256 mod put } for
gsave 300 600 translate 200 150 scale 200 150 8 [200 0 0 -150 0 150] { s } image grestore
gsave 72 450 translate 0 0 moveto 400 0 lineto stroke grestore
showpage
72 760 moveto (Second page: comment Hb stable.) show
showpage
"""


def text_pdf():
    """Two pages of real text plus a noisy image and a line on page 1."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(['gs', '-q', '-dNOPAUSE', '-dBATCH', '-dSAFER', '-sDEVICE=pdfwrite', '-sPAPERSIZE=a4',
                        f'-sOutputFile={d}/t.pdf', '-'], input=_PS.encode(), check=True)
        with open(f'{d}/t.pdf', 'rb') as f:
            return f.read()


def scan_pdf(pages=2):
    """Image-only pages like a scanner produces: 8-bit greyscale at 200 dpi with dark 'text' strokes."""
    imgs = []
    for p in range(pages):
        im = Image.new('L', (1654, 2339), 255)
        dr = ImageDraw.Draw(im)
        for i in range(40):
            dr.rectangle([150, 200 + i * 50, 150 + 900 + (i * 37 + p * 11) % 500, 220 + i * 50], fill=20)
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, 'PDF', save_all=True, append_images=imgs[1:], resolution=200)
    return buf.getvalue()


needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='Tesseract is required')

LETTER = [
    'Dear Doctor,',
    '',
    'Thank you for referring this patient with recurrent',
    'headaches over the past three months. Examination',
    'was unremarkable and the findings were normal.',
    '',
    'Haemoglobin      140      115-165',
    'Platelets        250      150-400',
    '',
    'Kind regards,',
]


def text_scan_pdf():
    """A scanned letter with real words (rendered at 200 dpi), for OCR tests."""
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 32)
    im = Image.new('L', (1654, 2339), 255)
    dr = ImageDraw.Draw(im)
    y = 200
    for line in LETTER:
        dr.text((150, y), line, fill=0, font=font)
        y += 60
    buf = io.BytesIO()
    im.save(buf, 'PDF', resolution=200)
    return buf.getvalue()
