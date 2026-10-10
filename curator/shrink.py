"""Shrink candidates: computed once per PDF payload, cached on disk, applied on Save.

    <state>/<wid>/shrink/<key>.p<part>.<hash>/
        analysis.json      page/text/image analysis of the current PDF
        strip.pdf          'Strip images' result (PDFs with a text layer)
        compress.pdf       'Compressed scan' result
        result.json        sizes, XML cost, verification and warnings per option; written last
        error.txt          set if computing failed
        .claim             present while a worker is computing it

<hash> is a hash of the payload exactly as stored in the working copy, so a candidate can never be applied to
a document that has changed since it was computed. Jobs run in background threads; a claim file stops two
gunicorn workers computing the same candidate.
"""
import hashlib
import json
import os
import shutil
import threading
import time
import traceback

from datetime import date

from . import documents as D
from . import ocr
from . import pdfops
from . import rtfops
from . import textpdf

PRECOMPUTE_MIN_BYTES = int(os.environ.get('CURATOR_PRECOMPUTE_MIN_KB', '500')) * 1024
ICON_MIN_BYTES = 100 * 1024
OPTIONS = ['strip', 'compress', 'text', 'rtfpic', 'rtfnopic']
RTF_OPTIONS = ('rtfpic', 'rtfnopic')
RTF_MIN_BYTES = 20 * 1024            # smaller RTFs are not worth offering
CLAIM_STALE_S = 30 * 60

_job_slots = threading.Semaphore(int(os.environ.get('CURATOR_JOBS', '1')))


def payload_hash(doc, part):
    key = ('phash', part.payload.start if part.payload is not None else -1)
    h = doc._cache.get(key)
    if h is None:
        h = hashlib.sha1(doc.inner_raw(part.payload)).hexdigest()[:16]
        doc._cache[key] = h
    return h


def cand_id(doc, rec, part):
    return f'{rec.key}.p{part.index}.{payload_hash(doc, part)}'


def _process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def has_pictures(doc, part):
    key = ('haspict', part.node.start, part.index)
    v = doc._cache.get(key)
    if v is None:
        try:
            v = b'\\pict' in D.part_bytes(doc, part)[1]
        except Exception:
            v = False
        doc._cache[key] = v
    return v


def shrinkable(part, doc=None):
    """PDFs, and RTF documents (files or letter text) that contain pictures."""
    if part.fmt == 'pdf' and part.encoding in ('b64', 'zip-b64'):
        return True
    return (part.fmt == 'rtf' and part.encoding in ('b64', 'zip-b64', 'rtf') and part.size >= RTF_MIN_BYTES
            and doc is not None and has_pictures(doc, part))


class Candidates:
    def __init__(self, ws):
        self.ws = ws

    def dir(self, wid, cid):
        if '/' in cid or cid.startswith('.'):
            raise ValueError('bad candidate id')
        return os.path.join(self.ws.state_dir(wid), 'shrink', cid)

    def _read_json(self, path):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write_json(self, path, obj):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
        os.replace(tmp, path)

    def state(self, wid, cid):
        """{'status': 'done'|'running'|'error'|'none', 'result': ..., 'analysis': ..., 'error': ...}"""
        d = self.dir(wid, cid)
        result = self._read_json(os.path.join(d, 'result.json'))
        if result:
            text = result.get('text') or {}
            if text.get('status') == 'running' and not self._claimed(d):
                text.update(status='error', available=False, reason='OCR was interrupted (the server restarted).')
            return {'status': 'done', 'result': result, 'analysis': self._read_json(os.path.join(d, 'analysis.json'))}
        if os.path.exists(os.path.join(d, 'error.txt')):
            with open(os.path.join(d, 'error.txt'), encoding='utf-8') as f:
                return {'status': 'error', 'error': f.read()}
        if self._claimed(d):
            return {'status': 'running', 'analysis': self._read_json(os.path.join(d, 'analysis.json'))}
        return {'status': 'none'}

    def _claimed(self, d):
        claim = os.path.join(d, '.claim')
        try:
            if time.time() - os.path.getmtime(claim) >= CLAIM_STALE_S:
                return False
            with open(claim) as f:
                pid = int(f.read().split()[0])
        except (FileNotFoundError, ValueError, IndexError):
            return False
        return _process_alive(pid)

    def text_initial(self, wid, cid):
        try:
            with open(os.path.join(self.dir(wid, cid), 'text.txt'), encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return ''

    def done_ids(self, wid):
        root = os.path.join(self.ws.state_dir(wid), 'shrink')
        if not os.path.isdir(root):
            return set()
        return {n for n in os.listdir(root) if os.path.exists(os.path.join(root, n, 'result.json'))}

    def file(self, wid, cid, option):
        if option not in OPTIONS:
            raise ValueError('bad option')
        ext = 'rtf' if option in RTF_OPTIONS else 'pdf'
        return os.path.join(self.dir(wid, cid), f'{option}.{ext}')

    # ---- computing ----------------------------------------------------------------------------------------

    def _claim(self, d, retry_error=False):
        os.makedirs(d, exist_ok=True)
        rpath = os.path.join(d, 'result.json')
        if os.path.exists(rpath):
            text = (self._read_json(rpath) or {}).get('text') or {}
            unfinished = text.get('status') == 'error' or (text.get('status') == 'running' and not self._claimed(d))
            if not (retry_error and unfinished):
                return False
            os.unlink(rpath)                           # redo; the fast options take seconds
        err = os.path.join(d, 'error.txt')
        if os.path.exists(err):
            if not retry_error:
                return False
            os.unlink(err)
        claim = os.path.join(d, '.claim')
        if os.path.exists(claim) and time.time() - os.path.getmtime(claim) >= CLAIM_STALE_S:
            os.unlink(claim)
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f'{os.getpid()} {time.time()}'.encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False

    def compute(self, wid, key, part_index, full=True):
        """Compute the candidate for one part of the current working copy (blocking). With full=False only
        the analysis is stored unless the PDF is a scan (used by the background pre-compute).
        Returns 'busy' when another job holds the candidate, so a caller can come back to it."""
        doc = self.ws.load(wid)
        rec = D.record_map(doc).get(key)
        if rec is None or part_index >= len(rec.parts) or not shrinkable(rec.parts[part_index], doc):
            return 'gone'
        part = rec.parts[part_index]
        d = self.dir(wid, cand_id(doc, rec, part))
        if not self._claim(d, retry_error=full):
            return 'busy' if self._claimed(d) else 'done'
        try:
            with _job_slots:
                if part.fmt == 'rtf':
                    self._compute_rtf(doc, part, d)
                    return 'done'
                _name, pdf, _fmt = D.part_bytes(doc, part)
                apath = os.path.join(d, 'analysis.json')
                analysis = self._read_json(apath)
                if analysis is None:
                    analysis = pdfops.analyse(pdf)
                    self._write_json(apath, analysis)
                if not full and analysis['kind'] != 'scan':
                    return
                original_inner = doc.inner_raw(part.payload)
                result = {'original': {'bytes': len(pdf), 'xml_bytes': len(original_inner)},
                          'kind': analysis['kind'], 'pages': analysis['pages']}
                for option in ('strip', 'compress'):
                    result[option] = self._option(option, pdf, analysis, part, original_inner, d)
                rpath = os.path.join(d, 'result.json')
                if analysis['kind'] == 'text':
                    result['text'] = self._text_option(textpdf.text_layer_pages(pdf), analysis, None, part,
                                                       original_inner, d)
                    self._store(rpath, result)
                    return
                # Strip/Compressed are usable now; OCR fills in the Text only tab afterwards.
                result['text'] = {'available': False, 'status': 'running'}
                self._store(rpath, result)
                result['text'] = self._ocr_text(pdf, analysis, part, original_inner, d, result)
                self._store(rpath, result)
        except Exception as e:
            with open(os.path.join(d, 'error.txt'), 'w', encoding='utf-8') as f:
                f.write(f'{type(e).__name__}: {e}\n\n{traceback.format_exc()}')
        finally:
            try:
                os.unlink(os.path.join(d, '.claim'))
            except FileNotFoundError:
                pass

    def _compute_rtf(self, doc, part, d):
        rtf = D.part_bytes(doc, part)[1]
        analysis = rtfops.analyse(rtf)
        self._write_json(os.path.join(d, 'analysis.json'), analysis)
        original_inner = doc.inner_raw(part.payload)
        result = {'original': {'bytes': len(rtf), 'xml_bytes': len(original_inner)}, 'kind': 'rtf',
                  'pictures': analysis['pictures'], 'pages': 0}
        for option, fn in (('rtfpic', rtfops.compress_pictures), ('rtfnopic', rtfops.remove_pictures)):
            try:
                new, notes = fn(rtf)
            except rtfops.RtfError as e:
                result[option] = {'available': False, 'reason': f'Could not be produced: {e}'}
                continue
            with open(os.path.join(d, f'{option}.rtf'), 'wb') as f:
                f.write(new)
            v = rtfops.verify_text(rtf, new)
            out = {'available': True, 'bytes': len(new), 'notes': notes, 'warnings': [],
                   'xml_bytes': len(pdfops.build_payload(part.encoding, original_inner, new, doc.encoding)),
                   'verification': {'kind': 'text', 'identical': v['identical'], 'words': v['chars']}}
            if not v['identical']:
                out['warnings'].append('The text differs from the original.')
            if option == 'rtfnopic':
                out['warnings'].append(f"{len(analysis['pictures'])} picture(s) removed: check none of them is "
                                       'clinical content (e.g. a photo, chart or diagram).')
            result[option] = out
        self._store(os.path.join(d, 'result.json'), result)

    def _store(self, path, result):
        """Write result.json, withdrawing any option whose result is not smaller than the original (e.g. a scan that
        is already compact black-and-white: re-making it plus a searchable layer would only add bytes)."""
        orig = result['original']['xml_bytes']
        for opt in OPTIONS:
            o = result.get(opt)
            if isinstance(o, dict) and o.get('available') and o.get('xml_bytes', 0) >= orig:
                result[opt] = {'available': False, 'not_smaller': True, 'status': o.get('status', 'ready'),
                               'reason': f"Not smaller than the original ({_size(o['xml_bytes'])} vs {_size(orig)}), "
                                         'so it is not offered.'}
        self._write_json(path, result)

    def _ocr_text(self, pdf, analysis, part, original_inner, d, result):
        try:
            image_pages = [p['page'] for p in analysis['page_info'] if p['kind'] != 'text']
            dpi = min(300, max(200, analysis.get('max_image_ppi') or 300))
            o = ocr.ocr_pdf(pdf, pages=image_pages if analysis['kind'] == 'mixed' else None, dpi=dpi)
            self._write_json(os.path.join(d, 'ocr.json'), o)
            by_page = {p['page']: p['text'] for p in o['pages']}
            layer = textpdf.text_layer_pages(pdf) if analysis['kind'] == 'mixed' else []
            texts = [by_page.get(i, layer[i - 1] if i - 1 < len(layer) else '') for i in range(1, analysis['pages'] + 1)]
            if analysis['kind'] == 'scan' and result['compress'].get('available'):
                self._add_hidden_text(o, part, original_inner, d, result)
            return self._text_option(texts, analysis, o['engine'], part, original_inner, d)
        except Exception as e:                         # OCR failure must not take the other options down
            return {'available': False, 'status': 'error', 'reason': f'OCR failed: {e}'}

    def _add_hidden_text(self, o, part, original_inner, d, result):
        path = os.path.join(d, 'compress.pdf')
        with open(path, 'rb') as f:
            layered = textpdf.add_hidden_text(f.read(), o['pages'])
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(layered)
        os.replace(tmp, path)
        c = result['compress']
        c.update(bytes=len(layered), searchable=True, ocr_engine=o['engine'],
                 xml_bytes=len(pdfops.build_payload(part.encoding, original_inner, layered)))

    def _text_option(self, page_texts, analysis, engine, part, original_inner, d):
        text = textpdf.initial_text(page_texts)
        n_words = len(pdfops.words(text))
        if n_words == 0:
            return {'available': False, 'status': 'ready', 'reason': 'No text was found in this document.'}
        with open(os.path.join(d, 'text.txt'), 'w', encoding='utf-8') as f:
            f.write(text)
        footer = footer_text(analysis, engine, edited=False)
        pdf, drawn = textpdf.build_text_pdf(text, footer)
        with open(os.path.join(d, 'text.pdf'), 'wb') as f:
            f.write(pdf)
        v = textpdf.verify_text_pdf(text, pdf, drawn)
        out = {'available': True, 'status': 'ready', 'engine': engine, 'source': analysis['kind'],
               'bytes': len(pdf), 'words': n_words,
               'xml_bytes': len(pdfops.build_payload(part.encoding, original_inner, pdf)),
               'verification': {'kind': 'text-build', **v}, 'warnings': []}
        if not v['identical']:
            out['warnings'].append('The built PDF does not contain exactly the text shown.')
        return out

    def _option(self, option, pdf, analysis, part, original_inner, d):
        if option == 'strip' and analysis['kind'] == 'scan':
            return {'available': False, 'reason': 'This PDF has no text layer (it is a scan), so stripping its '
                                                  'images would leave blank pages.'}
        started = time.time()
        try:
            new = pdfops.strip_images(pdf) if option == 'strip' else pdfops.compress(pdf, analysis)
        except pdfops.PdfError as e:
            return {'available': False, 'reason': f'Could not be produced: {e}'}
        with open(os.path.join(d, f'{option}.pdf'), 'wb') as f:
            f.write(new)
        out = {'available': True, 'bytes': len(new),
               'xml_bytes': len(pdfops.build_payload(part.encoding, original_inner, new)),
               'seconds': round(time.time() - started, 1), 'warnings': []}
        if analysis['kind'] == 'scan':
            out['verification'] = {'kind': 'visual'}
        else:
            v = pdfops.verify_text(pdf, new)
            out['verification'] = {'kind': 'text', **v}
            if not v['identical']:
                out['warnings'].append(f"The text differs from the original: {v['missing_count']} word(s) missing, "
                                       f"{v['added_count']} added.")
        if option == 'strip':
            out['warnings'] += pdfops.strip_warnings(analysis)
        if option == 'compress' and analysis['kind'] == 'mixed':
            out['warnings'].append('Some pages are scans: they are kept as compressed greyscale/colour images.')
        return out

    # ---- background --------------------------------------------------------------------------------------

    def start(self, wid, key, part_index):
        threading.Thread(target=self.compute, args=(wid, key, part_index), daemon=True).start()

    def start_precompute(self, wid):
        """Background pass over PDFs of at least PRECOMPUTE_MIN_BYTES, largest first; computes scans fully and
        stores only the analysis for PDFs with a text layer. Safe to call repeatedly."""
        flag = os.path.join(self.ws.state_dir(wid), '.precompute')
        try:
            fd = os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(flag) < CLAIM_STALE_S:
                    return                             # another thread or worker is on it
                os.utime(flag)                         # stale (its worker died): take it over
            except FileNotFoundError:
                return                                 # a pass has just finished

        def run():
            try:
                doc = self.ws.load(wid)
                done = self.done_ids(wid)
                todo = []
                for rec in D.records(doc):
                    for part in rec.parts:
                        if part.fmt == 'pdf' and shrinkable(part) and part.size >= PRECOMPUTE_MIN_BYTES \
                                and cand_id(doc, rec, part) not in done:
                            todo.append((part.size, rec.key, part.index))
                for _size, key, idx in sorted(todo, reverse=True):
                    try:
                        os.utime(flag)                 # keep the claim fresh while working
                    except FileNotFoundError:
                        return                         # workspace removed or reset meanwhile
                    self.compute(wid, key, idx, full=False)
            finally:
                try:
                    os.unlink(flag)
                except FileNotFoundError:
                    pass

        threading.Thread(target=run, daemon=True).start()

    def targets(self, wid, doc=None):
        """[(rec, part, cid)] for every shrinkable PDF in the working copy, largest first."""
        doc = doc or self.ws.load(wid)
        out = [(rec, part, cand_id(doc, rec, part)) for rec in D.records(doc) for part in rec.parts
               if shrinkable(part, doc)]
        out.sort(key=lambda t: t[1].size, reverse=True)
        return out

    def start_all(self, wid):
        """Background pass that prepares every PDF's options (including OCR), largest first."""
        flag = os.path.join(self.ws.state_dir(wid), '.computeall')
        try:
            os.close(os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(flag) < CLAIM_STALE_S:
                    return
                os.utime(flag)
            except FileNotFoundError:
                return

        def run():
            try:
                todo = [(rec.key, part.index) for rec, part, _cid in self.targets(wid)]
                # A PDF another job is working on (e.g. the after-upload pass, which only analyses text PDFs) is
                # skipped and retried on the next round, until nothing is left or it stops making progress.
                for _round in range(60):
                    busy = []
                    for key, idx in todo:
                        try:
                            os.utime(flag)
                        except FileNotFoundError:
                            return
                        if self.compute(wid, key, idx) == 'busy':
                            busy.append((key, idx))
                    if not busy:
                        return
                    todo = busy
                    time.sleep(5)
            finally:
                try:
                    os.unlink(flag)
                except FileNotFoundError:
                    pass

        threading.Thread(target=run, daemon=True).start()

    def all_running(self, wid):
        flag = os.path.join(self.ws.state_dir(wid), '.computeall')
        try:
            return time.time() - os.path.getmtime(flag) < CLAIM_STALE_S
        except FileNotFoundError:
            return False

    def clear(self, wid):
        root = os.path.join(self.ws.state_dir(wid), 'shrink')
        shutil.rmtree(root, ignore_errors=True)

    def recheck(self, wid, everything=False):
        """Forget failed candidates and claims left by stopped processes (or, with everything=True, all prepared
        options), so the next preparation pass works them out again. Returns how many were reset."""
        root = os.path.join(self.ws.state_dir(wid), 'shrink')
        if not os.path.isdir(root):
            return 0
        if everything:
            n = len(os.listdir(root))
            self.clear(wid)
            return n
        n = 0
        for name in os.listdir(root):
            d = os.path.join(root, name)
            for leftover, test in (('error.txt', lambda: True), ('.claim', lambda: not self._claimed(d))):
                path = os.path.join(d, leftover)
                if os.path.exists(path) and test():
                    os.unlink(path)
                    n += 1
            # results whose OCR stopped half-way (its process is gone) are worked out again
            rpath = os.path.join(d, 'result.json')
            r = self._read_json(rpath) if os.path.exists(rpath) else None
            if r and ((r.get('text') or {}).get('status') in ('running', 'error')) and not self._claimed(d):
                os.unlink(rpath)
                n += 1
        flag = os.path.join(self.ws.state_dir(wid), '.computeall')
        if os.path.exists(flag):
            os.unlink(flag)                            # let a fresh preparation pass start now
        return n


def footer_text(analysis, engine, edited, on=None):
    """The transcription notice printed at the foot of every page of a text-only PDF."""
    on = (on or date.today()).strftime('%d/%m/%Y')
    n = analysis['pages']
    size = _size(analysis.get('bytes', 0))
    what = f"({size}, {n} page{'s' if n != 1 else ''})"
    reviewed = 'the text was reviewed' + (' and edited' if edited else '')
    if analysis['kind'] == 'scan':
        return (f'Text transcribed by OCR ({engine}) from the original scanned document {what} on {on}. '
                f'Images removed; {reviewed}.')
    if analysis['kind'] == 'mixed':
        return (f'Text from the original PDF document {what}, with OCR ({engine}) for its scanned pages, on {on}. '
                f'Images and original layout removed; {reviewed}.')
    return f'Text extracted from the original PDF document {what} on {on}. Images and original layout removed; {reviewed}.'


def _size(n):
    return f'{n / 1048576:.1f} MB' if n >= 1048576 else f'{max(1, round(n / 1024))} KB'
