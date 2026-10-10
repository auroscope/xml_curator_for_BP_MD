"""Byte-faithful access to a patient XML export.

The file is never re-serialised. expat is used only to find the byte range of every element; reads decode
those ranges, and writes splice new bytes into them. Everything outside an edited range (declaration, BOM,
encoding, whitespace, attribute quoting, element order) stays byte-for-byte identical.
"""
import re
import xml.parsers.expat as expat

# expat rejects the C0 control characters that these exports sometimes contain. They are mapped to spaces
# in the copy that is parsed only, which keeps every byte offset valid for the real data.
_CTRL = bytearray(range(256))
for _b in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)):
    _CTRL[_b] = 0x20
_CTRL = bytes(_CTRL)

_DECL_ENCODING = re.compile(rb'^\s*<\?xml[^>]*encoding\s*=\s*["\']([A-Za-z0-9._-]+)["\']')
_ENTITY = re.compile(r'&(#x[0-9A-Fa-f]+|#[0-9]+|lt|gt|amp|quot|apos);')
_NAMED = {'lt': '<', 'gt': '>', 'amp': '&', 'quot': '"', 'apos': "'"}
_CDATA = re.compile(r'<!\[CDATA\[(.*?)\]\]>', re.S)


class Node:
    """One element. `start` is its '<', `stag_end` is just past its start tag, `etag_start` is the '<' of its
    end tag (None when self-closing) and `end` is just past the element."""
    __slots__ = ('tag', 'start', 'stag_end', 'etag_start', 'end', 'children', 'parent')

    def __init__(self, tag, start, stag_end, parent):
        self.tag, self.start, self.stag_end, self.parent = tag, start, stag_end, parent
        self.etag_start = self.end = None
        self.children = []

    @property
    def self_closing(self):
        return self.etag_start is None

    def find(self, tag):
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def findall(self, tag):
        return [c for c in self.children if c.tag == tag]

    def iter(self, tag=None):
        stack = [self]
        while stack:
            n = stack.pop()
            if tag is None or n.tag == tag:
                yield n
            stack.extend(reversed(n.children))

    def __repr__(self):
        return f'<Node {self.tag} {self.start}:{self.end}>'


def detect_encoding(data):
    if data.startswith(b'\xef\xbb\xbf'):
        return 'utf-8'
    m = _DECL_ENCODING.match(data[:400])
    return m.group(1).decode('ascii').lower() if m else 'utf-8'


def _start_tag_end(buf, i):
    """Index just past the '>' closing the start tag at buf[i], skipping '>' inside quoted attributes."""
    quote = None
    n = len(buf)
    while i < n:
        ch = buf[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in (0x22, 0x27):
            quote = ch
        elif ch == 0x3E:
            return i + 1
        i += 1
    raise ValueError('unterminated start tag')


def _unescape(text):
    def rep(m):
        e = m.group(1)
        if e[0] == '#':
            return chr(int(e[2:], 16) if e[1] in 'xX' else int(e[1:]))
        return _NAMED[e]
    return _ENTITY.sub(rep, text)


class XmlDoc:
    def __init__(self, data):
        self.data = data
        self.encoding = detect_encoding(data)
        self.root = self._parse()
        self._cache = {}

    def _parse(self):
        buf = self.data.translate(_CTRL)
        parser = expat.ParserCreate()
        stack = []
        root = []

        def start(name, attrs):
            i = parser.CurrentByteIndex
            parent = stack[-1] if stack else None
            node = Node(name, i, _start_tag_end(buf, i), parent)
            if parent is None:
                root.append(node)
            else:
                parent.children.append(node)
            stack.append(node)

        def end(name):
            node = stack.pop()
            if buf[node.stag_end - 2] == 0x2F:          # '/>' : self-closing
                node.end = node.stag_end
            else:
                node.etag_start = parser.CurrentByteIndex
                node.end = buf.index(b'>', node.etag_start) + 1

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.Parse(buf, True)
        return root[0]

    # ---- reading ---------------------------------------------------------------------------------------

    def raw(self, node):
        return self.data[node.start:node.end]

    def inner_raw(self, node):
        if node is None or node.self_closing:
            return b''
        return self.data[node.stag_end:node.etag_start]

    def text(self, node):
        """Decoded text content of a leaf element ('' for None or empty)."""
        raw = self.inner_raw(node)
        if not raw:
            return ''
        s = raw.decode(self.encoding, errors='replace')
        if '<![CDATA[' in s:
            parts, pos = [], 0
            for m in _CDATA.finditer(s):
                parts.append(_unescape(s[pos:m.start()]))
                parts.append(m.group(1))
                pos = m.end()
            parts.append(_unescape(s[pos:]))
            return ''.join(parts)
        return _unescape(s)

    def child_text(self, node, tag, strip=True):
        if node is None:
            return ''
        t = self.text(node.find(tag))
        return t.strip() if strip else t

    # ---- edits (each returns (start, end, replacement_bytes)) ------------------------------------------

    def escape(self, value):
        s = value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return s.encode(self.encoding, errors='xmlcharrefreplace')

    def edit_set_text(self, node, value):
        body = self.escape(value)
        if node.self_closing:
            tag = node.tag.encode(self.encoding)
            return (node.start, node.end, b'<' + tag + b'>' + body + b'</' + tag + b'>')
        return (node.stag_end, node.etag_start, body)

    def edit_set_raw_inner(self, node, body):
        """Replace the content with bytes that are already safe XML text (e.g. base64)."""
        if node.self_closing:
            tag = node.tag.encode(self.encoding)
            return (node.start, node.end, b'<' + tag + b'>' + body + b'</' + tag + b'>')
        return (node.stag_end, node.etag_start, body)

    def edit_remove(self, node):
        """Remove an element plus the line break that follows it, so no blank line is left behind."""
        end = node.end
        m = re.compile(rb'[ \t]*\r?\n').match(self.data, end)
        if m:
            end = m.end()
        return (node.start, end, b'')

    def leading_whitespace(self, node):
        """The whitespace run directly before an element (used to re-insert it the same way)."""
        i = node.start
        while i > 0 and self.data[i - 1] in b' \t\r\n':
            i -= 1
        return self.data[i:node.start]


def apply_edits(data, edits):
    """Apply non-overlapping (start, end, bytes) splices in one pass."""
    edits = sorted(edits, key=lambda e: (e[0], e[1]))
    out, pos = [], 0
    for start, end, repl in edits:
        if start < pos:
            raise ValueError(f'overlapping edits at byte {start}')
        out.append(data[pos:start])
        out.append(repl)
        pos = end
    out.append(data[pos:])
    return b''.join(out)
