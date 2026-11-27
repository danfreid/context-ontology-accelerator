# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Page-level PDF operations with deterministic bytes: merge with blank pages, and image-only "scans".

* ``merge`` imports whole pages with pdfium and inserts blank A4 pages where asked. pdfium writes a random
  trailer ``/ID``; it is replaced by a same-length digest of the file, so the cross-reference offsets stay valid
  and equal inputs give equal bytes.
* ``scan`` renders each page with pdfium, then skews, blurs, speckles and thresholds it with Pillow (seeded) and
  writes the bitmaps into a new PDF with ReportLab. The result has no text layer: one image per page, as a
  flatbed or fax scan would. Output depends on the pinned pypdfium2 and Pillow versions (see requirements.txt).
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Sequence
from io import BytesIO

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

BLANK = None  # a part of ``merge`` that is one blank A4 page

_ID = re.compile(rb"/ID\s*\[<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>\]")
_DATE = re.compile(rb"(/(?:CreationDate|ModDate)\(D:)(\d{14})")
_FIXED_DATE = b"20000101000000"


def _save(doc: pdfium.PdfDocument) -> bytes:
    buf = BytesIO()
    doc.save(buf)
    # pdfium stamps the wall clock into the new document's Info dict; pin it (same length, so offsets hold).
    data = _DATE.sub(lambda m: m.group(1) + _FIXED_DATE[: len(m.group(2))].ljust(len(m.group(2)), b"0"), buf.getvalue())
    found = _ID.search(data)
    if not found:
        return data
    (s1, e1), (s2, e2) = found.span(1), found.span(2)

    def put(buf: bytes, hex1: bytes, hex2: bytes) -> bytes:
        return buf[:s1] + hex1 + buf[e1:s2] + hex2 + buf[e2:]

    zeroed = put(data, b"0" * (e1 - s1), b"0" * (e2 - s2))
    digest = hashlib.sha256(zeroed).hexdigest().upper().encode() * 2
    return put(zeroed, digest[: e1 - s1], digest[: e2 - s2])


def page_count(pdf: bytes) -> int:
    """Number of pages in ``pdf``."""
    return len(pdfium.PdfDocument(pdf))


def merge(parts: Sequence[bytes | None]) -> tuple[bytes, list[tuple[int, int]]]:
    """Concatenate PDFs, with ``BLANK`` parts as single blank pages.

    Returns the merged bytes and, for each part, its ``(first, last)`` page numbers (1-based, inclusive).
    """
    if not parts:
        raise ValueError("nothing to merge")
    out = pdfium.PdfDocument.new()
    ranges: list[tuple[int, int]] = []
    keep: list[pdfium.PdfDocument] = []  # source documents must outlive the save
    page = 0
    width, height = A4
    for part in parts:
        if part is None:
            out.new_page(width, height)
            ranges.append((page + 1, page + 1))
            page += 1
            continue
        src = pdfium.PdfDocument(part)
        keep.append(src)
        n = len(src)
        out.import_pages(src)
        ranges.append((page + 1, page + n))
        page += n
    return _save(out), ranges


def scan(pdf: bytes, seed: int, *, dpi: int = 150, threshold: int = 170, speckles: int = 1200) -> bytes:
    """Return an image-only copy of ``pdf``: every page rasterised, skewed up to 0.8 degrees and binarised."""
    src = pdfium.PdfDocument(pdf)
    buf = BytesIO()
    width, height = A4
    c = canvas.Canvas(buf, pagesize=A4, invariant=1, pageCompression=1)
    c.setTitle("Scanned document")
    c.setAuthor("scanner")
    c.setCreator("fsi_commodity generator")
    for i in range(len(src)):
        img = src[i].render(scale=dpi / 72, grayscale=True).to_pil().convert("L")
        r = random.Random(seed * 1009 + i)
        img = img.rotate(r.uniform(-0.8, 0.8), resample=Image.Resampling.BICUBIC, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        px = img.load()
        assert px is not None
        for _ in range(speckles):
            px[r.randrange(img.width), r.randrange(img.height)] = r.choice((0, 96))
        bw = img.point(lambda v: 0 if v < threshold else 255, mode="1")
        c.drawImage(ImageReader(bw), 0, 0, width, height)
        c.showPage()
    c.save()
    return buf.getvalue()
