# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build the synthetic derivative contracts, ``cases.json`` and ``catalog.json``.

The derivatives are new documents in our own words, modelled on the structure and term types of the public
agreements in ``sources.json`` and signed by parties from ``corpus/universe.json``. No third-party text is
copied. Output is deterministic (``--check`` compares a fresh build with the committed files).
Nothing here calls AWS or ingests anything.

    uv run --frozen python -m tests.benchmark.fsi_commodity.contracts.derive [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..writers.pdf_writer import Grid, Heading, KeyValues, Text, build_pdf
from .fetch import classify

HERE = Path(__file__).parent
UNIVERSE = HERE.parent / "corpus" / "universe.json"
DERIVATIVES = HERE / "derivatives"
SEED_NOTE = "Synthetic test document. Not a real agreement."

# Single source of truth for terms that recur across documents, so counterparties and numbers stay consistent.
T = {
    "isda_date": "February 10, 2026",
    "isda_threshold_a": 25_000_000,
    "isda_threshold_b": 50_000_000,
    "csa_mta": 500_000,
    "csa_rounding": 10_000,
    "eei_date": "March 1, 2026",
    "eei_term_years": 3,
    "eei_notice_days": 60,
    "eei_threshold_a": 15_000_000,
    "eei_threshold_b": 10_000_000,
    "eei_mta": 250_000,
    "eei_pay_day": 20,
    "eei_late_margin": 2,
    "gas_hub": "Houston Ship Channel",
    "rec_days": 10,
    "ab32_days": 3,
    "naesb_date": "April 8, 2026",
    "naesb_point": "AECO-C hub, Alberta",
    "coal_trade_date": "June 15, 2026",
    "coal_qty": 150_000,
    "coal_price": "112.50",
    "coal_port": "Richards Bay",
    "ca_date": "April 15, 2026",
    "ca_total": 150_000_000,
    "ca_lonestar": 90_000_000,
    "ca_northgate": 60_000_000,
    "ca_margin": "1.75",
    "ca_fee": "0.25",
    "ca_leverage": "3.50",
    "ca_coverage": "3.00",
    "ca_maturity": "April 15, 2031",
}


def money(n: int) -> str:
    return f"USD {n:,}"


def _orgs() -> dict:
    return json.loads(UNIVERSE.read_text(encoding="utf-8"))["orgs"]


@dataclass
class Contract:
    doc_id: str
    title: str
    filename: str
    modelled_on: list[str]
    parties: list[str]  # universe org keys
    sections: list[tuple[str, list[str]]]
    schedule: tuple[tuple[str, str], ...] = ()
    qa: list[tuple[str, str, object, str]] = field(default_factory=list)  # (suffix, question, answer, type)


def contracts() -> list[Contract]:
    o = _orgs()
    n = lambda k: o[k]["name"]  # noqa: E731
    a_nyc, concord = n("newco-nyc"), n("bank-concord")
    hou, glen, vitol = n("newco-hou"), n("cp-glencore"), n("cp-vitol")
    lon, bp = n("newco-lon"), n("cp-bp")
    hldg, lone, north, hudson = n("newco-hldg"), n("bank-lonestar"), n("bank-northgate"), n("bank-hudson")
    out: list[Contract] = []

    out.append(Contract(
        "d-isda-master", "ISDA-style Master Agreement and Schedule", "isda_master_nyc_concord.pdf", ["src12"],
        ["newco-nyc", "bank-concord"],
        [("Parties", [f"This Master Agreement is dated {T['isda_date']} between {a_nyc} (Party A) and {concord} (Party B). "
                      "It governs every derivative transaction the parties enter into, and each confirmation forms part of it. "
                      f"{SEED_NOTE}"]),
         ("1. Interpretation", ["All transactions and confirmations form one single agreement. If a confirmation conflicts with the Schedule, the confirmation prevails for that transaction only."]),
         ("2. Payments", ["Payments are due on the settlement date in freely transferable funds. Amounts owed in the same currency on the same date are netted into a single payment."]),
         ("5. Events of Default", ["Failure to pay is an Event of Default if not cured within one Local Business Day of notice. "
                                   f"Cross Default applies to both parties. The Threshold Amount is {money(T['isda_threshold_a'])} for Party A and {money(T['isda_threshold_b'])} for Party B."]),
         ("6. Early Termination", ["On an Event of Default the non-defaulting party may designate an Early Termination Date on at least 20 days' notice. Automatic Early Termination does not apply to either party. Close-out uses the Close-out Amount method."]),
         ("Part 1 of the Schedule", ["Termination Currency is United States Dollars. The Agreement is governed by the laws of the State of New York. "
                                     f"Credit Support Document: the Credit Support Annex dated {T['isda_date']}. Process agent for each party: none."])],
        (("Termination Currency", "USD"), ("Governing law", "New York"), ("Cross Default", "Applies to both parties"),
         ("Automatic Early Termination", "Not applicable")),
        [("q1", "Which party is Party B in the NewCo New York ISDA-style master agreement?", concord, "metadata"),
         ("q2", "What is the Threshold Amount for Party A under Cross Default in the NewCo New York master agreement?", T["isda_threshold_a"], "metadata"),
         ("q3", "What law governs the NewCo New York and Concord Reserve master agreement?", "New York", "metadata")]))

    out.append(Contract(
        "d-csa-ny", "Credit Support Annex for Variation Margin (New York law style)", "csa_ny_nyc_concord.pdf", ["src13", "src12"],
        ["newco-nyc", "bank-concord"],
        [("Parties", [f"This Annex supplements the Master Agreement dated {T['isda_date']} between {a_nyc} and {concord}. It is a Credit Support Document for both parties. {SEED_NOTE}"]),
         ("1. Margin", ["Only variation margin is exchanged. Neither party posts independent amount. The Threshold is zero for both parties."]),
         ("2. Transfers", [f"The Minimum Transfer Amount is {money(T['csa_mta'])} for both parties. Transfers are rounded up to the next {money(T['csa_rounding'])} for a Delivery Amount and down for a Return Amount."]),
         ("3. Valuation and notices", ["Valuation Time is close of business in New York on the Local Business Day before the valuation date. A demand received by 1:00 p.m. New York time is settled the same day, and after that time on the next Local Business Day."]),
         ("4. Disputes", ["A party disputing a calculation must say so by the close of the Local Business Day after the demand and pay the undisputed amount. The parties resolve the dispute by obtaining four reference market-maker quotes."]),
         ("5. Interest", ["The Interest Rate on cash collateral is the effective federal funds rate, compounded daily and paid monthly."])],
        (("Eligible collateral", "USD cash, 100%; US Treasury securities under 1 year, 99%; US Treasury securities 1 to 5 years, 97%"),),
        [("q1", "What is the Minimum Transfer Amount in the NewCo New York and Concord Reserve credit support annex?", T["csa_mta"], "metadata"),
         ("q2", "What valuation percentage applies to US Treasuries of 1 to 5 years under the NewCo New York credit support annex?", 97, "metadata"),
         ("q3", "Which master agreement does the NewCo New York credit support annex supplement, by date?", T["isda_date"], "hybrid_retrieval")]))

    out.append(Contract(
        "d-eei-master", "Master Power Purchase and Sale Agreement Cover Sheet", "eei_master_houston_glencore.pdf", ["src10", "src09", "src05"],
        ["newco-hou", "cp-glencore"],
        [("Parties", [f"This Master Agreement is made as of {T['eei_date']} between {hou} (Party A) and {glen} (Party B). {SEED_NOTE}"]),
         ("1. Term", [f"The agreement runs for {T['eei_term_years']} years from the effective date and renews for successive one-year periods unless either party gives {T['eei_notice_days']} days' written notice of non-renewal before the end of the then-current term."]),
         ("2. Transactions", ["A transaction is binding when a confirmation is accepted by both parties or the parties' recorded call is confirmed. Annexes for gas, renewable energy certificates and California compliance products may be added by written amendment."]),
         ("3. Payment", [f"Invoices are paid on the {T['eei_pay_day']}th day of the month after delivery, or the next Business Day. Late amounts bear interest at the prime rate plus {T['eei_late_margin']} percentage points."]),
         ("4. Credit and collateral", [f"The Collateral Threshold is {money(T['eei_threshold_a'])} for Party A and {money(T['eei_threshold_b'])} for Party B. Collateral is posted under the Collateral Annex (Exhibit D)."]),
         ("5. Governing law", ["The laws of the State of New York govern. Each party waives jury trial."])],
        (("Delivery point", "As stated in each confirmation"), ("Governing law", "New York")),
        [("q1", "What is the Collateral Threshold for Glencore Ltd. under the NewCo Houston EEI master agreement?", T["eei_threshold_b"], "metadata"),
         ("q2", "How many days' notice of non-renewal does the NewCo Houston and Glencore EEI master agreement require?", T["eei_notice_days"], "metadata"),
         ("q3", "On which day of the following month are invoices due under the NewCo Houston and Glencore EEI master agreement?", T["eei_pay_day"], "metadata")]))

    out.append(Contract(
        "d-eei-collateral", "Collateral Annex (Exhibit D) to the Master Power Purchase and Sale Agreement", "eei_collateral_annex_houston_glencore.pdf", ["src04", "src10"],
        ["newco-hou", "cp-glencore", "bank-hudson"],
        [("Parties", [f"This Annex is made under the Master Agreement dated {T['eei_date']} between {hou} and {glen}. {SEED_NOTE}"]),
         ("1. Exposure and call", [f"Each party's Collateral Threshold is the amount in the Master Agreement ({money(T['eei_threshold_a'])} for {hou}; {money(T['eei_threshold_b'])} for {glen}). A call is made when exposure above the Collateral Threshold exceeds the Minimum Transfer Amount of {money(T['eei_mta'])}."]),
         ("2. Forms of collateral", [f"Collateral may be cash or a standby letter of credit from an acceptable issuer. {hudson} is an acceptable issuer. A letter of credit must have at least 30 days' remaining validity."]),
         ("3. Timing", ["A call made by 10:00 a.m. Central time is met by the close of the next Business Day."]),
         ("4. Return", ["Excess collateral is returned within two Business Days of a request, provided no Event of Default is continuing."])],
        (),
        [("q1", "What Minimum Transfer Amount applies in the NewCo Houston and Glencore collateral annex?", T["eei_mta"], "metadata"),
         ("q2", "Which bank is named as an acceptable letter of credit issuer in the NewCo Houston and Glencore collateral annex?", hudson, "metadata")]))

    out.append(Contract(
        "d-eei-gas-annex", "Gas Annex to the Master Power Purchase and Sale Agreement", "eei_gas_annex_houston_glencore.pdf", ["src09", "src08"],
        ["newco-hou", "cp-glencore"],
        [("Parties", [f"This Gas Annex is entered into under the Master Agreement dated {T['eei_date']} between {hou} and {glen}. {SEED_NOTE}"]),
         ("1. Delivery", [f"Gas is delivered at {T['gas_hub']}. Firm quantities are stated in each confirmation in MMBtu per day."]),
         ("2. Price", ["Unless a confirmation fixes a price, the price is the midpoint published for the delivery day for the relevant hub, plus or minus the stated basis."]),
         ("3. Imbalances", ["Monthly imbalances within 5 percent of the monthly quantity are carried forward. Larger imbalances are cashed out at the monthly index price."]),
         ("4. Excuse", ["Firm deliveries are excused only for force majeure or the buyer's failure to take."])],
        (),
        [("q1", "At which hub does the NewCo Houston and Glencore gas annex deliver gas?", T["gas_hub"], "metadata")]))

    out.append(Contract(
        "d-eei-rec-annex", "Renewable Energy Certificates Annex", "eei_rec_annex_houston_glencore.pdf", ["src06"],
        ["newco-hou", "cp-glencore"],
        [("Parties", [f"This Annex is made under the Master Agreement dated {T['eei_date']} between {hou} and {glen}. {SEED_NOTE}"]),
         ("1. Delivery", [f"The seller transfers certificates through the agreed tracking system within {T['rec_days']} Business Days after the end of the delivery month."]),
         ("2. Failure", ["If the seller does not deliver, it pays the buyer the replacement cost, never less than zero, and any cost of cover."]),
         ("3. Warranty", ["The seller warrants good title and that no certificate has been sold or claimed twice."])],
        (),
        [("q1", "Within how many business days must certificates be transferred under the NewCo Houston and Glencore REC annex?", T["rec_days"], "metadata")]))

    out.append(Contract(
        "d-eei-ab32-annex", "California Compliance Products Annex", "eei_ab32_annex_houston_glencore.pdf", ["src05"],
        ["newco-hou", "cp-glencore"],
        [("Parties", [f"This Annex is made under the Master Agreement dated {T['eei_date']} between {hou} and {glen}. {SEED_NOTE}"]),
         ("1. Delivery", [f"The seller transfers compliance instruments through the registry within {T['ab32_days']} Business Days of the transfer date in the confirmation."]),
         ("2. Vintage", ["Each confirmation names the vintage year. A different vintage is not a valid delivery unless the buyer agrees in writing."]),
         ("3. Regulatory change", ["If a regulatory change makes delivery impossible, the affected transaction ends and neither party owes damages for it."])],
        (),
        [("q1", "Within how many business days must compliance instruments be transferred under the NewCo Houston and Glencore California annex?", T["ab32_days"], "metadata")]))

    out.append(Contract(
        "d-naesb-canada", "Base Contract for Short-Term Sale and Purchase of Natural Gas, Canadian Addendum", "naesb_canada_houston_vitol.pdf", ["src14"],
        ["newco-hou", "cp-vitol"],
        [("Parties", [f"This Base Contract is entered into as of {T['naesb_date']} between {hou} and {vitol}. It covers gas sold in Canada and across the Canada-United States border. {SEED_NOTE}"]),
         ("1. Delivery", [f"The delivery point is the {T['naesb_point']}. The contract quantity is firm unless the transaction says interruptible."]),
         ("2. Currency and tax", ["Prices are in Canadian dollars per gigajoule for deliveries in Canada. The buyer pays goods and services tax where it applies."]),
         ("3. Payment", ["Payment is due on the 25th day of the month after delivery. Disputed invoices are paid in the undisputed amount."]),
         ("4. Governing law", ["The laws of the Province of Alberta govern."])],
        (),
        [("q1", "What is the delivery point in the NewCo Houston and Vitol Canadian addendum?", T["naesb_point"], "metadata"),
         ("q2", "Which province's law governs the NewCo Houston and Vitol Canadian gas base contract?", "Alberta", "metadata")]))

    out.append(Contract(
        "d-coal-confirm", "Physical Coal Transaction Confirmation", "isda_coal_confirm_london_bp.pdf", ["src11"],
        ["newco-lon", "cp-bp"],
        [("Parties", [f"This letter confirms a coal transaction between {lon} (Party A) and {bp} (Party B), made under their ISDA Master Agreement dated November 3, 2025. {SEED_NOTE}"])],
        (("Trade Date", T["coal_trade_date"]), ("Quantity", f"{T['coal_qty']:,} metric tonnes"),
         ("Price", f"USD {T['coal_price']} per metric tonne"), ("Delivery", f"FOB {T['coal_port']}, January 2027"),
         ("Seller", bp), ("Buyer", lon), ("Payment", "30 days after the bill of lading date")),
        [("q1", "What quantity of coal does the NewCo London and BP confirmation cover?", T["coal_qty"], "metadata"),
         ("q2", "At which port is the NewCo London and BP coal delivered?", T["coal_port"], "metadata"),
         ("q3", "Who is the buyer in the NewCo London and BP coal confirmation?", lon, "metadata")]))

    out.append(Contract(
        "d-credit-agreement", "Revolving Credit Agreement", "credit_agreement_newco_global.pdf", ["src02", "src03", "src01"],
        ["newco-hldg", "bank-lonestar", "bank-northgate", "bank-hudson"],
        [("Parties", [f"This Credit Agreement is dated {T['ca_date']} among {hldg} (the Borrower), {lone} and {north} (the Lenders) and {hudson} (the Administrative Agent). {SEED_NOTE}"]),
         ("1. Commitments", [f"The Lenders commit {money(T['ca_total'])} in total: {lone} {money(T['ca_lonestar'])} and {north} {money(T['ca_northgate'])}. Loans may be borrowed, repaid and reborrowed until the Maturity Date."]),
         ("2. Maturity", [f"The Maturity Date is {T['ca_maturity']}."]),
         ("3. Interest and fees", [f"Loans bear interest at Term SOFR plus {T['ca_margin']} percent a year. A commitment fee of {T['ca_fee']} percent a year accrues on the unused commitments."]),
         ("4. Financial covenants", [f"The ratio of total debt to EBITDA must not exceed {T['ca_leverage']} to 1.00. The ratio of EBITDA to interest expense must be at least {T['ca_coverage']} to 1.00. Both are tested each quarter."]),
         ("5. Events of Default", ["Events of Default include non-payment, breach of a covenant, cross default above USD 25,000,000 and insolvency. On an Event of Default the Agent may, and at the direction of Lenders holding over half of the commitments must, end the commitments and accelerate the loans."]),
         ("6. Agent", [f"{hudson} acts as Administrative Agent and may resign on 30 days' notice."])],
        (),
        [("q1", "What are the total revolving commitments under the NewCo Global Holdings credit agreement?", T["ca_total"], "metadata"),
         ("q2", "Who is the Administrative Agent under the NewCo Global Holdings credit agreement?", hudson, "metadata"),
         ("q3", "What is the maximum leverage ratio in the NewCo Global Holdings credit agreement?", float(T["ca_leverage"]), "metadata"),
         ("q4", "What is Northgate Bank plc's commitment under the NewCo Global Holdings credit agreement?", T["ca_northgate"], "metadata")]))

    out.append(Contract(
        "d-8k-credit", "Current Report: Entry into a Material Definitive Agreement", "form_8k_newco_global_credit.pdf", ["src01", "src02"],
        ["newco-hldg", "bank-hudson"],
        [("Item 1.01", [f"On {T['ca_date']}, {hldg} entered into a revolving Credit Agreement with {lone}, {north} and {hudson} as administrative agent, providing {money(T['ca_total'])} of commitments maturing {T['ca_maturity']}. {SEED_NOTE}"]),
         ("Item 2.03", [f"Borrowings bear interest at Term SOFR plus {T['ca_margin']} percent. The agreement contains leverage and interest-coverage covenants."])],
        (),
        [("q1", "Which agent is named in the NewCo Global Holdings current report on the credit agreement?", hudson, "hybrid_retrieval")]))
    return out


def render(c: Contract) -> bytes:
    o = _orgs()
    blocks: list = []
    blocks.append(KeyValues(tuple((f"Party {i + 1}", f"{o[k]['name']} (LEI {o[k]['lei']})") for i, k in enumerate(c.parties))))
    for head, paras in c.sections:
        blocks.append(Heading(head))
        blocks.extend(Text(p) for p in paras)
    if c.schedule:
        blocks.append(Heading("Schedule"))
        blocks.append(Grid(("Term", "Value"), tuple(c.schedule)))
    return build_pdf(c.title, blocks, author="NewCo synthetic contract corpus")


def pages_of(data: bytes) -> int:
    import pypdfium2
    return len(pypdfium2.PdfDocument(data))


def build() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    derived, cases = [], {}
    for c in contracts():
        data = render(c)
        files[f"derivatives/{c.filename}"] = data
        pages = pages_of(data)
        derived.append({
            "id": c.doc_id, "kind": "derivative", "title": c.title, "file": f"derivatives/{c.filename}",
            "modelled_on": c.modelled_on, "parties": c.parties, "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "pages": pages, "size_class": classify(pages, len(data)),
        })
        for suffix, question, answer, qtype in c.qa:
            cases[f"{c.doc_id}-{suffix}"] = {
                "expected": 1, "tolerance_pct": 0, "tolerance": 0, "type": qtype, "question": question,
                "answer": answer, "doc": c.doc_id, "origin": "derivative",
            }
    cases.update(as_is_cases())
    files["cases.json"] = _j({
        "//format": "Same entry shape as ../golden_answers.json plus doc (document id) and origin (as_is or derivative). Do not edit by hand; derive.py writes it.",
        **cases,
    })
    files["catalog.json"] = _j({"documents": catalog(derived)})
    return files


AS_IS = [
    ("src01", "q1", "Which bank is the administrative agent under the Chrysler Group LLC Senior Credit Agreement of May 24, 2011?", "Citibank, N.A.", "metadata"),
    ("src01", "q2", "What principal amount is the Chrysler Tranche B term facility, in billions of dollars?", 3.0, "metadata"),
    ("src01", "q3", "What is the size of the Chrysler Group LLC revolving credit facility, in billions of dollars?", 1.3, "metadata"),
    ("src02", "q1", "Who is the borrower under the June 2, 2006 First Amended and Restated Credit Agreement?", "Cleco Corporation", "metadata"),
    ("src02", "q2", "Who is the administrative agent under the Cleco Corporation credit agreement?", "The Bank of New York", "metadata"),
    ("src03", "q1", "Who is the borrower under the April 25, 2005 credit agreement?", "Cleco Power LLC", "metadata"),
    ("src12", "q1", "Who is Party A under the April 6, 2006 master agreement for Long Beach Mortgage Loan Supplemental Interest Trust Series 2006-3?", "Bear Stearns Financial Products Inc.", "metadata"),
    ("src12", "q2", "Which trust is Party B in the April 6, 2006 Bear Stearns Financial Products master agreement?", "Long Beach Mortgage Loan Supplemental Interest Trust Series 2006-3", "metadata"),
    ("src05", "q1", "What version and date does the EEI California AB32 Product Annex carry?", "Version 1.0, 7/16/12", "metadata"),
    ("src06", "q1", "What version and date does the EEI Renewable Energy Certificates Annex carry?", "Version 1.0, 11/4/10", "metadata"),
    ("src08", "q1", "What is the title of the NAEMA and IECA 2019 model form?", "2019 Power and Gas Master Agreement", "metadata"),
    ("src13", "q1", "Under which ISDA protocol is the NY-NEW exhibit applicable?", "ISDA 2016 Variation Margin Protocol", "metadata"),
]


def as_is_cases() -> dict:
    """Questions on the public originals. Gold answers come from their first pages. Needs the original only to be ingested later."""
    out = {}
    for src, suffix, q, a, t in AS_IS:
        out[f"{src}-{suffix}"] = {
            "expected": 1, "tolerance_pct": 0, "tolerance": 0, "type": t, "question": q, "answer": a,
            "doc": src, "origin": "as_is",
        }
    return out


def catalog(derived: list[dict]) -> list[dict]:
    fetched = json.loads((HERE / "fetched.json").read_text(encoding="utf-8"))["documents"]
    docs = []
    for f in fetched:
        d = {
            "id": f["id"], "kind": "as_is", "local_name": f["local_name"], "match": f["match"], "status": f["status"],
            "url": f["url"], "resolved_url": f.get("resolved_url"), "sha256": f.get("sha256"), "bytes": f.get("bytes"),
            "pages": f.get("pages"), "pages_estimated": f.get("pages_estimated", False), "size_class": f.get("size_class"),
        }
        if f.get("local_pages"):
            d["local_pages"] = f["local_pages"]
            if f["local_pages"] >= 100:
                d["size_class"] = "large"
                d["size_note"] = f"user's local copy is {f['local_pages']} pages; tagged large"
        if f["status"] != "ok":
            d["error"] = f.get("error")
        docs.append(d)
    for d in derived + docs:
        d["ingest"] = "eligible" if d.get("size_class") == "small" and d.get("status", "ok") == "ok" else "deferred"
    return docs + derived


def _j(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files = build()
    if args.check:
        bad = [r for r, b in files.items() if not (HERE / r).exists() or (HERE / r).read_bytes() != b]
        extra = sorted(p.relative_to(HERE).as_posix() for p in DERIVATIVES.glob("*") if p.relative_to(HERE).as_posix() not in files) if DERIVATIVES.exists() else []
        for r in bad + extra:
            print("differs:", r)
        return 1 if bad or extra else 0
    for rel, data in files.items():
        p = HERE / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    print(f"wrote {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
