# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Onboarding phase (s1..s6): counts, planted problems, credit arithmetic, links to later phases and class URIs."""

from __future__ import annotations

import hashlib
import zipfile
from io import BytesIO
from typing import Any

import pytest

from tests.benchmark.fsi_commodity.build import BuildResult, build, render_files
from tests.benchmark.fsi_commodity.onboarding import ONBOARDING
from tests.benchmark.fsi_commodity.onboarding.common import onboarded
from tests.benchmark.fsi_commodity.ontology import FALLBACKS, allowed_uris
from tests.benchmark.fsi_commodity.universe import beneficial_owners, ownership_paths

pytestmark = pytest.mark.unit

COUNTS = {
    "s1-houston-vitol-wti": (26, 17),
    "s2-london-kestrel-vlsfo": (28, 17),
    "s3-sydney-harbourline-ulp": (26, 20),
    "s4-newyork-glencore-isda": (24, 22),
    "s5-sydney-bp-jet": (20, 12),
    "s6-london-tiberpeak-decline": (14, 7),
}


@pytest.fixture(scope="module")
def result() -> BuildResult:
    return build()


@pytest.fixture(scope="module")
def sc(result: BuildResult) -> dict[str, Any]:
    return {s.id: s for s in result.scenarios if s.phase == "onboarding"}


def _uris(node: Any) -> list[str]:
    """Every string in a nested structure that looks like a class URI."""
    if isinstance(node, str):
        return [node] if node.startswith("http") else []
    if isinstance(node, dict):
        return [u for v in node.values() for u in _uris(v)]
    if isinstance(node, list):
        return [u for v in node for u in _uris(v)]
    return []


def _contradictions(s: Any) -> dict[str, dict[str, Any]]:
    return {c["id"].split(":", 1)[1]: c for c in s.expected_contradictions}


# --- counts -------------------------------------------------------------------------------------


def test_registry_has_six_scenarios() -> None:
    assert len(ONBOARDING) == 6


@pytest.mark.parametrize(("sid", "expected"), sorted(COUNTS.items()))
def test_scenario_counts(sc: dict[str, Any], sid: str, expected: tuple[int, int]) -> None:
    assert sc[sid].counts == {"emails": expected[0], "attachments": expected[1], "chats": 0}


def test_phase_totals_match_brief(result: BuildResult) -> None:
    built = [s for s in result.scenarios if s.phase == "onboarding"]
    assert [s.id for s in built] == list(COUNTS)
    assert sum(s.counts["emails"] for s in built) == 138
    assert sum(s.counts["attachments"] for s in built) == 95
    assert sum(s.counts["chats"] for s in built) == 0


def test_files_on_disk_match_counts(result: BuildResult) -> None:
    files = render_files(result)
    eml = [f for f in files if f.startswith("onboarding/") and "/messages/" in f and f.endswith(".eml")]
    md = [f for f in files if f.startswith("onboarding/") and "/messages/" in f and f.endswith(".md")]
    att = [f for f in files if f.startswith("onboarding/") and "/attachments/" in f]
    assert (len(eml), len(md), len(att)) == (138, 138, 95)
    assert not any("/chats/" in f for f in files if f.startswith("onboarding/"))


def test_attachment_hashes_match_files(result: BuildResult) -> None:
    files = render_files(result)
    for s in result.scenarios:
        if s.phase != "onboarding":
            continue
        for a in s.attachments:
            data = files[f"onboarding/{s.id}/{a['path']}"]
            assert hashlib.sha256(data).hexdigest() == a["sha256"]
            assert a["filename"].rsplit(".", 1)[-1] in {"pdf", "docx"}


def test_build_is_byte_identical(result: BuildResult) -> None:
    again = render_files(build())
    first = render_files(result)
    assert first.keys() == again.keys()
    assert all(first[k] == again[k] for k in first)


# --- planted problems ---------------------------------------------------------------------------


def test_s1_is_a_control_case(sc: dict[str, Any]) -> None:
    assert sc["s1-houston-vitol-wti"].expected_contradictions == []


@pytest.mark.parametrize(
    ("sid", "planted"),
    [
        ("s2-london-kestrel-vlsfo", {"questionnaire-denies-pep": True}),
        (
            "s3-sydney-harbourline-ulp",
            {"stale-good-standing": False, "sblc-expires-too-early": False, "email-disagrees-with-redline": False},
        ),
        ("s4-newyork-glencore-isda", {"alias-glencore-limited": False, "qualified-opinion": False}),
        ("s5-sydney-bp-jet", {"stale-london-kyc": False}),
        ("s6-london-tiberpeak-decline", {"hidden-sanctioned-owner": True}),
    ],
)
def test_planted_problems_and_caught_flags(sc: dict[str, Any], sid: str, planted: dict[str, bool]) -> None:
    found = _contradictions(sc[sid])
    assert {k: v["caught"] for k, v in found.items()} == planted
    keys = {m["key"] for m in sc[sid].messages} | {a["filename"] for a in sc[sid].attachments}
    for c in found.values():
        assert set(c["where"]) <= keys, c["id"]
        assert (c["caught_by"] is not None) == c["caught"]


def test_s2_questionnaire_denies_what_screening_finds(sc: dict[str, Any]) -> None:
    s = sc["s2-london-kestrel-vlsfo"]
    quest = next(a for a in s.attachments if a["kind"] == "kyc-questionnaire")
    report = next(a for a in s.attachments if a["kind"] == "screening-report")
    assert quest["facts"]["answers"]["Is any owner or director a politically exposed person?"] == "No"
    assert report["facts"]["result"] == "hit"
    assert report["facts"]["hits"][0]["category"] == "pep"


def test_s3_stale_certificate_is_older_than_validity(sc: dict[str, Any]) -> None:
    s = sc["s3-sydney-harbourline-ulp"]
    c = _contradictions(s)["stale-good-standing"]["values"]
    assert c["age_days"] > c["validity_months"] * 31


def test_s3_sblc_expires_before_required(sc: dict[str, Any]) -> None:
    rec = sc["s3-sydney-harbourline-ulp"].data["onboarded"]
    assert rec["sblc_expiry"] < rec["required_sblc_expiry"]
    sblc = [a for a in sc["s3-sydney-harbourline-ulp"].attachments if a["kind"] == "sblc-confirmation"][0]
    assert sblc["facts"]["expiry"] == rec["sblc_expiry"]
    assert {sblc["facts"][k] for k in ("issuing_bank", "advising_bank", "confirming_bank")} == set(rec["sblc_banks"])


def test_s3_email_disagrees_with_its_redline(sc: dict[str, Any]) -> None:
    s = sc["s3-sydney-harbourline-ulp"]
    values = _contradictions(s)["email-disagrees-with-redline"]["values"]
    reply = next(m for m in s.messages if m["key"] == "mpta-r2")
    assert reply["facts"]["payment_days_stated"] == values["email_payment_days"]
    assert values["redline_payment_days"] != values["email_payment_days"]
    assert s.data["onboarded"]["payment_days"] == values["redline_payment_days"]


def test_s4_alias_and_qualified_opinion(sc: dict[str, Any], result: BuildResult) -> None:
    s = sc["s4-newyork-glencore-isda"]
    alias = s.data["onboarded"]["alias"]
    assert alias == "Glencore Limited"
    assert alias in result.universe["orgs"]["cp-glencore"]["aliases"]
    opinion = next(a for a in s.attachments if a["kind"] == "legal-opinion")
    assert opinion["facts"]["qualified"] is True
    assert len(opinion["facts"]["qualifications"]) == 2
    summary = next(m for m in s.messages if m["key"] == "opinion-summary")
    assert "no qualifications" in summary["facts"]["stated"]
    assert s.review_events[0]["event"] == "merge-alias"


def test_s5_relies_on_stale_london_kyc(sc: dict[str, Any]) -> None:
    c = _contradictions(sc["s5-sydney-bp-jet"])["stale-london-kyc"]["values"]
    assert c["age_days"] > c["policy_months"] * 31


def test_s6_decline_is_neutral(sc: dict[str, Any]) -> None:
    s = sc["s6-london-tiberpeak-decline"]
    outbound = [m for m in s.messages if m["direction"] == "outbound"]
    assert {m["key"] for m in outbound} >= {"decline-letter", "reason-reply"}
    assert s.outcome["reason_disclosed"] is False
    letter = next(a for a in s.attachments if a["kind"] == "decline-letter")
    assert letter["facts"]["reason_disclosed"] is False
    files = s.files()
    for key in ("decline-letter", "reason-reply"):
        msg = next(m for m in s.messages if m["key"] == key)
        text = files[msg["files"]["md"]].decode("utf-8").lower()
        assert not any(w in text for w in ("sanction", "screening", "owner", "nominee", "block"))
    group = next(m for m in s.messages if m["key"] == "group-block")
    assert {t["org"] for t in group["to"]} == {"newco-syd", "newco-hou", "newco-nyc"}


# --- ownership ----------------------------------------------------------------------------------


def test_hop_counts_for_hidden_and_pep_owners(result: BuildResult) -> None:
    u = result.universe
    pep = ownership_paths(u, "own-kestrel-pep", "cp-kestrel")
    hidden = ownership_paths(u, "own-tiber-hidden", "cp-tiberpeak")
    assert [len(p) - 1 for p in pep] == [3]
    assert [len(p) - 1 for p in hidden] == [4]
    assert beneficial_owners(u, "cp-tiberpeak")["own-tiber-hidden"] == pytest.approx(60.0)
    assert beneficial_owners(u, "cp-kestrel")["own-kestrel-pep"] == pytest.approx(55.0)


def test_s6_chart_hides_the_owner_and_scenario_records_them(sc: dict[str, Any], result: BuildResult) -> None:
    s = sc["s6-london-tiberpeak-decline"]
    chart = next(a for a in s.attachments if a["kind"] == "ownership-chart")
    assert chart["facts"]["complete"] is False
    assert set(chart["facts"]["declared_beneficial_owners"].values()) == {40.0}
    assert "own-tiber-hidden" in s.beneficial_owners
    assert s.beneficial_owners == beneficial_owners(result.universe, "cp-tiberpeak")


def test_newco_founders_are_beneficial_owners(result: BuildResult) -> None:
    for entity in ("newco-syd", "newco-hou", "newco-nyc", "newco-lon"):
        owners = beneficial_owners(result.universe, entity)
        assert owners == {"own-founder-1": pytest.approx(34.1), "own-founder-2": pytest.approx(27.9)}


# --- credit arithmetic and the onboarded() record -----------------------------------------------


@pytest.mark.parametrize("sid", sorted(COUNTS))
def test_limit_minus_exposure_is_headroom(sc: dict[str, Any], sid: str) -> None:
    credit = sc[sid].data["onboarded"]["credit"]
    assert credit["limit"] - credit["exposure"] == credit["headroom"]
    assert credit["limit"] == credit["unsecured"] + credit["guaranteed"] + credit["sblc"]


def test_credit_figures_match_the_brief(sc: dict[str, Any]) -> None:
    s1 = sc["s1-houston-vitol-wti"].data["onboarded"]["credit"]
    assert (s1["limit"], s1["requested"]) == (40_000_000, 60_000_000)
    s2 = sc["s2-london-kestrel-vlsfo"].data["onboarded"]
    assert s2["credit"]["limit"] == 0 and s2["secured_only"] and s2["prepayment_only"]
    s3 = sc["s3-sydney-harbourline-ulp"].data["onboarded"]["credit"]
    assert (s3["unsecured"], s3["guaranteed"], s3["sblc"]) == (5_000_000, 15_000_000, 10_000_000)
    s4 = sc["s4-newyork-glencore-isda"].data["onboarded"]
    assert s4["threshold"] == 2_500_000
    s5 = sc["s5-sydney-bp-jet"].data["onboarded"]
    assert (s5["group_limit"], s5["london_exposure"]) == (150_000_000, 62_000_000)
    assert s5["group_headroom"] == s5["group_limit"] - s5["london_exposure"]
    assert s5["credit"]["limit"] <= s5["group_headroom"]
    s6 = sc["s6-london-tiberpeak-decline"].data["onboarded"]
    assert s6["declined"] and s6["credit"]["limit"] == 0


@pytest.mark.parametrize("sid", ["s1-houston-vitol-wti", "s2-london-kestrel-vlsfo", "s3-sydney-harbourline-ulp"])
def test_final_redline_equals_onboarded_values(sc: dict[str, Any], sid: str) -> None:
    s = sc[sid]
    rec = s.data["onboarded"]
    last = [a for a in s.attachments if a["kind"] == "agreement-redline"][-1]
    assert last["facts"]["values_after"] == rec["clauses"]
    assert rec["payment_days"] == rec["clauses"]["payment"]["days"]
    agreed = {p["clause"]: p["value"] for p in s.positions if p["status"] == "agreed"}
    assert agreed["payment"] == rec["payment_days"]
    assert agreed["quantity_tolerance"] == rec["tolerance_pct"]


def test_s1_has_four_redline_rounds_with_tracked_changes(sc: dict[str, Any], result: BuildResult) -> None:
    s = sc["s1-houston-vitol-wti"]
    rounds = [a for a in s.attachments if a["kind"] == "agreement-redline"]
    assert [a["facts"]["round"] for a in rounds] == [1, 2, 3, 4]
    files = render_files(result)
    for a in rounds:
        with zipfile.ZipFile(BytesIO(files[f"onboarding/{s.id}/{a['path']}"])) as zf:
            xml = zf.read("word/document.xml").decode("utf-8")
        assert "<w:ins " in xml and "<w:del " in xml
    statuses = [p["status"] for p in s.positions if p["clause"] == "payment"]
    assert statuses == ["superseded", "superseded", "superseded", "agreed"]


def test_s4_has_three_rounds_each_for_schedule_and_csa(sc: dict[str, Any]) -> None:
    s = sc["s4-newyork-glencore-isda"]
    rounds = [a for a in s.attachments if a["kind"] == "agreement-redline"]
    assert len(rounds) == 6
    rec = s.data["onboarded"]
    assert rec["csa"]["threshold"]["amount"] == rec["threshold"]
    assert {a["facts"]["round"] for a in rounds} == {1, 2, 3}


def test_onboarded_accessor_reads_upstream_and_builds(sc: dict[str, Any], result: BuildResult) -> None:
    from_upstream = onboarded(result.universe, sc)
    assert set(from_upstream) == set(COUNTS)
    rebuilt = onboarded(result.universe)
    assert rebuilt == from_upstream


def test_accounts_in_onboarded_exist_in_universe(sc: dict[str, Any], result: BuildResult) -> None:
    for sid, s in sc.items():
        for acct in s.data["onboarded"]["accounts"].values():
            assert acct in result.universe["accounts"], (sid, acct)


# --- ontology grounding -------------------------------------------------------------------------


def test_every_class_uri_is_in_sample_classes(sc: dict[str, Any]) -> None:
    allowed = allowed_uris()
    for s in sc.values():
        used = set(_uris(s.manifest()))
        assert used, s.id
        assert used <= allowed, (s.id, sorted(used - allowed))


def test_message_and_attachment_classes_follow_the_brief_table(sc: dict[str, Any]) -> None:
    by_kind = {
        "agreement-redline": {"Contract", "CollateralAgreement"},
        "credit-memo": {"CreditFacility"},
        "credit-limit-letter": {"CreditFacility"},
        "ssi-letter": {"Account"},
        "sblc-issuance-advice": {"CreditFacility"},
        "sblc-confirmation": {"CEG"},
    }
    for s in sc.values():
        for a in s.attachments:
            assert a["provenance"]["wasDerivedFrom"].startswith("<")
            assert a["provenance"]["class_uri"].endswith("prov#Entity")
            if a["kind"] in by_kind:
                assert a["class_uri"].rsplit("/", 1)[-1].rsplit("#", 1)[-1] in {
                    *by_kind[a["kind"]],
                    *(FALLBACKS.get(n) for n in by_kind[a["kind"]]),
                }, (s.id, a["filename"])


def test_ownership_edges_carry_gleif_consolidation_classes(sc: dict[str, Any]) -> None:
    edges = [e for s in sc.values() for e in s.ownership_edges if e["class_uri"]]
    assert edges
    assert {e["class_uri"].rsplit("/", 1)[-1] for e in edges} <= {"DirectConsolidation", "UltimateConsolidation"}


def test_every_scenario_has_questions_and_ids(sc: dict[str, Any]) -> None:
    for sid, s in sc.items():
        assert [q["id"] for q in s.questions] == [f"{sid}-q{i}" for i in (1, 2, 3)]
        assert all({"type", "question", "expected", "tolerance_pct"} <= q.keys() for q in s.questions)
