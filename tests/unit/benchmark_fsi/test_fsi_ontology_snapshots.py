# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pinned ontology snapshots: manifest integrity, no id collisions with the live pack, loader selection."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from tests.benchmark.fsi_commodity import load_ontologies as lo

pytestmark = pytest.mark.unit

PACK = Path(__file__).resolve().parents[3] / "packages" / "ontology-engine" / "demo" / "config-fsi-commodity.yaml"
ONTOLOGY_TYPES = {"foundational", "induced", "user_created", "user_uploaded"}


@pytest.fixture(scope="module")
def snapshots() -> list[lo.Snapshot]:
    return lo.load_manifest()


@pytest.fixture(scope="module")
def committed(snapshots: list[lo.Snapshot]) -> list[lo.Snapshot]:
    return [s for s in snapshots if not s.optional]


def test_every_committed_snapshot_matches_its_pinned_size_and_sha256(committed: list[lo.Snapshot]) -> None:
    assert committed, "manifest lists no committed snapshots"
    for s in committed:
        assert len(lo.read_verified(s)) == s.size


def test_keys_and_ontology_ids_are_unique_fixture_urns_with_valid_types(snapshots: list[lo.Snapshot]) -> None:
    keys = [s.key for s in snapshots]
    ids = [s.ontology_id for s in snapshots]
    assert len(keys) == len(set(keys))
    assert len(ids) == len(set(ids))
    for s in snapshots:
        assert s.ontology_id == f"urn:coa:fixture:fsi-commodity:{s.key}"
        assert s.path.name == f"{s.key}.ttl"
        assert s.ontology_type in ONTOLOGY_TYPES


def test_pack_source_file_entries_match_their_manifest_snapshot(snapshots: list[lo.Snapshot]) -> None:
    pack = yaml.safe_load(PACK.read_text(encoding="utf-8"))
    entries = [o for group in ("foundational_ontologies", "customer_ontologies") for o in pack.get(group) or []]
    by_id = {s.ontology_id: s for s in snapshots}
    uploaded = [o for o in entries if "source_file" in o]
    assert uploaded, "the pack loads no uploaded snapshot"
    for o in uploaded:
        snap = by_id[o["uri"]]  # the pack registers the snapshot under its manifest id
        assert (PACK.parent / o["source_file"]).resolve() == snap.path.resolve()
        assert o["sha256"] == snap.sha256
        assert "source_url" not in o
    # A live entry never takes a fixture id, so live and uploaded records cannot merge.
    for o in entries:
        if "source_file" not in o:
            assert o["uri"] not in by_id


def test_gpl_snapshots_ship_with_the_licence_text_and_every_file_has_a_notice() -> None:
    entries = yaml.safe_load(lo.MANIFEST_PATH.read_text(encoding="utf-8"))["ontologies"]
    notice = (lo.ONTOLOGY_DIR / "NOTICE.md").read_text(encoding="utf-8")
    gpl = [e for e in entries if "GPL" in e["license"]]
    assert gpl, "expected the UN/CEFACT snapshots to be GPL"
    for e in gpl:
        text = (lo.ONTOLOGY_DIR / e["license_file"]).read_text(encoding="utf-8")
        assert text.lstrip().startswith("GNU GENERAL PUBLIC LICENSE") and "Version 3" in text
    for e in entries:
        assert f"`{e['file']}`" in notice or e["file"].startswith("fibo"), e["file"]


def test_every_ttl_in_the_directory_is_in_the_manifest(snapshots: list[lo.Snapshot]) -> None:
    on_disk = {p.name for p in lo.ONTOLOGY_DIR.glob("*.ttl")}
    assert on_disk <= {s.path.name for s in snapshots}


def test_client_token_is_stable_printable_and_within_the_api_limit(snapshots: list[lo.Snapshot]) -> None:
    for s in snapshots:
        token = s.client_token
        assert 1 <= len(token) <= 64
        assert all(0x21 <= ord(c) <= 0x7E for c in token)
        assert token == dataclasses.replace(s).client_token


def test_tampered_bytes_are_rejected_before_upload(tmp_path: Path, committed: list[lo.Snapshot]) -> None:
    original = committed[0]
    copy = tmp_path / original.path.name
    copy.write_bytes(original.path.read_bytes() + b"\n")
    with pytest.raises(lo.ManifestError, match="manifest expects"):
        lo.read_verified(dataclasses.replace(original, path=copy))
    with pytest.raises(lo.ManifestError, match="does not exist"):
        lo.read_verified(dataclasses.replace(original, path=tmp_path / "missing.ttl"))


def test_selection_skips_optional_by_default_and_rejects_unknown_keys(snapshots: list[lo.Snapshot]) -> None:
    chosen, notes = lo.select(snapshots)
    assert [s.key for s in chosen] == [s.key for s in snapshots if not s.optional]
    assert len(notes) == sum(1 for s in snapshots if s.optional)

    chosen, _ = lo.select(snapshots, only=["dcsa-ebl", "isda-cdm"])
    assert [s.key for s in chosen] == ["dcsa-ebl", "isda-cdm"]

    with pytest.raises(lo.ManifestError, match="unknown snapshot key"):
        lo.select(snapshots, only=["no-such-ontology"])


def test_optional_snapshot_without_its_file_is_skipped_even_when_requested(
    tmp_path: Path, snapshots: list[lo.Snapshot]
) -> None:
    optional = [dataclasses.replace(s, path=tmp_path / s.path.name) for s in snapshots if s.optional]
    chosen, notes = lo.select(optional, include_optional=True)
    assert chosen == []
    assert all("not present" in n for n in notes)


def test_deployed_target_requires_a_uuid_namespace() -> None:
    with pytest.raises(ValueError):
        lo.DeployedTarget("https://example.execute-api.us-east-1.amazonaws.com/dev", "fsi-commodity", "us-east-1", 1)
    lo.DeployedTarget(
        "https://example.execute-api.us-east-1.amazonaws.com/dev",
        "6f1c2b8e-0c3a-4d2e-9b1f-1a2b3c4d5e6f",
        "us-east-1",
        1,
    )


def test_dry_run_verifies_every_committed_file_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert lo.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "verified" in out
    assert "plan  isda-cdm" in out


def test_snapshots_parse_and_declare_the_manifest_class_count(committed: list[lo.Snapshot]) -> None:
    rdflib = pytest.importorskip("rdflib")
    from rdflib import OWL, RDF, RDFS, URIRef

    manifest = {e["key"]: e for e in yaml.safe_load(lo.MANIFEST_PATH.read_text(encoding="utf-8"))["ontologies"]}
    for s in committed:
        g = rdflib.Graph().parse(data=s.path.read_bytes(), format="turtle")
        assert any(g.subjects(RDF.type, OWL.Ontology)), s.key
        classes = {c for t in (OWL.Class, RDFS.Class) for c in g.subjects(RDF.type, t) if isinstance(c, URIRef)}
        assert len(classes) == manifest[s.key]["classes"], s.key
