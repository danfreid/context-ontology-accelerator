# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Control-plane handlers with ``METADATA_BACKEND=aurora`` over the in-memory Aurora metadata store."""

from __future__ import annotations

import json

import pytest
from coa_common.testing_aurora import install  # noqa: E402
from coa_control_plane.grants.create_handler import handler as create_grant  # noqa: E402
from coa_control_plane.grants.delete_handler import handler as delete_grant  # noqa: E402
from coa_control_plane.grants.list_handler import handler as list_grants  # noqa: E402
from coa_control_plane.namespace import cleanup  # noqa: E402

pytestmark = pytest.mark.unit

NS = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def store(monkeypatch):
    for name, value in {
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-resource-role-mappings",
        "NAMESPACES_TABLE": "test-namespaces",
        "CACHE_INVALIDATION_TABLE_NAME": "test-cache-invalidation",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    return install(monkeypatch)


def _grant_event(body: dict | None = None, grant_id: str | None = None) -> dict:
    event: dict = {
        "pathParameters": {"namespaceId": NS, **({"grantId": grant_id} if grant_id else {})},
        "requestContext": {"authorizer": {"claims": {"email": "admin@example.com"}, "canViewSource": "true"}},
        "queryStringParameters": None,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _version(store) -> int:
    return int(store["test-cache-invalidation"][("CACHE_VERSION", "CURRENT")]["version"])


def test_grant_lifecycle_and_cache_invalidation(store):
    store["test-roles"] = {(f"NS#{NS}", "ROLE#analyst"): {"PK": f"NS#{NS}", "SK": "ROLE#analyst"}}
    body = {"principalType": "User", "principalId": "alice@company.com", "role": "analyst"}

    created = create_grant(_grant_event(body), None)
    assert created["statusCode"] == 201
    grant_id = json.loads(created["body"])["grant"]["grantId"]
    assert _version(store) == 1  # the roles/mappings stream's job on DynamoDB

    assert create_grant(_grant_event(body), None)["statusCode"] == 409

    listed = json.loads(list_grants(_grant_event(), None)["body"])
    assert [g["principalId"] for g in listed["grants"]] == ["alice@company.com"]

    assert delete_grant(_grant_event(grant_id=grant_id), None)["statusCode"] == 200
    assert json.loads(list_grants(_grant_event(), None)["body"])["grants"] == []
    assert _version(store) == 2  # the rejected duplicate wrote nothing


def test_grant_for_unknown_role_is_404(store):
    body = {"principalType": "User", "principalId": "alice@company.com", "role": "nope"}
    assert create_grant(_grant_event(body), None)["statusCode"] == 404
    assert "test-cache-invalidation" not in store


def test_namespace_teardown_deletes_record_and_name_reservation(store):
    store["test-namespaces"] = {
        (f"NS#{NS}", "METADATA"): {"PK": f"NS#{NS}", "SK": "METADATA", "name": "sales"},
        ("NS_NAME#sales", "RESERVATION"): {"PK": "NS_NAME#sales", "SK": "RESERVATION", "namespaceId": NS},
        (f"NS#{NS}", "OTHER"): {"PK": f"NS#{NS}", "SK": "OTHER"},
    }
    cleanup.delete_namespace_records(NS, "sales")
    assert list(store["test-namespaces"]) == [(f"NS#{NS}", "OTHER")]
    cleanup.delete_namespace_records(NS, "sales")  # idempotent, like the DynamoDB transaction


def test_namespace_teardown_deletes_roles_and_role_mappings(store):
    store["test-roles"] = {
        (f"NS#{NS}", "ROLE#a"): {"PK": f"NS#{NS}", "SK": "ROLE#a"},
        ("NS#other", "ROLE#a"): {"PK": "NS#other", "SK": "ROLE#a"},
    }
    store["test-resource-role-mappings"] = {
        ("m1", "ROLE#a"): {
            "PK": "m1",
            "SK": "ROLE#a",
            "namespaceKey": f"NS#{NS}",
            "principalRoleKey": "User::a#ROLE#a",
        },
        ("m2", "ROLE#a"): {
            "PK": "m2",
            "SK": "ROLE#a",
            "namespaceKey": "NS#other",
            "principalRoleKey": "User::a#ROLE#a",
        },
    }
    cleanup.delete_roles(NS)
    cleanup.delete_role_mappings(NS)
    assert list(store["test-roles"]) == [("NS#other", "ROLE#a")]
    assert list(store["test-resource-role-mappings"]) == [("m2", "ROLE#a")]
