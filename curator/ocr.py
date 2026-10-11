"""OCR engines behind one interface.

    ocr_pdf(pdf, pages=None) -> {'engine': 'Tesseract 5.5.0', 'pages': [
        {'page': 1, 'width': pt, 'height': pt, 'text': layout text,
         'words': [{'text': ..., 'x': pt, 'y': pt, 'w': pt, 'h': pt}]}   # top-left origin, PDF points
    ]}

Tesseract is built in (system package). liteparse (an HTTP service, e.g. http://localhost:5004) is used instead
when CURATOR_OCR=liteparse, or with CURATOR_OCR=auto (the default) when its /health reports ok. The app only
calls liteparse's existing GET /health and POST /parse.
"""
import json
import os
import re
import shutil
import statistics
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from .pdfops import PdfError, _run, _Tmp

ENGINE = os.environ.get('CURATOR_OCR', 'auto').lower()                 # auto | tesseract | liteparse
LITEPARSE_URL = os.environ.get('CURATOR_LITEPARSE_URL', 'http://localhost:5004').rstrip('/')
OCR_LANG = os.environ.get('CURATOR_OCR_LANG', 'eng')
OCR_DPI = 300
PAGE_TIMEOUT = 180

_health_cache = {'t': 0, 'ok': False, 'version': ''}


class OcrError(Exception):
    pass


def tesseract_version():
    if not shutil.which('tesseract'):
        return None
    out = subprocess.run(['tesseract', '--version'], capture_output=True, text=True).stdout
    m = re.search(r'tesseract\s+([\d.]+)', out)
    return m.group(1) if m else '?'


def liteparse_health():
    """(ok, version), cached for 60 s."""
    if time.time() - _health_cache['t'] < 60:
        return _health_cache['ok'], _health_cache['version']
    ok, version = False, ''
    try:
        with urllib.request.urlopen(LITEPARSE_URL + '/health', timeout=2) as r:
            data = json.loads(r.read().decode())
            ok = data.get('status') == 'ok'
            version = data.get('liteparse_version', '')
    except (OSError, ValueError, urllib.error.URLError):
        pass
    _health_cache.update(t=time.time(), ok=ok, version=version)
    return ok, version


def choose_engine():
    """('liteparse' | 'tesseract', label). Raises OcrError if no engine is usable."""
    if ENGINE in ('auto', 'liteparse'):
        ok, version = liteparse_health()
        if ok:
            return 'liteparse', f'liteparse {version}'.strip()
        if ENGINE == 'liteparse':
            raise OcrError(f'liteparse is not reachable at {LITEPARSE_URL}')
    v = tesseract_version()
    if v:
        return 'tesseract', f'Tesseract {v}'
    raise OcrError('No OCR engine: install tesseract-ocr, or set CURATOR_LITEPARSE_URL to a liteparse service')


def engine_status():
    lp_ok, lp_version = liteparse_health() if ENGINE in ('auto', 'liteparse') else (False, '')
    return {'setting': ENGINE, 'tesseract': tesseract_version(), 'liteparse_url': LITEPARSE_URL,
            'liteparse_ok': lp_ok, 'liteparse_version': lp_version}


def ocr_pdf(pdf, pages=None, dpi=None, progress=None):
    """OCR the given 1-based pages (all if None). progress(msg, done, total) is told how far it has got."""
    progress = progress or (lambda *a: None)
    engine, label = choose_engine()
    if engine == 'liteparse':
        progress(f'Reading the text with {label}' + (f' ({len(pages)} pages)' if pages else ''))
        result = _liteparse(pdf, pages)
    else:
        result = _tesseract(pdf, pages, dpi or OCR_DPI, progress)
    result['engine'] = label
    return result


# ---- Tesseract -------------------------------------------------------------------------------------------

def _page_sizes(path):
    out = _run(['pdfinfo', '-f', '1', '-l', '9999', path]).decode('utf-8', 'replace')
    return {int(m.group(1)): (float(m.group(2)), float(m.group(3)))
            for m in re.finditer(r'^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+) pts', out, re.M)}


def _tesseract(pdf, pages, dpi, progress=lambda *a: None):
    with _Tmp() as t:
        src = t.write('in.pdf', pdf)
        sizes = _page_sizes(src)
        wanted = pages or sorted(sizes)
        out_pages = []
        for i, n in enumerate(wanted):
            progress('Reading the text (OCR)', i, len(wanted))
            png = t.path(f'p{n}.png')
            _run(['gs', '-q', '-dNOPAUSE', '-dBATCH', '-dSAFER', '-sDEVICE=pnggray', f'-r{dpi}',
                  f'-dFirstPage={n}', f'-dLastPage={n}', f'-sOutputFile={png}', src])
            try:
                p = subprocess.run(['tesseract', png, 'stdout', '-l', OCR_LANG, '--psm', '3', 'tsv'],
                                   capture_output=True, timeout=PAGE_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise OcrError(f'Tesseract took longer than {PAGE_TIMEOUT}s on page {n}')
            if p.returncode != 0:
                raise OcrError('Tesseract failed: ' + p.stderr.decode(errors='replace')[:300])
            w_pt, h_pt = sizes.get(n, (595.0, 842.0))
            words, text = _parse_tsv(p.stdout.decode('utf-8', errors='replace'), 72.0 / dpi)
            out_pages.append({'page': n, 'width': w_pt, 'height': h_pt, 'text': text, 'words': words})
        return {'pages': out_pages}


def _parse_tsv(tsv, scale):
    """Words (in points) and a layout-like text: words of a line joined with spaces, wide gaps kept as runs of
    spaces (so table columns survive), a blank line between paragraphs."""
    rows = []
    for line in tsv.splitlines()[1:]:
        cols = line.split('\t')
        if len(cols) < 12 or cols[0] != '5' or not cols[11].strip():
            continue
        block, par, ln = int(cols[2]), int(cols[3]), int(cols[4])
        left, top, width, height = (int(c) for c in cols[6:10])
        rows.append((block, par, ln, left, top, width, height, cols[11].strip()))
    words = [{'text': r[7], 'x': r[3] * scale, 'y': r[4] * scale, 'w': r[5] * scale, 'h': r[6] * scale} for r in rows]
    char_ws = [r[5] / len(r[7]) for r in rows if len(r[7]) >= 3]
    char_w = statistics.median(char_ws) if char_ws else 20
    lines, prev_par, cur, cur_key = [], None, [], None
    for r in rows:
        key = (r[0], r[1], r[2])
        if key != cur_key:
            if cur:
                lines.append(_join_line(cur, char_w))
            if prev_par is not None and (r[0], r[1]) != prev_par:
                lines.append('')
            cur, cur_key, prev_par = [], key, (r[0], r[1])
        cur.append(r)
    if cur:
        lines.append(_join_line(cur, char_w))
    return words, '\n'.join(lines)


def _join_line(words, char_w):
    words = sorted(words, key=lambda r: r[3])
    out = words[0][7]
    for prev, w in zip(words, words[1:]):
        gap = w[3] - (prev[3] + prev[5])
        out += ' ' if gap < 2.5 * char_w else ' ' * min(40, max(3, round(gap / char_w)))
        out += w[7]
    return out


# ---- liteparse -------------------------------------------------------------------------------------------

def _liteparse(pdf, pages):
    boundary = uuid.uuid4().hex
    fields = [('format', 'json')]
    if pages:
        fields.append(('target_pages', ','.join(str(p) for p in pages)))
    body = b''
    for k, v in fields:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="document.pdf"\r\n'
             f'Content-Type: application/pdf\r\n\r\n').encode() + pdf + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(LITEPARSE_URL + '/parse', data=body, method='POST',
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=290) as r:
            data = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise OcrError(f'liteparse returned {e.code}: {e.read()[:200].decode(errors="replace")}')
    except (OSError, ValueError) as e:
        raise OcrError(f'liteparse request failed: {e}')
    out = []
    for p in data.get('pages', []):
        words = [{'text': i['text'], 'x': i['x'], 'y': i['y'], 'w': i['width'], 'h': i['height']}
                 for i in p.get('text_items', []) if i.get('text', '').strip()]
        out.append({'page': p.get('page'), 'width': p.get('width'), 'height': p.get('height'),
                    'text': p.get('text', ''), 'words': words})
    return {'pages': out}
