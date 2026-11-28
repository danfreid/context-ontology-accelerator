# Contract test corpus (public master agreements and derivatives)

Extra contract test cases for the FSI/commodity benchmark. Nothing here calls AWS, Bedrock or any LLM, and nothing is ingested.

| File | Purpose |
|---|---|
| `sources.json` | The 14 public sources (URL, match quality). `naesb_Cale_Case_Workshop_Presentations.pdf` is skipped (no public match). |
| `fetch.py` | Downloads the sources into `originals/` (gitignored) and writes `fetched.json`. Sends a descriptive User-Agent with contact email. |
| `fetched.json` | Per source: resolved URL, sha256, bytes, pages, size class, or the failure. |
| `derive.py` | Builds `derivatives/*.pdf` (synthetic contracts in our own words, parties from `corpus/universe.json`), `cases.json` and `catalog.json`. |
| `catalog.json` | Every document (as-is and derivative) with pages, bytes, sha256, `size_class` and `ingest` (`eligible` or `deferred`). |
| `cases.json` | Questions and gold answers in the `golden_answers.json` shape, plus `doc` and `origin` (`as_is` or `derivative`). |

```bash
uv run --frozen python -m tests.benchmark.fsi_commodity.contracts.fetch            # download originals (network)
uv run --frozen python -m tests.benchmark.fsi_commodity.contracts.derive [--check] # rebuild or verify derivatives
uv run --frozen pytest tests/unit/benchmark_fsi/test_fsi_contracts.py
```

## Size tags
A document is `large` at 100 pages or more, or 5 MB or more. `src02` (Cleco credit agreement) is tagged large because the user's local copy is 287 pages, although the public substitute has 91. Anything `large` or not downloaded is `ingest: deferred`. Ingesting large documents is a later user decision.

## Not downloaded
`src04`, `src07` and `src10` (Justia and San Jose URLs) returned HTTP 403 to scripted fetches. They are recorded as failed in `fetched.json` and carry no as-is test cases.
HTML sources (`src01`) have an estimated page count (about 500 words per page).

## Copyright
EEI, ISDA and NAESB forms are copyrighted. The originals are not committed. Derivatives contain no third-party text. Committing the originals needs the user's approval.
