"""PDF analysis and the shrink methods. Uses Ghostscript and poppler-utils (system packages), pikepdf and img2pdf.

    strip      The image-drawing instructions and the images are removed from the page content with pikepdf.
               Text objects and fonts are not touched, so the text is the same text, not a copy of it.
    compress   Scans: each page rendered by Ghostscript to black-and-white CCITT G4 and wrapped as PDF without
               re-encoding. PDFs with a text layer: only the images are re-encoded, without colour (black-and-white
               content as 1-bit images up to 300 dpi, photos and shaded images as 150 dpi greyscale); text untouched.

Ghostscript is deliberately not used to rewrite PDFs that have a text layer: its pdfwrite drops the Unicode
mapping of ligature glyphs (e.g. Calibri "ti"/"ft"), so "examination" would extract as "examina on".
"""
import base64
import collections
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import zlib

logging.getLogger('fontTools').setLevel(logging.ERROR)   # it reports tables a PDF does not need

TIMEOUT = int(os.environ.get('CURATOR_PDF_TIMEOUT', '240'))
TEXT_PAGE_CHARS = 40          # a page with fewer non-space characters than this has no usable text layer
IMAGE_HEAVY_FRACTION = 0.25   # a text page whose images cover more than this may hold content as an image


class PdfError(Exception):
    pass


def tools_available():
    return {t: shutil.which(t) is not None for t in ('gs', 'pdftotext', 'pdfinfo', 'pdfimages')}


def _run(cmd, cwd=None, timeout=TIMEOUT):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise PdfError(f'{cmd[0]} is not installed')
    except subprocess.TimeoutExpired:
        raise PdfError(f'{cmd[0]} took longer than {timeout}s')
    if p.returncode != 0:
        raise PdfError(f'{cmd[0]} failed: {p.stderr.decode(errors="replace").strip()[:300]}')
    return p.stdout


class _Tmp:
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix='curator-')
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def path(self, name):
        return os.path.join(self.dir, name)


# ---- analysis --------------------------------------------------------------------------------------------

def page_texts(pdf_path, layout=False):
    out = _run(['pdftotext'] + (['-layout'] if layout else []) + ['-enc', 'UTF-8', pdf_path, '-'])
    pages = out.decode('utf-8', errors='replace').split('\f')
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def words(text):
    return re.findall(r'\S+', text)


def analyse(pdf):
    """Pages, per-page text and image coverage, and an overall kind: 'scan', 'text' or 'mixed'."""
    with _Tmp() as t:
        path = t.write('in.pdf', pdf)
        info = _run(['pdfinfo', path]).decode('utf-8', errors='replace')
        m = re.search(r'^Pages:\s+(\d+)', info, re.M)
        n_pages = int(m.group(1)) if m else 0
        sizes = {}
        for line in _run(['pdfinfo', '-f', '1', '-l', str(max(n_pages, 1)), path]).decode('utf-8', 'replace').splitlines():
            pm = re.match(r'^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+) pts', line)
            if pm:
                sizes[int(pm.group(1))] = (float(pm.group(2)), float(pm.group(3)))
        texts = page_texts(path)
        img_area = collections.defaultdict(float)
        img_count = collections.Counter()
        obj_ppi = {}
        max_ppi = 0
        for line in _run(['pdfimages', '-list', path]).decode('utf-8', 'replace').splitlines()[2:]:
            cols = line.split()
            if len(cols) < 14 or not cols[0].isdigit():
                continue
            page, w, h, xppi, yppi = int(cols[0]), int(cols[3]), int(cols[4]), cols[12], cols[13]
            if cols[2] in ('smask', 'mask', 'stencil'):
                continue
            try:
                xp, yp = float(xppi), float(yppi)
            except ValueError:
                continue
            img_count[page] += 1
            if xp > 0 and yp > 0:
                img_area[page] += (w / xp * 72) * (h / yp * 72)
                max_ppi = max(max_ppi, min(xp, yp))
                obj = cols[10]
                obj_ppi[obj] = max(obj_ppi.get(obj, 0), round(max(xp, yp)))
    pages = []
    for i in range(1, n_pages + 1):
        text = texts[i - 1] if i - 1 < len(texts) else ''
        chars = len(re.sub(r'\s', '', text))
        pw, ph = sizes.get(i, (595.0, 842.0))
        coverage = min(img_area[i] / (pw * ph), 1.0) if pw and ph else 0.0
        kind = 'text' if chars >= TEXT_PAGE_CHARS else ('image' if img_count[i] else 'blank')
        pages.append({'page': i, 'chars': chars, 'images': img_count[i], 'image_coverage': round(coverage, 2),
                      'kind': kind})
    kinds = {p['kind'] for p in pages if p['kind'] != 'blank'}
    kind = 'scan' if kinds == {'image'} else 'text' if kinds <= {'text'} else 'mixed'
    producer = re.search(r'^Producer:\s+(.*)$', info, re.M)
    return {'pages': n_pages, 'kind': kind, 'page_info': pages, 'max_image_ppi': round(max_ppi), 'image_ppi': obj_ppi,
            'producer': producer.group(1).strip() if producer else '', 'bytes': len(pdf)}


def strip_warnings(analysis):
    """Pages whose content may exist only as an image, which 'strip images' would lose."""
    out = []
    for p in analysis['page_info']:
        if p['kind'] == 'image':
            out.append(f"Page {p['page']} has no text layer (image only) and would be blank.")
        elif p['kind'] == 'text' and p['image_coverage'] > IMAGE_HEAVY_FRACTION:
            out.append(f"Page {p['page']}: images cover about {round(p['image_coverage'] * 100)}% of the page "
                       f"and may contain content (e.g. a table or a scanned section).")
    return out


# ---- methods ---------------------------------------------------------------------------------------------

def _save(pdf_obj):
    import pikepdf
    out = io.BytesIO()
    pdf_obj.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return out.getvalue()


def strip_images(pdf):
    """Remove every image (XObject and inline) from pages and form XObjects; leave all else untouched."""
    import pikepdf
    try:
        doc = pikepdf.open(io.BytesIO(pdf))
    except pikepdf.PdfError as e:
        raise PdfError(f'cannot open PDF: {e}')
    with doc:
        visited = set()

        def clean(content_owner, resources):
            xobjs = resources.get('/XObject') if resources is not None else None
            patterns = resources.get('/Pattern') if resources is not None else None
            if patterns is not None:                   # tiling patterns have their own content (e.g. letterheads)
                for name in list(patterns.keys()):
                    pat = patterns[name]
                    if isinstance(pat, pikepdf.Stream) and int(pat.get('/PatternType', 0)) == 1 \
                            and pat.objgen not in visited:
                        visited.add(pat.objgen)
                        pat.write(clean(pat, pat.get('/Resources')))
            images = set()
            if xobjs is not None:
                for name in list(xobjs.keys()):
                    x = xobjs[name]
                    sub_type = x.get('/Subtype')
                    if sub_type == '/Image':
                        images.add(name)
                    elif sub_type == '/Form' and x.objgen not in visited:
                        visited.add(x.objgen)
                        x.write(clean(x, x.get('/Resources')))
            kept = []
            for ins in pikepdf.parse_content_stream(content_owner):
                if isinstance(ins, pikepdf.ContentStreamInlineImage):
                    continue
                if str(ins.operator) == 'Do' and ins.operands and str(ins.operands[0]) in images:
                    continue
                kept.append(ins)
            if xobjs is not None:
                for name in images:
                    del xobjs[name]
            return pikepdf.unparse_content_stream(kept)

        for page in doc.pages:
            data = clean(page.obj, page.obj.get('/Resources') or page.resources)
            page.obj.Contents = doc.make_stream(data)
        return subset_fonts_checked(_save(doc))


# ---- font subsetting -------------------------------------------------------------------------------------

def _font_file(font):
    """(kind, FontFile2 stream) for fonts whose glyphs we can work out from the text, else (None, None).
    'cid': Type0 / Identity-H / CIDFontType2 with identity CIDToGIDMap (2-byte codes are glyph ids).
    'simple': single-byte TrueType font (codes are mapped to glyphs through the font's cmap)."""
    import pikepdf
    try:
        sub_type = font.get('/Subtype')
        if sub_type == '/Type0':
            if str(font.get('/Encoding')) not in ('/Identity-H', '/Identity-V'):
                return None, None
            desc = font.DescendantFonts[0]
            cmap = desc.get('/CIDToGIDMap')
            if desc.get('/Subtype') != '/CIDFontType2' or (cmap is not None and str(cmap) != '/Identity'):
                return None, None
            ff = desc.FontDescriptor.get('/FontFile2')
            return ('cid', ff) if isinstance(ff, pikepdf.Stream) else (None, None)
        if sub_type == '/TrueType':
            fd = font.get('/FontDescriptor')
            ff = fd.get('/FontFile2') if fd is not None else None
            return ('simple', ff) if isinstance(ff, pikepdf.Stream) else (None, None)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        pass
    return None, None


def _simple_glyphs(font, codes, tt):
    """Glyph names a single-byte TrueType font may draw for these codes. A superset is fine: every way a
    viewer could map a code (Differences, the base encoding through the Unicode cmap, the symbol cmap and
    the Mac cmap) is included."""
    import pikepdf
    from fontTools import agl
    enc = font.get('/Encoding')
    base, diffs = 'cp1252', {}
    if isinstance(enc, pikepdf.Name):
        base = 'mac_roman' if str(enc) == '/MacRomanEncoding' else 'cp1252'
    elif isinstance(enc, pikepdf.Dictionary):
        if str(enc.get('/BaseEncoding', '')) == '/MacRomanEncoding':
            base = 'mac_roman'
        code = 0
        for item in enc.get('/Differences') or []:
            if isinstance(item, int) or (hasattr(item, 'is_integer') and not isinstance(item, pikepdf.Name)):
                code = int(item)
            else:
                diffs[code] = str(item)[1:]
                code += 1
    order = set(tt.getGlyphOrder())
    cmaps = {(t.platformID, t.platEncID): t.cmap for t in tt['cmap'].tables} if 'cmap' in tt else {}
    out = set()
    for c in codes:
        unicodes = set()
        name = diffs.get(c)
        if name:
            if name in order:
                out.add(name)
            u = agl.toUnicode(name)
            if u:
                unicodes.add(u)
        ch = bytes([c]).decode(base, errors='ignore')
        if ch:
            unicodes.add(ch)
        for u in unicodes:
            for key in ((3, 1), (0, 3), (0, 4), (3, 10)):
                g = cmaps.get(key, {}).get(ord(u[0]))
                if g:
                    out.add(g)
        for code in (c, 0xF000 + c, 0xF100 + c, 0xF200 + c):
            g = cmaps.get((3, 0), {}).get(code)
            if g:
                out.add(g)
        g = cmaps.get((1, 0), {}).get(c)
        if g:
            out.add(g)
    return out


def subset_fonts(pdf):
    """Subset embedded TrueType fonts to the glyphs the text uses, keeping glyph ids so content streams,
    widths and ToUnicode maps stay valid. A font file used by any font we cannot analyse is left alone.
    Returns the same object when nothing was subset. Callers verify the result visually."""
    import pikepdf
    from fontTools import subset, ttLib
    doc = pikepdf.open(io.BytesIO(pdf))
    with doc:
        files = {}                                       # ff objgen -> stream
        cid_gids = collections.defaultdict(set)          # ff objgen -> glyph ids
        simple_codes = collections.defaultdict(dict)     # ff objgen -> {font objgen: (font, codes)}
        visited = set()

        def scan(owner, resources):
            if resources is None:
                return
            fonts = resources.get('/Font')
            xobjs = resources.get('/XObject')
            patterns = resources.get('/Pattern')
            current = (None, None, None)
            for ins in pikepdf.parse_content_stream(owner):
                if isinstance(ins, pikepdf.ContentStreamInlineImage):
                    continue
                op = str(ins.operator)
                if op == 'Tf' and fonts is not None and ins.operands:
                    f = fonts.get(str(ins.operands[0]))
                    kind, ff = _font_file(f) if f is not None else (None, None)
                    current = (kind, ff, f)
                    if ff is not None:
                        files[ff.objgen] = ff
                elif op in ('Tj', "'", '"', 'TJ') and current[0]:
                    kind, ff, f = current
                    args = ins.operands[-1:] if op != 'TJ' else list(ins.operands[0])
                    for a in args:
                        if not isinstance(a, pikepdf.String):
                            continue
                        b = bytes(a)
                        if kind == 'cid':
                            cid_gids[ff.objgen].update((b[i] << 8) | b[i + 1] for i in range(0, len(b) - 1, 2))
                        else:
                            entry = simple_codes[ff.objgen].setdefault(f.objgen, (f, set()))
                            entry[1].update(b)
                elif op == 'Do' and xobjs is not None and ins.operands:
                    x = xobjs.get(str(ins.operands[0]))
                    if x is not None and x.get('/Subtype') == '/Form' and x.objgen not in visited:
                        visited.add(x.objgen)
                        scan(x, x.get('/Resources') or resources)
            if patterns is not None:
                for name in list(patterns.keys()):
                    pat = patterns[name]
                    if isinstance(pat, pikepdf.Stream) and pat.objgen not in visited:
                        visited.add(pat.objgen)
                        scan(pat, pat.get('/Resources'))

        for page in doc.pages:
            scan(page.obj, page.obj.get('/Resources') or page.resources)
            for annot in page.obj.get('/Annots') or []:
                ap = annot.get('/AP')
                n = ap.get('/N') if ap is not None else None
                if isinstance(n, pikepdf.Stream) and n.objgen not in visited:
                    visited.add(n.objgen)
                    scan(n, n.get('/Resources'))

        # Font files also used by a font we cannot analyse (Type1, Type3, other CID set-ups) are not touched.
        blocked = set()
        for obj in doc.objects:
            if isinstance(obj, pikepdf.Dictionary) and obj.get('/Type') == '/Font' \
                    and obj.get('/Subtype') in ('/Type0', '/TrueType', '/Type1', '/MMType1', '/Type3'):
                kind, ff = _font_file(obj)
                if kind is None:
                    for desc in ([obj] + list(obj.get('/DescendantFonts') or [])):
                        fd = desc.get('/FontDescriptor')
                        f2 = fd.get('/FontFile2') if fd is not None else None
                        if isinstance(f2, pikepdf.Stream):
                            blocked.add(f2.objgen)

        changed = False
        for og, ff in files.items():
            if og in blocked:
                continue
            try:
                tt = ttLib.TTFont(io.BytesIO(ff.read_bytes()))
                order = tt.getGlyphOrder()
                gids = {g for g in cid_gids[og] if g < len(order)} | {0}
                names = set()
                for f, codes in simple_codes[og].values():
                    names |= _simple_glyphs(f, codes, tt)
                opts = subset.Options()
                opts.retain_gids = True
                opts.notdef_outline = True
                opts.name_IDs = ['*']
                opts.name_languages = ['*']
                opts.glyph_names = True
                # Glyph substitution/positioning was applied when the PDF was made; viewers do not use these
                # tables, and keeping them would pull every alternate glyph back in. Hinting is kept.
                opts.layout_features = []
                opts.drop_tables += ['GSUB', 'GPOS', 'GDEF', 'JSTF', 'BASE', 'MATH', 'DSIG', 'LTSH', 'VDMX',
                                     'hdmx', 'PCLT', 'meta', 'kern']
                sub = subset.Subsetter(opts)
                sub.populate(gids=sorted(gids), glyphs=sorted(names))
                sub.subset(tt)
                out = io.BytesIO()
                tt.save(out)
                data = out.getvalue()
            except Exception:
                continue
            if len(zlib.compress(data, 9)) < len(ff.read_raw_bytes()) * 0.9:
                ff.write(data)
                ff.Length1 = len(data)
                changed = True
        return _save(doc) if changed else pdf


def render_pages(pdf, dpi=72):
    with _Tmp() as t:
        src = t.write('in.pdf', pdf)
        _run(['pdftoppm', '-r', str(dpi), '-gray', src, t.path('p')])
        return [open(os.path.join(t.dir, n), 'rb').read() for n in sorted(os.listdir(t.dir)) if n.startswith('p')]


def subset_fonts_checked(pdf):
    """subset_fonts, kept only if every page renders pixel-identical to the unsubset PDF."""
    try:
        smaller = subset_fonts(pdf)
    except Exception:
        return pdf
    if smaller is pdf:
        return pdf
    try:
        if render_pages(smaller) == render_pages(pdf):
            return smaller
    except PdfError:
        pass
    return pdf


def compress(pdf, analysis):
    if analysis['kind'] == 'scan':
        return _compress_scan(pdf, analysis)
    return _compress_images(pdf, analysis)


SCAN_THRESHOLD = int(os.environ.get('CURATOR_SCAN_THRESHOLD', '165'))   # 0-255; lighter greys become white


def _compress_scan(pdf, analysis):
    """Each page rendered in greyscale, thresholded to black and white (no dithering, so highlighter and
    paper tint drop out and strokes stay solid), and stored as CCITT G4 without re-encoding."""
    import img2pdf
    from PIL import Image
    dpi = min(300, max(150, analysis.get('max_image_ppi') or 300))   # never upscale the scan
    with _Tmp() as t:
        src = t.write('in.pdf', pdf)
        _run(['gs', '-q', '-dNOPAUSE', '-dBATCH', '-dSAFER', '-sDEVICE=pnggray', f'-r{dpi}',
              '-dTextAlphaBits=1', '-dGraphicsAlphaBits=1', f'-sOutputFile={t.path("p-%04d.png")}', src])
        pngs = sorted(n for n in os.listdir(t.dir) if n.startswith('p-') and n.endswith('.png'))
        if not pngs:
            raise PdfError('no pages were rendered')
        lut = [255 if v > SCAN_THRESHOLD else 0 for v in range(256)]
        tifs = []
        for n in pngs:
            with Image.open(os.path.join(t.dir, n)) as im:
                bw = im.convert('L').point(lut, mode='1')
                out = os.path.join(t.dir, n[:-4] + '.tif')
                bw.save(out, compression='group4', dpi=(dpi, dpi))
                tifs.append(out)
        return img2pdf.convert(tifs, rotation=img2pdf.Rotation.ifvalid)


TARGET_PPI = 150          # greyscale images (photos, shaded logos)
BITONAL_PPI = 300         # black-and-white images (text fragments, signatures, scanned pages): kept sharp
BITONAL_SHARE = 0.92      # an image is black-and-white content if this share of its pixels is near black or white


def _is_bitonal(grey):
    h = grey.histogram()
    total = sum(h) or 1
    return (sum(h[:72]) + sum(h[184:])) / total >= BITONAL_SHARE


def _compress_images(pdf, analysis, quality=60):
    """Re-encode the images without colour: black-and-white content as 1-bit images (up to 300 dpi), anything with
    real tones (photos, shaded logos) as greyscale JPEG at 150 dpi. Text, fonts and vector content are not touched."""
    import pikepdf
    from pikepdf import Name, PdfImage
    from PIL import Image
    ppi_by_obj = {int(k): v for k, v in (analysis.get('image_ppi') or {}).items() if str(k).isdigit()}
    lut = [255 if v > SCAN_THRESHOLD else 0 for v in range(256)]
    try:
        doc = pikepdf.open(io.BytesIO(pdf))
    except pikepdf.PdfError as e:
        raise PdfError(f'cannot open PDF: {e}')
    with doc:
        masks = set()
        for obj in doc.objects:
            if isinstance(obj, pikepdf.Stream) and obj.get('/Subtype') == '/Image':
                for key in ('/SMask', '/Mask'):
                    m = obj.get(key)
                    if isinstance(m, pikepdf.Stream):
                        masks.add(m.objgen)
        for obj in list(doc.objects):
            if not isinstance(obj, pikepdf.Stream) or obj.get('/Subtype') != '/Image' or obj.objgen in masks:
                continue
            if obj.get('/ImageMask') or int(obj.get('/BitsPerComponent', 8)) == 1:
                continue                               # already black and white
            try:
                im = PdfImage(obj).as_pil_image()
            except Exception:
                continue
            if im.mode in ('RGBA', 'LA', 'PA'):
                bg = Image.new('RGB', im.size, 'white')
                bg.paste(im.convert('RGBA'), mask=im.convert('RGBA').split()[-1])
                im = bg
            grey = im.convert('L')
            ppi = ppi_by_obj.get(obj.objgen[0], 0)
            bitonal = _is_bitonal(grey)
            target = BITONAL_PPI if bitonal else TARGET_PPI
            if ppi > target:
                f = target / ppi
                grey = grey.resize((max(1, round(grey.width * f)), max(1, round(grey.height * f))), Image.LANCZOS)
            if bitonal:
                bw = grey.point(lut, mode='1')
                data = zlib.compress(bw.tobytes(), 9)          # 1 bit per pixel, 1 = white, as DeviceGray expects
                flt, bpc = Name.FlateDecode, 1
            else:
                buf = io.BytesIO()
                grey.save(buf, 'JPEG', quality=quality, optimize=True)
                data, flt, bpc = buf.getvalue(), Name.DCTDecode, 8
            if len(data) >= len(obj.read_raw_bytes()):
                continue
            obj.write(data, filter=flt)
            obj.Width, obj.Height = grey.width, grey.height
            obj.ColorSpace = Name.DeviceGray
            obj.BitsPerComponent = bpc
            for key in ('/DecodeParms', '/Decode', '/SMaskInData', '/Intent'):
                if key in obj:
                    del obj[key]
            if isinstance(obj.get('/Mask'), pikepdf.Array):       # colour-key masks do not survive re-encoding
                del obj['/Mask']
        return subset_fonts_checked(_save(doc))


def verify_text(original, new):
    """Compare the words of two PDFs. Returns {'identical': bool, 'words': n, 'missing': [...], 'added': [...]}."""
    with _Tmp() as t:
        a = words(''.join(page_texts(t.write('a.pdf', original))))
        b = words(''.join(page_texts(t.write('b.pdf', new))))
    ca, cb = collections.Counter(a), collections.Counter(b)
    missing = list((ca - cb).elements())
    added = list((cb - ca).elements())
    return {'identical': not missing and not added, 'words': len(a), 'missing': missing[:20],
            'added': added[:20], 'missing_count': len(missing), 'added_count': len(added)}


# ---- XML payload -----------------------------------------------------------------------------------------

def encode_like(original_inner, data):
    """base64 of data, laid out like the original element content (single line, or wrapped at the same
    width with the same line ending, keeping a leading/trailing line break if the original had one)."""
    b64 = base64.b64encode(data)
    raw = original_inner
    body = raw.strip(b' \t\r\n')
    lead = raw[:len(raw) - len(raw.lstrip(b' \t\r\n'))]
    trail = raw[len(raw.rstrip(b' \t\r\n')):]
    eol = b'\r\n' if b'\r\n' in body else b'\n'
    first = body.find(b'\n')
    if first > 0:
        width = first - (1 if body[first - 1:first] == b'\r' else 0)
        b64 = eol.join(b64[i:i + width] for i in range(0, len(b64), width))
    return lead + b64 + trail


def _is_zip(original_payload):
    head = re.sub(rb'\s+', b'', original_payload[:200])[:8]
    try:
        return base64.b64decode(head[:8])[:4] == b'PK\x03\x04'
    except ValueError:
        return False


def zip_name(original_payload):
    """Name of the single file inside a BP ZIP payload."""
    raw = base64.b64decode(re.sub(rb'\s+', b'', original_payload))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
    return names[0] if names else 'document.pdf'


def build_payload(encoding, original_inner, pdf, xml_encoding='utf-8'):
    """New element content holding the new file, in the same encoding as the original payload. For 'rtf' (RTF stored
    as element text, e.g. BP outgoing letters) the bytes are the UTF-8 form of that text, as part_bytes returns it."""
    if encoding == 'rtf':
        text = pdf.decode('utf-8')
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return text.encode(xml_encoding, errors='xmlcharrefreplace')
    if encoding == 'zip-b64':
        if not _is_zip(original_inner):
            # Some BP exports store a document page as the plain file, without the ZIP wrapper: keep that form.
            return encode_like(original_inner, pdf)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(zip_name(original_inner), pdf)
        return encode_like(original_inner, buf.getvalue())
    if encoding == 'b64':
        return encode_like(original_inner, pdf)
    raise PdfError(f'cannot replace a {encoding} payload')
