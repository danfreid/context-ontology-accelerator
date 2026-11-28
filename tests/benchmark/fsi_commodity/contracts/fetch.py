# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Download the public contracts listed in ``sources.json`` and write ``fetched.json``.

Originals go to ``originals/`` (gitignored). ``fetched.json`` (URL, resolved URL, sha256, bytes, pages,
size class, status) is committed. Nothing here calls AWS or ingests anything.

    uv run --frozen python -m tests.benchmark.fsi_commodity.contracts.fetch [--only src02]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).parent
ORIGINALS = HERE / "originals"
USER_AGENT = "coa-contract-corpus/1.0 (test corpus research; contact dan.reid.5550@gmail.com)"
LARGE_PAGES = 100
LARGE_BYTES = 5 * 1024 * 1024
WORDS_PER_PAGE = 500  # estimate for HTML sources


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _get(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed public https/http URLs from sources.json
        return resp.read(), resp.headers.get("Content-Type", "")


def pdf_pages(data: bytes) -> int | None:
    try:
        import pypdfium2
        return len(pypdfium2.PdfDocument(data))
    except Exception:
        return None


def classify(pages: int | None, size: int) -> str:
    return "large" if (pages is not None and pages >= LARGE_PAGES) or size >= LARGE_BYTES else "small"


def _resolve_justia(html: str, base: str) -> str | None:
    """A Justia contract page embeds or links to the underlying SEC exhibit."""
    p = _Text()
    p.feed(html)
    for link in p.links:
        if "sec.gov/Archives" in link:
            return link
    return None


def fetch_one(src: dict) -> dict:
    rec = {k: src[k] for k in ("id", "local_name", "match", "url") if k in src}
    if "local_pages" in src:
        rec["local_pages"] = src["local_pages"]
    try:
        data, ctype = _get(src["url"])
        resolved = src["url"]
        is_pdf = data[:5] == b"%PDF-"
        if not is_pdf and "justia.com" in src["url"]:
            link = _resolve_justia(data.decode("utf-8", "replace"), src["url"])
            if link:
                time.sleep(0.2)
                data, ctype = _get(link)
                resolved = link
                is_pdf = data[:5] == b"%PDF-"
        rec["resolved_url"] = resolved
        rec["content_type"] = ctype
        ext = "pdf" if is_pdf else ("html" if b"<html" in data[:4096].lower() or "html" in ctype else "bin")
        pages = pdf_pages(data) if is_pdf else None
        pages_estimated = False
        if ext == "html":
            tp = _Text()
            tp.feed(data.decode("utf-8", "replace"))
            words = len(re.findall(r"\w+", " ".join(tp.parts)))
            pages = max(1, math.ceil(words / WORDS_PER_PAGE))
            pages_estimated = True
        ORIGINALS.mkdir(exist_ok=True)
        fname = f"{src['id']}.{ext}"
        (ORIGINALS / fname).write_bytes(data)
        rec.update(
            status="ok", file=f"originals/{fname}", format=ext, sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data), pages=pages, pages_estimated=pages_estimated, size_class=classify(pages, len(data)),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        rec.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", help="source id; repeatable")
    args = ap.parse_args()
    sources = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))["sources"]
    out_path = HERE / "fetched.json"
    prior = {r["id"]: r for r in json.loads(out_path.read_text(encoding="utf-8"))["documents"]} if out_path.exists() else {}
    for src in sources:
        if args.only and src["id"] not in args.only:
            continue
        rec = fetch_one(src)
        prior[src["id"]] = rec
        print(src["id"], rec["status"], rec.get("pages"), rec.get("bytes"), rec.get("size_class", rec.get("error", "")))
        time.sleep(0.5)
    docs = [prior[s["id"]] for s in sources if s["id"] in prior]
    out_path.write_text(json.dumps({"documents": docs}, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
