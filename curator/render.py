"""Turn a document part into something a browser can show."""
import html
import io

from flask import Response, current_app, render_template, request, send_file, url_for
from PIL import Image
from striprtf.striprtf import rtf_to_text

from .documents import part_bytes, sniff
from .msgfile import MsgError, read_msg

MIMETYPES = {
    'pdf': 'application/pdf', 'jpg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'bmp': 'image/bmp',
    'txt': 'text/plain; charset=utf-8',
}


def text_to_html(text):
    """Plain text to HTML. Runs of lines with two or more '|' become a table (as in BPSXMLViewer)."""
    out, in_table = [], False
    for line in (text or '').split('\n'):
        if line.count('|') >= 2:
            if not in_table:
                out.append('<table class="rtf-table">')
                in_table = True
            cells = [c.strip() for c in line.split('|')]
            if cells and not cells[0]:
                cells = cells[1:]
            if cells and not cells[-1]:
                cells = cells[:-1]
            out.append('<tr>' + ''.join(f'<td>{html.escape(c)}</td>' for c in cells) + '</tr>')
        else:
            if in_table:
                out.append('</table>')
                in_table = False
            out.append(f'<div>{html.escape(line)}</div>' if line.strip() else '<br>')
    if in_table:
        out.append('</table>')
    return '\n'.join(out)


def _rtf_plain(rtf):
    try:
        return rtf_to_text(rtf, errors='ignore')
    except Exception:
        return rtf


HTML_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"


def _pipe_tables_only(rtf_bytes):
    """Text-style reports (lab results laid out with '|' separators) with no pictures and no real RTF tables."""
    if b'\\pict' in rtf_bytes or b'\\trowd' in rtf_bytes:
        return False
    text = _rtf_plain(rtf_bytes.decode('utf-8', errors='ignore'))
    return any(line.count('|') >= 2 for line in text.split('\n'))


def rtf_view(rtf_bytes, title, ext='rtf'):
    """An RTF shown as LibreOffice renders it: HTML with its pictures, or a PDF page view with ?view=pdf.
    Falls back to the plain-text view when LibreOffice is unavailable or fails."""
    from .officeconv import OfficeError
    conv = current_app.extensions.get('office')
    fmt = 'pdf' if request.args.get('view') == 'pdf' else 'html'
    if fmt == 'html' and _pipe_tables_only(rtf_bytes):
        conv = None                                      # the text view turns '|' tables into real tables
    if conv is not None:
        try:
            out = conv.convert(rtf_bytes, fmt, ext)
            if fmt == 'pdf':
                return send_file(io.BytesIO(out), mimetype='application/pdf', download_name='page-view.pdf')
            resp = Response(out, mimetype='text/html')
            resp.headers['Content-Security-Policy'] = HTML_CSP
            return resp
        except OfficeError:
            pass
    return _text_page(title, text_to_html(_rtf_plain(rtf_bytes.decode('utf-8', errors='ignore'))))


def _text_page(title, body_html):
    return render_template('text_document.html', title=title, content=body_html)


FORMAT_NAMES = {
    'msg': 'Outlook email (.msg)', 'zip': 'ZIP / Office document', 'doc': 'Word document', 'docx': 'Word document',
    'xls': 'Excel spreadsheet', 'xlsx': 'Excel spreadsheet', 'bin': 'binary file',
}


def _unsupported(rec, name, data, fmt, reason=''):
    """Shown instead of a file the browser cannot display, so opening a document never starts a download."""
    return render_template('unsupported.html', title=rec.subject or rec.category, name=name, size=len(data),
                           kind=FORMAT_NAMES.get(fmt, (fmt or 'unknown').upper() + ' file'), reason=reason,
                           download_url=request.path + '?download=1')


def render_email(rec, data, attachment_url):
    msg = read_msg(data)
    for i, a in enumerate(msg['attachments']):
        a['url'] = attachment_url(i)
    return render_template('email.html', title=msg['subject'] or rec.subject, msg=msg)


def render_attachment(rec, data, index):
    """One attachment of an Outlook email, shown like any other document part."""
    msg = read_msg(data)
    if index >= len(msg['attachments']) or not msg['attachments'][index]['data']:
        return None
    a = msg['attachments'][index]
    return _show_bytes(rec, a['name'], a['data'], sniff(a['data'][:64]) or a['name'].rsplit('.', 1)[-1].lower())


def render_part(doc, rec, part):
    if part.encoding == 'report':
        header = _rtf_plain(doc.child_text(part.node, 'REPORTHEADER', strip=False))
        body = _rtf_plain(doc.child_text(part.node, 'REPORTBODY', strip=False))
        content = text_to_html(header)
        if header.strip() and body.strip():
            content += '<hr>'
        content += text_to_html(body)
        return _text_page(rec.subject, content)

    if part.encoding == 'mdreport':
        header = doc.child_text(part.node, 'REPORTHEAD', strip=False)
        body = _rtf_plain(doc.child_text(part.node, 'REPORTTEXT', strip=False))
        content = text_to_html(header)
        if header.strip() and body.strip():
            content += '<hr>'
        content += text_to_html(body)
        return _text_page(rec.subject, content)

    name, data, fmt = part_bytes(doc, part)
    if request.args.get('download') == '1':
        return send_file(io.BytesIO(data), mimetype='application/octet-stream', as_attachment=True, download_name=name)

    if fmt == 'msg':
        try:
            return render_email(rec, data, lambda i: url_for('view_attachment', wid=request.view_args['wid'],
                                                            key=rec.key, part=part.index, index=i))
        except MsgError as e:
            return _unsupported(rec, name, data, fmt, str(e))
    return _show_bytes(rec, name, data, fmt)


def _show_bytes(rec, name, data, fmt):
    if request.args.get('download') == '1':
        return send_file(io.BytesIO(data), mimetype='application/octet-stream', as_attachment=True, download_name=name)

    if fmt == 'rtf':
        return rtf_view(data, rec.subject)

    if fmt == 'tif':
        try:
            img = Image.open(io.BytesIO(data))
            frames = []
            for i in range(getattr(img, 'n_frames', 1)):
                img.seek(i)
                frames.append(img.convert('RGB') if img.mode not in ('RGB', 'L', '1') else img.copy())
            out = io.BytesIO()
            if len(frames) > 1:                      # multi-page TIF: show every page as a PDF
                frames[0].save(out, 'PDF', save_all=True, append_images=frames[1:], resolution=150)
                mimetype = 'application/pdf'
            else:
                frames[0].save(out, 'PNG')
                mimetype = 'image/png'
            out.seek(0)
            return send_file(out, mimetype=mimetype, download_name=name.rsplit('.', 1)[0] + (
                '.pdf' if mimetype == 'application/pdf' else '.png'))
        except Exception:
            pass

    if fmt == 'html':
        # Attached HTML is shown with scripts, forms and plugins disabled.
        resp = Response(data, mimetype='text/html')
        resp.headers['Content-Security-Policy'] = HTML_CSP
        return resp

    if fmt in MIMETYPES:
        return send_file(io.BytesIO(data), mimetype=MIMETYPES[fmt], download_name=name)

    if fmt == 'msg':
        try:
            return render_template('email.html', title=name, msg=read_msg(data), nested=True)
        except MsgError:
            pass
    return _unsupported(rec, name, data, fmt)
