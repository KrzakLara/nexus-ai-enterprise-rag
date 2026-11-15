# Third-party notices

NEXUS AI's own source code is licensed under the **MIT License** (see [`LICENSE`](LICENSE), Copyright (c) 2026 Lara Krzak). **The MIT License applies only to the project's own code and content. It does not supersede, replace or relicense any third-party component**: each component below stays under its own license, and whoever redistributes it must meet that license's terms. This file records the third-party material in or used by this repository. Licenses were read from installed package metadata, the Debian copyright files in the API Docker image and the model card on 2026-11-14. This is not legal advice.

## 1. Third-party material committed to this repository

| Material | Where | License | Obligation |
|---|---|---|---|
| **DejaVu Sans / DejaVu Sans Bold** (subsets embedded by reportlab) | the 10 text-layer PDFs and the text pages of the mixed PDF in `eval/docintel/documents/`; glyphs rasterized into the scanned PDFs | Bitstream Vera Fonts license + public-domain DejaVu changes + Arev fonts license (full text in section 5) | Include the copyright and permission notice with copies of the Font Software. This file does that; it is the conservative reading for embedded subsets. The font files themselves are **not** committed. |

Everything else in the repository (code, synthetic documents, labels, reports, documentation) is authored for this project. All company names, people and documents in the corpora are fictional.

## 2. Dependencies installed from the lock files (not vendored)

They are not committed, so the repository itself has no redistribution obligation for them. **Whoever distributes a built artifact**, such as the API Docker image or the frontend bundle, distributes these components and must keep their notices. Python packages keep their license files in `site-packages/*.dist-info/`, and Debian packages in `/usr/share/doc/*/copyright`.

| Component | License (as declared) | Note |
|---|---|---|
| FastAPI, SQLAlchemy, Alembic, Pydantic, pgvector (Python), pdfplumber, pdfminer.six, python-docx, openpyxl, PyJWT | MIT | |
| **psycopg / psycopg-binary** | **LGPL-3.0-only** | Used as an unmodified, separately installed library that Python imports dynamically. When distributing the image, keep its license text (present in its `dist-info`) and keep users able to replace the library (a pip install does). Modified copies would carry LGPL obligations. |
| pypdf, uvicorn | BSD-3-Clause | |
| **pypdfium2** | BSD-3-Clause, Apache-2.0, plus licenses of the bundled **PDFium** binary and its dependencies | Its wheel ships those license files; keep them in distributions. |
| Pillow | MIT-CMU (HPND) | |
| pytesseract, openai, sentence-transformers, transformers | Apache-2.0 | Keep NOTICE files where present. |
| PyTorch (CPU) | BSD-style with Apache-2.0 parts (`Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause ...`) | Many bundled third-party licenses in the wheel. |
| NumPy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | |
| reportlab (dev / dataset generation only) | BSD | Not installed in the API image. |
| React, react-dom, Vite (build) | MIT | |
| **lucide-react** (icons bundled into the frontend build) | ISC | Keep the copyright notice when distributing the built bundle. |
| TypeScript (build only) | Apache-2.0 | |

## 3. API Docker image (`backend/Dockerfile`)

- Base image `python:3.12-slim` on Debian GNU/Linux 13 (trixie): Debian packages under their own licenses, with copyright files in `/usr/share/doc/`.
- **Tesseract OCR** (`tesseract-ocr`) and its language data (`tesseract-ocr-eng`, `tesseract-ocr-hrv`): **Apache-2.0** (Debian copyright files).
- Publishing the image means distributing all of the above; the in-image license files must stay intact.

## 4. Models and fonts fetched at runtime (not redistributed by this repository)

| Component | License | Note |
|---|---|---|
| **intfloat/multilingual-e5-small** (embedding model, downloaded from Hugging Face into the `model_cache` volume) | **MIT** (model card) | Not committed. Shipping an image or volume that contains the weights distributes them: keep the model card / license. |
| DM Sans, Manrope (Google Fonts, loaded from `fonts.googleapis.com` by the frontend) | SIL Open Font License 1.1 | Not redistributed (loaded from Google's CDN). |
| PostgreSQL + pgvector (`pgvector/pgvector:pg16` image, used as is) | PostgreSQL License | Not modified or redistributed by this repository. |

## 5. DejaVu fonts license (verbatim)

```
Fonts are (c) Bitstream (see below). DejaVu changes are in public domain.
Glyphs imported from Arev fonts are (c) Tavmjong Bah (see below)

Bitstream Vera Fonts Copyright
------------------------------

Copyright (c) 2003 by Bitstream, Inc. All Rights Reserved. Bitstream Vera is
a trademark of Bitstream, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of the fonts accompanying this license ("Fonts") and associated
documentation files (the "Font Software"), to reproduce and distribute the
Font Software, including without limitation the rights to use, copy, merge,
publish, distribute, and/or sell copies of the Font Software, and to permit
persons to whom the Font Software is furnished to do so, subject to the
following conditions:

The above copyright and trademark notices and this permission notice shall
be included in all copies of one or more of the Font Software typefaces.

The Font Software may be modified, altered, or added to, and in particular
the designs of glyphs or characters in the Fonts may be modified and
additional glyphs or characters may be added to the Fonts, only if the fonts
are renamed to names not containing either the words "Bitstream" or the word
"Vera".

This License becomes null and void to the extent applicable to Fonts or Font
Software that has been modified and is distributed under the "Bitstream
Vera" names.

The Font Software may be sold as part of a larger software package but no
copy of one or more of the Font Software typefaces may be sold by itself.

THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF COPYRIGHT, PATENT,
TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL BITSTREAM OR THE GNOME
FOUNDATION BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, INCLUDING
ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF
THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER DEALINGS IN THE
FONT SOFTWARE.

Except as contained in this notice, the names of Gnome, the Gnome
Foundation, and Bitstream Inc., shall not be used in advertising or
otherwise to promote the sale, use or other dealings in this Font Software
without prior written authorization from the Gnome Foundation or Bitstream
Inc., respectively. For further information, contact: fonts at gnome dot
org. 

Arev Fonts Copyright
------------------------------

Copyright (c) 2006 by Tavmjong Bah. All Rights Reserved.

Permission is hereby granted, free of charge, to any person obtaining
a copy of the fonts accompanying this license ("Fonts") and
associated documentation files (the "Font Software"), to reproduce
and distribute the modifications to the Bitstream Vera Font Software,
including without limitation the rights to use, copy, merge, publish,
distribute, and/or sell copies of the Font Software, and to permit
persons to whom the Font Software is furnished to do so, subject to
the following conditions:

The above copyright and trademark notices and this permission notice
shall be included in all copies of one or more of the Font Software
typefaces.

The Font Software may be modified, altered, or added to, and in
particular the designs of glyphs or characters in the Fonts may be
modified and additional glyphs or characters may be added to the
Fonts, only if the fonts are renamed to names not containing either
the words "Tavmjong Bah" or the word "Arev".

This License becomes null and void to the extent applicable to Fonts
or Font Software that has been modified and is distributed under the 
"Tavmjong Bah Arev" names.

The Font Software may be sold as part of a larger software package but
no copy of one or more of the Font Software typefaces may be sold by
itself.

THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL
TAVMJONG BAH BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.

Except as contained in this notice, the name of Tavmjong Bah shall not
be used in advertising or otherwise to promote the sale, use or other
dealings in this Font Software without prior written authorization
from Tavmjong Bah. For further information, contact: tavmjong @ free
. fr.

$Id: LICENSE 2133 2007-11-28 02:46:28Z lechimp $
```
