# Pinned ontology snapshots

Turtle snapshots of published FSI and trade ontologies, generated from their upstream sources and pinned by
SHA-256 in [`manifest.yaml`](manifest.yaml). They are the deterministic, offline counterpart to the live pack
[`config-fsi-commodity.yaml`](../../../../packages/ontology-engine/demo/config-fsi-commodity.yaml), whose source
URLs change with each upstream release. They also cover two standards the live pack cannot fetch, because neither
publishes OWL at a URL: ISDA CDM and the DCSA electronic bill of lading.

| Key | Classes | Source | Licence |
|---|---:|---|---|
| `bfo-2020` | 36 | Basic Formal Ontology 2020 | CC-BY-4.0 |
| `fibo` | 1244 | FIBO, combined subset | MIT |
| `fibo-be`, `fibo-debt`, `fibo-debt-instruments`, `fibo-derivatives`, `fibo-loans`, `fibo-securities` | 114–492 each | FIBO modules | MIT |
| `gleif-lei` | 40 | GLEIF Base, L1, L2 | GLEIF terms of use |
| `uncefact` | 542 | UN/CEFACT Web Vocabulary, subset | GPL-3.0-or-later (UN/CEFACT vocabulary repo) |
| `dcsa-ebl` | 106 | DCSA eBL, converted from OpenAPI | Apache-2.0 |
| `isda-cdm` | 523 | ISDA/FINOS CDM, converted from Rosetta | Community Specification License 1.0 |
| `folio` (optional, not committed) | 18327 | FOLIO legal ontology | CC-BY-4.0 |

`fibo` overlaps the `fibo-*` modules. Load either the combined file or the modules with `--only`.

## Loading

The engine's URL fetch accepts `https://` only, so these files go through the upload API instead:

```bash
# Deployed stack: presigned S3 upload, ingest-from-s3, poll ingest-status
uv run python -m tests.benchmark.fsi_commodity.load_ontologies \
  --api-endpoint https://<id>.execute-api.<region>.amazonaws.com/dev --namespace-id <namespace-uuid>

# Local engine (the demo's uvicorn app)
uv run python -m tests.benchmark.fsi_commodity.load_ontologies --engine-url http://localhost:8001 --namespace default

# Check files and print the plan only
uv run python -m tests.benchmark.fsi_commodity.load_ontologies --dry-run
```

Every file is checked against its pinned size and SHA-256 before the first request. Ontologies already in the
namespace are skipped. Each snapshot registers as `urn:coa:fixture:fsi-commodity:<key>`.

The demo pack `config-fsi-commodity.yaml` uses six of these files (`gleif-lei`, `fibo`, `uncefact`, `isda-cdm`,
`dcsa-ebl`, `bfo-2020`) through `source_file` entries, under the same ids. So `setup.py --config
config-fsi-commodity.yaml` loads them on a local engine, and running this loader as well skips them. The
`fibo-*` module files overlap `fibo`; the pack does not use them.

To load FOLIO, copy the generated `FOLIO-v1.ttl` here as `folio.ttl` and pass `--include-optional`. Its SHA-256 is
pinned in the manifest too.

## Updating a snapshot

Replace the file, then update `sha256`, `bytes`, `classes` and `snapshot` in the manifest.
`tests/unit/benchmark_fsi/test_fsi_ontology_snapshots.py` fails until they match.

## Known artifacts

`gleif-lei`, `uncefact` and `folio` each declare their own namespace IRI (ending in `#`) as a class. That class comes
from the generator, not the upstream source; it is harmless for loading.
