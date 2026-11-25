# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for Aurora Data API config resolution."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from coa_common.aurora_data_api import AuroraConfig, AuroraConfigError, clear_ssm_cache

pytestmark = pytest.mark.unit

CLUSTER = "arn:aws:rds:us-east-1:111122223333:cluster:c1"
SECRET = "arn:aws:secretsmanager:us-east-1:111122223333:secret:s1-AbCdEf"


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_ssm_cache()
    yield
    clear_ssm_cache()


def _ssm(values: dict[str, str]) -> MagicMock:
    client = MagicMock()

    def get_parameter(Name):
        if Name not in values:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
        return {"Parameter": {"Value": values[Name]}}

    client.get_parameter.side_effect = get_parameter
    return client


def test_literal_arns_need_no_ssm():
    ssm = _ssm({})
    cfg = AuroraConfig.from_env({"AURORA_CLUSTER_ARN": CLUSTER, "AURORA_SECRET_ARN": SECRET}, ssm_client=ssm)
    assert cfg.resource_arn == CLUSTER
    assert cfg.secret_arn == SECRET
    assert cfg.database == "postgres"
    assert cfg.resume_timeout_seconds == 45
    ssm.get_parameter.assert_not_called()


def test_resource_arn_alias_and_database_override():
    cfg = AuroraConfig.from_env(
        {
            "AURORA_RESOURCE_ARN": CLUSTER,
            "AURORA_SECRET_ARN": SECRET,
            "AURORA_DATABASE": "ontology",
            "AURORA_RESUME_TIMEOUT_SECONDS": "12.5",
        }
    )
    assert cfg.database == "ontology"
    assert cfg.resume_timeout_seconds == 12.5


def test_cluster_arn_preferred_over_resource_arn():
    cfg = AuroraConfig.from_env(
        {"AURORA_CLUSTER_ARN": CLUSTER, "AURORA_RESOURCE_ARN": "arn:other", "AURORA_SECRET_ARN": SECRET}
    )
    assert cfg.resource_arn == CLUSTER


def test_ssm_names_resolved_from_sibling_parameter():
    ssm = _ssm({"/coa/aurora/cluster-arn": CLUSTER, "/coa/aurora/secret-arn": SECRET})
    cfg = AuroraConfig.from_env(
        {"AURORA_CLUSTER_ENDPOINT": "/coa/aurora/cluster-endpoint", "AURORA_SECRET_ARN": "/coa/aurora/secret-arn"},
        ssm_client=ssm,
    )
    assert cfg.resource_arn == CLUSTER
    assert cfg.secret_arn == SECRET


def test_ssm_reads_are_cached():
    ssm = _ssm({"/p/cluster-arn": CLUSTER})
    env = {"AURORA_CLUSTER_ENDPOINT": "/p/endpoint", "AURORA_SECRET_ARN": SECRET}
    AuroraConfig.from_env(env, ssm_client=ssm)
    AuroraConfig.from_env(env, ssm_client=ssm)
    assert ssm.get_parameter.call_count == 1


def test_hostname_endpoint_without_arn_is_an_error():
    with pytest.raises(AuroraConfigError, match="cluster ARN"):
        AuroraConfig.from_env(
            {"AURORA_CLUSTER_ENDPOINT": "db.cluster-x.us-east-1.rds.amazonaws.com", "AURORA_SECRET_ARN": SECRET}
        )


def test_missing_secret_is_an_error():
    with pytest.raises(AuroraConfigError, match="AURORA_SECRET_ARN"):
        AuroraConfig.from_env({"AURORA_CLUSTER_ARN": CLUSTER})


def test_access_denied_names_the_missing_grant():
    ssm = MagicMock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetParameter"
    )
    with pytest.raises(AuroraConfigError, match="ssm:GetParameter"):
        AuroraConfig.from_env({"AURORA_CLUSTER_ARN": CLUSTER, "AURORA_SECRET_ARN": "/s/secret"}, ssm_client=ssm)


def test_missing_parameter_is_reported():
    with pytest.raises(AuroraConfigError, match="does not exist"):
        AuroraConfig.from_env({"AURORA_CLUSTER_ARN": CLUSTER, "AURORA_SECRET_ARN": "/nope"}, ssm_client=_ssm({}))


def test_bad_timeout_is_an_error():
    with pytest.raises(AuroraConfigError, match="AURORA_RESUME_TIMEOUT_SECONDS"):
        AuroraConfig.from_env(
            {"AURORA_CLUSTER_ARN": CLUSTER, "AURORA_SECRET_ARN": SECRET, "AURORA_RESUME_TIMEOUT_SECONDS": "soon"}
        )


def test_repr_does_not_leak_arns():
    cfg = AuroraConfig(resource_arn=CLUSTER, secret_arn=SECRET)
    assert CLUSTER not in repr(cfg)
    assert SECRET not in repr(cfg)
