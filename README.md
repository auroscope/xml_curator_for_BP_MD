# xml_curator_for_BP_MD

For General Practice administrators and IT support staff.

Review and curate patient EHR XML exports (Best Practice `BPSEHRV2` and MedicalDirector `<PATIENT>` exports) before
importing them into Best Practice:

- **View** every attached document, delete what isn't needed, and edit document details (date, sender, recipient,
  category, subject) where needed.
- **Shrink** bloated PDF and RTF files by up to 90% without loss of readability, document by document or in bulk
  (with a final review before anything is changed).
- **Compress a single PDF or RTF file** on its own (no XML needed) and download the smaller version.
- **Install** with the included scripts on an existing Ubuntu host, or in a Windows WSL2 virtual machine using
  PowerShell.

## Runs entirely on your local computer or a host on your LAN

xml_curator_for_BP_MD is installed and run locally. It is **not a cloud service**: "uploading" a file means sending it
from your browser to the app on your own machine, where it is stored in the app's `data/` folder.

- **Works offline.** Installing needs an internet connection once (to download packages). After that the app needs no
  internet connection, and can be used in a browser on the same computer with the machine disconnected from any
  network. Install with `--local-only` (see [Setup](#setup)) to make it reachable from that computer alone; by default
  it can also be reached from other computers on your network.
- **No accounts, analytics or telemetry.** It sends nothing anywhere and loads nothing from the internet; all its
  scripts, styles and libraries are served by the app itself.
- **The one optional exception is OCR.** By default text is read from scans with Tesseract on the same machine. It can
  instead use a [liteparse](#ocr-engines) service that you run and configure, which then receives the PDFs to read
  (`CURATOR_OCR=tesseract` keeps OCR on this machine even when a liteparse service is available).

## How files are handled

- **The uploaded XML is never modified.** It is stored read-only (`data/sources/<workspace>/`, mode 0444).
- Every edit goes to a **working copy** (`data/working/<workspace>/`), which is what you download
  ("Download curated XML" → `<name>_curated_DDMMYYYY.xml`, dated the day of download).
- Edits are **byte-level splices**: the XML is never re-serialised. Only the changed elements' bytes change;
  the encoding, declaration, BOM, whitespace and everything else stay identical to the original.
- Saves are atomic (temp file + rename) under a file lock, and the result is re-parsed before it is written.
- Every change is recorded in a change log (viewable, and downloadable as CSV/JSON, or as a **PDF** to file alongside
  the curated XML, e.g. by importing it into the patient's record: it lists the patient, original and curated files and
  sizes, a summary and every change in effect, and the full history). Deleted documents can be
  restored individually from the original, and **Reset** restores the whole working copy.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Workspaces, safe save, document list and viewer (both formats), delete/restore, change log | done |
| 2 | Document metadata editor: date, from, to, category, subject (MedicalDirector: kept in sync with the document-handling record) | done |
| 3 | Shrink tabs: Original / Strip images / Compressed scan, background pre-compute of scans > 500 KB | done |
| 4 | Text-only tab: OCR (Tesseract, or liteparse when available), editable text, transcription footer, searchable compressed scans | done |
| 5 | Clinical summary (demographics, allergies, history, medications, immunisations, measurements, visits, results, reports, letters) with delete/restore, and trend charts | done |
| 6 | Review queue, bulk actions, duplicate detection | done |

## Document details (metadata)

| Field | Best Practice document | BP investigation | BP outgoing letter | MedicalDirector document (all parts + handling item) |
|---|---|---|---|---|
| Date | `CORRESPONDENCEDATE` | `REPORTDATE` | `CORRESPONDENCEDATE` | `DOCDATE` + `ITEM/DOCUMENT_DATETIME` (time part kept) |
| From | `CONTACTNAME` | `PROVIDERNAME` | — | `ITEM/DOCUMENT_FROM` (the sender) |
| To | — | — | `CONTACTNAME` | `DOCTORNAME` + `ITEM/DOCUMENT_TO` |
| Category | `CATEGORY` | — | — | `DOCTYPE` + `ITEM/DOCUMENT_TYPE` |
| Subject | `SUBJECT` | `TESTNAME` | `SUBJECT` | `DOCTITLE` + `ITEM/SUBJECT` |

Only fields whose value changed are written. A second copy (the MedicalDirector `DOCUMENTHANDLING/ITEM`) is
updated only when it currently agrees with the document (ignoring case); a copy that already disagreed keeps
its own value. "Type" is the file format and is read-only. In MedicalDirector exports `DOCTORNAME` is the
practice doctor the document was addressed to, not its author.

## Working through a large file

- **Queue:** the shrink screen shows *PDF n of N* (ordered by size in the original upload, largest first) with
  ◀ / ▶ and **Save & next** on every tab, which saves and opens the next PDF not yet shrunk. The header shows
  the running total (original → working copy).
- **Bulk** page: one click applies **Strip images** to every text-layer PDF whose text verified identical with no
  warnings, and another applies **Compressed scan** to every scan (after ticking that black-and-white copies are
  acceptable). Only options that save at least 10% qualify; everything else (warnings, mixed pages, small
  savings) is listed for review one by one. **Text only** is never applied in bulk. Opening the page prepares the
  options for every PDF in the background (OCR included) and shows progress. Each bulk change is logged per
  document (marked *bulk*) and can be restored individually.
- **Duplicates:** documents whose files are byte-for-byte identical (content of at least 64 bytes; every part must
  match) are badged *duplicate* in the document list and grouped on the Bulk page, where the copies after the
  first are pre-ticked for deletion. Their details can differ, so the groups show them side by side.

On the 81 MB MedicalDirector sample, the two bulk actions take the file from 81.5 MB to 18.4 MB, leaving 8 PDFs
for review and 9 duplicate groups (2.4 MB) to check.

## Summary and charts

**Summary** shows every clinical section of the export in collapsible cards, each row with a checkbox:
demographics, allergies & reactions, past history, current and past medications, immunisations,
measurements & observations, visits (RTF notes as text), atomised results (abnormal flags highlighted), text
reports and outgoing letters. Selected rows are deleted from the working copy in one step and listed under
their section with **Restore**.

| Section | Best Practice | MedicalDirector |
|---|---|---|
| Allergies | `Reactions/Reaction` | `REACTIONS/ALLERGY` (+ free-text `ADDITIONAL_DEMOGRAPHICS/ALLERGIES`, `WARNINGS` shown as a note) |
| Past history | `PastHistory/Condition` | `PMH/PMHITEM` |
| Medications | `CurrentRx/Medication` (RXSTATUS 1/2 current) | `RX/RXITEM` (names from `SCRIPTS`; `CEASED_DATE` = past) |
| Immunisations | `Immunisations/Immunisation` | `IMM/IMMUNISATION` |
| Measurements | `Observations/Observation` | `MEASURES/MEASUREMENT` |
| Visits | `Visit` | `PROGRESS/VISIT` |
| Results | `InvestigationValues/Result` | `ATOM/RESULT` |
| Reports | `Investigation` without attachment (RTF) | `IX/RESULT` (header + RTF), now also in the document viewer with editable details |

Clinical items are only deleted or restored, never edited, so each is identified by a hash of its exact bytes,
which is the same in the original and the working copy. MedicalDirector pathology reports are linked to their
`DOCUMENTHANDLING` item by `PATHOLOGY_ID`; a report that shares its GUID with an attached scanned document leaves
that item to the document.

**Charts** plot measurements (blood pressure as systolic + diastolic, pulse, weight, height, BMI, temperature,
SpO₂, …) and the pathology results whose LOINC codes BPSXMLViewer charted, with the most common normal range
shaded. One-off unusual measurements are listed but not charted. Charts print one per block. Chart.js 4.4.1 (MIT) is
bundled in `static/vendor/`, so charts work offline.

## Shrinking PDFs

Every PDF has a 🗜 icon (list) / **Shrink…** button (viewer) that opens a tabbed screen with the document's
details and one tab per option, each showing the resulting size in the XML and its own Save button.
**Cancel** returns to the document list without changing anything.

| Option | For | How | Check |
|---|---|---|---|
| Original | all | Keeps the PDF; Save writes only the details | — |
| Strip images | PDFs with a text layer | pikepdf removes image-drawing operators and images (pages, forms, tiling patterns); embedded TrueType fonts are subset to the glyphs used, keeping glyph ids | Words compared with the original; font subsetting kept only if every page renders pixel-identical |
| Compressed scan | scans | Ghostscript renders greyscale, thresholded to black and white (no dithering), stored as CCITT G4 via img2pdf | Visual: compare the preview with the original |
| Compressed scan | PDFs with a text layer (incl. OCR'd scans, e.g. Acrobat "Paper Capture") | Images re-encoded without colour: black-and-white content (text fragments, signatures, scanned areas) as 1-bit images up to 300 dpi, photos and shaded images as 150 dpi greyscale; text untouched; fonts subset as above | Words compared with the original |

### RTF documents

RTF letters and documents that contain pictures (files, or letter text in BP outgoing correspondence) get the 🗜
icon too. Most of their size is usually pictures stored far larger than they are shown (e.g. a 1 MB JPEG shown as
an 8 x 1 cm letterhead, or an uncompressed 150 KB bitmap logo shown 3 x 3 cm). The result is always still RTF.

| Option | How | Check |
|---|---|---|
| Compress pictures | each raster picture (JPEG, PNG, bitmap) re-saved at its displayed size at 150 dpi, as PNG or JPEG, keeping its displayed size; WMF/EMF vector pictures kept; Word's ignorable housekeeping blocks (theme data, revision tables, …) dropped | text identical; preview |
| Remove pictures | every picture removed, with Word's shape/fallback wrappers | text identical; warns that pictures might be clinical content |

RTFs are shown as LibreOffice renders them (HTML with pictures; **Page view** gives the exact printed layout as PDF),
cached by content. Text-style reports laid out with `|` separators and no pictures keep the text view, which turns
them into tables. The Bulk page has a **Compress RTF pictures** action; removing pictures is never done in bulk.
LibreOffice (`soffice`) is needed for the rendering; without it RTFs fall back to the text view.

### Text only

The fourth tab replaces the PDF with a plain, reflowed text PDF (A4, proportional font) holding only the text:

- **Source of the text:** the PDF's own text layer when it has one; otherwise OCR (for mixed PDFs, OCR for the
  scanned pages only). The initial text joins the lines of each paragraph; short lines (addresses, headings),
  list items and table rows (lines with wide gaps, set in a monospaced font scaled to fit) stay on their own lines.
- **Editing:** the text box is exactly what will be printed. **🖼 Original** opens the original in one reusable
  window to check against; the preview updates as you type; unsaved edits are kept as a draft in the browser;
  **Reset** returns to the original text.
- **Footer:** every page carries a notice, e.g. *"Text transcribed by OCR (liteparse 2.15.1) from the original
  scanned document (3.7 MB, 4 pages) on 09/10/2026. Images removed; the text was reviewed and edited."*
- **Check:** the built PDF must contain every character of the text in order, and every character must have a
  glyph in the font; otherwise saving needs an explicit confirm. Word-processor symbol bullets (private-use
  codes) are shown and printed as "•".
- **Size:** text that fits the Western character set uses the PDF base fonts (not embedded), typically
  3–20 KB a document; other text uses embedded DejaVu fonts.
- **Audit:** the saved text is kept in `data/state/<workspace>/texts/`, and the change log records the engine
  and how many words were edited.

Compressed scans also get an invisible OCR text layer (a few KB a page), so they stay searchable.

### OCR engines

| Setting (`CURATOR_OCR`) | Behaviour |
|---|---|
| `auto` (default) | liteparse if `CURATOR_LITEPARSE_URL` (default `http://localhost:5004`) answers `GET /health` with `status: ok`, else Tesseract |
| `tesseract` | always Tesseract (built in; pages rendered at the scan's resolution, 200–300 dpi) |
| `liteparse` | always liteparse; fails if it is not reachable |

The app only calls liteparse's existing `GET /health` and `POST /parse`. The engine in use is shown on the
start page.

Ghostscript is not used to rewrite PDFs that have a text layer: its pdfwrite drops the Unicode mapping of
ligature glyphs, so "examination" would extract as "examina on". Options with warnings (text differences,
pages whose content may be an image, no saving) need "I have checked the preview" ticked before Save.

Candidates are cached per document payload (`data/state/<workspace>/shrink/`), so they can never be applied to
a document that changed after the preview. After upload, scans of 500 KB or more are prepared in the
background, largest first; everything else is prepared when opened.

### Compressing a single file

`/file_compress` (linked from the start page) takes any one PDF or RTF file, by drag and drop or by choosing it,
with no XML involved. It offers the same options as the shrink screen, on tabs with previews, and each has a
**Download** button. The download is named after the original, e.g. `<name>_compressed.pdf`, `<name>_no_images.pdf`
or `<name>_text_only.pdf`. The text-only version can be edited before download.

- Only PDF and RTF files are accepted, checked by their content rather than their name.
- Uploaded files are kept in `data/files/` and removed 24 hours after upload (`CURATOR_FILE_KEEP_HOURS`),
  or straight away with **Delete**.

## Setup

### Install options

| Your situation | Use | What it does |
|---|---|---|
| **Windows 11 PC, no Linux set up yet.** You want the app in its own isolated Linux VM. | [`install_scripts/create_xml_curator_wsl2_vm.ps1`](install_scripts/create_xml_curator_wsl2_vm.ps1)<br>Guide: [`readme_create_xml_curator_wsl2_vm.md`](install_scripts/readme_create_xml_curator_wsl2_vm.md) | Builds a **new** hardened WSL2 Ubuntu instance (SSH, fail2ban, mirrored networking), installs the app with `install.sh` as the `xml-curator` service, and creates the Windows and Hyper-V firewall rules for the app and SSH. The one-stop option for Windows. |
| **An Ubuntu system already exists** (bare metal, VM, cloud, or a WSL2 Ubuntu you set up). | [`install_scripts/setup_xml_curator.sh`](install_scripts/setup_xml_curator.sh) | Run as a normal sudo user. Checks the port is free, clones the repo to `~/xml_curator_for_BP_MD`, runs `install.sh` (dependencies, `.venv`, service running as you, starts at boot) and opens the port in `ufw`, for your local network only or from anywhere (you choose). Re-run it to update. It does **not** harden the operating system or SSH. |
| **Windows 11 PC, you want a hardened Ubuntu WSL2 VM with SSH but not the app** (yet). | [`install_scripts/create_wsl2_ubuntu_sandbox.ps1`](install_scripts/create_wsl2_ubuntu_sandbox.ps1)<br>Guide: [`readme_wsl2_ubuntu_sandbox.md`](install_scripts/readme_wsl2_ubuntu_sandbox.md) | Builds a **new** hardened WSL2 Ubuntu instance with no application. You can then run `setup_xml_curator.sh` inside it. |
| **You already have the repository on Debian/Ubuntu.** | [`install.sh`](install.sh) (below) | Installs the dependencies and, with `--service`, the service. Used by the other scripts. |
| **Another operating system, or by hand.** | [Manual setup](#manual-setup) below | Step-by-step commands. |

The PowerShell scripts run from an **elevated (Administrator)** Windows PowerShell 5.1, are interactive (instance name,
ports, user, password, firewall scope; passwords are never stored), create a **new** WSL instance and never modify an
existing one, and check Windows 11 22H2+ and WSL 2.4.4+ first (offering to install or update WSL; a reboot may be
needed). They need only the script file itself. If a script was downloaded, unblock it first with
`Unblock-File .\<script>.ps1`. To run one from a desktop shortcut as Administrator, see
[`win_powershell_shortcut_how-to.md`](install_scripts/win_powershell_shortcut_how-to.md).

Windows (new isolated VM with the app), from an elevated PowerShell in the folder holding the script:

```powershell
powershell.exe -NoExit -ExecutionPolicy Bypass -File .\create_xml_curator_wsl2_vm.ps1
```

Existing Ubuntu system, as your normal sudo user (not root):

```bash
curl -fsSLO https://raw.githubusercontent.com/auroscope/xml_curator_for_BP_MD/main/install_scripts/setup_xml_curator.sh
less setup_xml_curator.sh          # read it first
chmod +x setup_xml_curator.sh && ./setup_xml_curator.sh
```

**Before you run them:** read the scripts first (they install software, create users and change firewall rules). This
application handles patient health data: when asked who may reach it, choose **local network only** unless you have a
specific reason not to, and keep the host on a trusted network. On Windows, WSL stops an idle instance, which takes the
app offline; the VM script can create a scheduled task that keeps it running (see its guide).

### install.sh

On Debian / Ubuntu, the installer does everything (it asks before changing anything):

```bash
git clone https://github.com/auroscope/xml_curator_for_BP_MD.git
cd xml_curator_for_BP_MD
./install.sh --service          # or ./install.sh without --service to start it by hand
```

It installs the system packages with apt (Ghostscript, poppler-utils, Tesseract with English, DejaVu fonts,
LibreOffice Writer, Python 3 venv/pip), creates `.venv` with the Python packages from `requirements.txt`, checks the
installation and, with `--service`, runs the app as the systemd service `xml-curator` (port 5008, starts at boot).
Options: `--port N`, `--local-only` (listen on 127.0.0.1: usable from this computer only), `--user NAME`,
`--name NAME` (service name), `--no-apt` (install system packages yourself), `--no-libreoffice` (RTFs shown as
text), `--yes`, `--dry-run`; see `./install.sh --help`. It is safe to run again to update everything.

The installer downloads each dependency from its usual source (apt repositories, PyPI); nothing third-party is
included in this repository. Dependencies and their licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

<a id="manual-setup"></a>
Manual setup elsewhere: install Python 3.11+, Ghostscript, poppler-utils, Tesseract (+ English data) and optionally
LibreOffice, then:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                 # dev server on http://localhost:5008
```

For production, `deploy/xml-curator.service` is an example systemd unit (gunicorn, port 5008). After pulling new
code: `sudo systemctl restart xml-curator`; status and logs: `systemctl status xml-curator`,
`journalctl -u xml-curator -f`.

Settings (environment variables): `CURATOR_DATA` (data directory, default `./data`), `PORT` (dev server,
default 5008), `CURATOR_HOST` (dev server address, default `0.0.0.0`; `127.0.0.1` = this computer only), `CURATOR_MAX_UPLOAD_MB` (default 500), `CURATOR_SECRET` (session key; otherwise generated in
`data/.secret_key`), `CURATOR_OCR` / `CURATOR_LITEPARSE_URL` (see OCR engines), `CURATOR_FILE_KEEP_HOURS`
(how long single files for `/file_compress` are kept, default 24).

## Tests

```bash
.venv/bin/python -m pytest -q
CURATOR_REAL_TESTS=1 CURATOR_SAMPLES_DIR=/path/to/exports .venv/bin/python -m pytest -q   # also real exports (slow)
```

Test fixtures are synthetic; no patient data is committed. `data/` and all `*.xml` files are git-ignored.

## Layout

```
app.py                  Flask routes
curator/xmlstore.py     byte-offset XML index and splice edits
curator/workspace.py    sources / working copies / locking / atomic save / change log
curator/documents.py    document records for both export formats, delete and restore
curator/render.py       showing a document part in the browser
curator/pdfops.py       PDF analysis, strip images, compress, font subsetting, payload encoding
curator/shrink.py       shrink candidates (cache, background jobs, pre-compute)
curator/filecompress.py single PDF/RTF files compressed on their own (/file_compress)
curator/ocr.py          Tesseract / liteparse OCR
curator/textpdf.py      initial text (reflow), text-only PDF, verification, hidden text layer
curator/clinical.py     clinical sections, demographics and chart series for both formats
curator/rtfops.py       RTF pictures: find, compress, remove; text verification
curator/officeconv.py   LibreOffice conversion (RTF -> HTML / PDF), cached
curator/msgfile.py      Outlook .msg reader
install.sh              installer for Debian/Ubuntu (dependencies, .venv, systemd service)
install_scripts/        Windows WSL2 provisioning (PowerShell), Ubuntu setup script, and their guides
templates/, static/     UI
tests/                  pytest suite
```

## License

MIT
