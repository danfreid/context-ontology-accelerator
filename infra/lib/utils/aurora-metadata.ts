// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Construct } from "constructs";
import { TABLE_NAMES } from "@coa/shared";
import { prefixed, resolveBackendFlags, resolveContext } from "../context";
import type { BackendFlags } from "../context";
import {
  auroraSearchEnv,
  grantAuroraDataApi,
  isAuroraMetadataEnabled,
} from "./aurora-search";

/**
 * Environment for a workload that reads or writes metadata (the DAO tables) and the
 * graph through Aurora: `METADATA_BACKEND=aurora`, `GRAPH_BACKEND=aurora`, and the Data
 * API settings of the engine role (`coa_engine_rw`, the only role with access to
 * `coa_metadata` and `coa_triple`).
 *
 * Empty unless the full Aurora flag set is on, so default templates get no new variables.
 */
export function auroraBackendEnv(
  ssmPrefix: string,
  flags: BackendFlags,
  dimensions?: number,
): Record<string, string> {
  if (!isAuroraMetadataEnabled(flags)) {
    return {};
  }
  return {
    METADATA_BACKEND: "aurora",
    GRAPH_BACKEND: "aurora",
    ...auroraSearchEnv(ssmPrefix, flags, "engine-rw", dimensions),
  };
}

/**
 * Points a Lambda at Aurora for metadata and graph data: sets {@link auroraBackendEnv}
 * and the cache-invalidation table name, and grants the Data API calls as the engine
 * role. No-op unless the full Aurora flag set is on.
 */
export function wireAuroraBackend(
  scope: Construct,
  fn: lambda.Function,
  dimensions?: number,
): void {
  const flags = resolveBackendFlags(scope.node);
  if (!isAuroraMetadataEnabled(flags)) {
    return;
  }
  const { ssmPrefix } = resolveContext(scope.node);
  for (const [key, value] of Object.entries(
    auroraBackendEnv(ssmPrefix, flags, dimensions),
  )) {
    fn.addEnvironment(key, value);
  }
  // The DAO bumps the authorizer cache version in this logical table after every write
  // to a roles or mappings table. Its default name has no deployment prefix, but the
  // authorizer reads the prefixed one, so name it explicitly.
  fn.addEnvironment(
    "CACHE_INVALIDATION_TABLE_NAME",
    prefixed(scope.node, TABLE_NAMES.CACHE_INVALIDATION),
  );
  grantAuroraDataApi(scope, fn, ssmPrefix, "engine-rw");
}

/**
 * Stand-in for a DynamoDB table on the Aurora path. It carries the physical table name
 * (the DAO uses it as the logical `table_name` in `coa_metadata`) but creates no
 * resource, and every `grant*` call is a no-op so no `dynamodb:*` statement is emitted.
 * Callers that build raw `dynamodb:` statements or stream event sources must skip them
 * themselves on this path.
 */
export function logicalTable(
  scope: Construct,
  id: string,
  tableName: string,
): dynamodb.ITable {
  const table = dynamodb.Table.fromTableName(scope, id, tableName);
  const drop = (grantee: iam.IGrantable): iam.Grant =>
    iam.Grant.drop(
      grantee,
      `Aurora metadata backend: no DynamoDB table ${tableName}`,
    );
  Object.assign(table, {
    grant: drop,
    grantStream: drop,
    grantReadData: drop,
    grantStreamRead: drop,
    grantWriteData: drop,
    grantReadWriteData: drop,
    grantFullAccess: drop,
  });
  return table;
}
