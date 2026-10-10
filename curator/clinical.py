"""Clinical summary: allergies, history, medications, immunisations, measurements, visits and results, for both
export formats, plus the series for the trend charts.

Items are never edited, only deleted (and restored), so an item's key is a hash of its exact bytes. That key
is identical in the original upload and the working copy, which is what restore needs.
"""
import hashlib
import re
from dataclasses import dataclass, field

from striprtf.striprtf import rtf_to_text

from .documents import _clean, parse_date, schema_of

# LOINC codes charted by BPSXMLViewer (common blood tests); results with other codes are listed but not charted.
CHART_LOINC = {
    '10334-1', '10535-3', '12841-3', '13965-9', '14334-7', '14631-6', '14635-7', '14646-4', '14647-2', '14685-2',
    '14771-0', '14804-9', '14805-6', '14920-3', '14927-8', '14928-6', '14933-6', '1742-6', '1743-4', '17856-6',
    '1920-8', '1988-5', '2064-4', '2157-6', '22748-8', '2276-4', '2324-2', '24108-3', '2532-0',
    '2601-3', '2823-3', '2857-1', '2885-2', '2890-2', '29265-6', '2951-2', '3016-3', '32294-1', '32309-7',
    '33914-3', '39469-2', '43396-1', '4537-7', '4544-3', '4548-4', '50210-4', '59261-8', '61152-5', '6690-2',
    '6768-6', '70204-3', '711-2', '718-7', '72160-5', '731-0', '742-7', '751-8', '777-3', '787-2', '789-8', '9830-1',
}


@dataclass
class Item:
    key: str
    section: str
    values: dict
    nodes: list
    date: str = ''
    date_sort: str = ''
    flag: str = ''
    size: int = 0

    @property
    def subject(self):
        return self.values.get('_label', '')

    @property
    def category(self):
        return SECTION_TITLES.get(self.section, self.section)


@dataclass
class Section:
    id: str
    title: str
    columns: list                      # [(field, label, css class)]
    items: list = field(default_factory=list)
    note: str = ''


SECTION_TITLES = {
    'alg': 'Allergies & reactions', 'pmh': 'Past history', 'rx': 'Current medications',
    'rxp': 'Past / ceased medications', 'imm': 'Immunisations', 'obs': 'Measurements & observations',
    'vis': 'Visits', 'res': 'Results (atomised)',
}

COLUMNS = {
    'alg': [('item', 'Substance', 'strong danger'), ('reaction', 'Reaction', ''), ('detail', 'Details', 'muted')],
    'pmh': [('year', 'Year', 'nowrap'), ('condition', 'Condition', 'strong'), ('detail', 'Details', 'muted')],
    'rx': [('drug', 'Drug', 'strong accent'), ('type', 'Type', ''), ('dose', 'Dose', ''),
           ('instructions', 'Instructions', ''), ('start', 'Started', 'nowrap'), ('last', 'Last prescribed', 'nowrap')],
    'rxp': [('drug', 'Drug', 'strong muted'), ('instructions', 'Instructions', 'muted'),
            ('reason', 'Indication / reason', 'muted'), ('start', 'Started', 'nowrap'), ('stopped', 'Stopped', 'nowrap')],
    'imm': [('date', 'Date', 'nowrap'), ('vaccine', 'Vaccine', 'strong'), ('batch', 'Batch', 'muted'), ('seq', 'Dose', '')],
    'obs': [('date', 'Date', 'nowrap'), ('name', 'Measurement', 'strong'), ('value', 'Value', '')],
    'vis': [('date', 'Date', 'nowrap'), ('doctor', 'Doctor', 'strong nowrap'), ('notes', 'Notes', 'notes')],
    'res': [('date', 'Date', 'nowrap'), ('loinc', 'LOINC', 'muted small'), ('name', 'Test', 'strong'),
            ('value', 'Value', ''), ('units', 'Units', 'muted'), ('range', 'Range', 'muted small'), ('flag', 'Flag', 'danger strong')],
}


def _rtf(text):
    if not text or not text.lstrip().startswith('{\\rtf'):
        return text or ''
    try:
        return rtf_to_text(text, errors='ignore').strip()
    except Exception:
        return text


def _year(v):
    v = (v or '').strip()
    return '' if v in ('', '0') else v


class _Builder:
    def __init__(self, doc):
        self.doc = doc
        self.seen = {}
        self.sections = {sid: Section(sid, SECTION_TITLES[sid], COLUMNS[sid]) for sid in SECTION_TITLES}

    def t(self, node, tag):
        return _clean(self.doc.child_text(node, tag))

    def add(self, sid, node, values, date_raw='', label='', flag=''):
        h = hashlib.sha1(self.doc.raw(node)).hexdigest()[:12]
        key = f'c{sid}-{h}'
        n = self.seen.get(key, 0)
        self.seen[key] = n + 1
        if n:
            key = f'{key}~{n}'
        date, sort = parse_date(date_raw)
        values = dict(values, _label=label)
        if any(c[0] == 'date' for c in COLUMNS[sid]):
            values.setdefault('date', date)
        self.sections[sid].items.append(Item(key, sid, values, [node], date, sort, flag, len(self.doc.raw(node))))


def _bp(b, root):
    doc = b.doc
    for cont in root.findall('Reactions'):
        for n in cont.findall('Reaction'):
            item = b.t(n, 'ITEMNAME')
            b.add('alg', n, {'item': item, 'reaction': b.t(n, 'REACTION'), 'detail': b.t(n, 'SEVERITY')}, label=item)
    for cont in root.findall('PastHistory'):
        for n in cont.findall('Condition'):
            cond = b.t(n, 'ITEMTEXT')
            b.add('pmh', n, {'year': _year(b.t(n, 'YEAR')), 'condition': cond, 'detail': b.t(n, 'DETAILS')}, label=cond)
    for cont in root.findall('CurrentRx'):
        for n in cont.findall('Medication'):
            drug = b.t(n, 'DRUGNAME')
            status = b.t(n, 'RXSTATUS')
            if status in ('1', '2'):
                b.add('rx', n, {'drug': drug, 'type': 'Regular' if status == '1' else 'PRN / occasional',
                                'dose': b.t(n, 'DOSE'), 'instructions': b.t(n, 'INSTRUCTIONS'),
                                'start': parse_date(b.t(n, 'FIRSTDATE'))[0], 'last': parse_date(b.t(n, 'LASTDATE'))[0]},
                      date_raw=b.t(n, 'FIRSTDATE'), label=drug)
            else:
                reason = b.t(n, 'INDICATION')
                if b.t(n, 'DELETIONREASON'):
                    reason = (reason + ' ' if reason else '') + f"(ceased: {b.t(n, 'DELETIONREASON')})"
                b.add('rxp', n, {'drug': drug, 'instructions': b.t(n, 'INSTRUCTIONS'), 'reason': reason,
                                 'start': parse_date(b.t(n, 'FIRSTDATE'))[0],
                                 'stopped': parse_date(b.t(n, 'DELETIONDATE') or b.t(n, 'LASTDATE'))[0]},
                      date_raw=b.t(n, 'DELETIONDATE') or b.t(n, 'LASTDATE'), label=drug)
    for cont in root.findall('Immunisations'):
        for n in cont.findall('Immunisation'):
            v = b.t(n, 'VACCINENAME')
            b.add('imm', n, {'vaccine': v, 'batch': b.t(n, 'BATCHNUMBER'), 'seq': b.t(n, 'SEQUENCE')},
                  date_raw=b.t(n, 'GIVENDATE'), label=v)
    for cont in root.findall('Observations'):
        for n in cont.findall('Observation'):
            name = b.t(n, 'DATANAME')
            b.add('obs', n, {'name': name, 'value': b.t(n, 'DATAVALUE')}, date_raw=b.t(n, 'OBSDATE'),
                  label=f"{name} {b.t(n, 'DATAVALUE')}")
    for n in root.iter('Visit'):
        doctor = b.t(n, 'DRNAME')
        b.add('vis', n, {'doctor': doctor, 'notes': _rtf(doc.child_text(n, 'VISITNOTES', strip=False))},
              date_raw=b.t(n, 'VISITDATE'), label=f'Visit {doctor}'.strip())
    for cont in root.findall('InvestigationValues'):
        for n in cont.findall('Result'):
            name = b.t(n, 'RESULTNAME')
            if not name:
                continue
            flag = b.t(n, 'ABNORMALFLAG')
            b.add('res', n, {'loinc': b.t(n, 'LOINCCODE'), 'name': name, 'value': b.t(n, 'RESULTVALUE'),
                             'units': b.t(n, 'UNITS'), 'range': b.t(n, 'RANGE'), 'flag': flag},
                  date_raw=b.t(n, 'REPORTDATE'), label=name, flag=flag)


def _md(b, root):
    doc = b.doc
    for cont in root.findall('REACTIONS'):
        for n in cont.findall('ALLERGY'):
            item = b.t(n, 'ITEM')
            kind = 'Adverse reaction' if b.t(n, 'ADR') == 'True' else (b.t(n, 'TYPE') or 'Allergy')
            b.add('alg', n, {'item': item, 'reaction': kind,
                             'detail': '' if b.t(n, 'SEVERITY') in ('', '0') else f"severity {b.t(n, 'SEVERITY')}"},
                  label=item)
    extra = root.find('ADDITIONAL_DEMOGRAPHICS')
    notes = [f'{lbl}: {b.t(extra, tag)}' for tag, lbl in (('ALLERGIES', 'Allergies (free text)'), ('WARNINGS', 'Warnings'))
             if extra is not None and b.t(extra, tag)]
    b.sections['alg'].note = ' · '.join(notes)
    for cont in root.findall('PMH'):
        for n in cont.findall('PMHITEM'):
            cond = b.t(n, 'CONDITION')
            detail = b.t(n, 'COMMENT') + (' (inactive)' if b.t(n, 'ACTIVE') == 'False' else '')
            b.add('pmh', n, {'year': _year(b.t(n, 'YEAR')), 'condition': cond, 'detail': detail.strip()}, label=cond)
    names = {}
    for cont in root.findall('SCRIPTS'):
        for n in cont.findall('SCRIPTITEM'):
            no, name = b.t(n, 'DRUG_NO'), b.t(n, 'P1')
            if no and name:
                names.setdefault(no, name)
    for cont in root.findall('RX'):
        for n in cont.findall('RXITEM'):
            drug = names.get(b.t(n, 'DRUG_NO'), f"Drug #{b.t(n, 'DRUG_NO')}")
            freq, special = b.t(n, 'FREQUENCY'), b.t(n, 'SPECIAL')
            instr = ' '.join(x for x in (b.t(n, 'DOSE'), freq) if x) + (f' ({special})' if special else '')
            if b.t(n, 'CEASED_DATE'):
                reason = b.t(n, 'INDICATION') or b.t(n, 'REASON')
                if b.t(n, 'DELREASON'):
                    reason = (reason + ' ' if reason else '') + f"(ceased: {b.t(n, 'DELREASON')})"
                b.add('rxp', n, {'drug': drug, 'instructions': instr, 'reason': reason,
                                 'start': parse_date(b.t(n, 'FIRSTDATE'))[0], 'stopped': parse_date(b.t(n, 'CEASED_DATE'))[0]},
                      date_raw=b.t(n, 'CEASED_DATE'), label=drug)
            else:
                prn = 'PRN' in (freq + special).upper()
                b.add('rx', n, {'drug': drug, 'type': 'PRN / occasional' if prn else 'Regular', 'dose': b.t(n, 'DOSE'),
                                'instructions': instr, 'start': parse_date(b.t(n, 'FIRSTDATE'))[0],
                                'last': parse_date(b.t(n, 'LASTDATE'))[0]},
                      date_raw=b.t(n, 'FIRSTDATE'), label=drug)
    for cont in root.findall('IMM'):
        for n in cont.findall('IMMUNISATION'):
            v = b.t(n, 'TYPE')
            b.add('imm', n, {'vaccine': v, 'batch': b.t(n, 'BATCH'), 'seq': b.t(n, 'SEQ')},
                  date_raw=b.t(n, 'IMMDATE'), label=v)
    for cont in root.findall('MEASURES'):
        for n in cont.findall('MEASUREMENT'):
            name = b.t(n, 'TYPE')
            b.add('obs', n, {'name': name, 'value': b.t(n, 'VALUE')}, date_raw=b.t(n, 'MEAS_DATE'),
                  label=f"{name} {b.t(n, 'VALUE')}")
    for cont in root.findall('PROGRESS'):
        for n in cont.findall('VISIT'):
            doctor = b.t(n, 'DR')
            reason = b.t(n, 'REASON')
            notes = _rtf(doc.child_text(n, 'NOTES', strip=False))
            b.add('vis', n, {'doctor': doctor, 'notes': (f'Reason: {reason}\n' if reason else '') + notes},
                  date_raw=b.t(n, 'VISITDATE'), label=f'Visit {doctor}'.strip())
    for cont in root.findall('ATOM'):
        for n in cont.findall('RESULT'):
            name = b.t(n, 'TESTNAME')
            b.add('res', n, {'loinc': b.t(n, 'LOINC'), 'name': name, 'value': b.t(n, 'RESULT'),
                             'units': b.t(n, 'UNITS'), 'range': b.t(n, 'RANGE'), 'flag': ''},
                  date_raw=b.t(n, 'RESULTDATE'), label=name)


def sections(doc):
    """[Section] in display order (cached on the immutable XmlDoc)."""
    cached = doc._cache.get('clinical')
    if cached is None:
        b = _Builder(doc)
        schema = schema_of(doc)
        if schema == 'bp':
            _bp(b, doc.root)
        elif schema == 'md':
            _md(b, doc.root)
        for sec in b.sections.values():
            if sec.id == 'res':
                sec.items.sort(key=lambda i: i.date_sort, reverse=True)
                sec.items.sort(key=lambda i: i.values.get('loinc', ''))
            elif sec.id == 'pmh':
                sec.items.sort(key=lambda i: int(i.values['year']) if i.values['year'].isdigit() else 0, reverse=True)
            elif sec.id not in ('alg',):
                sec.items.sort(key=lambda i: i.date_sort, reverse=True)
        cached = doc._cache['clinical'] = list(b.sections.values())
    return cached


def item_map(doc):
    return {i.key: i for s in sections(doc) for i in s.items}


def demographics(doc):
    schema = schema_of(doc)
    root = doc.root
    t = lambda n, tag: _clean(doc.child_text(n, tag))
    if schema == 'bp':
        d = root.find('Demographics')
        p = d.find('Patient') if d is not None else None
        if p is None:
            return {}
        addr = ', '.join(x for x in (t(p, 'ADDRESS1'), t(p, 'ADDRESS2'), t(p, 'CITY'), t(p, 'POSTCODE')) if x)
        return {'Name': ' '.join(x for x in (t(p, 'FIRSTNAME'), t(p, 'MIDDLENAME'), t(p, 'SURNAME')) if x),
                'Date of birth': parse_date(t(p, 'DOB'))[0], 'Medicare no.': t(p, 'MEDICARENO'),
                'Mobile': t(p, 'MOBILEPHONE'), 'Address': addr}
    if schema == 'md':
        p = root.find('DEMOGRAPHICS')
        if p is None:
            return {}
        addr = ', '.join(x for x in (t(p, 'ADDRESS'), t(p, 'ADDRESS2'), t(p, 'CITY'), t(p, 'POSTCODE')) if x)
        return {'Name': ' '.join(x for x in (t(p, 'TITLE'), t(p, 'FIRSTNAME'), t(p, 'MIDDLENAME'), t(p, 'SURNAME')) if x),
                'Date of birth': parse_date(t(p, 'DOB'))[0], 'Medicare no.': t(p, 'MC_NO'),
                'Mobile': t(p, 'MOB_PHONE') or t(p, 'PHONE'), 'Address': addr}
    return {}


# ---- charts --------------------------------------------------------------------------------------------

def _number(v):
    m = re.search(r'[-+]?\d*\.\d+|[-+]?\d+', v or '')
    return float(m.group(0)) if m else None


def _range(r):
    r = (r or '').strip()
    if not r or r.upper() == 'NIL':
        return None, None
    m = re.match(r'^\s*([-+]?\d*\.?\d+)\s*-\s*([-+]?\d*\.?\d+)\s*$', r)
    if m:
        return float(m.group(1)), float(m.group(2))
    if '<' in r or 'BELOW' in r.upper():
        return None, _number(r)
    if '>' in r or 'ABOVE' in r.upper():
        return _number(r), None
    return None, None


VITAL_NAMES = {
    'systolic': ('Blood pressure', 'Systolic', 'mmHg'), 'diastolic': ('Blood pressure', 'Diastolic', 'mmHg'),
    'pulse': ('Pulse', 'Pulse', '/min'), 'weight': ('Weight', 'Weight', 'kg'), 'height': ('Height', 'Height', 'cm'),
    'bmi': ('BMI', 'BMI', 'kg/m²'), 'temp': ('Temperature', 'Temperature', '°C'), 'spo2': ('SpO₂', 'SpO₂', '%'),
    'resp': ('Respiratory rate', 'Respiratory rate', '/min'), 'waist': ('Waist', 'Waist', 'cm'),
}


def charts(doc):
    """Chart series: lab results grouped by LOINC (codes in CHART_LOINC) and measurements by name."""
    secs = {s.id: s for s in sections(doc)}
    groups = {}
    for it in secs['res'].items:
        code = it.values.get('loinc', '')
        val = _number(it.values.get('value'))
        if code not in CHART_LOINC or val is None or not it.date_sort:
            continue
        g = groups.setdefault(('lab', code), {'title': it.values['name'], 'code': code, 'units': it.values.get('units', ''),
                                              'kind': 'Pathology', 'series': {}, 'ranges': []})
        if len(it.values['name']) < len(g['title']):
            g['title'] = it.values['name']
        g['series'].setdefault('Result', []).append((it.date_sort, val, it.date))
        lo, hi = _range(it.values.get('range'))
        if lo is not None or hi is not None:
            g['ranges'].append((lo, hi))
    for it in secs['obs'].items:
        name = re.sub(r'\s+', ' ', it.values.get('name', '')).strip()
        spec = VITAL_NAMES.get(name.lower())
        val = _number(it.values.get('value'))
        if val is None or not it.date_sort:
            continue
        title, series, units = spec if spec else (name.title(), name.title(), '')
        g = groups.setdefault(('vital', title), {'title': title, 'code': '', 'units': units, 'kind': 'Measurements',
                                                 'series': {}, 'ranges': []})
        g['series'].setdefault(series, []).append((it.date_sort, val, it.date))
    out = []
    known = {v[0] for v in VITAL_NAMES.values()}
    for (kind, _), g in groups.items():
        dates = sorted({p[0] for pts in g['series'].values() for p in pts})
        if not dates:
            continue
        if kind == 'vital' and g['title'] not in known and len(dates) < 2:
            continue                                   # one-off, unusual measurements are listed, not charted
        labels = {}
        for pts in g['series'].values():
            for sort, _v, disp in pts:
                labels[sort] = disp
        datasets = []
        for name, pts in g['series'].items():
            by_date = {}
            for sort, v, _d in pts:
                by_date[sort] = v                     # one value a day: the last listed
            datasets.append({'label': name, 'data': [by_date.get(d) for d in dates]})
        rng = max(set(g['ranges']), key=g['ranges'].count) if g['ranges'] else (None, None)
        out.append({'title': g['title'], 'code': g['code'], 'units': g['units'], 'kind': g['kind'],
                    'labels': [labels[d] for d in dates], 'datasets': datasets, 'range': list(rng),
                    'points': sum(len(p) for p in g['series'].values())})
    out.sort(key=lambda c: (c['kind'] != 'Measurements', c['title'].lower()))
    return out
