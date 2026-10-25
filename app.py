"""xml_curator_for_BP_MD: review, trim and curate Best Practice and MedicalDirector XML exports before import."""
import csv
import io
import json
import os
import re
import secrets
import threading
from datetime import datetime

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session,
                   url_for)

from curator import clinical as C
from curator import documents as D
from curator import filecompress as FC
from curator import imageops
from curator.imageops import NAMES as IMAGE_NAMES
from curator import pdfops
from curator import ocr
from curator import shrink as S
from curator import textpdf
from curator.render import render_part
from curator.workspace import HOLD_HOURS, Unreadable, WorkspaceError, Workspaces, curated_name

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('CURATOR_DATA', os.path.join(BASE_DIR, 'data'))


def _secret_key():
    path = os.path.join(DATA_DIR, '.secret_key')
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('CURATOR_SECRET') or _secret_key()
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('CURATOR_MAX_UPLOAD_MB', '500')) * 1024 * 1024
ws = Workspaces(DATA_DIR)
cands = S.Candidates(ws)
files = FC.FileJobs(os.path.join(DATA_DIR, 'files'))
from curator.officeconv import Converter
app.extensions['office'] = Converter(os.path.join(DATA_DIR, 'cache', 'office'))

OPTION_LABELS = {'original': 'Original', 'strip': 'Strip images', 'compress': 'Compressed scan', 'text': 'Text only',
                 'imgpdf': 'PDF (lossless)', 'imgjpeg': 'Compressed (keeps colour)',
                 'rtfpic': 'Compress pictures', 'rtfnopic': 'Remove pictures'}


# ---- helpers ---------------------------------------------------------------------------------------------

@app.template_filter('size')
def format_size(n):
    if n is None:
        return ''
    n = int(n)
    if n >= 1024 * 1024:
        return f'{n / 1048576:.1f} MB'
    if n >= 1024:
        return f'{n / 1024:.0f} KB'
    return f'{n} B'


@app.template_global()
def change_details(entry):
    """One-line description of what a change-log entry changed."""
    labels = dict(D.META_FIELDS)
    changes = entry.get('changes') or {}
    out = [f"{labels.get(k, k)}: {v['old'] or '(empty)'} -> {v['new'] or '(empty)'}" for k, v in changes.items()]
    if entry.get('action') == 'repair':
        out.insert(0, f"{entry.get('message')} ({entry.get('count')} place{'s' if entry.get('count') != 1 else ''}"
                      + (', best guess' if entry.get('tier') == 'guess' else '') + ')')
    if entry.get('action') == 'upload' and entry.get('repaired'):
        out.insert(0, f"not well-formed XML; opened from a repaired copy ({format_size(entry.get('repaired_bytes'))})")
    if entry.get('action') == 'shrink':
        out.insert(0, (f"{IMAGE_NAMES.get(entry['converted_from'], '')} converted to PDF, " if entry.get('converted_from') else '')
                      + f"{OPTION_LABELS.get(entry.get('option'), entry.get('option'))}, part {entry.get('part', 0) + 1}: "
                      f"{format_size(entry.get('bytes'))} -> {format_size(entry.get('new_bytes'))} in the XML"
                      + (f", {entry['edited_words']} word(s) edited" if entry.get('option') == 'text' else '')
                      + (' (bulk)' if entry.get('bulk') else ''))
    return '; '.join(out)


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def check_csrf():
    if request.method == 'POST':
        sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
        if not sent or sent != session.get('_csrf'):
            abort(400, 'Missing or invalid form token. Reload the page and try again.')


def require(wid):
    if not ws.exists(wid):
        abort(404, 'Workspace not found')


# ---- workspaces ------------------------------------------------------------------------------------------

@app.route('/')
def index():
    """Start page: XML exports (default) and single-file compress, as tabs."""
    files.cleanup()
    ws.cleanup_held()
    return render_template('index.html', workspaces=ws.list(), held=ws.held_list(), ocr_status=ocr.engine_status(),
                           today=f'{datetime.now():%d%m%Y}', tab='file' if request.args.get('tab') == 'file' else 'xml',
                           recent=files.recent(), keep_hours=FC.KEEP_HOURS)


def _after_upload(url):
    """Where to go after an upload: a redirect, or for the page's own uploader (which shows upload progress and
    then goes there itself) the address as JSON. Flash messages are kept for that page either way."""
    if request.headers.get('X-Upload') == '1':
        return jsonify({'location': url})
    return redirect(url)


@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('Choose an XML file to upload.', 'warning')
        return _after_upload(url_for('index'))
    try:
        wid, existed = ws.create(f.stream, f.filename)
    except Unreadable as e:
        return _after_upload(url_for('health', hid=e.hid))
    except WorkspaceError as e:
        flash(str(e), 'danger')
        return _after_upload(url_for('index'))
    if existed:
        flash(f'{f.filename} is already in the curator (same file). Opened its existing workspace, with any '
              'changes you made to it.', 'warning')
        return _after_upload(url_for('documents', wid=wid))
    cands.start_precompute(wid)
    flash(f'Uploaded {f.filename}. The original is kept read-only; all edits go to a working copy.', 'success')
    return _after_upload(url_for('documents', wid=wid))


@app.route('/w/<wid>/download')
def download(wid):
    require(wid)
    stem = curated_name(os.path.basename(ws.source_path(wid)))[:-4]
    return send_file(ws.working_path(wid), mimetype='application/xml', as_attachment=True,
                     download_name=f'{stem}_{datetime.now():%d%m%Y}.xml')


@app.route('/w/<wid>/source')
def download_source(wid):
    require(wid)
    path = ws.source_path(wid)
    return send_file(path, mimetype='application/xml', as_attachment=True, download_name=os.path.basename(path))


@app.route('/w/<wid>/unrepaired')
def download_unrepaired(wid):
    """The upload as it was, before it was repaired."""
    require(wid)
    path = ws.unrepaired_path(wid)
    if not path:
        abort(404, 'This file was not repaired')
    return send_file(path, mimetype='application/xml', as_attachment=True, download_name=os.path.basename(path))


# ---- uploads that are not well-formed XML ----------------------------------------------------------------

def _held(hid):
    info = ws.held(hid)
    if not info:
        abort(404, 'This file is no longer held. Files that are not repaired are removed after a day; upload it again.')
    return info


@app.route('/health/<hid>')
def health(hid):
    return render_template('health.html', hid=hid, r=_held(hid), keep_hours=HOLD_HOURS)


@app.route('/health/<hid>/file')
def health_file(hid):
    info = _held(hid)
    repaired = request.args.get('repaired') == '1'
    try:
        path = ws.held_file(hid, repaired=repaired)
    except WorkspaceError as e:
        abort(404, str(e))
    name = info['name'][:-4] + '_repaired.xml' if repaired else info['name']
    return send_file(path, mimetype='application/xml', as_attachment=True, download_name=name)


@app.route('/health/<hid>/repair', methods=['POST'])
def health_repair(hid):
    info = _held(hid)
    if info.get('repaired_bytes') is None:
        abort(400, 'This file could not be repaired.')
    if info.get('needs_guess') and request.form.get('accept_guess') != '1':
        flash('Tick the box to confirm you will check the records the best-guess repairs changed.', 'warning')
        return redirect(url_for('health', hid=hid))
    wid = ws.repair(hid)
    cands.start_precompute(wid)
    flash(f"Opened {info['name']} from a repaired copy. The file as uploaded is kept read-only; the repairs are "
          'listed in the change log.', 'success')
    return redirect(url_for('documents', wid=wid))


@app.route('/health/<hid>/discard', methods=['POST'])
def health_discard(hid):
    info = _held(hid)
    ws.discard_held(hid)
    flash(f"Removed {info['name']}.", 'success')
    return redirect(url_for('index'))


@app.route('/w/<wid>/changes')
def changes(wid):
    require(wid)
    rows = ws.changes(wid)
    fmt = request.args.get('format')
    if fmt == 'json':
        return jsonify(rows)
    if fmt == 'csv':
        cols = ['ts', 'action', 'key', 'subject', 'date', 'category', 'bytes', 'details']
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({**r, 'details': change_details(r)})
        stem = os.path.basename(ws.working_path(wid))[:-4]
        return send_file(io.BytesIO(out.getvalue().encode('utf-8')), mimetype='text/csv', as_attachment=True,
                         download_name=f'{stem}_changes.csv')
    return render_template('changes.html', wid=wid, name=os.path.basename(ws.working_path(wid)), rows=rows)


@app.route('/w/<wid>/changes.pdf')
def changes_pdf(wid):
    """The change log as a PDF, to file alongside the curated XML (e.g. import it into the EHR yourself)."""
    require(wid)
    from curator import changelog_pdf
    doc = ws.load(wid)
    entries = ws.changes(wid)
    src_name = os.path.basename(ws.source_path(wid))
    stem = curated_name(src_name)[:-4]
    dated = f"{stem}_{datetime.now():%d%m%Y}"
    up = next((e['ts'] for e in entries if e['action'] == 'upload'), '')
    pat = D.patient_summary(doc)
    info = {'name': src_name, 'curated_name': dated + '.xml', 'patient': pat.get('name'), 'dob': pat.get('dob'),
            'source_bytes': os.path.getsize(ws.source_path(wid)), 'working_bytes': len(doc.data),
            'uploaded': changelog_pdf._ts(up)}
    unrepaired = ws.unrepaired_path(wid)
    if unrepaired:
        info['unrepaired_bytes'] = os.path.getsize(unrepaired)
    pdf = changelog_pdf.build(entries, info, change_details)
    return send_file(io.BytesIO(pdf), mimetype='application/pdf', as_attachment=True, download_name=dated + '_log.pdf')


@app.route('/w/<wid>/reset', methods=['POST'])
def reset(wid):
    require(wid)
    saved = ws.reset(wid, save_copy=request.form.get('save_copy') == '1')
    fresh = request.form.get('fresh') == '1'
    what = 'Opened the original fresh' if fresh else 'Reset to the original upload'
    if saved:
        flash(f'Saved the curated copy as {saved["file"]}. {what}. The saved copy is listed on the start page, '
              'where you can resume from it or download it.', 'success')
    else:
        flash(f'{what}; the earlier changes were undone.', 'success')
    return redirect(url_for('documents', wid=wid))


@app.route('/w/<wid>/copies/<name>')
def download_copy(wid, name):
    require(wid)
    try:
        path = ws.snapshot_path(wid, name)
    except WorkspaceError:
        abort(404, 'Saved copy not found')
    return send_file(path, mimetype='application/xml', as_attachment=True, download_name=name)


@app.route('/w/<wid>/copies/<name>/restore', methods=['POST'])
def restore_copy(wid, name):
    require(wid)
    try:
        ws.restore_snapshot(wid, name)
    except WorkspaceError as e:
        abort(404, str(e))
    flash(f'{name} is the curated copy again. (If there were unsaved changes, they were kept as another saved copy.)', 'success')
    return redirect(url_for('documents', wid=wid))


@app.route('/w/<wid>/copies/<name>/delete', methods=['POST'])
def delete_copy(wid, name):
    require(wid)
    try:
        ws.delete_snapshot(wid, name)
    except WorkspaceError as e:
        abort(404, str(e))
    flash(f'Deleted the saved copy {name}.', 'success')
    return redirect(url_for('index'))


@app.route('/w/<wid>/remove', methods=['POST'])
def remove(wid):
    require(wid)
    ws.remove(wid)
    flash('Workspace removed.', 'success')
    return redirect(url_for('index'))


# ---- documents -------------------------------------------------------------------------------------------

def _next_copy_name(wid):
    return ws.next_snapshot_name(wid)


def _summary(rec):
    return {'subject': rec.subject or rec.category, 'date': rec.date, 'category': rec.category, 'bytes': rec.size}


@app.route('/w/<wid>/')
@app.route('/w/<wid>/documents')
def documents(wid):
    require(wid)
    doc = ws.load(wid)
    recs = D.records(doc)
    status = ws.status_by_key(wid)
    deleted = []
    if any(v == 'deleted' for v in status.values()):
        src_map = D.record_map(ws.load_source(wid))
        present = {r.key for r in recs}
        deleted = [src_map[k] for k, v in status.items() if v == 'deleted' and k in src_map and k not in present]
    by_section = {s: [r for r in recs if r.section == s] for s, _ in D.SECTIONS}
    cands.start_precompute(wid)
    ready = cands.done_ids(wid)
    shrink_ready = {r.key for r in recs for p in r.parts if S.shrinkable(p, doc) and S.cand_id(doc, r, p) in ready}
    dup_of = D.duplicate_of(doc)
    active_key = request.args.get('key') or (recs[0].key if recs else None)
    active = D.record_map(doc).get(active_key)
    part = int(request.args.get('part', 0) or 0)
    return render_template(
        'documents.html', wid=wid, name=os.path.basename(ws.working_path(wid)),
        schema=D.SCHEMA_LABELS[D.schema_of(doc)], patient=D.patient_summary(doc),
        sections=D.SECTIONS, by_section=by_section, status=status, deleted=deleted,
        active=active, part=part if active and part < len(active.parts) else 0,
        meta_fields=D.editable_fields(active) if active else [],
        meta_values=D.current_values(active) if active else {},
        categories=D.category_suggestions(doc, active.kind) if active else [],
        shrink_ready=shrink_ready, shrinkable=lambda p: S.shrinkable(p, doc), icon_min=S.ICON_MIN_BYTES, dup_of=dup_of,
        working_bytes=len(doc.data), source_bytes=os.path.getsize(ws.source_path(wid)),
        changed_count=len(status), next_copy_name=_next_copy_name(wid))


@app.route('/w/<wid>/doc/<key>/part/<int:part>')
def view_part(wid, key, part):
    require(wid)
    doc = ws.load(wid)
    rec = D.record_map(doc).get(key)
    if rec is None or part >= len(rec.parts):
        abort(404, 'Document not found in the working copy')
    return render_part(doc, rec, rec.parts[part])


@app.route('/w/<wid>/doc/<key>/part/<int:part>/attachment/<int:index>')
def view_attachment(wid, key, part, index):
    """An attachment inside an Outlook email document."""
    require(wid)
    doc = ws.load(wid)
    rec = D.record_map(doc).get(key)
    if rec is None or part >= len(rec.parts):
        abort(404, 'Document not found in the working copy')
    from curator.render import render_attachment
    from curator.msgfile import MsgError
    try:
        resp = render_attachment(rec, D.part_bytes(doc, rec.parts[part])[1], index)
    except MsgError:
        resp = None
    if resp is None:
        abort(404, 'Attachment not found')
    return resp


@app.route('/w/<wid>/source/doc/<key>/part/<int:part>')
def view_source_part(wid, key, part):
    """A record as it is in the original upload (used to preview deleted documents)."""
    require(wid)
    doc = ws.load_source(wid)
    rec = D.record_map(doc).get(key)
    if rec is None or part >= len(rec.parts):
        abort(404, 'Document not found in the original')
    return render_part(doc, rec, rec.parts[part])


@app.route('/w/<wid>/documents/delete', methods=['POST'])
def delete_documents(wid):
    require(wid)
    keys = request.form.getlist('keys')
    if not keys:
        flash('No documents selected.', 'warning')
        return redirect(url_for('documents', wid=wid))

    def build(doc):
        return D.delete_edits(doc, keys)

    removed = ws.apply(wid, build)
    for rec in removed:
        ws.log(wid, 'delete', rec.key, _summary(rec))
    total = sum(r.size for r in removed)
    flash(f'Deleted {len(removed)} document(s), about {format_size(total)}.', 'success')
    return redirect(url_for('documents', wid=wid, key=request.form.get('next_key') or None))


@app.route('/w/<wid>/doc/<key>/part/<int:part>/shrink/delete', methods=['POST'])
def shrink_delete(wid, key, part):
    """Delete the whole document from the shrink screen (none of the options suits), then go to the next one."""
    require(wid)
    nxt = _queue_nav(wid, ws.load(wid), key, part)['next']        # worked out before the document disappears

    def build(doc):
        return D.delete_edits(doc, [key])

    removed = ws.apply(wid, build)
    for rec in removed:
        ws.log(wid, 'delete', rec.key, _summary(rec))
    if removed:
        rec = removed[0]
        flash(f'Deleted "{rec.subject or rec.category}" ({format_size(rec.size)}). It can be restored from the '
              'document list.', 'success')
    if nxt:
        return redirect(nxt)
    return redirect(url_for('documents', wid=wid))


@app.route('/w/<wid>/doc/<key>/meta', methods=['POST'])
def save_metadata(wid, key):
    require(wid)
    values = {name: request.form.get(name, '') for name, _ in D.META_FIELDS if name in request.form}

    def build(doc):
        rec = D.record_map(doc).get(key)
        if rec is None:
            abort(404, 'Document not found in the working copy')
        edits, changed = D.metadata_edits(doc, rec, values)
        return edits, (rec, changed)

    try:
        rec, changed = ws.apply(wid, build)
    except D.MetadataError as e:
        flash(str(e), 'danger')
        return redirect(url_for('documents', wid=wid, key=key))
    if not changed:
        flash('No changes to save.', 'warning')
    else:
        summary = _summary(rec)
        if 'subject' in changed:
            summary['subject'] = changed['subject'][1]
        ws.log(wid, 'metadata', key, {**summary, 'changes': {k: {'old': o, 'new': n} for k, (o, n) in changed.items()}})
        labels = dict(D.META_FIELDS)
        flash('Saved ' + ', '.join(labels[k].lower() for k in changed) + '.', 'success')
    return redirect(url_for('documents', wid=wid, key=key, part=request.form.get('part') or None))


# ---- shrink ----------------------------------------------------------------------------------------------

def _shrink_target(doc, key, part):
    rec = D.record_map(doc).get(key)
    if rec is None or part >= len(rec.parts):
        abort(404, 'Document not found in the working copy')
    if not S.shrinkable(rec.parts[part], doc):
        abort(404, 'Only PDFs, and RTF documents with pictures, can be shrunk')
    return rec, rec.parts[part]


def _queue(wid, doc):
    """Every PDF in the working copy as (key, part), ordered by its size in the original upload (largest first),
    so the order does not change as documents are shrunk."""
    src = ws.load_source(wid)
    src_sizes = {(r.key, p.index): p.size for r in D.records(src) for p in r.parts}
    items = [(rec.key, p.index) for rec, p, _cid in cands.targets(wid, doc)]
    items.sort(key=lambda kp: src_sizes.get(kp, 0), reverse=True)
    return items


def _queue_nav(wid, doc, key, part):
    items = _queue(wid, doc)
    status = ws.status_by_key(wid)
    cur = (key, part)
    i = items.index(cur) if cur in items else -1
    after = [kp for kp in items[i + 1:] if status.get(kp[0]) != 'shrunk']
    before = items[:i] if i > 0 else []
    url = lambda kp: url_for('shrink_page', wid=wid, key=kp[0], part=kp[1]) if kp else None
    return {'pos': i + 1, 'total': len(items), 'remaining': sum(1 for k, _ in items if status.get(k) != 'shrunk'),
            'prev': url(before[-1] if before else None), 'next': url(after[0] if after else None)}


@app.route('/w/<wid>/doc/<key>/part/<int:part>/shrink')
def shrink_page(wid, key, part):
    require(wid)
    doc = ws.load(wid)
    rec, p = _shrink_target(doc, key, part)
    cid = S.cand_id(doc, rec, p)
    state = cands.state(wid, cid)
    if state['status'] == 'none':
        cands.start(wid, key, part)
        state = {'status': 'running'}
    return render_template(
        'shrink_rtf.html' if p.fmt == 'rtf' else 'shrink.html',
        wid=wid, name=os.path.basename(ws.working_path(wid)), patient=D.patient_summary(doc),
        rec=rec, part=p, cid=cid, state=state, labels=OPTION_LABELS,
        opts=S.IMAGE_OPTIONS if p.fmt in imageops.IMAGE_FORMATS else S.PDF_OPTIONS,
        meta_fields=D.editable_fields(rec), meta_values=D.current_values(rec),
        categories=D.category_suggestions(doc, rec.kind), status=ws.status_by_key(wid).get(key),
        tools=pdfops.tools_available(), text_initial=cands.text_initial(wid, cid),
        queue=_queue_nav(wid, doc, key, part), working_bytes=len(doc.data),
        source_bytes=os.path.getsize(ws.source_path(wid)))


@app.route('/w/<wid>/doc/<key>/part/<int:part>/shrink/status')
def shrink_status(wid, key, part):
    require(wid)
    doc = ws.load(wid)
    rec, p = _shrink_target(doc, key, part)
    return _status_json(cands.state(wid, S.cand_id(doc, rec, p)))


@app.route('/w/<wid>/doc/<key>/part/<int:part>/shrink/start', methods=['POST'])
def shrink_start(wid, key, part):
    require(wid)
    doc = ws.load(wid)
    _shrink_target(doc, key, part)
    threading.Thread(target=cands.compute, args=(wid, key, part), kwargs={'full': True}, daemon=True).start()
    return redirect(url_for('shrink_page', wid=wid, key=key, part=part))


@app.route('/w/<wid>/cand/<cid>/<option>.pdf')
def candidate_pdf(wid, cid, option):
    require(wid)
    try:
        path = cands.file(wid, cid, option)
    except ValueError:
        abort(404)
    if not os.path.exists(path):
        abort(404, 'Preview not available')
    return send_file(path, mimetype='application/pdf', download_name=f'{option}.pdf', max_age=0)


@app.route('/w/<wid>/cand/<cid>/<option>/view')
def candidate_view(wid, cid, option):
    """An RTF candidate rendered by LibreOffice (HTML, or ?view=pdf for the page view)."""
    require(wid)
    if option not in S.RTF_OPTIONS:
        abort(404)
    try:
        path = cands.file(wid, cid, option)
    except ValueError:
        abort(404)
    if not os.path.exists(path):
        abort(404, 'Preview not available')
    from curator.render import rtf_view
    with open(path, 'rb') as f:
        return rtf_view(f.read(), OPTION_LABELS[option])


class ShrinkError(Exception):
    pass


@app.template_global()
def state_key(st):
    """Changes whenever a shrink page's tabs would look different: the page swaps in the new tabs."""
    return f"{st['status']}/{((st.get('result') or {}).get('text') or {}).get('status') or ''}"


def _status_json(st):
    return jsonify({'status': st['status'], 'text': ((st.get('result') or {}).get('text') or {}).get('status'),
                    'key': state_key(st), 'progress': st.get('progress') or {}})


def _build_text_option(initial, st, text):
    """Build the text-only PDF from the (edited) text; initial is the text first offered. Returns (pdf, info)."""
    text = textpdf.clean_text(text)
    if not pdfops.words(text):
        raise ShrinkError('The text is empty.')
    analysis = st.get('analysis') or {}
    engine = (st['result'].get('text') or {}).get('engine')
    a, b = pdfops.words(initial), pdfops.words(text)
    import difflib
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    edited = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != 'equal')
    footer = S.footer_text(analysis, engine, edited=edited > 0)
    pdf, drawn = textpdf.build_text_pdf(text, footer)
    return pdf, {'verification': textpdf.verify_text_pdf(text, pdf, drawn), 'engine': engine, 'words': len(b),
                 'edited_words': edited, 'footer': footer}


@app.route('/w/<wid>/doc/<key>/part/<int:part>/shrink/text-preview', methods=['POST'])
def shrink_text_preview(wid, key, part):
    """The text-only PDF as it would be saved from the current (unsaved) text."""
    require(wid)
    doc = ws.load(wid)
    rec, p = _shrink_target(doc, key, part)
    cid = S.cand_id(doc, rec, p)
    st = cands.state(wid, cid)
    if st['status'] != 'done' or not (st['result'].get('text') or {}).get('available'):
        abort(404, 'Text option not available')
    try:
        pdf, info = _build_text_option(cands.text_initial(wid, cid), st, request.form.get('text', ''))
    except ShrinkError as e:
        abort(400, str(e))
    old = len(doc.inner_raw(p.payload))
    new = len(pdfops.build_payload(p.encoding, doc.inner_raw(p.payload), pdf))
    resp = send_file(io.BytesIO(pdf), mimetype='application/pdf', download_name='text-preview.pdf', max_age=0)
    resp.headers['X-Xml-Bytes'] = str(new)
    resp.headers['X-Xml-Saving'] = str(old - new)
    resp.headers['X-Text-Identical'] = '1' if info['verification']['identical'] else '0'
    resp.headers['X-Edited-Words'] = str(info['edited_words'])
    return resp


@app.route('/w/<wid>/doc/<key>/part/<int:part>/shrink/save', methods=['POST'])
def shrink_save(wid, key, part):
    require(wid)
    option = request.form.get('option')
    cid = request.form.get('cid', '')
    if option not in ('original', 'strip', 'compress', 'text', 'rtfpic', 'rtfnopic', 'imgpdf', 'imgjpeg'):
        abort(400, 'Unknown option')
    values = {name: request.form.get(name, '') for name, _ in D.META_FIELDS if name in request.form}
    back = url_for('shrink_page', wid=wid, key=key, part=part)

    def build(doc):
        rec, p = _shrink_target(doc, key, part)
        if S.cand_id(doc, rec, p) != cid:
            raise ShrinkError('This document changed after the preview was made. The page has been reloaded; '
                              'please check the options again.')
        edits, changed = D.metadata_edits(doc, rec, values)
        sizes = None
        if option != 'original':
            st = cands.state(wid, cid)
            opt = (st.get('result') or {}).get(option) or {}
            if st['status'] != 'done' or not opt.get('available'):
                raise ShrinkError('That option is not available for this document.')
            if option == 'text':
                new_pdf, text_info = _build_text_option(cands.text_initial(wid, cid), st, request.form.get('text', ''))
                if not text_info['verification']['identical'] and request.form.get('confirm_text') != '1':
                    raise ShrinkError('The built PDF does not contain exactly the text shown. Tick "I have checked '
                                      'the preview" to save it anyway.')
            elif opt.get('warnings') and request.form.get(f'confirm_{option}') != '1':
                raise ShrinkError('Tick "I have checked the preview" to save an option that has warnings.')
            else:
                text_info = None
                with open(cands.file(wid, cid, option), 'rb') as f:
                    new_pdf = f.read()
            file_edits, old_size, new_size = S.replace_edits(doc, p, new_pdf)
            edits += file_edits
            sizes = (old_size, new_size, text_info)
        return edits, (rec, changed, sizes, p.fmt)

    try:
        rec, changed, sizes, from_fmt = ws.apply(wid, build)
    except (ShrinkError, D.MetadataError) as e:
        flash(str(e), 'danger')
        return redirect(back)
    summary = _summary(rec)
    if 'subject' in changed:
        summary['subject'] = changed['subject'][1]
    change_map = {k: {'old': o, 'new': n} for k, (o, n) in changed.items()}
    if sizes:
        extra = {}
        if sizes[2]:
            texts_dir = os.path.join(ws.state_dir(wid), 'texts')
            os.makedirs(texts_dir, exist_ok=True)
            name = f"{key}.p{part}.{datetime.now():%Y%m%d-%H%M%S}.txt"
            with open(os.path.join(texts_dir, name), 'w', encoding='utf-8') as f:
                f.write(textpdf.clean_text(request.form.get('text', '')))
            sizes[2]['text_file'] = name
        if sizes[2]:
            extra = {k: sizes[2][k] for k in ('engine', 'words', 'edited_words', 'text_file')}
        if from_fmt in imageops.IMAGE_FORMATS:
            extra['converted_from'] = from_fmt
        ws.log(wid, 'shrink', key, {**summary, 'option': option, 'part': part, 'bytes': sizes[0],
                                    'new_bytes': sizes[1], 'changes': change_map, **extra})
        flash(f'Saved "{OPTION_LABELS[option]}": {format_size(sizes[0])} → {format_size(sizes[1])} in the XML'
              + (' and the details.' if changed else '.'), 'success')
    elif changed:
        ws.log(wid, 'metadata', key, {**summary, 'changes': change_map})
        flash('Saved the details. The PDF was not changed.', 'success')
    else:
        flash('Nothing changed.', 'warning')
    if request.form.get('then') == 'next' or request.args.get('then') == 'next':
        nxt = _queue_nav(wid, ws.load(wid), key, part)['next']
        if nxt:
            return redirect(nxt)
        flash('That was the last PDF in the queue.', 'success')
    return redirect(url_for('documents', wid=wid, key=key, part=part))


def _mapper(key):
    """Clinical item keys start with 'c' (e.g. 'cpmh-…'); everything else is a document record."""
    return C.item_map if re.match(r'^c[a-z]+-', key) else D.record_map


@app.route('/w/<wid>/doc/<key>/revert', methods=['POST'])
def revert(wid, key):
    require(wid)
    src = ws.load_source(wid)

    def build(doc):
        return D.revert_edits(doc, src, key, _mapper(key))

    rec = ws.apply(wid, build)
    if rec is None:
        flash('That document is not in the original upload.', 'warning')
    else:
        ws.log(wid, 'revert', key, _summary(rec))
        flash(f'Restored "{rec.subject or rec.category}" to how it is in the original upload.', 'success')
    if request.form.get('back') == 'summary':
        return redirect(url_for('summary', wid=wid) + f'#{getattr(rec, "section", "")}')
    return redirect(url_for('documents', wid=wid, key=key))


# ---- clinical summary and charts ------------------------------------------------------------------------

@app.route('/w/<wid>/summary')
def summary(wid):
    require(wid)
    doc = ws.load(wid)
    status = ws.status_by_key(wid)
    secs = C.sections(doc)
    deleted = {}
    if any(v == 'deleted' for v in status.values()):
        src = ws.load_source(wid)
        src_items, src_docs = C.item_map(src), D.record_map(src)
        present = set(C.item_map(doc)) | set(D.record_map(doc))
        for k, v in status.items():
            if v == 'deleted' and k not in present:
                it = src_items.get(k) or src_docs.get(k)
                if it is not None:
                    deleted.setdefault(getattr(it, 'section', ''), []).append(it)
    recs = D.records(doc)
    return render_template(
        'summary.html', wid=wid, name=os.path.basename(ws.working_path(wid)), schema=D.SCHEMA_LABELS[D.schema_of(doc)],
        patient=D.patient_summary(doc), demographics=C.demographics(doc), sections=secs, deleted=deleted,
        reports=[r for r in recs if r.section == 'reports'], letters=[r for r in recs if r.section == 'letters'],
        documents=[r for r in recs if r.section == 'documents'], status=status,
        working_bytes=len(doc.data), source_bytes=os.path.getsize(ws.source_path(wid)), active_nav='summary')


@app.route('/w/<wid>/items/delete', methods=['POST'])
def delete_items(wid):
    """Delete clinical items and/or document records selected on the summary page."""
    require(wid)
    keys = request.form.getlist('keys')
    if not keys:
        flash('Nothing selected.', 'warning')
        return redirect(url_for('summary', wid=wid))

    def build(doc):
        items = C.item_map(doc)
        edits, removed = D.delete_edits(doc, [k for k in keys if k not in items])
        for k in keys:
            it = items.get(k)
            if it is not None:
                removed.append(it)
                edits.extend(doc.edit_remove(n) for n in it.nodes)
        return edits, removed

    removed = ws.apply(wid, build)
    for it in removed:
        ws.log(wid, 'delete', it.key, _summary(it))
    if request.form.get('back') == 'bulk':
        flash(f'Deleted {len(removed)} duplicate document(s). They can be restored from the document list.', 'success')
        return redirect(url_for('bulk', wid=wid))
    flash(f'Deleted {len(removed)} item(s) from the working copy. They can be restored below.', 'success')
    return redirect(url_for('summary', wid=wid))


# ---- bulk actions -----------------------------------------------------------------------------------------

SAVING_MIN = 0.9          # a bulk option must make the PDF at least 10% smaller in the XML


def _bulk_plan(wid, doc):
    """Classify every PDF for the bulk actions. Only results that need no human judgement are eligible:
    'strip' for text-layer PDFs (text verified identical, no warnings), 'compress' for scans (no warnings,
    searchable layer added), 'imgpdf' for TIFF/BMP scans (same pixels, verified). A TIFF/BMP without colour can be
    in both 'imgpdf' and 'compress': whichever is applied first is used. Text only is never applied in bulk."""
    status = ws.status_by_key(wid)
    plan = {'strip': [], 'compress': [], 'rtfpic': [], 'imgpdf': [], 'pending': 0, 'review': [], 'shrunk': 0, 'total': 0,
            'nothing': 0}
    for rec, part, cid in cands.targets(wid, doc):
        plan['total'] += 1
        if status.get(rec.key) == 'shrunk':
            plan['shrunk'] += 1
            continue
        st = cands.state(wid, cid)
        r = st.get('result')
        if st['status'] in ('none', 'running') or (r and (r.get('text') or {}).get('status') == 'running'):
            plan['pending'] += 1
            continue
        if not r:
            plan['review'].append((rec, part, 'Options could not be produced'))
            continue
        orig = r['original']['xml_bytes']
        if not any(isinstance(r.get(o), dict) and r[o].get('available') and r[o]['xml_bytes'] < orig * SAVING_MIN
                   for o in S.OPTIONS):
            plan['nothing'] += 1                       # already as small as any option would make it
            continue
        entry = lambda opt: {'rec': rec, 'part': part, 'cid': cid, 'old': orig, 'new': r[opt]['xml_bytes']}
        good = lambda o: (o.get('available') and not o.get('warnings') and o['xml_bytes'] < orig * SAVING_MIN)
        if r['kind'] == 'rtf':
            if good(r['rtfpic']) and r['rtfpic']['verification'].get('identical'):
                plan['rtfpic'].append(entry('rtfpic'))
            elif r['rtfpic'].get('available') and r['rtfpic']['xml_bytes'] >= orig * SAVING_MIN:
                pass                                   # pictures already small or vector: nothing to review
            else:
                plan['review'].append((rec, part, 'RTF: check the pictures one by one'))
            continue
        if r.get('source') in imageops.IMAGE_FORMATS:
            placed = False
            if good(r['imgpdf']) and r['imgpdf']['verification'].get('identical'):
                plan['imgpdf'].append(entry('imgpdf'))
                placed = True
            if not (r.get('image') or {}).get('colour') and good(r['compress']):
                plan['compress'].append(entry('compress'))
                placed = True
            if not placed:
                plan['review'].append((rec, part, f"{IMAGE_NAMES[r['source']]}: "
                                       + ('has colour pages: choose an option' if (r.get('image') or {}).get('colour')
                                          else 'has warnings or saves little')))
            continue
        if r['kind'] == 'text' and good(r['strip']) and r['strip']['verification'].get('identical'):
            plan['strip'].append(entry('strip'))
        elif r['kind'] == 'scan' and good(r['compress']):
            plan['compress'].append(entry('compress'))
        else:
            if r['kind'] == 'scan' and (r.get('compress') or {}).get('not_smaller'):
                reason = 'Already a compact scan: only Text only would make it smaller'
            elif r['kind'] == 'text' and (r.get('strip') or {}).get('not_smaller'):
                reason = 'Stripping images would not make it smaller'
            else:
                reason = {'mixed': 'Some pages scanned, some text'}.get(r['kind'], 'Has warnings or saves little')
            plan['review'].append((rec, part, reason))
    for opt in ('strip', 'compress', 'rtfpic', 'imgpdf'):
        plan[opt + '_saving'] = sum(e['old'] - e['new'] for e in plan[opt])
    return plan


@app.route('/w/<wid>/bulk')
def bulk(wid):
    require(wid)
    doc = ws.load(wid)
    plan = _bulk_plan(wid, doc)
    if plan['pending']:
        cands.start_all(wid)
    groups = D.duplicate_groups(doc)
    return render_template('bulk.html', wid=wid, name=os.path.basename(ws.working_path(wid)),
                           patient=D.patient_summary(doc), plan=plan, groups=groups,
                           dup_bytes=sum(r.size for g in groups for r in g[1:]), working_bytes=len(doc.data),
                           source_bytes=os.path.getsize(ws.source_path(wid)), active_nav='bulk',
                           running=cands.all_running(wid))


@app.route('/w/<wid>/bulk/recheck', methods=['POST'])
def bulk_recheck(wid):
    """Re-check: retry failed or interrupted preparations ('all': work out every option again from scratch)."""
    require(wid)
    everything = request.form.get('scope') == 'all'
    n = cands.recheck(wid, everything=everything)
    cands.start_all(wid)
    if everything:
        flash('Recalculating the options for every document; the figures fill in as they are ready.', 'success')
    else:
        flash(f'Re-checking: {n} failed or interrupted preparation(s) will be redone, together with any not yet prepared.'
              if n else 'Re-checking: nothing had failed; any documents not yet prepared are being prepared now.', 'success')
    return redirect(url_for('bulk', wid=wid))


@app.route('/w/<wid>/bulk/progress')
def bulk_progress(wid):
    require(wid)
    plan = _bulk_plan(wid, ws.load(wid))
    if plan['pending'] and not cands.all_running(wid):
        cands.start_all(wid)                    # e.g. the server restarted while preparing
    return jsonify({'pending': plan['pending'], 'total': plan['total'], 'review': len(plan['review']),
                    'shrunk': plan['shrunk'], 'nothing': plan['nothing'],
                    'strip': {'count': len(plan['strip']), 'saving': plan['strip_saving']},
                    'compress': {'count': len(plan['compress']), 'saving': plan['compress_saving']},
                    'rtfpic': {'count': len(plan['rtfpic']), 'saving': plan['rtfpic_saving']},
                    'imgpdf': {'count': len(plan['imgpdf']), 'saving': plan['imgpdf_saving']}})


@app.route('/w/<wid>/bulk/shrink', methods=['POST'])
def bulk_shrink(wid):
    require(wid)
    option = request.form.get('option')
    if option not in ('strip', 'compress', 'rtfpic', 'imgpdf'):
        abort(400, 'Unknown option')
    if option == 'compress' and request.form.get('accept') != '1':
        flash('Tick the box to confirm black-and-white copies of the scans without reviewing each one.', 'warning')
        return redirect(url_for('bulk', wid=wid))

    def build(doc):
        edits, done = [], []
        for e in _bulk_plan(wid, doc)[option]:     # re-checked under the lock against the current working copy
            p = e['part']
            with open(cands.file(wid, e['cid'], option), 'rb') as f:
                file_edits, old_size, new_size = S.replace_edits(doc, p, f.read())
            edits += file_edits
            done.append((e['rec'], p.index, old_size, new_size, p.fmt))
        return edits, done

    done = ws.apply(wid, build)
    for rec, idx, old, new, fmt in done:
        ws.log(wid, 'shrink', rec.key, {**_summary(rec), 'option': option, 'part': idx, 'bytes': old,
                                        'new_bytes': new, 'bulk': True,
                                        **({'converted_from': fmt} if fmt in imageops.IMAGE_FORMATS else {})})
    saved = sum(old - new for _r, _i, old, new, _f in done)
    images = sum(1 for *_x, fmt in done if fmt in imageops.IMAGE_FORMATS)
    flash(f'Applied "{OPTION_LABELS[option]}" to {len(done)} document(s), saving {format_size(saved)}'
          + (f'; {images} of them were TIFF/BMP scans, now PDFs' if images and option != 'imgpdf' else '')
          + '. Each can be restored individually.', 'success')
    return redirect(url_for('bulk', wid=wid))


@app.route('/w/<wid>/charts')
def charts(wid):
    require(wid)
    doc = ws.load(wid)
    return render_template('charts.html', wid=wid, name=os.path.basename(ws.working_path(wid)),
                           patient=D.patient_summary(doc), charts=C.charts(doc), active_nav='charts')


# ---- single-file compress (no XML) -----------------------------------------------------------------------

DOWNLOAD_SUFFIX = {'strip': '_no_images', 'compress': '_compressed', 'text': '_text_only', 'rtfpic': '_compressed',
                   'rtfnopic': '_no_pictures', 'imgpdf': '', 'imgjpeg': '_compressed_colour'}


def _file_meta(fid):
    meta = files.meta(fid)
    if meta is None:
        abort(404, f'This file is no longer here: files are removed {FC.KEEP_HOURS:g} hours after upload, '
                   'or when deleted.')
    return meta


@app.route('/file_compress', methods=['GET', 'POST'])
def file_compress():
    """Compress a single PDF or RTF file and download the result: a convenience, separate from the XML files.
    The upload form and recent files are the start page's second tab."""
    home = url_for('index', tab='file')
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('Choose a PDF, RTF, TIFF or BMP file first.', 'danger')
            return _after_upload(home)
        try:
            fid = files.create(f.filename, f.read())
        except FC.FileError as e:
            flash(str(e), 'danger')
            return _after_upload(home)
        files.start(fid)
        return _after_upload(url_for('file_result', fid=fid))
    return redirect(home)


@app.route('/file_compress/<fid>')
def file_result(fid):
    meta = _file_meta(fid)
    state = files.state(fid, '')
    if state['status'] == 'none':
        files.start(fid)
        state = {'status': 'running'}
    return render_template('file_result.html', fid=fid, meta=meta, state=state, labels=OPTION_LABELS,
                           options=['rtfpic', 'rtfnopic'] if meta['fmt'] == 'rtf' else
                           S.IMAGE_OPTIONS if meta['fmt'] in imageops.IMAGE_FORMATS else S.PDF_OPTIONS,
                           text_initial=files.text_initial(fid, ''), tools=pdfops.tools_available(),
                           keep_hours=FC.KEEP_HOURS)


@app.route('/file_compress/<fid>/status')
def file_status(fid):
    _file_meta(fid)
    return _status_json(files.state(fid, ''))


@app.route('/file_compress/<fid>/start', methods=['POST'])
def file_start(fid):
    _file_meta(fid)
    files.start(fid)
    return redirect(url_for('file_result', fid=fid))


@app.route('/file_compress/<fid>/delete', methods=['POST'])
def file_delete(fid):
    meta = _file_meta(fid)
    files.delete(fid)
    flash(f"Deleted {meta['name']} and its compressed versions.", 'success')
    return redirect(url_for('index', tab='file'))


@app.route('/file_compress/<fid>/file/<option>')
def file_option(fid, option):
    """The original or an option: shown in the page, or as a download with ?download=1."""
    meta = _file_meta(fid)
    if option == 'original':
        path, name = files.original_path(fid, meta), meta['name']
        if meta['fmt'] in imageops.IMAGE_FORMATS and request.args.get('download') != '1':
            # Browsers cannot show a TIFF: the preview is the same pixels as a PDF.
            ready = files.file(fid, '', 'imgpdf')
            if not os.path.exists(ready):
                with open(path, 'rb') as f:
                    return send_file(io.BytesIO(imageops.lossless_pdf(f.read())), mimetype='application/pdf',
                                     download_name=FC.safe_stem(name) + '.pdf', max_age=0)
            return send_file(ready, mimetype='application/pdf', download_name=FC.safe_stem(name) + '.pdf', max_age=0)
    else:
        try:
            path = files.file(fid, '', option)
        except ValueError:
            abort(404)
        if not os.path.exists(path):
            abort(404, 'Not available')
        name = FC.safe_stem(meta['name']) + DOWNLOAD_SUFFIX[option] + os.path.splitext(path)[1]
    rtf = path.endswith('.rtf')
    if request.args.get('download') == '1':
        mime = 'application/rtf' if rtf else 'application/pdf'
        if option == 'original' and meta['fmt'] in imageops.IMAGE_FORMATS:
            mime = 'image/tiff' if meta['fmt'] == 'tif' else 'image/bmp'
        return send_file(path, mimetype=mime, as_attachment=True,
                         download_name=name, max_age=0)
    if rtf:
        from curator.render import rtf_view
        with open(path, 'rb') as f:
            return rtf_view(f.read(), OPTION_LABELS[option])
    return send_file(path, mimetype='application/pdf', download_name=name, max_age=0)


@app.route('/file_compress/<fid>/text', methods=['POST'])
def file_text(fid):
    """The text-only PDF built from the (edited) text: a preview, or a download with ?download=1."""
    meta = _file_meta(fid)
    st = files.state(fid, '')
    if st['status'] != 'done' or not (st['result'].get('text') or {}).get('available'):
        abort(404, 'Text option not available')
    try:
        pdf, info = _build_text_option(files.text_initial(fid, ''), st, request.form.get('text', ''))
    except ShrinkError as e:
        abort(400, str(e))
    download = request.args.get('download') == '1'
    resp = send_file(io.BytesIO(pdf), mimetype='application/pdf', as_attachment=download, max_age=0,
                     download_name=FC.safe_stem(meta['name']) + DOWNLOAD_SUFFIX['text'] + '.pdf')
    resp.headers['X-Xml-Bytes'] = str(len(pdf))
    resp.headers['X-Xml-Saving'] = str(meta['bytes'] - len(pdf))
    resp.headers['X-Text-Identical'] = '1' if info['verification']['identical'] else '0'
    resp.headers['X-Edited-Words'] = str(info['edited_words'])
    return resp


@app.errorhandler(400)
@app.errorhandler(404)
def http_error(e):
    return render_template('error.html', error=e), e.code


if __name__ == '__main__':
    # Development server. CURATOR_HOST=127.0.0.1 makes it reachable from this computer only.
    app.run(host=os.environ.get('CURATOR_HOST', '0.0.0.0'), port=int(os.environ.get('PORT', '5008')),
            debug=os.environ.get('FLASK_DEBUG') == '1')
