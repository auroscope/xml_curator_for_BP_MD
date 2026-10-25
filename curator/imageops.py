"""TIFF and BMP scans (single or multi-page) converted to PDF.

Early scanners stored each document as a TIFF (often several pages in one file) or a BMP, frequently without any
compression, so a single letter can take megabytes. Three conversions are offered:

    lossless_pdf   the same pixels: black-and-white pages as CCITT G4, others as PNG (Flate), colour pages whose
                   pixels are all grey as greyscale. Verified pixel by pixel against the image.
    photo_pdf      greyscale and colour pages as JPEG at up to PHOTO_PPI (black-and-white pages as in lossless_pdf),
                   for photos and shaded scans
    (black and white: pdfops.compress on the lossless PDF, the same as for a scanned PDF)

Every page keeps its size on paper: the image's own resolution is used, or, when the file gives none or an
implausible one, the resolution that fits its longer side to an A4 page.
"""
import io

from .pdfops import PdfError, _is_bitonal

IMAGE_FORMATS = ('tif', 'bmp')
NAMES = {'tif': 'TIFF', 'bmp': 'BMP'}
PHOTO_PPI = 200
PHOTO_QUALITY = 70
COLOUR_SHARE = 0.01          # a page is colour if more than this share of its pixels has visible colour
COLOUR_SATURATION = 40       # 0-255: weaker saturation than this counts as grey (paper tint, scanner noise)
A4_LONG_IN = 11.69


def _no_progress(msg, done=None, total=None):
    pass


def _open(data):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise PdfError(f'cannot read the image: {e}')
    return im


def _dpi(frame, info):
    """((x, y) dots per inch, guessed)."""
    d = info.get('dpi')
    try:
        x, y = float(d[0]), float(d[1])
    except (TypeError, ValueError, IndexError):
        x = y = 0
    if 100 <= x <= 1200 and 100 <= y <= 1200:
        return (round(x), round(y)), False
    guess = min(600, max(100, round(max(frame.size) / A4_LONG_IN)))
    return (guess, guess), True


def _normalise(frame):
    """The frame in a mode a PDF can hold losslessly ('1', 'L', 'P' or 'RGB'), and a note when that changed pixels."""
    from PIL import Image
    mode = frame.mode
    if mode in ('1', 'L', 'RGB'):
        return frame, None
    if mode == 'P':
        if 'transparency' in frame.info:
            return _normalise(frame.convert('RGBA'))
        return frame, None
    if mode in ('RGBA', 'LA', 'PA'):
        rgba = frame.convert('RGBA')
        if rgba.getchannel('A').getextrema()[0] == 255:
            # Scanners often save an alpha channel that is opaque everywhere: dropping it changes no pixel.
            return rgba.convert('RGB') if mode != 'LA' else frame.convert('L'), None
        bg = Image.new('RGB', rgba.size, 'white')
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg, 'transparent areas were filled with white'
    if mode in ('I;16', 'I;16B', 'I;16L', 'I', 'F'):
        return frame.point(lambda v: v / 256).convert('L') if mode.startswith('I;16') else frame.convert('L'), \
            'its 16-bit greys were reduced to 8-bit'
    if mode == 'CMYK':
        return frame.convert('RGB'), 'CMYK colours were converted to RGB'
    return frame.convert('RGB'), f'{mode} pixels were converted to RGB'


def _is_grey_rgb(rgb):
    from PIL import ImageChops
    r, g, b = rgb.split()
    return ImageChops.difference(r, g).getbbox() is None and ImageChops.difference(r, b).getbbox() is None


def _kind(frame):
    """'bw' (1-bit), 'bitonal' (greys, but black-and-white content), 'grey' or 'colour'."""
    if frame.mode == '1':
        return 'bw'
    if frame.mode in ('RGB', 'P'):
        small = frame.convert('RGB')
        small.thumbnail((400, 400))
        sat = small.convert('HSV').split()[1].histogram()
        if sum(sat[COLOUR_SATURATION:]) > COLOUR_SHARE * (sum(sat) or 1):
            return 'colour'
    return 'bitonal' if _is_bitonal(frame.convert('L')) else 'grey'


def frames(data):
    """[(frame, dpi, dpi_guessed, note)] for every page of the image."""
    im = _open(data)
    out = []
    for i in range(getattr(im, 'n_frames', 1)):
        im.seek(i)
        frame = im.copy()
        dpi, guessed = _dpi(frame, im.info)
        frame, note = _normalise(frame)
        out.append((frame, dpi, guessed, note))
    return out


def analyse(data, fmt):
    pages = []
    for i, (frame, dpi, guessed, note) in enumerate(frames(data), 1):
        pages.append({'page': i, 'mode': frame.mode, 'width': frame.width, 'height': frame.height, 'dpi': dpi[0],
                      'dpi_guessed': guessed, 'kind': _kind(frame), 'note': note})
    kinds = {p['kind'] for p in pages}
    return {'format': fmt, 'name': NAMES.get(fmt, fmt.upper()), 'bytes': len(data), 'pages': len(pages),
            'page_info': pages, 'all_bw': kinds == {'bw'}, 'colour': 'colour' in kinds,
            'dpi_guessed': any(p['dpi_guessed'] for p in pages),
            'notes': sorted({p['note'] for p in pages if p['note']})}


def _page_bytes(frame, dpi, lossless=True):
    """One page encoded for img2pdf: G4 TIFF for 1-bit, PNG (lossless) or JPEG (photo) for the rest."""
    from PIL import Image
    buf = io.BytesIO()
    if frame.mode == '1':
        frame.save(buf, 'TIFF', compression='group4', dpi=dpi)
        return buf.getvalue()
    if frame.mode == 'RGB' and _is_grey_rgb(frame):
        frame = frame.convert('L')                     # exact: every pixel already had R = G = B
    if lossless:
        frame.save(buf, 'PNG', optimize=True, dpi=dpi)
        return buf.getvalue()
    img = frame.convert('RGB' if _kind(frame) == 'colour' else 'L')
    if dpi[0] > PHOTO_PPI:
        f = PHOTO_PPI / dpi[0]
        img = img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))), Image.LANCZOS)
        dpi = (PHOTO_PPI, round(dpi[1] * f))
    img.save(buf, 'JPEG', quality=PHOTO_QUALITY, optimize=True, dpi=dpi)
    return buf.getvalue()


def _to_pdf(data, lossless, progress):
    import img2pdf
    pages = frames(data)
    encoded = []
    for i, (frame, dpi, _guessed, _note) in enumerate(pages):
        progress('Converting the pages', i, len(pages))
        encoded.append(_page_bytes(frame, dpi, lossless))
    progress('Assembling the PDF')
    try:
        return img2pdf.convert(encoded)
    except Exception as e:
        raise PdfError(f'could not build the PDF: {e}')


def lossless_pdf(data, progress=_no_progress):
    return _to_pdf(data, True, progress)


def photo_pdf(data, progress=_no_progress):
    return _to_pdf(data, False, progress)


def verify_lossless(data, pdf):
    """True when every page of the PDF holds exactly the image's pixels (as normalised by frames())."""
    import pikepdf
    from PIL import ImageChops
    from pikepdf import PdfImage
    pages = frames(data)
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        if len(doc.pages) != len(pages):
            return False
        for page, (frame, _dpi, _g, _n) in zip(doc.pages, pages):
            imgs = list(page.get_images().values()) if hasattr(page, 'get_images') else list(page.images.values())
            if len(imgs) != 1:
                return False
            got = PdfImage(imgs[0]).as_pil_image()
            want = frame.convert('L') if frame.mode in ('1', 'L') or (frame.mode == 'RGB' and _is_grey_rgb(frame)) \
                else frame.convert('RGB')
            got = got.convert(want.mode)
            if got.size != want.size or ImageChops.difference(got, want).getbbox() is not None:
                return False
    return True
