"""Health check and repair for uploads that are not well-formed XML.

Only used when an upload fails to parse. The checks run in two tiers:

    safe    one obvious correct answer: invalid UTF-8 bytes (read as Windows-1252), a bare & or < in text,
            HTML entities XML does not define (&nbsp; -> &#160;), a file cut off part-way (the half-written
            last record is dropped and the open sections are closed), and
            a tag that lost its "<" or ">" where the element's name shows where it goes ('<A2020</A>',
            '</A<B>', 'A>x</A>', '<A>x/A>')
    guess   last resort, only when the safe repairs are not enough: an end tag that lost its "/" ('<A>x<A>'),
            an element that was never closed (closed where its own text ends, else just before its parent closes),
            a closing tag with no opening tag (removed), and content after the end of the export (removed)

The uploaded bytes are never changed: check() returns a report and, when the repairs succeed, a repaired copy.
"""
import codecs
import html.entities
import re
import threading
import xml.parsers.expat as expat

from .xmlstore import _CTRL, XmlDoc, detect_encoding

MAX_STRUCTURAL = 25          # structural repairs (each needs a full re-parse) before giving up
MAX_EXAMPLES = 5             # examples kept per kind of safe repair
RECORD_DEPTH = 2             # root > section > record (BP CorrespondenceIn/Document, MD DOCUMENTS2/DOCUMENT)
DEMOGRAPHICS = 'demographics'   # BP <Demographics>, MD <DEMOGRAPHICS>: a section of the root in both formats

_TRUNCATED = {expat.errors.codes[m] for m in (expat.errors.XML_ERROR_NO_ELEMENTS, expat.errors.XML_ERROR_UNCLOSED_TOKEN,
                                               expat.errors.XML_ERROR_PARTIAL_CHAR,
                                               expat.errors.XML_ERROR_UNCLOSED_CDATA_SECTION)}
_MISMATCH = expat.errors.codes[expat.errors.XML_ERROR_TAG_MISMATCH]

# Markup that text fixes must leave alone; everything between these is text.
_MARKUP = re.compile(rb'<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|<![^<>]*>|</?[A-Za-z_:][^<>]*>', re.S)
_AMP = re.compile(rb'&(?:(#x[0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);)?')
_XML_ENTITIES = {b'lt', b'gt', b'amp', b'quot', b'apos'}
_END_TAG = re.compile(rb'</\s*([^\s>]+)\s*>')


class _Open:
    __slots__ = ('tag', 'start', 'stag_end', 'first_child', 'kids')

    def __init__(self, tag, start, stag_end):
        self.tag, self.start, self.stag_end, self.first_child = tag, start, stag_end, None
        self.kids = {}                         # tag -> how many direct children so far


class _Failure:
    """Where a parse stopped: expat's error plus the elements that were still open."""

    def __init__(self, code, message, pos, stack, last_end, root_closed):
        self.code, self.message, self.pos = code, message, pos
        self.stack, self.last_end, self.root_closed = stack, last_end, root_closed


def _parse(buf):
    """None when buf is well-formed, else a _Failure. Control characters are tolerated as in xmlstore."""
    buf = buf.translate(_CTRL)
    parser = expat.ParserCreate()
    stack, state = [], {'last_end': 0, 'root_closed': False}

    def start(name, attrs):
        i = parser.CurrentByteIndex
        if stack:
            if stack[-1].first_child is None:
                stack[-1].first_child = i
            stack[-1].kids[name] = stack[-1].kids.get(name, 0) + 1
        end = buf.find(b'>', i)
        stack.append(_Open(name, i, end + 1 if end >= 0 else len(buf)))

    def end(name):
        stack.pop()
        gt = buf.find(b'>', parser.CurrentByteIndex)     # missing when the file stops inside this end tag
        state['last_end'] = gt + 1 if gt >= 0 else len(buf)
        if not stack:
            state['root_closed'] = True

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(buf, True)
        return None
    except expat.ExpatError as e:
        return _Failure(e.code, expat.errors.messages[e.code], parser.ErrorByteIndex, stack, state['last_end'],
                        state['root_closed'])


def _where(buf, pos, encoding, width=60):
    """Line number and the text around a byte position, for showing in the report."""
    pos = max(0, min(pos, len(buf)))
    line = buf.count(b'\n', 0, pos) + 1
    ls = buf.rfind(b'\n', 0, pos) + 1
    le = buf.find(b'\n', pos)
    le = len(buf) if le < 0 else le
    dec = lambda b: b.decode(encoding, errors='replace').replace('\r', '').translate(_SHOW)
    before, after = buf[max(ls, pos - width):pos], buf[pos:min(le, pos + width)]
    return {'line': line, 'before': ('…' if pos - width > ls else '') + dec(before),
            'after': dec(after) + ('…' if pos + width < le else '')}


_SHOW = {c: ' ' for c in range(32)}


class _Kind:
    """One kind of repair: how many times it was applied and a few examples."""

    def __init__(self, tier, kind, message):
        self.d = {'tier': tier, 'kind': kind, 'message': message, 'count': 0, 'examples': []}

    def add(self, example):
        """example: a dict, or a function making one (only called for the examples that are kept)."""
        self.d['count'] += 1
        if len(self.d['examples']) < MAX_EXAMPLES:
            self.d['examples'].append(example() if callable(example) else example)


# ---- safe repairs ----------------------------------------------------------------------------------------

_bad_bytes = threading.local()


def _cp1252_bytes(err):
    """codecs error handler: each byte that is not valid UTF-8, read as Windows-1252 instead."""
    _bad_bytes.positions.append(err.start)
    bad = err.object[err.start:err.end]
    return bad.decode('cp1252', errors='replace'), err.end


codecs.register_error('curator-cp1252', _cp1252_bytes)


def _fix_encoding(buf, encoding, kinds):
    if encoding.replace('-', '').replace('_', '') not in ('utf8', 'utf8bom'):
        return buf
    try:
        buf.decode('utf-8')
        return buf
    except UnicodeDecodeError:
        pass
    k = _Kind('safe', 'encoding', 'Characters that are not valid UTF-8 (the file says it is UTF-8) were read as '
                                  'Windows-1252, e.g. é or a curly quote saved by an older program.')
    _bad_bytes.positions = []
    fixed = buf.decode('utf-8', errors='curator-cp1252').encode('utf-8')
    for pos in _bad_bytes.positions:
        k.add(lambda: _where(buf, pos, 'cp1252'))
    kinds.append(k)
    return fixed


def _fix_text(buf, encoding, kinds):
    """Escape a bare & or < in text, and turn HTML-only entities into character references."""
    amp = _Kind('safe', 'ampersand', 'A bare "&" in text was written as "&amp;".')
    ent = _Kind('safe', 'entity', 'An HTML entity that XML does not know (e.g. &nbsp;) was written as a character '
                                  'reference (&#160;), which shows the same character.')
    lt = _Kind('safe', 'less-than', 'A "<" in text that does not start a tag (e.g. "< 5") was written as "&lt;".')
    out, pos, n = [], 0, len(buf)

    def text(seg, base):
        res, i = [], 0
        for m in _AMP.finditer(seg):
            name = m.group(1)
            if name is not None and (name.startswith(b'#') or name in _XML_ENTITIES):
                continue
            res.append(seg[i:m.start()])
            if name is not None and name.decode() in html.entities.name2codepoint:
                res.append(b'&#%d;' % html.entities.name2codepoint[name.decode()])
                ent.add(lambda: _where(buf, base + m.start(), encoding))
            else:
                res.append(b'&amp;' + seg[m.start() + 1:m.end()])
                amp.add(lambda: _where(buf, base + m.start(), encoding))
            i = m.end()
        res.append(seg[i:])
        return b''.join(res)

    while pos < n:
        m = _MARKUP.search(buf, pos)
        stop = m.start() if m else n
        seg = buf[pos:stop]
        if b'<' in seg:                       # a '<' that no markup pattern matched
            parts, i = [], 0
            for j in (lt_m.start() for lt_m in re.finditer(rb'<', seg)):
                nxt = seg[j + 1:j + 2]
                if not nxt or nxt.isalpha() or nxt in b'_:/!?':
                    continue                  # looks like a tag (e.g. cut off at the end): left for the structure checks
                parts.append(text(seg[i:j], pos + i))
                parts.append(b'&lt;')
                lt.add(lambda: _where(buf, pos + j, encoding))
                i = j + 1
            parts.append(text(seg[i:], pos + i))
            out.append(b''.join(parts))
        elif b'&' in seg:
            out.append(text(seg, pos))
        else:
            out.append(seg)
        if not m:
            break
        out.append(m.group(0))
        pos = m.end()
    kinds.extend(k for k in (amp, ent, lt) if k.d['count'])
    return b''.join(out)


# ---- structural repairs ----------------------------------------------------------------------------------

def _record_label(buf, el, encoding):
    """A short description of a record that was dropped or changed: its tag and the start of its content."""
    peek = buf[el.stag_end:el.stag_end + 600]
    text = re.sub(rb'<[^>]*>', b' ', peek)
    text = re.sub(rb'\s+', b' ', text).strip()[:120]
    return f'<{el.tag}>' + (f' starting "{text.decode(encoding, errors="replace")}…"' if text else '')


def _truncation(buf, f, newline, encoding):
    """(repaired bytes, info), or (None, reason) when the cut-off file does not hold all of the patient's
    demographics: a copy without them, or with only some of them, must not be imported."""
    inside = any(e.tag.lower() == DEMOGRAPHICS for e in f.stack[1:])
    if inside or not any(t.lower() == DEMOGRAPHICS for t in f.stack[0].kids):
        why = ('the file stops part-way through the patient\'s demographics' if inside else
               'the file stops before the patient\'s demographics')
        return None, {'reason': 'demographics', **_where(buf, len(buf), encoding),
                      'message': f'{why}, so a repaired copy would have {"only some" if inside else "none"} of the '
                                 'patient\'s details (name, date of birth, Medicare number, address …). Importing it '
                                 'could create an incomplete patient, or attach the record to the wrong one'}
    keep = f.stack[:RECORD_DEPTH]
    if len(f.stack) > RECORD_DEPTH:
        rec = f.stack[RECORD_DEPTH]
        cut = rec.start
        dropped = _record_label(buf, rec, encoding)
    else:
        cut = max(f.last_end, f.stack[-1].stag_end)
        dropped = None
    closers = newline.join(f'</{e.tag}>'.encode() for e in reversed(keep))
    head = buf[:cut].rstrip(b' \t\r\n')
    fixed = head + newline + closers + newline
    info = {'where': _where(buf, cut, encoding), 'dropped_bytes': len(buf) - cut, 'record': dropped,
            'closed': [e.tag for e in reversed(keep)]}
    return fixed, info


_NAME = re.compile(rb'[A-Za-z_:][A-Za-z0-9_.:-]*')
_ATTRS = re.compile(rb'(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*\s*=\s*(?:"[^"<]*"|\'[^\'<]*\'))*')
_INVALID_TOKEN = expat.errors.codes[expat.errors.XML_ERROR_INVALID_TOKEN]


def _delimiter(tag, missing, buf, pos, encoding):
    return {**_where(buf, pos, encoding), 'element': tag, 'missing': missing}


def _broken_tag(buf, f, encoding):
    """A tag that lost its closing '>': '<LASTDATE2020-03-04</LASTDATE>', '<Practice<PracticeName>', '<X /<Y>' or
    '</LASTDATE<NEXT>'. The '>' goes back only where the element's own end tag, the open element, or the next tag
    shows exactly where it belongs."""
    for lt in {buf.rfind(b'<', 0, f.pos), f.pos if buf[f.pos:f.pos + 1] == b'<' else -1}:
        if lt < 0:
            continue
        nxt = buf.find(b'<', lt + 1)
        nxt = len(buf) if nxt < 0 else nxt
        if b'>' in buf[lt:nxt]:
            continue                           # this tag is closed: not the broken one
        if buf[lt + 1:lt + 2] == b'/':
            m = _NAME.match(buf, lt + 2)
            if m and f.stack and m.group(0).decode('ascii', 'replace') == f.stack[-1].tag and not buf[m.end():nxt].strip():
                return ('safe', 'delimiter', buf[:m.end()] + b'>' + buf[m.end():],
                        _delimiter(f.stack[-1].tag, '>', buf, m.end(), encoding))
            continue
        m = _NAME.match(buf, lt + 1)
        if not m:
            continue
        attrs = _ATTRS.match(buf, m.end()).end()
        rest = buf[attrs:nxt]
        end = _END_TAG.match(buf, nxt)
        name = end.group(1) if end else b''
        if attrs > m.end() and not rest.strip():
            at = attrs                                     # '<PATIENT VERSION="3.2"<DEMOGRAPHICS>'
        elif attrs > m.end():
            continue
        elif rest.strip() == b'/':
            at = buf.rindex(b'/', 0, nxt) + 1             # '<MIDDLENAME /<NEXT>'
        elif name and m.group(0).startswith(name):
            at = lt + 1 + len(name)                        # '<DOB01/02/1950</DOB>': its own end tag follows
        elif not rest.strip():
            at = m.end()                                   # '<Practice<PracticeName>': the name ends at the next tag
        else:
            continue
        tag = buf[lt + 1:at].rstrip(b' /').decode('ascii', 'replace')
        return ('safe', 'delimiter', buf[:at] + b'>' + buf[at:], _delimiter(tag, '>', buf, at, encoding))
    return None


def _text_at(buf, i):
    """True when buf[i] starts text right after a tag or whitespace (not part of a tag name)."""
    return i > 0 and (buf[i - 1:i] == b'>' or buf[i - 1:i].isspace())


def _structural(buf, f, newline, encoding):
    """One structural repair for failure f: (tier, kind, new buf, example) or None when there is no safe guess."""
    if f.root_closed and f.last_end:
        # Something after the export's last closing tag.
        keep = re.compile(rb'[ \t]*\r?\n?').match(buf, f.last_end).end()      # the line break after it stays
        return ('guess', 'trailing', buf[:keep],
                {**_where(buf, keep, encoding), 'dropped_bytes': len(buf) - keep})
    if f.stack and all(e.tag == f.stack[0].tag for e in f.stack) and len(f.stack) <= 2:
        # '…</NextOfKin>/BPSEHRV2>', '</BPSEHRV2' or '<BPSEHRV2>' at the very end: the export's last tag is damaged.
        root = f.stack[0].tag.encode()
        at = max(f.last_end, f.stack[-1].stag_end if len(f.stack) == 1 else f.stack[-1].start)
        m = re.compile(rb'\s*').match(buf, at)
        tail = buf[m.end():].rstrip()
        if tail in (b'/' + root + b'>', b'</' + root, b'<' + root + b'>', b'</' + root + b'>', b''):
            if len(f.stack) == 1 and not tail:
                pass                                       # simply cut off: the truncation repair below
            else:
                fixed = buf[:m.end()] + b'</' + root + b'>' + buf[m.end() + len(tail):]
                return ('safe', 'delimiter', fixed, _delimiter(f.stack[0].tag, '"<", ">" or "/"', buf, m.end(), encoding))
    if f.code in _TRUNCATED and f.stack:
        fixed, info = _truncation(buf, f, newline, encoding)
        return ('safe', 'truncated', fixed, info) if fixed is not None else ('refuse', 'truncated', None, info)
    if not f.stack and not f.root_closed:
        m = _NAME.match(buf, f.pos)
        if m and buf[_ATTRS.match(buf, m.end()).end():].lstrip()[:1] == b'>':
            # 'BPSEHRV2>…' before any element: the export's first tag lost its '<'.
            return ('safe', 'delimiter', buf[:f.pos] + b'<' + buf[f.pos:],
                    _delimiter(m.group(0).decode('ascii', 'replace'), '<', buf, f.pos, encoding))
    if f.code == _INVALID_TOKEN or not f.stack:
        return _broken_tag(buf, f, encoding)
    if f.code == _MISMATCH and f.stack:
        at = buf.rfind(b'</', 0, f.pos + 1)
        m = _END_TAG.match(buf, at) if at >= 0 else None
        if not m:
            return None
        name = m.group(1).decode('ascii', errors='replace')
        top = f.stack[-1]
        if any(e.tag == name for e in f.stack[:-1]):
            # <top> was never closed. If it holds text and then elements, it was meant to hold only the text.
            mixed = top.first_child is not None and buf[top.stag_end:top.first_child].strip()
            pos = top.first_child if mixed else at
            for el in reversed(f.stack[1:]):
                # '<A>x/A>' or '…</B>/A><C>': an end tag that lost its '<' (as text inside the element).
                lost_lt = b'/' + el.tag.encode() + b'>'
                i = buf.find(lost_lt, el.stag_end, at)
                while i >= 0 and buf[i - 1:i] == b'<':
                    i = buf.find(lost_lt, i + 1, at)
                if i >= 0:
                    return ('safe', 'delimiter', buf[:i] + b'<' + buf[i:], _delimiter(el.tag, '<', buf, i, encoding))
            for outer, inner in zip(f.stack, f.stack[1:]):
                # '<A>x<A>' or '<A><B/><A>': an end tag that lost its '/'. Not '<A><A>' (BP nests records in a
                # section of the same name), and not when <A> already held an <A> (that is a list of records).
                text_first = inner.start == outer.first_child and buf[outer.stag_end:inner.start].strip()
                after_others = inner.start != outer.first_child
                if outer.tag == inner.tag and outer.kids.get(inner.tag) == 1 and (text_first or after_others):
                    return ('guess', 'slash', buf[:inner.start] + b'</' + buf[inner.start + 1:],
                            _delimiter(inner.tag, '/', buf, inner.start + 1, encoding))
            ex = {**_where(buf, top.start, encoding), 'element': top.tag, 'closed_at': _where(buf, pos, encoding),
                  'parent': name, 'how': 'after its text' if mixed else f'just before </{name}>'}
            return ('guess', 'unclosed', buf[:pos] + f'</{top.tag}>'.encode() + buf[pos:], ex)
        tag = name.encode()
        i = buf.rfind(tag + b'>', top.stag_end, at)
        while i >= 0 and not _text_at(buf, i):
            i = buf.rfind(tag + b'>', top.stag_end, i)
        if i >= 0:
            # 'LASTDATE>x</LASTDATE>': the start tag is there but lost its '<'.
            return ('safe', 'delimiter', buf[:i] + b'<' + buf[i:], _delimiter(name, '<', buf, i, encoding))
        ex = {**_where(buf, at, encoding), 'element': name, 'open': top.tag}
        return ('guess', 'stray', buf[:at] + buf[m.end():], ex)
    return None


_MESSAGES = {
    'truncated': 'The file stops part-way through (the export or the copy was cut off). The half-written last record '
                 'was dropped and the open sections were closed.',
    'delimiter': 'A tag had lost a "<" or ">". It was put back where the element\'s name shows it belongs.',
    'slash': 'A closing tag had lost its "/" (it read <A> where </A> was meant), and the "/" was put back.',
    'unclosed': 'An element was never closed. It was closed where it most likely ended.',
    'stray': 'A closing tag had no matching opening tag, and was removed.',
    'trailing': 'There was more content after the end of the export, and it was removed.',
}


def _describe(f, buf, encoding):
    msg = {
        'mismatched tag': 'a closing tag does not match the element that is open there',
        'no element found': 'the file is empty or ends before the export does',
        'unclosed token': 'the file ends in the middle of a tag',
        'not well-formed (invalid token)': 'a character or tag is not allowed at this point',
        'undefined entity': 'an "&…;" entity that XML does not define',
        'duplicate attribute': 'a tag has the same attribute twice',
        'junk after document element': 'there is content after the end of the export',
    }.get(f.message, f.message)
    return {'message': msg, 'parser': f.message, **_where(buf, f.pos, encoding)}


# ---- entry point -----------------------------------------------------------------------------------------

def check(data):
    """Report on bytes that fail to parse as XML, and a repaired copy when possible.

    Returns {'size', 'encoding', 'problem' (the first parse error), 'repairs' ([{tier, kind, message, count,
    examples}]), 'needs_guess', 'repaired' (bytes or None), 'remaining' (the error left when repair failed),
    'root'}."""
    encoding = detect_encoding(data)
    newline = b'\r\n' if b'\r\n' in data[:65536] else b'\n'
    report = {'size': len(data), 'encoding': encoding, 'problem': None, 'repairs': [], 'needs_guess': False,
              'repaired': None, 'remaining': None, 'root': None}
    first = _parse(data)
    if first is None:
        report['repaired'] = data
        return report
    report['problem'] = _describe(first, data, encoding)
    if not data.strip():
        report['remaining'] = {'message': 'the file is empty', 'parser': first.message, 'line': 1, 'before': '',
                               'after': ''}
        return report

    kinds = []
    buf = _fix_encoding(data, encoding, kinds)
    buf = _fix_text(buf, encoding, kinds)

    by_kind = {}
    f = _parse(buf)
    for _ in range(MAX_STRUCTURAL):
        if f is None:
            break
        step = _structural(buf, f, newline, encoding)
        if step is None:
            break
        tier, kind, fixed, example = step
        if tier == 'refuse':
            report['repairs'] = [k.d for k in kinds]
            report['remaining'] = example
            return report
        buf = fixed
        if kind not in by_kind:
            by_kind[kind] = _Kind(tier, kind, _MESSAGES[kind])
            kinds.append(by_kind[kind])
        by_kind[kind].add(example)
        f = _parse(buf)

    report['repairs'] = [k.d for k in kinds]
    report['needs_guess'] = any(k.d['tier'] == 'guess' for k in kinds)
    if f is not None:
        report['remaining'] = _describe(f, buf, encoding)
        return report
    try:
        doc = XmlDoc(buf)                     # the same parse the app uses
    except Exception as e:                    # pragma: no cover - _parse() and XmlDoc agree
        report['remaining'] = {'message': str(e), 'parser': str(e), 'line': 0, 'before': '', 'after': ''}
        return report
    report['root'] = doc.root.tag
    report['repaired'] = buf
    return report
