"""Document records for both export formats, built on the byte index in xmlstore.

    BP Premier (<BPSEHRV2>)
        CorrespondenceIn/Document            -> 1..n DocumentPage/Content   (base64 of a one-file ZIP)
        Investigations/Investigation         -> InvestigationPage/Content   (base64 of a one-file ZIP)
                                                or RTF REPORTHEADER / REPORTBODY
        CorrespondenceOut/Correspondence     -> RTF CONTENT
    MedicalDirector (<PATIENT SCHEMA=...>)
        DOCUMENTS2/DOCUMENT                  -> BASE64_DATA (raw file); rows sharing DOC_NO are parts of one
                                                document (PAGE_NO 1, 2, ...)
        DOCUMENTHANDLING/ITEM                -> second copy of the metadata, joined on MD_GUID = DMS_MD_GUID
        IX/RESULT                            -> pathology/imaging report (REPORTHEAD text + REPORTTEXT RTF);
                                                its handling ITEM is joined on DMS_MD_GUID or PATHOLOGY_ID

Record keys are prefixed by table because ids repeat across tables.
"""
import base64
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field

SECTIONS = [
    ('documents', 'Documents & attachments'),
    ('reports', 'Reports (text)'),
    ('letters', 'Outgoing letters'),
]


@dataclass
class Part:
    index: int
    node: object                 # DocumentPage / InvestigationPage / DOCUMENT row / record node for text
    payload: object              # node holding the content
    encoding: str                # 'zip-b64' | 'b64' | 'rtf' | 'report' | 'mdreport'
    filename: str
    fmt: str
    size: int


@dataclass
class Record:
    key: str
    kind: str
    section: str
    date: str                    # dd/mm/yyyy for display
    date_sort: str               # yyyy-mm-dd for sorting
    provider: str
    category: str
    subject: str
    to: str = ''                 # recipient (MedicalDirector documents, BP outgoing letters)
    parts: list = field(default_factory=list)
    nodes: list = field(default_factory=list)   # every element that belongs to this record

    @property
    def size(self):
        return sum(p.size for p in self.parts)

    @property
    def fmt(self):
        fmts = []
        for p in self.parts:
            if p.fmt not in fmts:
                fmts.append(p.fmt)
        return '+'.join(fmts)


def schema_of(doc):
    return schema_of_tag(doc.root.tag)


def schema_of_tag(tag):
    return {'BPSEHRV2': 'bp', 'PATIENT': 'md'}.get(tag, 'unknown')


SCHEMA_LABELS = {'bp': 'Best Practice (BPSEHRV2)', 'md': 'MedicalDirector', 'unknown': 'Unknown format'}


# ---- small helpers ---------------------------------------------------------------------------------------

def _safe_id(value):
    return re.sub(r'[^A-Za-z0-9_.]', '_', value.strip()) or '_'


def parse_date(value):
    """Return (display dd/mm/yyyy, sortable yyyy-mm-dd) from either export's date format."""
    v = (value or '').strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', v)
    if m:
        y, mo, d = m.groups()
        return f'{d}/{mo}/{y}', f'{y}-{mo}-{d}'
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})', v)
    if m:
        d, mo, y = m.groups()
        return f'{int(d):02d}/{int(mo):02d}/{y}', f'{y}-{int(mo):02d}-{int(d):02d}'
    return ('' if v.upper() == 'NIL' else v), ''


_HL7 = {'\\T\\': '&', '\\F\\': '|', '\\S\\': '^', '\\R\\': '~', '\\E\\': '\\'}


def hl7_unescape(value):
    """HL7 escapes that MedicalDirector keeps in some sender names, e.g. 'Brain \\T\\ Spine'."""
    if '\\' not in value:
        return value
    return re.sub(r'\\[TFSRE]\\', lambda m: _HL7[m.group(0)], value)


def _clean(value):
    v = (value or '').strip()
    return '' if v.upper() == 'NIL' else v


def b64_size(doc, node):
    if node is None or node.self_closing:
        return 0
    return (node.etag_start - node.stag_end) * 3 // 4


_MAGIC = [
    (b'%PDF', 'pdf'), (b'II*\x00', 'tif'), (b'MM\x00*', 'tif'), (b'\xff\xd8', 'jpg'),
    (b'\x89PNG', 'png'), (b'GIF8', 'gif'), (b'BM', 'bmp'), (b'{\\rtf', 'rtf'),
    (b'\xd0\xcf\x11\xe0', 'msg'), (b'PK\x03\x04', 'zip'),
]


def sniff(head):
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    h = head.lstrip()[:20].lower()
    if h.startswith((b'<html', b'<!doctype html', b'<head', b'<body')):
        return 'html'
    return ''


def _fmt_from_name(name):
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    return {'tiff': 'tif', 'mtif': 'tif', 'jpeg': 'jpg', 'htm': 'html'}.get(ext, ext)


def _head_b64(doc, node, nbytes=48):
    raw = doc.inner_raw(node)[:nbytes * 2]
    raw = re.sub(rb'\s+', b'', raw)
    raw = raw[:len(raw) // 4 * 4]
    try:
        return base64.b64decode(raw)
    except Exception:
        return b''


# ---- BP Premier ------------------------------------------------------------------------------------------

def _bp_records(doc):
    root = doc.root
    records = []

    for cont in root.findall('CorrespondenceIn'):
        for d in cont.findall('Document'):
            doc_id = doc.child_text(d, 'DOCUMENTID')
            if not doc_id:
                continue
            parts = []
            for i, page in enumerate(d.findall('DocumentPage')):
                content = page.find('Content')
                fname = doc.child_text(page, 'FileName')
                fmt = _fmt_from_name(fname) or doc.child_text(page, 'DocType').lower()
                fmt = {'tiff': 'tif', 'mtif': 'tif', 'jpeg': 'jpg'}.get(fmt, fmt)
                parts.append(Part(i, page, content, 'zip-b64', fname, fmt or 'bin', b64_size(doc, content)))
            date, sort = parse_date(doc.child_text(d, 'CORRESPONDENCEDATE'))
            records.append(Record(
                key=f'bpdoc-{_safe_id(doc_id)}', kind='bp-document', section='documents',
                date=date, date_sort=sort, provider=_clean(doc.child_text(d, 'CONTACTNAME')),
                category=_clean(doc.child_text(d, 'CATEGORY')), subject=_clean(doc.child_text(d, 'SUBJECT')),
                parts=parts, nodes=[d]))

    result_ids = set()
    for vals in root.findall('InvestigationValues'):
        for r in vals.findall('Result'):
            result_ids.add(doc.child_text(r, 'REPORTID'))

    for cont in root.findall('Investigations'):
        for inv in cont.findall('Investigation'):
            inv_id = doc.child_text(inv, 'REPORTID')
            if not inv_id:
                continue
            date, sort = parse_date(doc.child_text(inv, 'REPORTDATE'))
            pages = inv.findall('InvestigationPage')
            rec = Record(
                key=f'bpinv-{_safe_id(inv_id)}', kind='bp-investigation', section='documents' if pages else 'reports',
                date=date, date_sort=sort, provider=_clean(doc.child_text(inv, 'PROVIDERNAME')),
                category='Investigation' + (' (has results)' if inv_id in result_ids else ''),
                subject=_clean(doc.child_text(inv, 'TESTNAME')), nodes=[inv])
            for i, page in enumerate(pages):
                content = page.find('Content')
                fmt = doc.child_text(page, 'DocType').lower()
                fmt = {'tiff': 'tif', 'mtif': 'tif', 'jpeg': 'jpg', 'htm': 'html'}.get(fmt, fmt)
                rec.parts.append(Part(i, page, content, 'zip-b64', '', fmt or 'bin', b64_size(doc, content)))
            if not pages:
                body, header = inv.find('REPORTBODY'), inv.find('REPORTHEADER')
                size = sum(len(doc.inner_raw(n)) for n in (body, header) if n is not None)
                if size:
                    rec.parts.append(Part(0, inv, body, 'report', '', 'rtf', size))
            if rec.parts:
                records.append(rec)

    for cont in root.findall('CorrespondenceOut'):
        for c in cont.findall('Correspondence'):
            rid = doc.child_text(c, 'RECORDID')
            if not rid:
                continue
            content = c.find('CONTENT')
            date, sort = parse_date(doc.child_text(c, 'CORRESPONDENCEDATE'))
            records.append(Record(
                key=f'bpcor-{_safe_id(rid)}', kind='bp-correspondence', section='letters',
                date=date, date_sort=sort, provider='', to=_clean(doc.child_text(c, 'CONTACTNAME')),
                category='Outgoing letter', subject=_clean(doc.child_text(c, 'SUBJECT')),
                parts=[Part(0, c, content, 'rtf', '', (doc.child_text(c, 'DOCTYPE') or 'rtf').lower(),
                            len(doc.inner_raw(content)))],
                nodes=[c]))
    return records


# ---- MedicalDirector (<PATIENT> export) ------------------------------------------------------------

def _md_records(doc):
    root = doc.root
    items_by_guid, items_by_path = {}, {}
    for dh in root.findall('DOCUMENTHANDLING'):
        for it in dh.findall('ITEM'):
            g = doc.child_text(it, 'DMS_MD_GUID')
            if g:
                items_by_guid.setdefault(g, []).append(it)
            pid = doc.child_text(it, 'PATHOLOGY_ID')
            if pid and pid != '0':
                items_by_path.setdefault(pid, []).append(it)

    groups = {}
    for cont in root.findall('DOCUMENTS2'):
        for row in cont.findall('DOCUMENT'):
            doc_no = doc.child_text(row, 'DOC_NO')
            if doc_no:
                groups.setdefault(doc_no, []).append(row)

    records = []
    for doc_no, rows in groups.items():
        rows.sort(key=lambda r: int(doc.child_text(r, 'PAGE_NO') or 0) if doc.child_text(r, 'PAGE_NO').isdigit() else 0)
        first = rows[0]
        parts = []
        for i, row in enumerate(rows):
            payload = row.find('BASE64_DATA')
            fname = doc.child_text(row, 'FILENAME')
            fmt = sniff(_head_b64(doc, payload)) or _fmt_from_name(fname) or 'bin'
            parts.append(Part(i, row, payload, 'b64', fname, fmt, b64_size(doc, payload)))
        guids = []
        for row in rows:
            g = doc.child_text(row, 'MD_GUID')
            if g and g not in guids:
                guids.append(g)
        items = [it for g in guids for it in items_by_guid.get(g, [])]
        date, sort = parse_date(doc.child_text(first, 'DOCDATE'))
        to = _clean(doc.child_text(first, 'DOCTORNAME'))
        # The sender is only in the handling item. DOCTORNAME is the practice doctor it was addressed to;
        # REFDOCTORNAME is used as a fallback only when it names someone else.
        provider = hl7_unescape(_clean(doc.child_text(items[0], 'DOCUMENT_FROM'))) if items else ''
        if not provider:
            ref = _clean(doc.child_text(first, 'REFDOCTORNAME'))
            provider = ref if ref.lower() != to.lower() else ''
        records.append(Record(
            key=f'md-{_safe_id(doc_no)}', kind='md-document', section='documents',
            date=date, date_sort=sort, provider=provider, to=to,
            category=_clean(doc.child_text(first, 'DOCTYPE')),
            subject=_clean(doc.child_text(first, 'DOCTITLE')) or _clean(doc.child_text(first, 'DOCDESC')),
            parts=parts, nodes=rows + items))

    for ix in root.findall('IX'):
        for res in ix.findall('RESULT'):
            pid = doc.child_text(res, 'PATHOLOGY_ID')
            if not pid:
                continue
            # A report with a GUID shares it with the scanned document attached to it, and that document owns
            # the handling item; other reports are linked to their handling item by PATHOLOGY_ID.
            g = doc.child_text(res, 'DMS_MD_GUID')
            items = []
            if not g and pid != '0':
                items = [it for it in items_by_path.get(pid, []) if not doc.child_text(it, 'DMS_MD_GUID')]
            body = res.find('REPORTTEXT')
            size = sum(len(doc.inner_raw(n)) for n in (body, res.find('REPORTHEAD')) if n is not None)
            date, sort = parse_date(doc.child_text(res, 'REPORTDATE') or doc.child_text(res, 'COLLDATE'))
            records.append(Record(
                key=f'mdix-{_safe_id(pid)}', kind='md-report', section='reports',
                date=date, date_sort=sort, provider=_clean(doc.child_text(res, 'LABNAME')),
                category='Pathology / imaging report', subject=_clean(doc.child_text(res, 'TESTNAME')),
                parts=[Part(0, res, body, 'mdreport', '', 'rtf', size)], nodes=[res] + items))
    return records


# ---- public API ------------------------------------------------------------------------------------------

def records(doc):
    """All document records, newest first. Cached on the XmlDoc (it is immutable)."""
    cached = doc._cache.get('records')
    if cached is None:
        schema = schema_of(doc)
        recs = _bp_records(doc) if schema == 'bp' else _md_records(doc) if schema == 'md' else []
        seen = {}
        for r in recs:                      # keep keys unique even if an export repeats an id
            n = seen.get(r.key, 0)
            seen[r.key] = n + 1
            if n:
                r.key = f'{r.key}~{n}'
        recs.sort(key=lambda r: r.date_sort, reverse=True)
        cached = doc._cache['records'] = recs
    return cached


def record_map(doc):
    return {r.key: r for r in records(doc)}


def patient_summary(doc):
    schema = schema_of(doc)
    root = doc.root
    if schema == 'bp':
        p = root.find('Demographics')
        p = p.find('Patient') if p is not None else None
    elif schema == 'md':
        p = root.find('DEMOGRAPHICS')
    else:
        p = None
    if p is None:
        return {'name': '', 'dob': ''}
    name = ' '.join(x for x in (doc.child_text(p, 'FIRSTNAME'), doc.child_text(p, 'SURNAME')) if x)
    return {'name': name, 'dob': parse_date(doc.child_text(p, 'DOB'))[0]}


def part_bytes(doc, part):
    """(filename, bytes, fmt) of one part, unpacked from base64/ZIP where needed."""
    if part.encoding in ('report', 'mdreport'):
        return 'report.rtf', doc.text(part.payload).encode('utf-8'), 'rtf'
    if part.encoding == 'rtf':
        return 'letter.rtf', doc.text(part.payload).encode('utf-8'), 'rtf'
    raw = base64.b64decode(re.sub(rb'\s+', b'', doc.inner_raw(part.payload)))
    name = part.filename
    if part.encoding == 'zip-b64' and raw[:4] == b'PK\x03\x04':
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = z.namelist()
            if names:
                name = name or names[0]
                raw = z.read(names[0])
    fmt = sniff(raw[:64]) or part.fmt
    return name or f'part{part.index + 1}.{fmt}', raw, fmt


# ---- edits -----------------------------------------------------------------------------------------------

def node_identity(doc, node):
    """Stable identity of a record-level element, used to match source and working copies."""
    tag = node.tag
    if tag == 'Document':
        v = doc.child_text(node, 'DOCUMENTID')
    elif tag == 'Investigation':
        v = doc.child_text(node, 'REPORTID')
    elif tag == 'Correspondence':
        v = doc.child_text(node, 'RECORDID')
    elif tag == 'DOCUMENT':
        v = doc.child_text(node, 'DOC_NO') + '/' + doc.child_text(node, 'PAGE_NO')
    elif tag == 'ITEM':
        v = doc.child_text(node, 'DMS_MD_GUID')
    elif tag == 'RESULT' and node.parent is not None and node.parent.tag == 'IX':
        v = doc.child_text(node, 'PATHOLOGY_ID')
    else:
        v = ''
    parent = node.parent.tag if node.parent else ''
    if v:
        return (parent, tag, v)
    # A top-level section (e.g. <CurrentRx>) that is the only one of its tag: match on the tag, as its bytes
    # change whenever something inside it is deleted. (Sections themselves are never deleted.)
    if node.parent is not None and node.parent.parent is None \
            and sum(1 for c in node.parent.children if c.tag == tag) == 1:
        return (parent, tag, '#only')
    # No id (e.g. a clinical item or a pathology handling item): match on the exact bytes instead.
    return (parent, tag, '#raw', hashlib.sha1(doc.raw(node)).hexdigest())


def delete_edits(doc, keys):
    """Edits removing every element of the given records. Returns (edits, removed_records)."""
    rmap = record_map(doc)
    edits, removed, seen = [], [], set()
    for key in keys:
        rec = rmap.get(key)
        if rec is None:
            continue
        removed.append(rec)
        for node in rec.nodes:
            if node.start not in seen:                 # never remove the same element twice
                seen.add(node.start)
                edits.append(doc.edit_remove(node))
    return edits, removed


def _container_path(node):
    path = []
    n = node.parent
    while n is not None and n.parent is not None:
        path.append(n.tag)
        n = n.parent
    return list(reversed(path))


def _find_path(root, path):
    n = root
    for tag in path:
        n = n.find(tag) if n is not None else None
    return n


def revert_edits(work, src, key, mapper=None):
    """Edits that make record `key` in the working copy identical to the source again (re-inserting it
    if it was deleted). `mapper(doc)` returns {key: record}; documents by default, or clinical items.
    Returns (edits, source_record or None)."""
    src_rec = (mapper or record_map)(src).get(key)
    if src_rec is None:
        return [], None
    edits = []
    for snode in src_rec.nodes:
        ident = node_identity(src, snode)
        wcont = _find_path(work.root, _container_path(snode))
        if ident is None or wcont is None:
            continue
        wmap = {}
        for c in wcont.children:
            ci = node_identity(work, c)
            if ci:
                wmap[ci] = c
        wnode = wmap.get(ident)
        if wnode is not None:
            if work.raw(wnode) != src.raw(snode):
                edits.append((wnode.start, wnode.end, src.raw(snode)))
            continue
        siblings = snode.parent.children
        pos = None
        for prev in reversed(siblings[:siblings.index(snode)]):
            pi = node_identity(src, prev)
            if pi in wmap:
                pos = wmap[pi].end
                break
        if pos is None:
            pos = wcont.stag_end
        edits.append((pos, pos, src.leading_whitespace(snode) + src.raw(snode)))
    return edits, src_rec


# ---- metadata --------------------------------------------------------------------------------------------

META_FIELDS = [('date', 'Date'), ('provider', 'From'), ('to', 'To'), ('category', 'Category'), ('subject', 'Subject')]

# field -> [(which nodes, element, style)]. The first target is the primary copy; the others are second
# copies that are kept in sync (MedicalDirector's DOCUMENTHANDLING item, or every part of a document).
META_MAP = {
    'bp-document': {
        'date': [('main', 'CORRESPONDENCEDATE', 'dmy')],
        'provider': [('main', 'CONTACTNAME', 'text')],
        'category': [('main', 'CATEGORY', 'text')],
        'subject': [('main', 'SUBJECT', 'text')],
    },
    'bp-investigation': {
        'date': [('main', 'REPORTDATE', 'dmy')],
        'provider': [('main', 'PROVIDERNAME', 'text')],
        'subject': [('main', 'TESTNAME', 'text')],
    },
    'bp-correspondence': {
        'date': [('main', 'CORRESPONDENCEDATE', 'dmy')],
        'to': [('main', 'CONTACTNAME', 'text')],
        'subject': [('main', 'SUBJECT', 'text')],
    },
    'md-report': {
        'date': [('main', 'REPORTDATE', 'iso'), ('items', 'DOCUMENT_DATETIME', 'iso')],
        'provider': [('main', 'LABNAME', 'text')],
        'subject': [('main', 'TESTNAME', 'text'), ('items', 'SUBJECT', 'text')],
    },
    'md-document': {
        'date': [('rows', 'DOCDATE', 'iso'), ('items', 'DOCUMENT_DATETIME', 'iso')],
        'provider': [('items', 'DOCUMENT_FROM', 'text')],
        'to': [('rows', 'DOCTORNAME', 'text'), ('items', 'DOCUMENT_TO', 'text')],
        'category': [('rows', 'DOCTYPE', 'text'), ('items', 'DOCUMENT_TYPE', 'text')],
        'subject': [('rows', 'DOCTITLE', 'text'), ('items', 'SUBJECT', 'text')],
    },
}


class MetadataError(ValueError):
    pass


def _target_nodes(rec, which):
    if which == 'main':
        return rec.nodes[:1]
    if which == 'rows':
        return [n for n in rec.nodes if n.tag == 'DOCUMENT']
    if which == 'items':
        return [n for n in rec.nodes if n.tag == 'ITEM']
    return []


def editable_fields(rec):
    """[(name, label)] of the fields that can be edited for this record, in display order."""
    spec = META_MAP.get(rec.kind, {})
    return [(name, label) for name, label in META_FIELDS if name in spec and _target_nodes(rec, spec[name][0][0])]


def current_values(rec):
    """Values as shown to the user. The date is ISO (yyyy-mm-dd) for an <input type=date>."""
    return {'date': rec.date_sort, 'provider': rec.provider, 'to': rec.to,
            'category': rec.category, 'subject': rec.subject}


def _norm(doc, value, md):
    v = _clean(value)
    return hl7_unescape(v) if md else v


def _format_date(iso, style, old_raw):
    y, m, d = iso.split('-')
    if style == 'dmy':
        return f'{d}/{m}/{y}'
    # MedicalDirector datetimes: keep the existing time part, if any
    time_part = old_raw.strip()[10:] if re.match(r'^\d{4}-\d{2}-\d{2}', old_raw.strip()) else ''
    return f'{y}-{m}-{d}' + (time_part or ' 00:00:00')


def _set_field(doc, node, tag, value):
    child = node.find(tag)
    if child is not None:
        return doc.edit_set_text(child, value)
    t = tag.encode(doc.encoding)
    return (node.etag_start, node.etag_start, b'<' + t + b'>' + doc.escape(value) + b'</' + t + b'>')


_IMAGE_EXT = re.compile(r'\.(tiff?|mtif|bmp|jpe?g|jfif)$', re.I)


def pdf_name(name):
    """A file name with its image extension changed to .pdf (.PDF when it was upper case)."""
    m = _IMAGE_EXT.search(name or '')
    if not m:
        return (name or 'document') + '.pdf'
    return name[:m.start()] + ('.PDF' if m.group(1).isupper() else '.pdf')


def to_pdf_edits(doc, part):
    """Edits that relabel an image part (TIFF/BMP/JPEG) as PDF: BP DocType and FileName, MedicalDirector FILENAME.
    A BP DocType keeps its width (BP pads it, e.g. 'PDF ')."""
    edits = []
    page = part.node
    dt = page.find('DocType')
    if dt is not None:
        old = doc.text(dt)
        edits.append(doc.edit_set_text(dt, 'PDF'.ljust(len(old)) if len(old) > 3 else 'PDF'))
    for tag in ('FileName', 'FILENAME'):
        fn = page.find(tag)
        if fn is not None and doc.text(fn).strip():
            edits.append(doc.edit_set_text(fn, pdf_name(doc.text(fn).strip())))
    return edits


def metadata_edits(doc, rec, new_values):
    """Edits that apply new_values (field -> str, date as yyyy-mm-dd) to every copy of the record's metadata.

    Only fields whose value differs from what is shown are written. A second copy (handling item, other
    parts) is updated only if it currently agrees with the primary copy, so a copy that already disagreed
    keeps its own value. Returns (edits, {field: (old, new)})."""
    spec = META_MAP.get(rec.kind)
    if not spec:
        raise MetadataError('This kind of record has no editable details.')
    md = rec.kind.startswith('md-')
    shown = current_values(rec)
    edits, changed = [], {}
    for name, _label in editable_fields(rec):
        if name not in new_values:
            continue
        new = (new_values[name] or '').strip()
        if name == 'date':
            if new and not re.match(r'^\d{4}-\d{2}-\d{2}$', new):
                raise MetadataError('Date must be a valid date.')
            if not new:
                raise MetadataError('Date cannot be empty.')
        if new == (shown[name] or ''):
            continue
        changed[name] = (shown[name], new)
        targets = spec[name]
        primary_which, primary_tag, _ = targets[0]
        primary_nodes = _target_nodes(rec, primary_which)
        primary_old = _norm(doc, doc.child_text(primary_nodes[0], primary_tag), md) if primary_nodes else ''
        for i, (which, tag, style) in enumerate(targets):
            for node in _target_nodes(rec, which):
                old_raw = doc.child_text(node, tag, strip=False)
                if i > 0 and _norm(doc, old_raw, md).lower() != primary_old.lower():
                    continue                           # an out-of-sync second copy keeps its own value
                value = _format_date(new, style, old_raw) if name == 'date' else new
                if node.find(tag) is None and not value:
                    continue
                edits.append(_set_field(doc, node, tag, value))
    return edits, changed


def category_suggestions(doc, kind):
    seen = {}
    for r in records(doc):
        if r.kind == kind and r.category:
            seen[r.category.lower()] = seen.get(r.category.lower(), r.category)
    if kind == 'md-document':
        for dh in doc.root.findall('DOCUMENTHANDLING'):
            for it in dh.findall('ITEM'):
                v = _clean(doc.child_text(it, 'DOCUMENT_TYPE'))
                if v:
                    seen.setdefault(v.lower(), v)
    return sorted(seen.values(), key=str.lower)


# ---- duplicates ------------------------------------------------------------------------------------------

def content_hash(doc, part):
    """sha1 of a part's decoded content ('' for empty content, which is never a duplicate)."""
    key = ('chash', part.node.start, part.index)
    h = doc._cache.get(key)
    if h is None:
        try:
            data = part_bytes(doc, part)[1]
        except Exception:
            data = b''
        h = hashlib.sha1(data).hexdigest() if len(data) >= 64 else ''
        doc._cache[key] = h
    return h


def duplicate_groups(doc):
    """[[record, ...]] for records whose parts are byte-for-byte identical, in file order (the first is the one
    to keep). Records with empty content are ignored."""
    cached = doc._cache.get('dups')
    if cached is None:
        groups = {}
        for rec in sorted(records(doc), key=lambda r: r.nodes[0].start):
            hashes = tuple(content_hash(doc, p) for p in rec.parts)
            if hashes and all(hashes):
                groups.setdefault(hashes, []).append(rec)
        cached = doc._cache['dups'] = [g for g in groups.values() if len(g) > 1]
    return cached


def duplicate_of(doc):
    """{key of an extra copy: record it duplicates}."""
    return {r.key: g[0] for g in duplicate_groups(doc) for r in g[1:]}
