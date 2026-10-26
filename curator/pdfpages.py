"""Removing pages from a PDF: page facts, thumbnails, and the removal itself.

Removal keeps everything else in the PDF as it is (pikepdf deletes the pages; objects only they used are not
written again, so their bytes go). Bookmarks that pointed at a removed page are dropped. A PDF with a digital
signature can have pages removed, but the signature no longer verifies afterwards: info() reports it so the screen
can warn. Encrypted PDFs cannot be changed.
"""
import io
import os
import subprocess
import tempfile

from .pdfops import PdfError

THUMB_WIDTH = 220


class PageError(Exception):
    pass


def _open(pdf):
    import pikepdf
    try:
        doc = pikepdf.open(io.BytesIO(pdf))
    except pikepdf.PasswordError:
        raise PageError('This PDF is password-protected, so its pages cannot be changed.')
    except pikepdf.PdfError as e:
        raise PageError(f'This PDF cannot be read: {e}')
    if doc.is_encrypted:
        doc.close()
        raise PageError('This PDF is encrypted, so its pages cannot be changed.')
    return doc


def _streams(obj, seen, depth=0):
    """{(objgen, raw bytes)} of the streams a page draws (content, images, forms), each counted once per page."""
    import pikepdf
    if depth > 4 or not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
        return set()
    owned = set()
    for it in (obj if isinstance(obj, pikepdf.Array) else [obj]):
        if isinstance(it, pikepdf.Stream) and it.objgen != (0, 0) and it.objgen not in seen:
            seen.add(it.objgen)
            owned.add((it.objgen, len(it.read_raw_bytes())))
            res = it.get('/Resources')
            if res is not None:
                owned |= _streams(res.get('/XObject') or pikepdf.Dictionary(), seen, depth + 1)
        elif isinstance(it, pikepdf.Dictionary):
            for k in list(it.keys()):
                owned |= _streams(it[k], seen, depth + 1)
    return owned


def info(pdf):
    """{'pages': n, 'page_bytes': [bytes only that page uses, ...], 'signed': bool, 'bookmarks': bool}."""
    import pikepdf
    with _open(pdf) as doc:
        per_page = []
        users = {}
        for page in doc.pages:
            seen = set()
            owned = _streams(page.obj.get('/Contents') or pikepdf.Array(), seen)
            res = page.obj.get('/Resources')
            if res is not None:
                owned |= _streams(res.get('/XObject') or pikepdf.Dictionary(), seen)
            per_page.append(owned)
            for og, _n in owned:
                users[og] = users.get(og, 0) + 1
        page_bytes = [sum(n for og, n in owned if users[og] == 1) for owned in per_page]
        acro = doc.Root.get('/AcroForm')
        signed = bool(acro is not None and int(acro.get('/SigFlags', 0)) & 1)
        if not signed and acro is not None:
            signed = any(f.get('/FT') == '/Sig' for f in (acro.get('/Fields') or []) if isinstance(f, pikepdf.Dictionary))
        return {'pages': len(doc.pages), 'page_bytes': page_bytes, 'signed': signed,
                'bookmarks': '/Outlines' in doc.Root}


def remove_pages(pdf, numbers):
    """(the PDF without the given pages (1-based), how many pages it had). At least one page must remain."""
    import pikepdf
    with _open(pdf) as doc:
        n = len(doc.pages)
        drop = sorted({int(x) for x in numbers})
        if not drop:
            raise PageError('No pages were chosen.')
        if drop[0] < 1 or drop[-1] > n:
            raise PageError(f'This PDF has {n} page(s); there is no page {drop[-1] if drop[-1] > n else drop[0]}.')
        if len(drop) >= n:
            raise PageError('At least one page must remain. To remove the whole document, delete it instead.')
        gone = {doc.pages[i - 1].obj.objgen for i in drop}
        _drop_bookmarks(doc, gone)
        for i in reversed(drop):
            del doc.pages[i - 1]
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue(), n


def _drop_bookmarks(doc, gone):
    import pikepdf
    if '/Outlines' not in doc.Root:
        return
    try:
        with doc.open_outline() as outline:
            def keep(items):
                kept = []
                for it in items:
                    it.children[:] = keep(list(it.children))
                    dest = it.destination
                    page = dest[0] if isinstance(dest, pikepdf.Array) and len(dest) else None
                    if isinstance(page, pikepdf.Dictionary) and page.objgen in gone and not it.children:
                        continue
                    kept.append(it)
                return kept
            outline.root[:] = keep(list(outline.root))
    except Exception:
        pass                                   # a damaged outline is left as it is; viewers ignore bad entries


def thumbnail(pdf, page, width=THUMB_WIDTH):
    """PNG of one page (1-based), width pixels wide."""
    with tempfile.TemporaryDirectory(prefix='curator-') as t:
        src = os.path.join(t, 'in.pdf')
        with open(src, 'wb') as f:
            f.write(pdf)
        base = os.path.join(t, 'thumb')
        p = subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-png', '-singlefile',
                            '-scale-to-x', str(width), '-scale-to-y', '-1', src, base],
                           capture_output=True, timeout=60)
        if p.returncode != 0 or not os.path.exists(base + '.png'):
            raise PdfError(f'page {page} could not be drawn')
        with open(base + '.png', 'rb') as f:
            return f.read()
