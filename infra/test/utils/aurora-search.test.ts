// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Template } from "aws-cdk-lib/assertions";
import { resolveBackendFlags } from "../../lib/context";
import {
  auroraSearchEnv,
  grantAuroraDataApi,
} from "../../lib/utils/aurora-search";

const SSM_PREFIX = "/coa/dev";

function flagsFor(context: Record<string, unknown>) {
  return resolveBackendFlags(new cdk.App({ context }).node);
}

describe("auroraSearchEnv", () => {
  test("is empty for the default aoss vector backend", () => {
    expect(auroraSearchEnv(SSM_PREFIX, flagsFor({}))).toEqual({});
  });

  test("carries the backend switch and SSM prefix for pgvector", () => {
    expect(
      auroraSearchEnv(SSM_PREFIX, flagsFor({ vector_backend: "pgvector" })),
    ).toEqual({
      HYBRID_SEARCH_BACKEND: "pgvector+tsvector",
      AURORA_SSM_PREFIX: SSM_PREFIX,
    });
  });
});

describe("grantAuroraDataApi", () => {
  test("adds Data API and secret statements resolved from SSM", () => {
    const stack = new cdk.Stack(new cdk.App(), "S");
    const role = new iam.Role(stack, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    grantAuroraDataApi(stack, role, SSM_PREFIX);
    const json = JSON.stringify(Template.fromStack(stack).toJSON());
    for (const action of [
      "rds-data:ExecuteStatement",
      "rds-data:BatchExecuteStatement",
      "rds-data:BeginTransaction",
      "rds-data:CommitTransaction",
      "rds-data:RollbackTransaction",
      "secretsmanager:GetSecretValue",
      "ssm:GetParameters",
    ]) {
      expect(json).toContain(action);
    }
    expect(json).toContain("/coa/dev/aurora/cluster-arn");
    expect(json).toContain("/coa/dev/aurora/secret-arn");
  });
});

