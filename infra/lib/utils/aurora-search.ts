// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as iam from "aws-cdk-lib/aws-iam";
import * as cdk from "aws-cdk-lib";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import type { BackendFlags } from "../context";

/** Database role a workload connects as: read-write or read-only on the search tables, or the engine role (coa_engine_rw, also owns triples and metadata). */
export type AuroraSearchRole = "rw" | "ro" | "engine-rw";

/**
 * True when the Aurora cluster that backs search is deployed. The cluster exists
 * only for `graph_backend=aurora && vector_backend=pgvector && metadata_backend=aurora`,
 * so env and IAM are gated on the same condition rather than on `vector_backend` alone.
 */
export function isAuroraSearchEnabled(flags: BackendFlags): boolean {
  return (
    flags.graphBackend === "aurora" &&
    flags.vectorBackend === "pgvector" &&
    flags.metadataBackend === "aurora"
  );
}

/**
 * Warns when `vector_backend=pgvector` is set without the other two Aurora flags.
 * No cluster exists on that path, so the search layer emits nothing.
 */
export function warnIfAuroraSearchIncomplete(
  scope: Construct,
  flags: BackendFlags,
): void {
  if (flags.vectorBackend === "pgvector" && !isAuroraSearchEnabled(flags)) {
    cdk.Annotations.of(scope).addWarning(
      "vector_backend=pgvector needs graph_backend=aurora and metadata_backend=aurora too. " +
        "Without them no Aurora cluster is created, so the Aurora search layer is not configured.",
    );
  }
}

/** SSM parameter (under `{ssmPrefix}/aurora/`) that holds the secret ARN for a role. */
function secretParameterName(ssmPrefix: string, role: AuroraSearchRole): string {
  return role === "engine-rw"
    ? `${ssmPrefix}/aurora/engine-rw-secret-arn`
    : `${ssmPrefix}/aurora/search-${role}-secret-arn`;
}

/**
 * Environment for services that search through Aurora (coa_common.aurora_search).
 *
 * Empty unless the full Aurora flag set is on (see {@link isAuroraSearchEnabled}), so
 * the default AOSS deployment gets no new variables. `AURORA_SSM_PREFIX` lets the
 * Python side resolve the cluster ARN from `{ssmPrefix}/aurora/cluster-arn`.
 * `AURORA_SECRET_ARN` names the SSM parameter that holds the secret of the role's
 * database user (AuroraConfig resolves a value that starts with `/` through SSM).
 */
export function auroraSearchEnv(
  ssmPrefix: string,
  flags: BackendFlags,
  role: AuroraSearchRole = "rw",
): Record<string, string> {
  if (!isAuroraSearchEnabled(flags)) {
    return {};
  }
  return {
    HYBRID_SEARCH_BACKEND: "pgvector+tsvector",
    AURORA_SSM_PREFIX: ssmPrefix,
    AURORA_SECRET_ARN: secretParameterName(ssmPrefix, role),
  };
}

/**
 * Grants the RDS Data API calls, the secret read for one database role, and the
 * SSM reads of the two parameters the Aurora search client resolves. Callers apply
 * it only when {@link isAuroraSearchEnabled} is true.
 */
export function grantAuroraDataApi(
  scope: Construct,
  grantee: iam.IGrantable,
  ssmPrefix: string,
  role: AuroraSearchRole,
): void {
  const secretParam = secretParameterName(ssmPrefix, role);
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
      resources: [ssm.StringParameter.valueForStringParameter(scope, secretParam)],
    }),
  );
  // AuroraConfig.from_env() reads the cluster ARN and the role's secret ARN from SSM.
  const stack = cdk.Stack.of(scope);
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: ["ssm:GetParameter", "ssm:GetParameters"],
      resources: [`${ssmPrefix}/aurora/cluster-arn`, secretParam].map((name) =>
        stack.formatArn({
          service: "ssm",
          resource: "parameter",
          resourceName: name.replace(/^\//, ""),
        }),
      ),
    }),
  );
}
