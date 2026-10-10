"""Text-only PDFs and the hidden OCR text layer.

The editable text is what gets printed: every line break in it is kept, so what the doctor sees and edits is
exactly what the PDF says. The reflow (joining the lines of a paragraph) happens once, when the initial text is
made from the OCR/text-layer output. Lines with runs of 3+ spaces are treated as table rows and set in a
monospaced font so their columns stay aligned.
"""
import collections
import html
import io
import os
import re

from .pdfops import page_texts, words, _Tmp

FONT_DIRS = [os.environ.get('CURATOR_FONT_DIR', ''), '/usr/share/fonts/truetype/dejavu',
             '/usr/share/fonts/dejavu', '/usr/share/fonts/TTF']
TABLE_LINE = re.compile(r'\S {3,}\S|\t')

# Word processors store bullets as private-use codes of symbol fonts (Wingdings/Symbol); no text font has
# glyphs for them, so they would print as nothing. They are shown and printed as ordinary bullets.
SYMBOLS = {'\uf0fc': '\u2713', '\uffed': '\u25a0', '\uf0e0': '\u2192', '\uf0e8': '\u2192'}
LIST_START = re.compile(r'^\s*([-*•●▪]|\d{1,2}[.)]|[a-z][.)])\s')


# ---- initial text --------------------------------------------------------------------------------------

def _dedent(lines):
    ind = [len(l) - len(l.lstrip(' ')) for l in lines if l.strip()]
    cut = min(ind) if ind else 0
    return [l[cut:] if len(l) >= cut else l.strip() for l in lines]


def reflow(layout_text):
    """Layout text (one page) to editable text: prose lines of a paragraph joined, table rows, short lines
    (addresses, headings) and list items kept on their own lines, paragraphs separated by one blank line."""
    lines = [l.rstrip().replace('\t', '    ') for l in layout_text.replace('\r', '').split('\n')]
    lines = _dedent(lines)
    paras, cur = [], []
    for l in lines + ['']:
        if l.strip():
            cur.append(l)
        elif cur:
            paras.append(cur)
            cur = []
    out = []
    for para in paras:
        prose = [l.strip() for l in para if not TABLE_LINE.search(l.strip())]
        longest = max((len(l) for l in prose), default=0)
        result = []
        for raw in para:
            line = raw.strip()
            if TABLE_LINE.search(line):
                result.append(raw.rstrip())            # keep a table row's spacing (and indentation)
                continue
            if result and not TABLE_LINE.search(result[-1]):
                prev = result[-1]
                joinable = (longest >= 45 and len(prev) >= 0.6 * longest and not prev.endswith(':')
                            and not LIST_START.match(line))
                if joinable:
                    result[-1] = prev + ('' if prev.endswith('-') and line[:1].islower() else ' ') + line
                    continue
            result.append(line)
        out.append('\n'.join(result))
    return '\n\n'.join(out)


def initial_text(page_layout_texts):
    pages = [reflow(clean_text(t)) for t in page_layout_texts]
    return '\n\n'.join(p for p in pages if p.strip()).strip() + '\n'


def text_layer_pages(pdf):
    """Layout text of each page from the PDF's own text layer."""
    with _Tmp() as t:
        return page_texts(t.write('in.pdf', pdf), layout=True)


# ---- building the PDF ----------------------------------------------------------------------------------

_fonts_registered = {}


def _fonts():
    if _fonts_registered:
        return _fonts_registered
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    names = {'Sans': 'DejaVuSans.ttf', 'Sans-Bold': 'DejaVuSans-Bold.ttf', 'Mono': 'DejaVuSansMono.ttf'}
    for key, fname in names.items():
        for d in FONT_DIRS:
            path = os.path.join(d, fname) if d else ''
            if path and os.path.exists(path):
                pdfmetrics.registerFont(TTFont(f'Curator{key}', path))
                _fonts_registered[key] = f'Curator{key}'
                break
    _fonts_registered.setdefault('Sans', 'Helvetica')          # Latin-1 only, but always available
    _fonts_registered.setdefault('Sans-Bold', 'Helvetica-Bold')
    _fonts_registered.setdefault('Mono', 'Courier')
    return _fonts_registered


def clean_text(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    # Invisible formatting characters (soft hyphen, zero-width space/joiners, BOM) print as nothing: drop them so
    # the text box holds exactly what is printed
    text = re.sub(r'[\u00ad\u200b-\u200d\u2060\ufeff]', '', text)
    return re.sub(r'[\uf000-\uf0ff\uffed]', lambda m: SYMBOLS.get(m.group(0), '\u2022'), text)


def _western(text):
    try:
        text.encode('cp1252')
        return True
    except UnicodeEncodeError:
        return False


def build_text_pdf(text, footer, title=''):
    """(pdf bytes, extra_strings) — extra_strings are the footer/page strings drawn on each page, so the
    verification can account for them."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit
    from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer

    text = clean_text(text)
    # Text that fits the standard Western character set uses the PDF base fonts (not embedded: a few KB);
    # anything else uses embedded DejaVu, which covers most scripts and symbols.
    f = {'Sans': 'Helvetica', 'Sans-Bold': 'Helvetica-Bold', 'Mono': 'Courier'} \
        if _western(text + footer) else _fonts()
    body = ParagraphStyle('body', fontName=f['Sans'], fontSize=10.5, leading=14.6, spaceAfter=7.5)
    mono = ParagraphStyle('mono', fontName=f['Mono'], fontSize=8.6, leading=11.4, spaceAfter=7.5)
    margin = 20 * mm
    width = A4[0] - 2 * margin

    story = []
    for block in re.split(r'\n\s*\n', text.strip('\n')):
        run, kind = [], None
        for line in block.split('\n'):
            k = 'table' if TABLE_LINE.search(line.strip()) else 'prose'
            if run and k != kind:
                story.append(_flow(run, kind, body, mono, width, Paragraph, Preformatted))
                run = []
            run.append(line)
            kind = k
        if run:
            story.append(_flow(run, kind, body, mono, width, Paragraph, Preformatted))
    if not story:
        story.append(Spacer(1, 1))

    drawn = []
    footer_lines = simpleSplit(footer, f['Sans'], 7.2, width - 22 * mm)

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(f['Sans'], 7.2)
        canvas.setFillColor(HexColor('#5b6670'))
        y = 12 * mm + (len(footer_lines) - 1) * 9
        canvas.setStrokeColor(HexColor('#c9d1d9'))
        canvas.line(margin, y + 11, A4[0] - margin, y + 11)
        for fl in footer_lines:
            canvas.drawString(margin, y, fl)
            y -= 9
        label = f'Page {doc.page}'
        canvas.drawRightString(A4[0] - margin, 12 * mm, label)
        drawn.append(list(footer_lines) + [label])
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=18 * mm,
                            bottomMargin=22 * mm + 9 * len(footer_lines), title=title or 'Text transcription',
                            subject=footer, creator='xml_curator_for_BP_MD', author='')
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue(), {'pages': drawn, 'strings': [x for page in drawn for x in page], 'fonts': f}


def _flow(lines, kind, body, mono, width, Paragraph, Preformatted):
    if kind == 'table':
        # Shrink the monospaced font (down to 6.2 pt) so the block's longest row fits; wrap only beyond that.
        from reportlab.lib.styles import ParagraphStyle
        longest = max(len(l.rstrip()) for l in lines)
        size = max(6.2, min(mono.fontSize, width / (0.602 * max(longest, 1))))
        style = ParagraphStyle('mono-fit', parent=mono, fontSize=size, leading=size * 1.32)
        return Preformatted('\n'.join(l.rstrip() for l in lines), style,
                            maxLineLength=int(width / (0.602 * size)), newLineChars='')
    return Paragraph('<br/>'.join(html.escape(l.strip(), quote=False) for l in lines), body)


def unsupported_chars(text, fonts):
    """Characters the chosen fonts have no glyph for (they would print as blanks or boxes)."""
    if fonts.get('Sans') == 'Helvetica':
        return sorted({c for c in text if not _western(c) and not c.isspace()})
    from reportlab.pdfbase import pdfmetrics
    missing = set()
    for key in ('Sans', 'Mono'):
        face = getattr(pdfmetrics.getFont(fonts[key]), 'face', None)
        cmap = getattr(face, 'charToGlyph', None)
        if cmap:
            missing |= {c for c in text if not c.isspace() and ord(c) not in cmap}
    return sorted(missing)


def _nows(s):
    return re.sub(r'\s+', '', s)


def verify_text_pdf(text, pdf, drawn):
    """The PDF must contain exactly the characters of the text, in order (spacing and line breaks aside),
    plus the footer/page strings, and every character must have a glyph.

    Two artefacts of text extraction are allowed for: a long token wrapped at '/' or '.' comes out as two
    words, and a '-' at the end of a line is dropped when pdftotext rejoins hyphenated words."""
    text = clean_text(text)
    with _Tmp() as t:
        pages = page_texts(t.write('t.pdf', pdf), layout=True)   # layout keeps table rows together
    extracted = ''.join(pages)
    got = ''
    for i, page in enumerate(pages):
        g = _nows(page)
        # The footer and page label are at the bottom of each page: remove their last occurrence there, so the
        # document's own "Page 2 of 3" text is never mistaken for them.
        for s in (drawn['pages'][i] if i < len(drawn['pages']) else []):
            k = g.rfind(_nows(s))
            if k >= 0:
                g = g[:k] + g[k + len(_nows(s)):]
        got += g
    exp = _nows(text)
    same = exp.replace('-', '') == got.replace('-', '') and got.count('-') <= exp.count('-')
    unsupported = unsupported_chars(text, drawn['fonts'])
    out = {'identical': same and not unsupported, 'words': len(words(text)), 'unsupported': unsupported,
           'missing': [], 'added': [], 'missing_count': 0, 'added_count': 0}
    if not same:
        ew = collections.Counter(words(text))
        gw = collections.Counter(words(extracted))
        for s in drawn['strings']:
            gw.subtract(words(s))
        out['missing'] = list((ew - gw).elements())[:20]
        out['added'] = [w for w in (gw - ew).elements()][:20]
        out['missing_count'], out['added_count'] = len(out['missing']), len(out['added'])
    return out


# ---- hidden text layer for compressed scans --------------------------------------------------------------

def add_hidden_text(pdf, ocr_pages):
    """Overlay invisible (render mode 3) OCR text on each page so the compressed scan stays searchable.
    Uses the standard Helvetica font, which is not embedded, so the layer costs only a few KB per page."""
    import pikepdf
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas as rl_canvas

    base = pikepdf.open(io.BytesIO(pdf))
    with base:
        by_page = {p['page']: p for p in ocr_pages}
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pageCompression=1)
        sizes = []
        for i, page in enumerate(base.pages, start=1):
            mb = [float(v) for v in page.mediabox]
            W, H = mb[2] - mb[0], mb[3] - mb[1]
            sizes.append((W, H))
            c.setPageSize((W, H))
            op = by_page.get(i)
            if op and op.get('width') and op.get('height'):
                sx, sy = W / float(op['width']), H / float(op['height'])
                t = c.beginText()
                t.setTextRenderMode(3)
                for w in op['words']:
                    s = w['text'].encode('latin-1', 'replace').decode('latin-1')
                    size = max(1.0, w['h'] * sy * 0.85)
                    natural = stringWidth(s, 'Helvetica', size)
                    if natural <= 0:
                        continue
                    t.setFont('Helvetica', size)
                    t.setHorizScale(max(10.0, min(400.0, 100.0 * w['w'] * sx / natural)))
                    t.setTextOrigin(w['x'] * sx, H - (w['y'] + w['h']) * sy + 0.2 * w['h'] * sy)
                    t.textOut(s)
                c.drawText(t)
            c.showPage()
        c.save()
        overlay = pikepdf.open(io.BytesIO(buf.getvalue()))
        with overlay:
            for page, opage in zip(base.pages, overlay.pages):
                page.add_overlay(opage)
            out = io.BytesIO()
            base.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
            return out.getvalue()
