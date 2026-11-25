// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as iam from "aws-cdk-lib/aws-iam";
import * as cdk from "aws-cdk-lib";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

import type { BackendFlags } from "../context";

/**
 * Environment for services that search through Aurora (coa_common.aurora_search).
 *
 * Empty unless `vector_backend=pgvector`, so the default AOSS deployment gets no
 * new variables. `AURORA_SSM_PREFIX` lets the Python side resolve the cluster and
 * secret ARNs from `{ssmPrefix}/aurora/cluster-arn` and `/aurora/secret-arn`.
 */
export function auroraSearchEnv(
  ssmPrefix: string,
  flags: BackendFlags,
): Record<string, string> {
  if (flags.vectorBackend !== "pgvector") {
    return {};
  }
  return {
    HYBRID_SEARCH_BACKEND: "pgvector+tsvector",
    AURORA_SSM_PREFIX: ssmPrefix,
  };
}

/**
 * Grants the RDS Data API calls and the admin secret read that the Aurora search
 * client needs. Callers apply it on the pgvector path only.
 */
export function grantAuroraDataApi(
  scope: Construct,
  grantee: iam.IGrantable,
  ssmPrefix: string,
): void {
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: [
        "rds-data:ExecuteStatement",
        "rds-data:BatchExecuteStatement",
        "rds-data:BeginTransaction",
        "rds-data:CommitTransaction",
        "rds-data:RollbackTransaction",
      ],
      resources: [
        ssm.StringParameter.valueForStringParameter(
          scope,
          `${ssmPrefix}/aurora/cluster-arn`,
        ),
      ],
    }),
  );
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: ["secretsmanager:GetSecretValue"],
      resources: [
        ssm.StringParameter.valueForStringParameter(
          scope,
          `${ssmPrefix}/aurora/secret-arn`,
        ),
      ],
    }),
  );
  // AuroraConfig.from_env() reads the two ARNs from SSM when only
  // AURORA_SSM_PREFIX is set.
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: ["ssm:GetParameter", "ssm:GetParameters"],
      resources: [
        cdk.Stack.of(scope).formatArn({
          service: "ssm",
          resource: "parameter",
          resourceName: `${ssmPrefix.replace(/^\//, "")}/aurora/*`,
        }),
      ],
    }),
  );
}
