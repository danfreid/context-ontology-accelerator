# Brief: a linked commodity-trading dataset, from onboarding to cash

This brief says what the NewCo trading corpus is, how its stages link, what it plants for a
system to find, and how to rebuild or adapt it. It is the companion dataset to the
`fsi-commodity` ontology pack (`packages/ontology-engine/demo/config-fsi-commodity.yaml`) and
the test bed for comparing the two COA deployments set by the switchable-backends work
(`.agents/tasks/switchable-backends/`):

| Stack | `graph_backend` | `vector_backend` | `metadata_backend` | `compute_backend` |
|-------|-----------------|------------------|--------------------|-------------------|
| A: original COA (default) | `neptune` | `aoss` | `dynamodb` | `fargate` |
| B: cost-optimised | `aurora` (pgRouting, recursive CTE) | `pgvector` + `pg_search` (BM25 hybrid) | `aurora` | `lambda` |

The same corpus is loaded into both stacks and scored with the same ground truth, so any
difference in answers comes from the backend, not the data.

Everything lives under `tests/benchmark/fsi_commodity/`: the generator package, this brief,
the committed `corpus/`, and the `golden_answers.json` that `tests/benchmark/compare.py`
(FEAT-003) reads.

---

## 1. What it is

One simulated trading group, **NewCo**, onboards counterparties, signs agreements, gets credit
approved, trades, ships, invoices and settles, all through the email, chat and documents a
trading house actually receives. Every message has ground truth, so a system that reads the
stream can be scored on what it understood at each point.

| Phase | Stages covered | Folder | Scenarios | Emails | Chats | Attachments |
|-------|---------------|--------|-----------|--------|-------|-------------|
| 1 Onboarding | KYC and AML, legal (master agreements, guarantees, SBLCs), credit (applications, approvals, limits), static data and settlement instructions | `corpus/onboarding/` | 6 | 138 | 0 | 95 |
| 2 Trade | Marketer and trader negotiation, credit checks, recaps, deal tickets | `corpus/trade/` | 6 (7 deals) | 42 | 13 | 18 |
| 3 Post-trade | Confirmations, logistics (nominations, bills of lading, inspection certificates), invoices, credit and debit notes, margin calls | `corpus/post-trade/` | 6 | 55 | 0 | 39 |
| 4 Settlement | Accounting on the cash side: payments, bank advices, cash reconciliation, short payments, netting, swap and FX settlement | `corpus/settlement/` | 6 | 34 | 0 | 19 |
| **Total** | | | **24** | **269** | **13** | **171** |

Legal and credit are not separate phases: they happen during onboarding, as they do in
practice, and every later phase reads their results.

---

## 2. The world

`universe.py` builds everything from one seed (`DEFAULT_SEED = 20260101`) and writes it to
`corpus/universe.json`, the master data for entity resolution.

- **NewCo group**: NewCo Global Holdings, Inc. (Delaware, parent) and four trading entities,
  each with its own incorporation, office, LEI, bank and accounts: Sydney (Pty Ltd, NSW),
  Houston (LLC, Texas), New York (Inc., Delaware, derivatives) and London (UK Limited). Each
  trading entity has a marketer, trader, lawyer, credit analyst, compliance analyst, ops,
  settlements and accounting mailbox. Ownership: founders' partnership 62%, infrastructure
  fund 28%, management pool 10%, so the two founders are beneficial owners of every NewCo
  entity (34.1% and 27.9%).
- **Counterparties**: Vitol, Glencore, Shell and BP under their real names with invented facts
  (OPEN-49), and three fictional ones: Harbourline Petroleum (Newcastle NSW), Kestrel Bunkering
  (London; BVI and Jersey owners) and Tiber Peak Trading DMCC (Dubai; Cypriot holding).
- **Service parties** (fictional): issuing, advising and confirming banks, two law firms, a
  screening vendor, a credit bureau, an independent inspector (Cardinal Inspection Services)
  and a shipping agent (Gulfstream Marine Agency).

---

## 3. The life cycle, counterparty by counterparty

Read across a row to follow one relationship from first contact to cash. Each cell is a
scenario folder; each later cell is built from the earlier ones (§4).

| Counterparty and desk | 1 Onboarding: legal and credit | 2 Trade | 3 Post-trade and logistics | 4 Settlement and accounting |
|---|---|---|---|---|
| Vitol, Houston | `s1`: clean KYC; credit USD 40m of 60m requested; MPTA negotiated in 4 redline rounds; static data and SSIs | `t1`: buys 250 kbbl WTI Midland FOB Corpus Christi; premium 1.10 to 0.85 in chat | `p1`: confirmation, nomination, B/L, quantity and quality certificates; invoice from five Platts assessments charges premium 0.95 (overcharge 25,138); check sheet, credit note | `x1`: treasury release above the USD 10m limit, debit advice on the due date, payment notice; USD 16,805,084.20 |
| Kestrel, London | `s2`: screening finds a PEP owner; enhanced due diligence with source of wealth; approval with conditions; secured-only credit | `t2`: sells 8,000 mt VLSFO FOB Rotterdam; prepayment because no SBLC exists | `p2`: confirmation, credit advice of the USD 4.36m prepayment, B/L, certificate; final invoice trued up against the prepayment; a quantity query answered by the 5% tolerance | `x2`: reminder for the USD 46,886.40 balance, paid two days late, late interest waived |
| Harbourline, Sydney | `s3`: layered credit (USD 5m unsecured, 15m parent guarantee, 10m confirmed SBLC with issuing, advising and confirming banks); MPTA under NSW law | `t3`: sells 20,000 mt ULP 91 CFR Newcastle; a second cargo declined by credit, resized to 18,000 mt | `p3`: confirmation, B/L, letter of indemnity counter-signed for 125% of cargo value, discharge certificate; invoice on B/L quantity where the agreement says discharge quantity (credit note) | `x3`: USD 45.00 bank charge deducted (short payment), cash reconciliation, query, top-up |
| Glencore, New York | `s4`: ISDA Schedule and CSA in 3 rounds through outside counsel; netting opinion; collateral set-up | `t4`: pays fixed 68.40 on a 12-month WTI swap | `p4`: confirmation against a ticket that says 68.04; variation margin call that ignores the USD 2.5m threshold | `x4`: collateral transfer; monthly swap statement from 23 daily prices, corrected, settled |
| BP, Sydney (via London) | `s5`: second NewCo entity relies on London's KYC; sub-limit inside a USD 150m group limit; accession letter to London's agreement | `t5`: sells 30,000 mt Jet A-1 CIF Brisbane; group exposure includes London's open USD 62m | `p5`: confirmation, B/L, certificates, tax invoice with AUD equivalent and GST; dispute over which FX rate date applies | `x5`: BP invoices a trade Sydney has no record of (London dealt it); netting statement under the MPTA; net settlement |
| Tiber Peak, London | `s6`: a Cypriot holding company hides a sanctioned 60% owner; neutral decline (no tipping off); group-wide block | none: the relationship ends here | | |
| Northgate Bank, London | (NewCo London's own bank) | `t6`: NewCo London buys GBP 4m forward | `p6`: bank confirmation with a SWIFT-style copy; NewCo's USD account digits swapped | `x6`: USD 5,138,000 against GBP 4,000,000 at 1.2845 |

---

## 4. How the stages link

The point of the dataset is that nothing is restated by hand: each stage reads the values the
previous one produced, so a system can check them against each other.

- **Onboarding to trade** (`trade/common.py`, `onboarded(u)`): a deal takes its governing
  agreement name, final clause values (payment days, tolerance, governing law), credit limit
  and headroom, and dates from its onboarding scenario. A credit approval uses the same limit
  and exposure arithmetic, including group limits and security.
- **Trade to post-trade** (`post_trade/common.py`, `traded(u)`, `latest_ticket()`):
  confirmations are built from the latest deal ticket (same trade id, same structure), and
  quantities, prices and due dates follow the deal. Every figure on an invoice, note or margin
  call is computed from figures printed on it; the manifest repeats every input.
- **Post-trade to settlement** (`settlement/common.py`, `posted(u)`, `last_doc()`): payments
  take the approved amounts, due dates and accounts from the last post-trade document of each
  trade; bank advices come from the bank that holds the account.
- **Shared keys** a reader can use to correlate: trade ids, agreement names, the organisation
  and role behind each address, account numbers from `universe.json`, `Message-ID` /
  `In-Reply-To` / `References` headers, and document versions.
- Dates follow business days in each party's calendar, and send times fall in each sender's
  working hours and time zone.

---

## 5. What is planted

Each scenario plants problems a real desk meets, and the manifest says where each is and
whether anyone in the corpus caught it.

- **Onboarding**: stale certificates, a questionnaire that denies what screening finds,
  ownership through trust layers, an alias ("Glencore Limited"), an email that disagrees with
  its redline, an SBLC that expires too early, a qualified legal opinion.
- **Trade**: a recap that differs from what was agreed in chat, a ticket with transposed digits,
  the wrong currency asked, the wrong incoterm, the wrong value date, a cargo declined by credit
  and resized.
- **Post-trade**: an invoice premium from the first offer, an invoice on the wrong quantity
  basis, a stale confirmation, a margin call that ignores the threshold, swapped account digits.
- **Settlement**: a late payment, a bank charge deducted against a no-deductions clause, a
  statement built from the wrong prices, a trade invoiced by the wrong entity.
- **Control cases** with nothing planted (`x1`, `x6`, the clean onboarding `s1`), so false
  alarms are measured too.

---

## 6. Ground truth

Each scenario folder has `manifest.json` (keys in `README.md`) and `messages/*.eml` (plus
`chats/` in the trade phase).

- **Per message**: true thread, the organisation and role behind every address, direction,
  class, phase, hard-case tags, and the facts stated in the body.
- **Per attachment**: document kind, SHA-256, the facts to extract, and the ontology class it
  should resolve to (§7).
- **Per scenario**: ownership edges and beneficial owners; every negotiated position (clause,
  value, proposer, version, message, superseded or agreed); deals with their planted
  mismatches; `expected_contradictions[]`, the reconciliation discrepancies; the outcome;
  evaluation questions; and `review_events[]`, the steward corrections to apply during replay.
- Chat lines carry an `intent` and the `terms` they state, and the line where a deal is agreed
  states the deal's terms exactly.

---

## 7. How it maps onto COA

### Ontology grounding (`config-fsi-commodity.yaml`)

Every entity, document and event in the manifest carries the class URI it should be grounded
to. The generator only uses classes that the pack lists in `sample_classes`; no new classes are
invented.

> **Update after the build.** The pack changed while the corpus was being built: it dropped
> DCSA eBL and ACTUS (now marked "not loadable") and `TradeTransaction`, and renamed several
> FIBO and GLEIF classes. `ontology.py` has a `FALLBACKS` map that sends each missing class to
> the closest class the pack still lists, and the build refuses any class not in the pack. The
> classes the corpus actually uses are:
>
> | Class in the table below | Class actually used |
> |---|---|
> | UN/CEFACT `TradeTransaction` | FIBO FND `Agreement` |
> | FIBO FBC `CreditFacility` | `LoanOrCreditAccount` |
> | GLEIF `LEIRegistration` | `LegalEntityIdentifierRegistryEntry` |
> | FIBO FND `PartyInRole` | `PartyRoleIdentifier` |
> | FIBO DER `SettlementTerms` | `DerivativeSettlementTerms` |
> | DCSA `BillOfLading` | UN/CEFACT `Consignment` |
> | DCSA `Shipper`, `Consignee`, `NotifyParty` | Schema.org `Organization` |
> | DCSA `CargoItem` | UN/CEFACT `SupplyChainTradeLineItem` |
> | ACTUS `CEG` (SBLC) | FIBO `BilateralContract` |
> | ACTUS `CLM` (margin call) | FIBO `CollateralAgreement` |
> | ACTUS `SWAPS`, `FXOUT` | FIBO `OverTheCounterDerivativeInstrument` |
>
> If DCSA or ACTUS are added back to the pack, remove their `FALLBACKS` entries and rebuild.

| Corpus object | Expected class |
|---|---|
| NewCo and counterparty legal entities, LEIs, ownership edges | GLEIF `LegalEntity`, `LEIRegistration`, L2 `DirectConsolidation` / `UltimateConsolidation`; FIBO BE `LegalEntity` |
| Contacts, desks and roles | Schema.org `Person`; FIBO FND `PartyInRole` |
| MPTA, ISDA Schedule, CSA, guarantees, accession letter | FIBO FND `Contract`, `BilateralContract`, `CollateralAgreement`; `Obligor`, `Obligee`, `Beneficiary` |
| Credit limits and SBLCs | FIBO FBC `CreditFacility`; ACTUS `CEG` |
| Accounts and SSIs | FIBO FBC `Account`, `AccountHolder` |
| Physical deals, tickets, recaps | UN/CEFACT `TradeTransaction`, `SupplyChainTradeLineItem`; Schema.org `Order` |
| WTI swap, GBP forward | FIBO DER `DerivativeInstrument`, `Underlier`, `SettlementTerms`; ACTUS `SWAPS`, `FXOUT` |
| Bills of lading, nominations | DCSA `BillOfLading`, `Shipper`, `Consignee`, `NotifyParty`, `CargoItem`; UN/CEFACT `Consignment`, `TransportMovement` |
| Quantity, quality and discharge certificates | UN/CEFACT `SpecifiedInspection`; GS1 `ObjectEvent`, `QuantityElement` |
| Invoices, credit and debit notes, margin calls, payments | Schema.org `Invoice`, `MonetaryAmount`; ACTUS `CLM` |
| Every email and attachment | PROV-O `Entity` with `wasDerivedFrom` (attachment to email) and `wasAttributedTo` (sender); Dublin Core `BibliographicResource` |

### Ingestion paths

- **Document source** (`packages/sources`): the preprocessing Lambda accepts `.pdf`, `.docx`,
  `.md` and `.txt`, but not `.eml`. Each email is therefore written twice: the `.eml` is the
  ground-truth original, and a `.md` rendering (headers, body, attachment list) is the
  ingestible copy. Attachments are written as PDF (confirmations, B/Ls, certificates, invoices,
  SBLCs, advices) or DOCX (agreement redlines with tracked changes).
- **Structured source**: the tables behind the documents (counterparties, accounts, limits,
  deals, invoices, payments) are written as `corpus/structured/*.csv`, plus
  `schema.sql`/`seed.sql` for a Postgres JDBC source, plus data-catalog fixtures in the format
  `demo/stage_fixtures.py` writes. Ontology induction (`table_to_ontology`) runs against these
  tables.

### Benchmark questions (`golden_answers.json`)

The evaluation questions in each manifest become query entries for the FEAT-003 harness. Each
has a query id, a question type, the expected answer and a tolerance. The question types are
chosen to exercise each backend difference:

- **Graph traversal**: beneficial owners through 2 to 4 ownership hops (Kestrel's trust layers,
  Tiber Peak's hidden owner, NewCo's founders). Neptune runs these natively; stack B runs them
  with recursive CTEs or pgRouting.
- **Hybrid retrieval**: find a clause or figure by meaning and by exact token, such as
  "no-deductions clause" or a trade id. AOSS serves these on stack A; pgvector plus `pg_search`
  BM25 serves them on stack B.
- **Reconciliation**: the `expected_contradictions[]`, such as the invoice premium against the
  chat, the ticket against the confirmation, and the payment against the invoice.
- **Metadata lookups**: namespace, source and ontology-version reads, which go to DynamoDB on
  stack A and Aurora on stack B.

`compare.py --run-a <stack A run> --run-b <stack B run> --golden golden_answers.json` reports
latency and correctness per question, and fails on any question that stack A answers correctly
and stack B does not.

---

## 8. Rules

- Every person and fact is invented; real counterparty names are used only so entity
  resolution has real names to match.
- Mail uses `.example` domains. LEIs start with `SYNT` and have valid ISO 17442 check digits.
  IBAN, ABA, ABN and BIC values pass their checksums.
- Sanctions, PEP and declined-credit findings attach only to fictional parties.
- Clause wording is our own; no published form text (GTC, EEI, NAESB, ISDA) is copied.
- One seed builds everything, and equal seeds give byte-identical files: Faker is seeded per
  entity (`seed + key`) and its version is pinned; PDFs use ReportLab with `invariant=1`;
  timestamps come from the scenario clock, never the wall clock.
- No AWS calls during the build. `pocsynth pii-audit` (Amazon Comprehend) is an optional check
  after the build, not a dependency.

---

## 9. Build, check, replay

From the repository root:

```bash
uv run --frozen python -m tests.benchmark.fsi_commodity                      # write corpus/ (all four phases)
uv run --frozen python -m tests.benchmark.fsi_commodity --check              # exit 1 if the committed corpus differs from a fresh build
uv run --frozen python -m tests.benchmark.fsi_commodity --scenario s1-houston-vitol-wti   # one scenario
uv run --frozen python -m tests.benchmark.fsi_commodity.replay               # offline replay in time order against ground truth
uv run --frozen python -m tests.benchmark.fsi_commodity.golden               # regenerate golden_answers.json from the manifests
```

Against deployed stacks:

1. Run `make deploy-dev-neptune` and `make deploy-dev-aurora`.
2. Upload `corpus/**` (`.md`, `.pdf`, `.docx`) as a document source, and register
   `corpus/structured/` as a database source.
3. Run setup and the demo with the ontology pack:
   `python packages/ontology-engine/demo/setup.py --config config-fsi-commodity.yaml`, then
   `demo.py`.
4. Run `tests/benchmark/run_benchmark.sh` once per stack, then run `compare.py`.

---

## 10. Adapting it

**Another commodity or desk** (for example LNG, metals or agriculture): add an entity or
counterparty in `universe.py`. Each is derived from `seed + key`, so the others do not change.
Then add one module per phase that builds on the previous one:

- **Onboarding:** `scenarios/sN_*.py`, covering KYC, agreement negotiation with `clauses.py`,
  and credit.
- **Trade:** `deals/tN_*.py`, covering chat and recap; it reads `onboarded(u)`.
- **Post-trade:** `post/pN_*.py`, covering the logistics documents in `post/docs.py`; it reads
  `traded(u)`.
- **Settlement:** `settlement/xN_*.py`; it reads `posted(u)`.

Register each module in its package's tuple (`ONBOARDING`, `TRADE`, `POST_TRADE`,
`SETTLEMENT`). Messages are sent with
`Scenario.send(key, fm, to, body, doc_class=..., attachments=..., reply_to=..., tags=...,
facts=...)` and chats with `Scenario.chat(...)`. Record each planted problem in the deal's
`mismatches` or the scenario's `expected_contradictions`, and give each entity and document
its class URI from the ontology pack.

**Another workflow** (for example demurrage claims, tax or month-end accruals): add a phase
package alongside `settlement/` that reads the last one (`posted(u)` is the pattern), add its
document builders, register it in `build.py`'s `_registry()`, and add question types to
`golden.py`.

---

## 11. What is not there yet

- Accounting stops at cash. Settlement has payments, advices, reconciliations, netting and swap
  statements, but no ledger: no journal entries, accruals, revenue recognition, inventory
  valuation or month-end close.
- Payment matching is not scored on its own; no question checks bank advices against invoices
  beyond the planted contradictions.
- Chat negotiation needs a model. The offline replay treats chat intents and terms as ground
  truth for a model run to be scored against.
- Stack B can be deployed but cannot store ontologies yet. The ontology engine's store factory
  (`packages/ontology-engine/src/coa_ontology/stores/factory.py`) accepts only
  `opensearch_neptune` and `na_only` for `WORKBENCH_BACKEND`. The Lambda that FEAT-002 creates
  sets `aurora_pgvector`, which the factory rejects. Live scoring on stack B needs an Aurora
  graph store and a pgvector vector store; until those exist, `na_only` is the only other stack
  to compare against. The offline replay works without either.
- Graph traversal was thin and has been extended: 46 of the 117 golden questions are now
  graph-traversal questions (the 4 original ones plus 42 graph-qNN questions from
  traversal.py), against 29 reconciliation, 28 metadata and 14 hybrid-retrieval. Of the 42 new
  ones, 15 need 4 or more hops (up to 6), 13 need 3, 13 need 1 or 2, and one is a no-path check. The
  Neptune-versus-recursive-CTE difference can now be measured per hop depth. The remaining gap:
  ownership chains stop at 4 hops (Tiber Peak), so depth beyond 4 comes from mixing ownership
  with accounts, trades, invoices and payments, not from a deeper ownership tree.
