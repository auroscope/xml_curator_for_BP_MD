"""Read Outlook .msg files (OLE compound documents) well enough to show them: headers, body and attachments.

Properties live in streams named __substg1.0_<TAG><TYPE> (001F = UTF-16 text, 001E = 8-bit text,
0102 = binary); dates are fixed-size values in the __properties_version1.0 stream. Uses olefile (BSD).
"""
import datetime
import html
import io
import re
import struct

import olefile

PROPS = {
    'subject': '0037', 'sender_name': '0C1A', 'sender_email': '0C1F', 'sender_smtp': '5D01',
    'to': '0E04', 'cc': '0E03', 'bcc': '0E02', 'body': '1000', 'html': '1013', 'headers': '007D',
}
ATTACH = {'long_name': '3707', 'name': '3704', 'display': '3001', 'mime': '370E'}
DATE_TAGS = (0x0039, 0x0E06)        # PR_CLIENT_SUBMIT_TIME, PR_MESSAGE_DELIVERY_TIME


class MsgError(Exception):
    pass


def _read(ole, path):
    try:
        return ole.openstream(path).read()
    except OSError:
        return None


def _text(ole, prefix, tag):
    raw = _read(ole, f'{prefix}__substg1.0_{tag}001F')
    if raw is not None:
        return raw.decode('utf-16-le', errors='replace').rstrip('\x00')
    raw = _read(ole, f'{prefix}__substg1.0_{tag}001E')
    if raw is not None:
        return raw.decode('cp1252', errors='replace').rstrip('\x00')
    return ''


def _date(ole):
    raw = _read(ole, '__properties_version1.0')
    if not raw:
        return None
    for off in range(32, len(raw) - 15, 16):            # top-level message: 32-byte header, 16-byte entries
        ptype, pid = struct.unpack_from('<HH', raw, off)
        if ptype == 0x0040 and pid in DATE_TAGS:
            ft = struct.unpack_from('<Q', raw, off + 8)[0]
            if ft:
                return datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ft // 10)
    return None


def _html_to_text(markup):
    markup = re.sub(r'(?is)<(script|style|head)[^>]*>.*?</\1>', '', markup)
    markup = re.sub(r'(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>', '\n', markup)
    text = html.unescape(re.sub(r'<[^>]+>', '', markup))
    return re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t]+\n', '\n', text)).strip()


def read_msg(data):
    """{'subject', 'from', 'to', 'cc', 'date', 'body', 'attachments': [{'name', 'size', 'mime', 'data'}]}"""
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except (OSError, ValueError) as e:
        raise MsgError(f'not an Outlook message: {e}')
    with ole:
        t = {k: _text(ole, '', v) for k, v in PROPS.items() if k != 'html'}
        body = t['body'].strip()
        if not body:
            raw = _read(ole, '__substg1.0_10130102')
            markup = raw.decode('utf-8', errors='replace') if raw else _text(ole, '', '1013')
            body = _html_to_text(markup) if markup else ''
        sender = t['sender_name']
        email = t['sender_smtp'] or t['sender_email']
        if email and '@' in email and email.lower() not in sender.lower():
            sender = f'{sender} <{email}>' if sender else email
        attachments = []
        for entry in sorted({e[0] for e in ole.listdir() if e[0].startswith('__attach_version1.0_')}):
            prefix = entry + '/'
            name = _text(ole, prefix, ATTACH['long_name']) or _text(ole, prefix, ATTACH['name']) \
                or _text(ole, prefix, ATTACH['display']) or 'attachment'
            payload = _read(ole, f'{prefix}__substg1.0_37010102')
            attachments.append({'name': name, 'mime': _text(ole, prefix, ATTACH['mime']),
                                'size': len(payload) if payload else 0, 'data': payload})
        when = _date(ole)
        return {'subject': t['subject'], 'from': sender, 'to': t['to'], 'cc': t['cc'],
                'date': when.strftime('%d/%m/%Y %H:%M') + ' UTC' if when else '',
                'body': body, 'attachments': attachments}
