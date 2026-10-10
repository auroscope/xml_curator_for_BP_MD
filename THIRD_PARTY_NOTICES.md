# Third-party software

xml_curator_for_BP_MD's own code is MIT-licensed (see `LICENSE`). The only third-party code in this repository is
Chart.js (below), included unmodified with its licence. Everything else is installed separately — by `install.sh` from
your distribution's repositories (apt) and from PyPI (pip), or by you — and each component is used unmodified under its
own licence.

## Python packages (installed into `.venv` from PyPI)

| Package | Licence | Used for |
|---|---|---|
| Flask, Werkzeug, Jinja2, MarkupSafe, itsdangerous, click | BSD-3-Clause | web application |
| blinker | MIT | Flask dependency |
| gunicorn | MIT | production web server |
| Pillow | MIT-CMU (bundled image libraries: permissive licences) | images, TIF viewing, picture re-encoding |
| striprtf | BSD-3-Clause | RTF to text |
| pikepdf | MPL-2.0 (bundles qpdf: Apache-2.0; GCC runtime libraries: GPL-3.0 with the GCC Runtime Library Exception) | PDF editing |
| img2pdf | LGPL-3.0-or-later | wrapping compressed scans as PDF |
| fonttools | MIT | font subsetting |
| reportlab | BSD | text-only PDFs, hidden OCR text layer |
| olefile | BSD-2-Clause | reading Outlook .msg files |
| lxml | BSD-3-Clause | pikepdf dependency |
| charset-normalizer | MIT | reportlab dependency |
| packaging | Apache-2.0 OR BSD-2-Clause | dependency of other packages |
| pytest, pluggy, iniconfig (development only) | MIT | tests |
| Pygments (development only) | BSD-2-Clause | pytest dependency |

## Programs the app runs (separate processes; installed with apt)

| Program | Licence | Used for |
|---|---|---|
| Ghostscript | AGPL-3.0-or-later (commercial licences available from Artifex) | rendering scanned pages for compression and OCR |
| poppler-utils (pdftotext, pdfinfo, pdfimages, pdftoppm) | GPL-2.0 / GPL-3.0 | PDF analysis, text verification, page rendering |
| Tesseract OCR and its English data | Apache-2.0 | built-in OCR |
| LibreOffice Writer (optional) | MPL-2.0 | RTF previews (HTML and page view) |
| DejaVu fonts | Bitstream Vera licence (free; embedding permitted) | fonts embedded in text-only PDFs |
| Python 3 | PSF-2.0 | runtime |

These programs are not linked into the app or distributed with it; the app calls them as separate commands.

## Optional service

| Software | Licence | Used for |
|---|---|---|
| liteparse (`@llamaindex/liteparse`), reached over HTTP when available | Apache-2.0 | OCR (instead of Tesseract) |

## Included in this repository

| Library | Licence | Used for |
|---|---|---|
| Chart.js 4.4.1 (`static/vendor/chart.umd.js`, from the npm package `chart.js`; licence in `static/vendor/chart.js-LICENSE.md`) | MIT | trend charts, served by the app itself (no internet needed) |

## If you redistribute a bundle

Installing with `install.sh` downloads each component from its usual source. If you instead distribute the app
together with these components (for example a Docker or VM image), you are also distributing them: include their
licence texts and, for the GPL/AGPL programs (Ghostscript, poppler), make their corresponding source available.
