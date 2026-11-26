# Third-party notices: ontology snapshots

The Turtle files in this directory are third-party works, redistributed under their own licences. They are not
covered by the repository's Apache-2.0 licence. Each is a separate file placed alongside the code; none is linked
into or combined with it.

| File | Work | Rights holder | Licence | Changes from the source |
|---|---|---|---|---|
| `uncefact.ttl` | UN/CEFACT Web Vocabulary, commit `ccd62cc` ([source](https://opensource.unicc.org/un/unece/uncefact/vocab-bsp)) | UN/CEFACT | GPL-3.0-or-later, text in [`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt) | Seven domain module files merged and re-serialised; code lists not included; no triple changed |
| `bfo-2020.ttl` | Basic Formal Ontology 2020 ([source](https://github.com/BFO-ontology/BFO-2020)) | BFO developers | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) | Re-serialised as Turtle |
| `fibo.ttl`, `fibo-*.ttl` | Financial Industry Business Ontology ([source](https://spec.edmcouncil.org/fibo/)) | EDM Council | [MIT](https://opensource.org/licenses/MIT) | Subsets of FIBO modules, re-serialised |
| `gleif-lei.ttl` | GLEIF Base, L1 and L2 ontologies ([source](https://www.gleif.org/ontology/)) | Global Legal Entity Identifier Foundation | [GLEIF terms of use](https://www.gleif.org/en/meta/lei-data-terms-of-use/) | Three ontologies combined in one file |
| `isda-cdm.ttl` | FINOS Common Domain Model ([source](https://github.com/finos/common-domain-model)) | FINOS and CDM Participants | [Community Specification License 1.0](https://github.com/finos/common-domain-model/blob/master/LICENSE.md) | Converted from the Rosetta model to OWL |
| `dcsa-ebl.ttl` | DCSA Electronic Bill of Lading ([source](https://github.com/dcsaorg/DCSA-EBL)) | Digital Container Shipping Association | Apache-2.0 (text in the repository's root `LICENSE`) | Converted from OpenAPI schemas to OWL |
| `folio.ttl` | FOLIO, Federated Open Legal Information Ontology ([source](https://github.com/alea-institute/FOLIO)) | ALEA Institute and FOLIO contributors | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) | Converted from RDF/XML to Turtle; some annotation triples omitted |

GPL-3.0-or-later applies to `uncefact.ttl` only. Anyone redistributing it must keep this notice and the licence
text with it; the file is its own source form.
