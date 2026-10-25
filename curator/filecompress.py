"""Single PDF, RTF, TIFF, BMP or JPEG files compressed on their own (/file_compress): no XML involved.

    <data>/files/<fid>/
        meta.json          original file name, format, size, upload time
        original.<fmt>     the uploaded file (pdf, rtf, tif, bmp or jpg)
        ...                the same option files as a shrink candidate (strip.pdf, compress.pdf, text.pdf, ...)

The options are made by the same code as the shrink screen; sizes are those of the files themselves. Files are
kept for a limited time only (CURATOR_FILE_KEEP_HOURS, default 24), then removed.
"""
import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
from datetime import datetime

from . import documents as D
from . import imageops
from .pdfops import PdfError
from .shrink import Candidates, Progress, _job_slots

KEEP_HOURS = float(os.environ.get('CURATOR_FILE_KEEP_HOURS', '24'))
FORMATS = {'pdf': 'PDF', 'rtf': 'RTF', **imageops.NAMES}


class FileError(Exception):
    pass


def detect_format(data):
    """'pdf', 'rtf', 'tif', 'bmp' or 'jpg' from the file's own bytes (not its name), else None."""
    head = data[:1024]
    if b'%PDF-' in head:
        return 'pdf'
    if head.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'{\\rtf'):
        return 'rtf'
    fmt = D.sniff(head[:64])
    if fmt in imageops.IMAGE_FORMATS:
        try:
            imageops.analyse(data, fmt)            # e.g. a text file that happens to start with "BM" is not a BMP
            return fmt
        except PdfError:
            return None
    return None


def safe_stem(name):
    """The file name without folder or extension, safe to offer as a download name."""
    stem = os.path.splitext(os.path.basename((name or '').replace('\\', '/')))[0]
    stem = re.sub(r'[\x00-\x1f"<>:/\\|?*]+', '_', stem).strip(' .')
    return stem[:150] or 'file'


class FileJobs(Candidates):
    def __init__(self, root):
        self.root = root

    def dir(self, fid, cid=''):
        if not re.fullmatch(r'[0-9a-f]{32}', fid or ''):
            raise ValueError('bad file id')
        return os.path.join(self.root, fid)

    def meta(self, fid):
        try:
            m = self._read_json(os.path.join(self.dir(fid), 'meta.json'))
        except ValueError:
            return None
        return m if m and os.path.exists(self.original_path(fid, m)) else None

    def original_path(self, fid, meta=None):
        meta = meta or self.meta(fid)
        return os.path.join(self.dir(fid), f"original.{meta['fmt']}")

    def create(self, name, data):
        fmt = detect_format(data)
        if fmt is None:
            raise FileError('Only PDF, RTF, TIFF, BMP and JPEG files can be compressed here (the file is checked by '
                            'its content, not its name).')
        self.cleanup()
        fid = secrets.token_hex(16)
        d = self.dir(fid)
        os.makedirs(d)
        with open(os.path.join(d, f'original.{fmt}'), 'wb') as f:
            f.write(data)
        self._write_json(os.path.join(d, 'meta.json'), {
            'name': os.path.basename((name or '').replace('\\', '/')) or f'file.{fmt}', 'fmt': fmt,
            'bytes': len(data), 'uploaded': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
        return fid

    def recent(self):
        """[(fid, meta)] newest first."""
        if not os.path.isdir(self.root):
            return []
        out = []
        for fid in os.listdir(self.root):
            m = self.meta(fid)
            if m:
                out.append((fid, m))
        return sorted(out, key=lambda r: r[1]['uploaded'], reverse=True)

    def delete(self, fid):
        shutil.rmtree(self.dir(fid), ignore_errors=True)

    def cleanup(self):
        """Remove files older than KEEP_HOURS (not while they are being worked on)."""
        if not os.path.isdir(self.root):
            return
        cutoff = time.time() - KEEP_HOURS * 3600
        for fid in os.listdir(self.root):
            try:
                d = self.dir(fid)
            except ValueError:
                continue
            if os.path.getmtime(d) < cutoff and not self._claimed(d):
                shutil.rmtree(d, ignore_errors=True)

    def compute(self, fid):
        meta = self.meta(fid)
        if meta is None:
            return 'gone'
        d = self.dir(fid)
        if not self._claim(d, retry_error=True):
            return 'busy' if self._claimed(d) else 'done'
        try:
            progress = Progress(d)
            progress('*', 'Waiting for another document or file to finish')
            with _job_slots:
                with open(self.original_path(fid, meta), 'rb') as f:
                    data = f.read()
                if meta['fmt'] == 'rtf':
                    self._compute_rtf(data, d, len, len(data), progress)
                elif meta['fmt'] in imageops.IMAGE_FORMATS:
                    self._compute_image(data, meta['fmt'], d, len, len(data), progress)
                else:
                    self._compute_pdf(data, d, len, len(data), progress)
            return 'done'
        except Exception as e:
            with open(os.path.join(d, 'error.txt'), 'w', encoding='utf-8') as f:
                f.write(f'{type(e).__name__}: {e}\n\n{traceback.format_exc()}')
        finally:
            try:
                os.unlink(os.path.join(d, '.claim'))
            except FileNotFoundError:
                pass

    def start(self, fid):
        threading.Thread(target=self.compute, args=(fid,), daemon=True).start()
