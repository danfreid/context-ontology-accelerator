// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Annotations, Match, Template } from "aws-cdk-lib/assertions";
import { resolveBackendFlags } from "../../lib/context";
import {
  auroraSearchEnv,
  grantAuroraDataApi,
  isAuroraSearchEnabled,
  warnIfAuroraSearchIncomplete,
} from "../../lib/utils/aurora-search";

const SSM_PREFIX = "/coa/dev";
const FULL = {
  graph_backend: "aurora",
  vector_backend: "pgvector",
  metadata_backend: "aurora",
};

function flagsFor(context: Record<string, unknown>) {
  return resolveBackendFlags(new cdk.App({ context }).node);
}

describe("isAuroraSearchEnabled", () => {
  test("is false by default and for pgvector alone", () => {
    expect(isAuroraSearchEnabled(flagsFor({}))).toBe(false);
    expect(isAuroraSearchEnabled(flagsFor({ vector_backend: "pgvector" }))).toBe(
      false,
    );
  });

  test("is false when any one of the three flags is missing", () => {
    expect(
      isAuroraSearchEnabled(
        flagsFor({ ...FULL, graph_backend: "neptune" }),
      ),
    ).toBe(false);
    expect(
      isAuroraSearchEnabled(
        flagsFor({ ...FULL, metadata_backend: "dynamodb" }),
      ),
    ).toBe(false);
  });

  test("is true for the full set", () => {
    expect(isAuroraSearchEnabled(flagsFor(FULL))).toBe(true);
  });
});

describe("warnIfAuroraSearchIncomplete", () => {
  function warnings(context: Record<string, unknown>) {
    const app = new cdk.App({ context });
    const stack = new cdk.Stack(app, "S");
    warnIfAuroraSearchIncomplete(stack, resolveBackendFlags(app.node));
    return Annotations.fromStack(stack).findWarning("*", Match.anyValue());
  }

  test("warns once for pgvector without the other flags", () => {
    expect(warnings({ vector_backend: "pgvector" })).toHaveLength(1);
  });

  test("is silent by default and for the full set", () => {
    expect(warnings({})).toHaveLength(0);
    expect(warnings(FULL)).toHaveLength(0);
  });
});

describe("auroraSearchEnv", () => {
  test("is empty for the default aoss vector backend", () => {
    expect(auroraSearchEnv(SSM_PREFIX, flagsFor({}))).toEqual({});
  });

  test("is empty for pgvector without graph and metadata aurora", () => {
    expect(
      auroraSearchEnv(SSM_PREFIX, flagsFor({ vector_backend: "pgvector" })),
    ).toEqual({});
  });

  test("names the rw secret parameter by default for the full set", () => {
    expect(auroraSearchEnv(SSM_PREFIX, flagsFor(FULL))).toEqual({
      HYBRID_SEARCH_BACKEND: "pgvector+tsvector",
      AURORA_SSM_PREFIX: SSM_PREFIX,
      AURORA_SECRET_ARN: "/coa/dev/aurora/search-rw-secret-arn",
    });
  });

  test("names the ro secret parameter for the ro role", () => {
    expect(
      auroraSearchEnv(SSM_PREFIX, flagsFor(FULL), "ro").AURORA_SECRET_ARN,
    ).toBe("/coa/dev/aurora/search-ro-secret-arn");
  });

  test("names the engine-rw secret parameter for the engine role", () => {
    expect(
      auroraSearchEnv(SSM_PREFIX, flagsFor(FULL), "engine-rw").AURORA_SECRET_ARN,
    ).toBe("/coa/dev/aurora/engine-rw-secret-arn");
  });
});

describe("grantAuroraDataApi", () => {
  function grant(role: "rw" | "ro" | "engine-rw") {
    const stack = new cdk.Stack(new cdk.App(), "S");
    const principal = new iam.Role(stack, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    grantAuroraDataApi(stack, principal, SSM_PREFIX, role);
    const template = Template.fromStack(stack);
    const statements: Array<{
      Action: string | string[];
      Resource: unknown;
    }> = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    return { json: JSON.stringify(template.toJSON()), statements };
  }

  test("adds Data API and secret statements resolved from SSM", () => {
    const { json } = grant("rw");
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
    expect(json).toContain("/coa/dev/aurora/search-rw-secret-arn");
  });

  test("ro reads only the ro secret parameter", () => {
    const { json } = grant("ro");
    expect(json).toContain("/coa/dev/aurora/search-ro-secret-arn");
    expect(json).not.toContain("search-rw-secret-arn");
    expect(json).not.toContain("/aurora/secret-arn");
  });

  test("engine-rw reads only the engine-rw secret parameter", () => {
    const { json } = grant("engine-rw");
    expect(json).toContain("/coa/dev/aurora/engine-rw-secret-arn");
    expect(json).not.toContain("search-rw-secret-arn");
    expect(json).not.toContain("search-ro-secret-arn");
  });

  test("names exact parameters and never uses a wildcard resource", () => {
    const { statements, json } = grant("rw");
    expect(json).not.toContain("aurora/*");
    for (const s of statements) {
      expect(JSON.stringify(s.Resource)).not.toContain('"*"');
    }
    const ssmStatement = statements.find((s) =>
      JSON.stringify(s.Action).includes("ssm:GetParameter"),
    );
    expect(ssmStatement!.Resource).toHaveLength(2);
  });
});