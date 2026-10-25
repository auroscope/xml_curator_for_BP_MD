"""The change log of a workspace as a PDF, for filing alongside the curated XML (e.g. importing it into the EHR)."""
import io
from collections import Counter
from datetime import datetime

from .textpdf import _fonts, _western

ACTION_LABELS = {
    'upload': 'Original uploaded', 'delete': 'Deleted', 'revert': 'Restored from original', 'metadata': 'Details edited',
    'shrink': 'Shrunk', 'reset': 'Reset to original', 'save_copy': 'Curated copy saved', 'restore_copy': 'Saved copy resumed',
    'delete_copy': 'Saved copy deleted', 'repair': 'Repaired on upload',
}
OPTION_LABELS = {'strip': 'images stripped (text kept)', 'compress': 'compressed scan', 'text': 'replaced by text transcription',
                 'rtfpic': 'RTF pictures compressed', 'rtfnopic': 'RTF pictures removed',
                 'imgpdf': 'scanned image converted to PDF (same pixels)',
                 'imgjpeg': 'scanned image converted to PDF (grey/colour as JPEG)'}


def _size(n):
    if n is None:
        return ''
    n = int(n)
    return f'{n / 1048576:.1f} MB' if n >= 1048576 else (f'{n / 1024:.0f} KB' if n >= 1024 else f'{n} B')


def _ts(ts):
    try:
        return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')
    except (TypeError, ValueError):
        return ts or ''


def in_effect(entries):
    """[(key, state, latest entry for the key, entries for the key)] for changes still in effect, following resets
    and resumed saved copies the same way the app does."""
    status, history = {}, {}
    all_hist = {}          # per key since its last restore-from-original, across resets (for resumed saved copies)
    for e in entries:
        a, k = e['action'], e.get('key')
        if a == 'reset':
            status.clear(); history.clear()
        elif a == 'restore_copy':
            status = dict(e.get('status') or {})
            history = {key: list(all_hist.get(key, [])) for key in status}
        elif k:
            if a == 'revert':
                status.pop(k, None); history.pop(k, None); all_hist.pop(k, None)
                continue
            history.setdefault(k, []).append(e)
            all_hist.setdefault(k, []).append(e)
            if a == 'delete':
                status[k] = 'deleted'
            elif a == 'shrink':
                status[k] = 'shrunk'
            else:
                status.setdefault(k, 'modified')
    out = []
    for k, state in status.items():
        hist = history.get(k, [])
        latest = next((h for h in reversed(hist) if h.get('subject')), hist[-1] if hist else {})
        out.append((k, state, latest, hist))
    out.sort(key=lambda r: (r[1], r[2].get('subject') or ''))
    return out


def build(entries, info, details):
    """info: name, patient, dob, source_bytes, working_bytes, uploaded. details(entry) -> one-line description."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    texts = [str(v) for e in entries for v in e.values() if isinstance(v, str)] + [str(v) for v in info.values()]
    f = {'Sans': 'Helvetica', 'Sans-Bold': 'Helvetica-Bold'} if _western(''.join(texts)) else _fonts()
    body = ParagraphStyle('b', fontName=f['Sans'], fontSize=9.5, leading=12.5)
    small = ParagraphStyle('s', parent=body, fontSize=8.2, leading=10.4)
    h1 = ParagraphStyle('h1', fontName=f['Sans-Bold'], fontSize=16, leading=20, spaceAfter=4)
    h2 = ParagraphStyle('h2', fontName=f['Sans-Bold'], fontSize=11.5, leading=15, spaceBefore=10, spaceAfter=5)
    P = lambda t, st=body: Paragraph(escape(t or ''), st)
    now = datetime.now()

    effect = in_effect(entries)
    counts = Counter(state for _k, state, _l, _h in effect)
    shrink_by = Counter(l.get('option') for _k, s, l, _h in effect if s == 'shrunk')
    edited_details = sum(1 for _k, s, _l, h in effect if any(e['action'] == 'metadata' or e.get('changes') for e in h))
    transcriptions = [(l, h) for _k, s, l, h in effect if s == 'shrunk' and l.get('option') == 'text']
    words_edited = sum(l.get('edited_words') or 0 for l, _h in transcriptions)

    story = [P('XML curation log', h1)]
    meta = [
        ['Patient', f"{info.get('patient') or '—'}" + (f"   DOB {info['dob']}" if info.get('dob') else '')],
        ['Original file', f"{info['name']}   ({_size(info.get('unrepaired_bytes') or info['source_bytes'])}, "
                          f"uploaded {info.get('uploaded') or '—'})"
                          + (f"; not well-formed XML, repaired on upload ({_size(info['source_bytes'])})"
                             if info.get('unrepaired_bytes') else '')],
        ['Curated file', f"{info['curated_name']}   ({_size(info['working_bytes'])}"
                         + (f", {_size(info['source_bytes'] - info['working_bytes'])} smaller" if info['source_bytes'] > info['working_bytes'] else '')
                         + ')'],
        ['Log produced', now.strftime('%d/%m/%Y %H:%M') + ' by xml_curator_for_BP_MD'],
    ]
    t = Table([[P(a, small), P(b)] for a, b in meta], colWidths=[30 * mm, None])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#5b6670')),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 2), ('TOPPADDING', (0, 0), (-1, -1), 1)]))
    story += [t, Spacer(1, 4)]

    story.append(P('Summary of changes in effect', h2))
    lines = []
    repairs = [e for e in entries if e['action'] == 'repair']
    if repairs:
        guessed = [e for e in repairs if e.get('tier') == 'guess']
        lines.append(f"The uploaded file was not well-formed XML and was repaired before curation ({len(repairs)} kind(s) "
                     "of repair, listed in the full history)."
                     + (f" {len(guessed)} of them were best guesses: the records they changed were to be checked."
                        if guessed else ''))
    if not effect:
        lines.append('No changes: the curated file is identical to the ' + ('repaired upload.' if repairs else 'original.'))
    if counts['deleted']:
        lines.append(f"{counts['deleted']} document(s) or item(s) deleted.")
    for opt, n in shrink_by.items():
        lines.append(f"{n} document(s): {OPTION_LABELS.get(opt, opt)}.")
    if edited_details:
        lines.append(f"{edited_details} document(s) with edited details (date, sender, recipient, category or subject).")
    if transcriptions:
        lines.append(f"{len(transcriptions)} document(s) replaced by a text transcription (images removed); "
                     f"{words_edited} word(s) were edited after OCR/extraction. Each transcription carries a notice on every page.")
    lines.append('Everything in the file not listed here is unchanged from the original.')
    for line in lines:
        story.append(Paragraph('•&nbsp;&nbsp;' + escape(line), body))

    if effect:
        story.append(P('Changes in effect', h2))
        rows = [[P('Document / item', small), P('Date', small), P('Change', small), P('Size', small)]]
        for _k, state, latest, hist in effect:
            change = []
            for e in hist:
                d = details(e)
                if e['action'] == 'delete':
                    change.append('Deleted')
                elif e['action'] == 'shrink':
                    change.append(f"Shrunk: {OPTION_LABELS.get(e.get('option'), e.get('option'))}"
                                  + (f", {e['edited_words']} word(s) edited" if e.get('option') == 'text' and e.get('edited_words') else ''))
                    if e.get('changes'):
                        change.append('Details: ' + '; '.join(f"{k}: {v['old'] or '(empty)'} → {v['new'] or '(empty)'}" for k, v in e['changes'].items()))
                elif e['action'] == 'metadata':
                    change.append('Details: ' + '; '.join(f"{k}: {v['old'] or '(empty)'} → {v['new'] or '(empty)'}" for k, v in (e.get('changes') or {}).items()))
                elif d:
                    change.append(d)
            size = ''
            sh = [e for e in hist if e['action'] == 'shrink']
            if sh:
                size = f"{_size(sh[0].get('bytes'))} → {_size(sh[-1].get('new_bytes'))}"
            elif state == 'deleted':
                size = _size(latest.get('bytes'))
            label = (latest.get('subject') or latest.get('key') or '') + (f" ({latest['category']})" if latest.get('category') else '')
            rows.append([P(label, small), P(latest.get('date') or '', small),
                         Paragraph('<br/>'.join(escape(c) for c in change) or escape(state), small), P(size, small)])
        t = Table(rows, colWidths=[58 * mm, 20 * mm, 63 * mm, 33 * mm], repeatRows=1)
        t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.HexColor('#9aa5b1')),
                               ('LINEBELOW', (0, 1), (-1, -1), 0.25, colors.HexColor('#dde3e9')),
                               ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef2f6'))]))
        story.append(t)

    story.append(P('Full history', h2))
    rows = [[P('When', small), P('Action', small), P('Document / item', small), P('Details', small)]]
    for e in entries:
        label = e.get('subject') or e.get('name') or e.get('file') or ''
        rows.append([P(_ts(e.get('ts')), small), P(ACTION_LABELS.get(e['action'], e['action']), small),
                     P(label, small), P(details(e) or (e.get('file') or ''), small)])
    t = Table(rows, colWidths=[26 * mm, 30 * mm, 52 * mm, 66 * mm], repeatRows=1)
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.HexColor('#9aa5b1')),
                           ('LINEBELOW', (0, 1), (-1, -1), 0.25, colors.HexColor('#dde3e9')),
                           ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef2f6'))]))
    story.append(t)

    footer = f"XML curation log · {info['curated_name']} · produced {now.strftime('%d/%m/%Y %H:%M')}"

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(f['Sans'], 7.5)
        canvas.setFillColor(colors.HexColor('#5b6670'))
        canvas.drawString(18 * mm, 10 * mm, footer)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f'Page {doc.page}')
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title='XML curation log', subject=info['curated_name'],
                            creator='xml_curator_for_BP_MD', author='')
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
