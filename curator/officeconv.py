"""RTF -> HTML (pictures embedded) or PDF with LibreOffice, for viewing. Results are cached by content hash.

LibreOffice allows one conversion at a time per profile, so conversions are serialised with a file lock and use a
profile directory of their own (not the user's).
"""
import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile

TIMEOUT = 120
FILTERS = {'html': 'html:XHTML Writer File:UTF8', 'pdf': 'pdf'}


def available():
    return shutil.which('soffice') is not None


class OfficeError(Exception):
    pass


class Converter:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def convert(self, data, fmt, ext='rtf'):
        if fmt not in FILTERS:
            raise ValueError(fmt)
        if not available():
            raise OfficeError('LibreOffice is not installed')
        key = hashlib.sha1(data).hexdigest()
        out_path = os.path.join(self.cache_dir, f'{key}.{fmt}')
        if os.path.exists(out_path):
            with open(out_path, 'rb') as f:
                return f.read()
        with open(os.path.join(self.cache_dir, '.lock'), 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if os.path.exists(out_path):                  # done by another worker while we waited
                    with open(out_path, 'rb') as f:
                        return f.read()
                with tempfile.TemporaryDirectory(prefix='curator-office-') as tmp:
                    src = os.path.join(tmp, f'doc.{ext}')
                    with open(src, 'wb') as f:
                        f.write(data)
                    profile = 'file://' + os.path.join(self.cache_dir, 'profile')
                    try:
                        subprocess.run(['soffice', '--headless', '--norestore', f'-env:UserInstallation={profile}',
                                        '--convert-to', FILTERS[fmt], '--outdir', tmp, src],
                                       capture_output=True, timeout=TIMEOUT, start_new_session=True)
                    except subprocess.TimeoutExpired:
                        raise OfficeError(f'LibreOffice took longer than {TIMEOUT}s')
                    produced = os.path.join(tmp, f'doc.{fmt}')
                    if not os.path.exists(produced):
                        raise OfficeError('LibreOffice could not convert this document')
                    with open(produced, 'rb') as f:
                        result = f.read()
                tmp_out = out_path + '.tmp'
                with open(tmp_out, 'wb') as f:
                    f.write(result)
                os.replace(tmp_out, out_path)
                return result
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
