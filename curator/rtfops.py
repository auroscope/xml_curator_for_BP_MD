"""Shrink RTF documents by compressing or removing their embedded pictures. The result is still RTF.

Pictures are {\\pict ...} groups holding hex-encoded image data (\\jpegblip, \\pngblip, \\dibitmap, metafiles).
Letterheads and logos are often stored far larger than they are shown (a 1 MB photo-quality JPEG shown 8 x 1 cm, an
uncompressed bitmap shown 3 x 3 cm). Compressing re-saves each raster picture at its displayed size (150 dpi) and
keeps its displayed size (\\picwgoal/\\pichgoal) unchanged; metafiles (WMF/EMF) are left as they are, except a WMF
that only wraps one bitmap (common for letterhead bands), which is treated as that bitmap. Ignorable Word
data blocks ({\\*\\themedata}, {\\*\\datastore}, ...) that no RTF reader needs are dropped too.

Everything else in the file is kept byte-for-byte, and the text (as extracted by striprtf) must be unchanged.
"""
import io
import re
import struct

from PIL import Image
from striprtf.striprtf import rtf_to_text

TARGET_DPI = 150
JPEG_QUALITY = 70
# Ignorable destinations ({\* ...}) that hold Word housekeeping only
BLOAT = ('themedata', 'colorschememapping', 'datastore', 'rsidtbl', 'latentstyles', 'xmlnstbl', 'generator')


class RtfError(Exception):
    pass


def _group_end(buf, start):
    """Index just past the '}' closing the group that opens at buf[start] == '{'. Handles escapes and \\binN."""
    depth = 0
    i, n = start, len(buf)
    while i < n:
        c = buf[i]
        if c == 0x5C:                                            # backslash
            m = re.match(rb'\\bin(\d+) ?', buf[i:i + 20])
            if m:
                i += m.end() + int(m.group(1))
                continue
            i += 2
            continue
        if c == 0x7B:
            depth += 1
        elif c == 0x7D:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise RtfError('unbalanced braces')


def _groups(buf, pattern):
    """(start, end) of every group whose opening matches pattern (bytes regex anchored at '{'), outermost only."""
    out, pos = [], 0
    rx = re.compile(pattern)
    while True:
        m = rx.search(buf, pos)
        if not m:
            return out
        end = _group_end(buf, m.start())
        out.append((m.start(), end))
        pos = end


def _words(header, name):
    m = re.search(rb'\\' + name.encode() + rb'(-?\d+)', header)
    return int(m.group(1)) if m else None


_CONTROL_WORD = re.compile(rb'\\(?!bin\d)[a-z]+-?\d* ?')


def _header_end(g):
    """Index of the first data byte of a {\\pict ...} group g: after its control words and nested groups (such as
    Word's {\\*\\picprop ...}, which holds groups of its own)."""
    i, n = 6, len(g)                                       # just past "{\\pict"
    while i < n:
        c = g[i:i + 1]
        if c in b' \t\r\n':
            i += 1
        elif c == b'{':
            i = _group_end(g, i)
        elif c == b'\\' and _CONTROL_WORD.match(g, i):
            i = _CONTROL_WORD.match(g, i).end()
        else:
            break
    return i


# WMF records that only set up the drawing (no output of their own)
_WMF_SETUP = {0x0000, 0x001E, 0x0035, 0x00F7, 0x0102, 0x0103, 0x0104, 0x0105, 0x0106, 0x0107, 0x0127, 0x012E, 0x01F0,
              0x0201, 0x0209, 0x020B, 0x020C, 0x020D, 0x020E, 0x0234, 0x012D, 0x02FA, 0x02FC, 0x0626}
_WMF_BLITS = {0x0940: 16, 0x0B41: 20, 0x0F43: 22}       # DibBitBlt, DibStretchBlt, StretchDIB: offset of the DIB
_SRCCOPY = 0x00CC0020


def wmf_bitmap(data):
    """The DIB of a WMF that does nothing but copy one bitmap, whole and unchanged (SRCCOPY), else None."""
    pos = 22 if data[:4] == b'\xd7\xcd\xc6\x9a' else 0      # placeable header
    if len(data) < pos + 18:
        return None
    pos += struct.unpack_from('<H', data, pos + 2)[0] * 2   # header size in words
    dib = None
    while pos + 6 <= len(data):
        size, func = struct.unpack_from('<IH', data, pos)
        if size < 3 or pos + size * 2 > len(data):
            return None
        if func in _WMF_BLITS:
            if dib is not None:
                return None                                  # more than one bitmap
            rop, = struct.unpack_from('<I', data, pos + 6)
            off = pos + 6 + _WMF_BLITS[func]
            if rop != _SRCCOPY or off + 40 > pos + size * 2:
                return None
            w, h = struct.unpack_from('<ii', data, off + 4)
            if func == 0x0940:
                y_src, x_src, src_h, src_w = struct.unpack_from('<hhhh', data, pos + 10)
            else:
                skip = 2 if func == 0x0F43 else 0             # StretchDIB has ColorUsage first
                src_h, src_w, y_src, x_src = struct.unpack_from('<hhhh', data, pos + 10 + skip)
            if (x_src, y_src, src_w, abs(src_h)) != (0, 0, w, abs(h)):
                return None                                  # only part of the bitmap is shown
            dib = data[off:pos + size * 2]
        elif func not in _WMF_SETUP:
            return None                                      # it draws or writes something else
        if func == 0x0000:
            break
        pos += size * 2
    return dib


class Picture:
    def __init__(self, buf, start, end):
        self.start, self.end = start, end
        g = buf[start:end]
        # the header is everything before the first data byte (hex digits after the control words), \binN aside
        h = _header_end(g)
        self.header = g[6:h]
        body = g[h:-1]
        kind = re.search(rb'\\(pngblip|jpegblip|emfblip|wmetafile|dibitmap|wbitmap|macpict|pmmetafile)', self.header)
        self.kind = kind.group(1).decode() if kind else 'unknown'
        self.goal_w, self.goal_h = _words(self.header, 'picwgoal'), _words(self.header, 'pichgoal')
        self.scale_x = _words(self.header, 'picscalex') or 100
        self.scale_y = _words(self.header, 'picscaley') or 100
        binm = re.match(rb'\\bin(\d+) ?', body)
        if binm:
            self.data = body[binm.end():binm.end() + int(binm.group(1))]
        else:
            hexdigits = re.sub(rb'[^0-9a-fA-F]', b'', body)
            self.data = bytes.fromhex(hexdigits[:len(hexdigits) // 2 * 2].decode())
        self.size = end - start
        self.wrapped_dib = wmf_bitmap(self.data) if self.kind == 'wmetafile' else None

    @property
    def raster(self):
        return self.kind in ('pngblip', 'jpegblip', 'dibitmap') or self.wrapped_dib is not None

    def shown_cm(self):
        if not self.goal_w or not self.goal_h:
            return None
        return (self.goal_w * self.scale_x / 100 / 567, self.goal_h * self.scale_y / 100 / 567)

    def image(self):
        if self.kind in ('pngblip', 'jpegblip'):
            return Image.open(io.BytesIO(self.data))
        if self.kind == 'dibitmap':
            return Image.open(io.BytesIO(_dib_to_bmp(self.data)))
        if self.wrapped_dib is not None:
            return Image.open(io.BytesIO(_dib_to_bmp(self.wrapped_dib)))
        raise RtfError(f'cannot decode {self.kind}')


def _dib_to_bmp(dib):
    """A device-independent bitmap (BITMAPINFOHEADER + palette + pixels) with a BMP file header in front."""
    hsize, = struct.unpack_from('<I', dib, 0)
    bits, = struct.unpack_from('<H', dib, 14)
    used, = struct.unpack_from('<I', dib, 32) if hsize >= 40 else (0,)
    compression, = struct.unpack_from('<I', dib, 16) if hsize >= 40 else (0,)
    palette = (used or (1 << bits if bits <= 8 else 0)) * 4
    if compression == 3 and hsize == 40:                         # BI_BITFIELDS masks follow the header
        palette += 12
    offset = 14 + hsize + palette
    return b'BM' + struct.pack('<IHHI', 14 + len(dib), 0, 0, offset) + dib


def pictures(rtf):
    return [Picture(rtf, s, e) for s, e in _groups(rtf, rb'\{\\pict(?![a-z])')]


def text_of(rtf):
    return re.sub(r'\s+', ' ', rtf_to_text(rtf.decode('latin-1'), errors='ignore')).strip()


def _hex_lines(data, width=128):
    h = data.hex().encode()
    return b'\n'.join(h[i:i + width] for i in range(0, len(h), width))


def _encode(img, target_px):
    """Smallest faithful encoding: PNG for line art / few colours / transparency, JPEG for photos."""
    if img.mode in ('P', '1', 'L', 'LA', 'RGBA', 'RGB', 'I;16'):
        pass
    if img.mode not in ('RGB', 'L', 'RGBA', 'LA', 'P', '1'):
        img = img.convert('RGB')
    w, h = img.size
    if target_px and (w > target_px[0] * 1.1 or h > target_px[1] * 1.1):
        img = img.convert('RGBA' if img.mode in ('RGBA', 'LA', 'P') else ('L' if img.mode in ('L', '1') else 'RGB'))
        img = img.resize((max(1, target_px[0]), max(1, target_px[1])), Image.LANCZOS)
    few = img.getcolors(256) if img.mode != '1' else True
    candidates = []
    png = io.BytesIO()
    pimg = img
    if few and img.mode not in ('P', '1'):
        pimg = img.convert('RGBA').quantize(256) if img.mode in ('RGBA', 'LA') else img.convert('RGB').quantize(256)
    pimg.save(png, 'PNG', optimize=True)
    candidates.append(('pngblip', png.getvalue()))
    if not few and img.mode not in ('RGBA', 'LA'):
        jpg = io.BytesIO()
        img.convert('L' if img.mode == 'L' else 'RGB').save(jpg, 'JPEG', quality=JPEG_QUALITY, optimize=True)
        candidates.append(('jpegblip', jpg.getvalue()))
    kind, data = min(candidates, key=lambda c: len(c[1]))
    return kind, data, img.size


def _rebuilt(pic, kind, data, size):
    keep = [w for w in re.findall(rb'\\(?:picscalex|picscaley|piccropl|piccropr|piccropt|piccropb|picwgoal|pichgoal)-?\d+',
                                  pic.header)]
    if pic.goal_w is None or pic.goal_h is None:               # keep the displayed size explicit
        try:
            img = pic.image()
            dpi = img.info.get('dpi', (96, 96))
            keep += [b'\\picwgoal%d' % round(img.size[0] / max(dpi[0], 1) * 1440),
                     b'\\pichgoal%d' % round(img.size[1] / max(dpi[1], 1) * 1440)]
        except Exception:
            pass
    head = b'{\\pict' + b''.join(keep) + b'\\' + kind.encode() + b'\\picw%d\\pich%d' % size
    return head + b'\n' + _hex_lines(data) + b'}'


def _drop_bloat(rtf):
    spans = []
    for word in BLOAT:
        spans += _groups(rtf, rb'\{\\\*\\' + word.encode() + rb'(?![a-z])')
    return spans


def _apply(rtf, replacements):
    out, pos = [], 0
    for s, e, new in sorted(replacements):
        if s < pos:
            continue
        out.append(rtf[pos:s])
        out.append(new)
        pos = e
    out.append(rtf[pos:])
    return b''.join(out)


def analyse(rtf):
    pics = pictures(rtf)
    return {
        'bytes': len(rtf),
        'pictures': [{'kind': p.kind, 'bytes': p.size, 'shown_cm': p.shown_cm(), 'raster': p.raster} for p in pics],
        'picture_bytes': sum(p.size for p in pics),
        'bloat_bytes': sum(e - s for s, e in _drop_bloat(rtf)),
    }


def compress_pictures(rtf):
    """Raster pictures re-saved at their displayed size (150 dpi); metafiles kept. Returns (rtf, notes)."""
    reps, notes = [], []
    for p in pictures(rtf):
        if not p.raster:
            notes.append(f'{p.kind} picture kept as it is (vector format)')
            continue
        try:
            img = p.image()
            img.load()
        except Exception:
            notes.append(f'a {p.kind} picture could not be decoded and was kept')
            continue
        cm = p.shown_cm()
        target = (round(cm[0] / 2.54 * TARGET_DPI), round(cm[1] / 2.54 * TARGET_DPI)) if cm else None
        if target and (target[0] < 4 or target[1] < 4):
            target = None
        kind, data, size = _encode(img, target)
        new = _rebuilt(p, kind, data, size)
        if len(new) < p.size * 0.9:
            reps.append((p.start, p.end, new))
    reps += [(s, e, b'') for s, e in _drop_bloat(rtf)]
    return _apply(rtf, reps), notes


def remove_pictures(rtf):
    """Every picture removed (with its Word shape/fallback wrappers when they hold nothing else)."""
    reps = []
    for s, e in _groups(rtf, rb'\{\\\*\\shppict(?![a-z])') + _groups(rtf, rb'\{\\nonshppict(?![a-z])'):
        reps.append((s, e, b''))
    for s, e in _groups(rtf, rb'\{\\shp(?![a-z])'):
        g = rtf[s:e]
        if b'\\pict' in g and b'\\shptxt' not in g:            # a floating picture, not a text box
            reps.append((s, e, b''))
    covered = sorted(reps)
    for p in pictures(rtf):
        if not any(s <= p.start and p.end <= e for s, e, _ in covered):
            reps.append((p.start, p.end, b''))
    reps += [(s, e, b'') for s, e in _drop_bloat(rtf)]
    return _apply(rtf, reps), []


def verify_text(original, new):
    a, b = text_of(original), text_of(new)
    return {'identical': a == b, 'chars': len(a)}
