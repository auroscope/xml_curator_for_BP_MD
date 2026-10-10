"""Workspaces: one per uploaded XML.

    <data>/sources/<wid>/<name>   the uploaded file, read-only (0444), never written again
    <data>/working/<wid>/<name>   the working copy that every edit goes to
    <data>/state/<wid>/           change log, lock file and later the shrink cache

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

from .xmlstore import XmlDoc, apply_edits

WID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')


class WorkspaceError(Exception):
    pass


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
        for sub in ('sources', 'working', 'state'):
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

    def create(self, stream, filename):
        """Store an upload as a new workspace. If the curator already has a file with exactly this content,
        nothing is created and that workspace is returned. Returns (wid, already_existed)."""
        base = re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.basename(filename or 'upload.xml')).strip('._') or 'upload.xml'
        if not base.lower().endswith('.xml'):
            base += '.xml'
        wid = f"{base[:-4][:80]}-{datetime.now():%Y%m%d-%H%M%S}"
        n = 1
        while os.path.exists(self._dir('sources', wid)):
            n += 1
            wid = f"{base[:-4][:80]}-{datetime.now():%Y%m%d-%H%M%S}-{n}"
        src_dir, work_dir = self._dir('sources', wid), self._dir('working', wid)
        os.makedirs(src_dir)
        src = os.path.join(src_dir, base)
        h = hashlib.sha1()
        with open(src, 'wb') as f:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                h.update(chunk)
                f.write(chunk)
        existing = self.find_by_sha(h.hexdigest())
        if existing:
            shutil.rmtree(src_dir)
            return existing[0], True
        try:
            with open(src, 'rb') as f:
                XmlDoc(f.read())                       # reject files that are not well-formed XML
        except Exception as e:
            shutil.rmtree(src_dir)
            raise WorkspaceError(f'not a readable XML file: {e}')
        os.chmod(src, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.makedirs(work_dir)
        shutil.copyfile(src, os.path.join(work_dir, base))
        with open(os.path.join(self.state_dir(wid), 'source.sha1'), 'w') as f:
            f.write(h.hexdigest())
        self.log(wid, 'upload', None, {'name': base, 'bytes': os.path.getsize(src)})
        return wid, False

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
