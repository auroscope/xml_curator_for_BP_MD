"""Workspaces: one per uploaded XML.

    <data>/sources/<wid>/<name>   the uploaded file, read-only (0444), never written again
    <data>/working/<wid>/<name>   the working copy that every edit goes to
    <data>/state/<wid>/           change log, lock file and later the shrink cache
    <data>/health/<hid>/          an upload that is not well-formed XML, with its health report, until it is
                                  repaired (it then becomes a workspace) or discarded; removed after HOLD_HOURS

A repaired upload's workspace has the repaired file as its source; the upload as it was is kept read-only in
<data>/state/<wid>/unrepaired/.

Saves are atomic (temp file + os.replace) under an exclusive flock, so gunicorn workers and threads cannot
interleave edits, and a crash mid-save leaves the previous working copy intact.
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime

from . import xmlhealth
from .documents import schema_of, schema_of_tag
from .xmlstore import XmlDoc, apply_edits

WID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')
HOLD_HOURS = float(os.environ.get('CURATOR_FILE_KEEP_HOURS', '24'))
NOT_AN_EXPORT = ('is not a Best Practice (<BPSEHRV2>) or MedicalDirector (<PATIENT>) export: its top element is '
                 '<{tag}>.')


class WorkspaceError(Exception):
    pass


class Unreadable(WorkspaceError):
    """The upload is not well-formed XML. It is held with a health report under this id (see held())."""

    def __init__(self, hid):
        super().__init__('not a readable XML file')
        self.hid = hid


def curated_name(name):
    """Download name of the working copy: Patient_Name.xml -> Patient_Name_curated.xml"""
    stem = name[:-4] if name.lower().endswith('.xml') else name
    return f'{stem}_curated.xml'


class _DocCache:
    """Small per-process LRU of parsed working files, keyed by path and invalidated by mtime/size."""

    def __init__(self, size=4):
        self.size = size
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, path):
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
        with self.lock:
            hit = self.items.get(path)
            if hit and hit[0] == key:
                self.items.move_to_end(path)
                return hit[1]
        with open(path, 'rb') as f:
            doc = XmlDoc(f.read())
        with self.lock:
            self.items[path] = (key, doc)
            self.items.move_to_end(path)
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return doc


class Workspaces:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        for sub in ('sources', 'working', 'state', 'health'):
            os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
        self.cache = _DocCache()

    # ---- paths -----------------------------------------------------------------------------------------

    def _dir(self, kind, wid):
        if not WID_RE.match(wid or ''):
            raise WorkspaceError('invalid workspace id')
        return os.path.join(self.data_dir, kind, wid)

    def _file_in(self, kind, wid):
        d = self._dir(kind, wid)
        names = [n for n in os.listdir(d) if not n.startswith('.')] if os.path.isdir(d) else []
        if len(names) != 1:
            raise WorkspaceError('workspace not found')
        return os.path.join(d, names[0])

    def source_path(self, wid):
        return self._file_in('sources', wid)

    def working_path(self, wid):
        return self._file_in('working', wid)

    def state_dir(self, wid):
        d = self._dir('state', wid)
        os.makedirs(d, exist_ok=True)
        return d

    def exists(self, wid):
        try:
            self.working_path(wid)
            return True
        except WorkspaceError:
            return False

    # ---- lifecycle -------------------------------------------------------------------------------------

    def source_sha(self, wid):
        """sha1 of the original upload, stored next to the change log the first time it is needed."""
        path = os.path.join(self.state_dir(wid), 'source.sha1')
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            pass
        h = hashlib.sha1()
        with open(self.source_path(wid), 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        with open(path, 'w') as f:
            f.write(h.hexdigest())
        return h.hexdigest()

    def find_by_sha(self, sha):
        """Workspaces whose original upload has this content, most recently changed first."""
        return [w['wid'] for w in self.list() if self.source_sha(w['wid']) == sha]

    def _new_wid(self, base):
        wid = f"{base[:-4][:80]}-{datetime.now():%Y%m%d-%H%M%S}"
        n = 1
        while any(os.path.exists(os.path.join(self.data_dir, k, wid)) for k in ('sources', 'health')):
            n += 1
            wid = f"{base[:-4][:80]}-{datetime.now():%Y%m%d-%H%M%S}-{n}"
        return wid

    def create(self, stream, filename):
        """Store an upload as a new workspace. If the curator already has a file with exactly this content,
        nothing is created and that workspace is returned. Returns (wid, already_existed). Raises Unreadable
        (holding the file with a health report) when it is not well-formed XML."""
        base = re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.basename(filename or 'upload.xml')).strip('._') or 'upload.xml'
        if not base.lower().endswith('.xml'):
            base += '.xml'
        wid = self._new_wid(base)
        src_dir = self._dir('sources', wid)
        os.makedirs(src_dir)
        src = os.path.join(src_dir, base)
        h = hashlib.sha1()
        with open(src, 'wb') as f:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                h.update(chunk)
                f.write(chunk)
        sha = h.hexdigest()
        existing = self.find_by_sha(sha)
        if existing:
            shutil.rmtree(src_dir)
            return existing[0], True
        with open(src, 'rb') as f:
            data = f.read()
        try:
            doc = XmlDoc(data)                         # reject files that are not well-formed XML
        except Exception:
            held = next((h for h in self.held_list() if h['sha'] == sha), None)
            if held:
                shutil.rmtree(src_dir)
                raise Unreadable(held['hid'])
            raise Unreadable(self._hold(wid, src_dir, base, data, sha))
        if schema_of(doc) == 'unknown':                # ... and XML that is not one of the two export formats
            shutil.rmtree(src_dir)
            raise WorkspaceError(f'{base} {NOT_AN_EXPORT.format(tag=doc.root.tag)} Nothing was uploaded.')
        self._install(wid, base, src, sha, os.path.getsize(src))
        return wid, False

    def _install(self, wid, base, src, sha, upload_bytes, extra=None):
        """Make the (already written) source file read-only, copy it as the working copy and start the log."""
        os.chmod(src, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        work_dir = self._dir('working', wid)
        os.makedirs(work_dir)
        shutil.copyfile(src, os.path.join(work_dir, base))
        with open(os.path.join(self.state_dir(wid), 'source.sha1'), 'w') as f:
            f.write(sha)
        self.log(wid, 'upload', None, {'name': base, 'bytes': upload_bytes, **(extra or {})})

    # ---- uploads held for repair ---------------------------------------------------------------------------

    def _hold(self, hid, src_dir, base, data, sha):
        """Move an unreadable upload to <data>/health/<hid>/ with its health report (and repaired copy)."""
        d = self._dir('health', hid)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        os.rename(src_dir, d)
        os.chmod(os.path.join(d, base), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            report = xmlhealth.check(data)
        except Exception as e:                         # never lose the upload to a bug in the checks
            report = {'size': len(data), 'encoding': '', 'problem': {'message': 'it could not be analysed', 'line': 0},
                      'repairs': [], 'needs_guess': False, 'repaired': None, 'root': None,
                      'remaining': {'message': f'the health check failed ({type(e).__name__}: {e})', 'line': 0}}
        repaired = report.pop('repaired')
        report['repaired_bytes'] = None
        if repaired is not None and schema_of_tag(report['root']) == 'unknown':
            report['remaining'] = {'message': NOT_AN_EXPORT.format(tag=report['root']), 'line': 0}
            repaired = None
        if repaired is not None:
            with open(os.path.join(d, '.repaired'), 'wb') as f:
                f.write(repaired)
            report['repaired_bytes'] = len(repaired)
        report.update(hid=hid, name=base, sha=sha, created=time.strftime('%Y-%m-%d %H:%M:%S'))
        with open(os.path.join(d, '.report.json'), 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False)
        return hid

    def held(self, hid):
        """The health report of a held upload ({'name', 'size', 'problem', 'repairs', 'needs_guess',
        'remaining', 'repaired_bytes', ...}), or None."""
        try:
            with open(os.path.join(self._dir('health', hid), '.report.json'), encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, ValueError, WorkspaceError):
            return None

    def held_list(self):
        d = os.path.join(self.data_dir, 'health')
        out = [self.held(h) for h in (sorted(os.listdir(d)) if os.path.isdir(d) else []) if WID_RE.match(h)]
        return [h for h in out if h]

    def held_file(self, hid, repaired=False):
        """Path of a held upload as it was, or of its repaired copy."""
        info = self.held(hid)
        if not info:
            raise WorkspaceError('held file not found')
        path = os.path.join(self._dir('health', hid), '.repaired' if repaired else info['name'])
        if not os.path.exists(path):
            raise WorkspaceError('no repaired copy')
        return path

    def discard_held(self, hid):
        d = self._dir('health', hid)
        if os.path.isdir(d):
            for name in os.listdir(d):
                os.chmod(os.path.join(d, name), stat.S_IWUSR | stat.S_IRUSR)
            shutil.rmtree(d)

    def cleanup_held(self):
        cutoff = time.time() - HOLD_HOURS * 3600
        for info in self.held_list():
            if os.path.getmtime(self._dir('health', info['hid'])) < cutoff:
                self.discard_held(info['hid'])

    def repair(self, hid):
        """Open a held upload as a workspace from its repaired copy. The upload as it was is kept read-only in
        the workspace (unrepaired_path) and every repair is logged. Returns the workspace id."""
        info = self.held(hid)
        if not info:
            raise WorkspaceError('held file not found')
        hold_dir = self._dir('health', hid)
        repaired = self.held_file(hid, repaired=True)
        base, wid = info['name'], hid
        if os.path.exists(self._dir('sources', wid)):
            wid = self._new_wid(base)
        src_dir = self._dir('sources', wid)
        os.makedirs(src_dir)
        src = os.path.join(src_dir, base)
        shutil.copyfile(repaired, src)
        keep = os.path.join(self.state_dir(wid), 'unrepaired')
        os.makedirs(keep)
        os.rename(os.path.join(hold_dir, base), os.path.join(keep, base))
        self._install(wid, base, src, info['sha'], info['size'],
                      {'repaired': True, 'repaired_bytes': os.path.getsize(src)})
        for r in info['repairs']:
            self.log(wid, 'repair', None, {'name': base, 'tier': r['tier'], 'kind': r['kind'], 'count': r['count'],
                                           'message': r['message']})
        self.discard_held(hid)
        return wid

    def unrepaired_path(self, wid):
        """The upload as it was, for a workspace opened from a repaired copy; else None."""
        d = os.path.join(self.state_dir(wid), 'unrepaired')
        names = os.listdir(d) if os.path.isdir(d) else []
        return os.path.join(d, names[0]) if names else None

    def list(self):
        out = []
        for wid in sorted(os.listdir(os.path.join(self.data_dir, 'working'))):
            try:
                src, work = self.source_path(wid), self.working_path(wid)
            except WorkspaceError:
                continue
            log = self.changes(wid)
            name = os.path.basename(src)
            uploaded = next((c['ts'] for c in log if c['action'] == 'upload'), '')
            out.append({
                'wid': wid,
                'name': name,
                'curated_name': curated_name(name),
                'source_bytes': os.path.getsize(src),
                'working_bytes': os.path.getsize(work),
                'changes': len(self.status_by_key(wid)),      # documents/items changed now (not log entries)
                'repaired': self.unrepaired_path(wid) is not None,
                'uploaded': datetime.strptime(uploaded, '%Y-%m-%d %H:%M:%S') if uploaded else None,
                'modified': datetime.fromtimestamp(os.path.getmtime(work)),
                'snapshots': self.snapshots(wid),
                'next_copy': self.next_snapshot_name(wid),
            })
        out.sort(key=lambda w: w['modified'], reverse=True)
        return out

    def remove(self, wid):
        for kind in ('sources', 'working', 'state'):
            d = self._dir(kind, wid)
            if os.path.isdir(d):
                for root, dirs, files in os.walk(d):
                    for name in files:
                        os.chmod(os.path.join(root, name), stat.S_IWUSR | stat.S_IRUSR)
                shutil.rmtree(d)

    # ---- reading & writing -----------------------------------------------------------------------------

    def load(self, wid):
        return self.cache.get(self.working_path(wid))

    def load_source(self, wid):
        return self.cache.get(self.source_path(wid))      # read-only file, so the cached parse stays valid

    @contextmanager
    def locked(self, wid):
        with open(os.path.join(self.state_dir(wid), '.lock'), 'a+') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _write_working(self, wid, data):
        path = self.working_path(wid)
        fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def apply(self, wid, build_edits):
        """Run build_edits(doc) on a freshly loaded working copy under the lock and save the result.

        build_edits returns (edits, result); the edits are applied atomically and result is passed back.
        The working copy is re-read inside the lock, so offsets can never be stale."""
        with self.locked(wid):
            with open(self.working_path(wid), 'rb') as f:
                doc = XmlDoc(f.read())
            edits, result = build_edits(doc)
            if edits:
                new = apply_edits(doc.data, edits)
                XmlDoc(new)                            # never save something that no longer parses
                self._write_working(wid, new)
            return result

    def reset(self, wid, save_copy=False):
        """Copy the original back over the working copy. With save_copy, the working copy is first kept as a
        saved copy (see snapshot). Returns the saved copy's info, or None."""
        saved = self.snapshot(wid) if save_copy else None
        with self.locked(wid):
            with open(self.source_path(wid), 'rb') as f:
                self._write_working(wid, f.read())
        self.log(wid, 'reset', None, {'saved_copy': saved['file']} if saved else {})
        return saved

    # ---- saved copies (snapshots of the working copy) ----------------------------------------------------

    SNAP_RE = re.compile(r'^[A-Za-z0-9_.-]+_curated_\d{8}(_\d+)?\.xml$')

    def _snap_dir(self, wid):
        d = os.path.join(self.state_dir(wid), 'snapshots')
        os.makedirs(d, exist_ok=True)
        return d

    def next_snapshot_name(self, wid):
        d = os.path.join(self.state_dir(wid), 'snapshots')
        base = f"{curated_name(os.path.basename(self.source_path(wid)))[:-4]}_{datetime.now():%d%m%Y}"
        name, n = f'{base}.xml', 1
        while os.path.exists(os.path.join(d, name)):
            n += 1
            name = f'{base}_{n}.xml'
        return name

    def snapshot(self, wid):
        """Keep the current working copy as <name>_curated_DDMMYYYY.xml (_2, _3 … for more the same day),
        together with which documents/items were changed at that point."""
        d = self._snap_dir(wid)
        name = self.next_snapshot_name(wid)
        with self.locked(wid):
            shutil.copyfile(self.working_path(wid), os.path.join(d, name))
            status = self.status_by_key(wid)
        info = {'file': name, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                'bytes': os.path.getsize(os.path.join(d, name)), 'status': status}
        with open(os.path.join(d, name + '.json'), 'w', encoding='utf-8') as f:
            json.dump(info, f)
        self.log(wid, 'save_copy', None, {'file': name, 'bytes': info['bytes'], 'changed': len(status)})
        return info

    def snapshots(self, wid):
        d = os.path.join(self.state_dir(wid), 'snapshots')
        if not os.path.isdir(d):
            return []
        out = []
        for name in os.listdir(d):
            if name.endswith('.xml.json'):
                with open(os.path.join(d, name), encoding='utf-8') as f:
                    info = json.load(f)
                if os.path.exists(os.path.join(d, info['file'])):
                    info['changed'] = len(info.get('status') or {})
                    out.append(info)
        out.sort(key=lambda i: i['created'], reverse=True)
        return out

    def snapshot_path(self, wid, name):
        if not self.SNAP_RE.match(name or ''):
            raise WorkspaceError('invalid saved copy name')
        path = os.path.join(self.state_dir(wid), 'snapshots', name)
        if not os.path.exists(path):
            raise WorkspaceError('saved copy not found')
        return path

    def restore_snapshot(self, wid, name):
        """Make a saved copy the working copy again (the current working copy is saved first if it differs)."""
        path = self.snapshot_path(wid, name)
        with open(path + '.json', encoding='utf-8') as f:
            info = json.load(f)
        if self.status_by_key(wid):
            self.snapshot(wid)
        with open(path, 'rb') as f:
            data = f.read()
        XmlDoc(data)
        with self.locked(wid):
            self._write_working(wid, data)
        self.log(wid, 'restore_copy', None, {'file': name, 'status': info.get('status') or {}})

    def delete_snapshot(self, wid, name):
        path = self.snapshot_path(wid, name)
        os.unlink(path)
        if os.path.exists(path + '.json'):
            os.unlink(path + '.json')
        self.log(wid, 'delete_copy', None, {'file': name})

    # ---- change log ------------------------------------------------------------------------------------

    def log(self, wid, action, key, details):
        entry = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'action': action, 'key': key, **details}
        with open(os.path.join(self.state_dir(wid), 'changes.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def changes(self, wid):
        path = os.path.join(self.state_dir(wid), 'changes.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def status_by_key(self, wid):
        """Latest state of each record key: 'deleted', 'shrunk', 'modified' or absent (unchanged / reverted)."""
        status = {}
        for c in self.changes(wid):
            if c['action'] == 'reset':
                status.clear()
            elif c['action'] == 'restore_copy':
                status = dict(c.get('status') or {})
            elif c.get('key'):
                if c['action'] == 'revert':
                    status.pop(c['key'], None)
                elif c['action'] == 'delete':
                    status[c['key']] = 'deleted'
                elif c['action'] == 'shrink':
                    status[c['key']] = 'shrunk'
                else:
                    status.setdefault(c['key'], 'modified')
        return status
