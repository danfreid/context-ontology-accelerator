// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as iam from "aws-cdk-lib/aws-iam";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { prefixed } from "../context";
import type { BackendFlags } from "../context";
import { isAuroraMetadataEnabled } from "./aurora-search";

/**
 * Index-name prefix of the GraphRAG toolkit's S3 Vectors indexes. The toolkit names an
 * index `<prefix>.<index with '_' replaced by '-'>` (e.g. `graphrag.chunk-<tenant>`);
 * S3 Vectors index names allow lowercase letters, digits, '-' and '.'.
 */
export const GRAPHRAG_S3_VECTOR_PREFIX = "graphrag";

/**
 * True on the ckg path: the GraphRAG document graph lives in the Aurora cluster and its
 * vectors in an S3 vector bucket, so no OpenSearch Serverless collection, no aoss search
 * proxy and no Neptune are deployed.
 *
 * Needs the full Aurora flag set (graph, metadata and `vector_backend=pgvector`, the
 * latter because that is what creates the cluster) plus `graphrag_vector_backend=s3vectors`.
 * With the default `graphrag_vector_backend=aoss` this is always false, so default
 * templates are unchanged.
 */
export function isCkgPathEnabled(flags: BackendFlags): boolean {
  return (
    flags.graphragVectorBackend === "s3vectors" &&
    isAuroraMetadataEnabled(flags)
  );
}

/**
 * Warns when `graphrag_vector_backend=s3vectors` is set without the Aurora flag set.
 * The S3 vector bucket is created only on the ckg path, so nothing is deployed for it.
 */
export function warnIfCkgPathIncomplete(
  scope: Construct,
  flags: BackendFlags,
): void {
  if (flags.graphragVectorBackend === "s3vectors" && !isCkgPathEnabled(flags)) {
    cdk.Annotations.of(scope).addWarning(
      "graphrag_vector_backend=s3vectors needs graph_backend=aurora, vector_backend=pgvector " +
        "and metadata_backend=aurora too. Without them no S3 vector bucket is created and " +
        "the deployment keeps the OpenSearch Serverless GraphRAG indexes.",
    );
  }
}

/** Name of the platform's S3 vector bucket. Deterministic so stacks need no cross-stack reference. */
export function graphragVectorBucketName(scope: Construct): string {
  return prefixed(scope.node, `graphrag-vec-${cdk.Stack.of(scope).account}`);
}

/** ARN of the S3 vector bucket. */
export function graphragVectorBucketArn(scope: Construct): string {
  return cdk.Stack.of(scope).formatArn({
    service: "s3vectors",
    resource: "bucket",
    resourceName: graphragVectorBucketName(scope),
    arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
  });
}

/** ARN pattern of the GraphRAG indexes (`<prefix>.*`) in the S3 vector bucket. */
export function graphragVectorIndexArn(scope: Construct): string {
  return cdk.Stack.of(scope).formatArn({
    service: "s3vectors",
    resource: "bucket",
    resourceName: `${graphragVectorBucketName(scope)}/index/${GRAPHRAG_S3_VECTOR_PREFIX}.*`,
    arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
  });
}

/**
 * Environment that selects the S3 Vectors GraphRAG backend (`GRAPH_BACKEND=aurora` comes
 * from `auroraBackendEnv`). Empty unless {@link isCkgPathEnabled}.
 */
export function ckgGraphragEnv(
  scope: Construct,
  flags: BackendFlags,
): Record<string, string> {
  if (!isCkgPathEnabled(flags)) {
    return {};
  }
  return {
    GRAPHRAG_VECTOR_BACKEND: "s3vectors",
    GRAPHRAG_S3_VECTOR_BUCKET: graphragVectorBucketName(scope),
    GRAPHRAG_S3_VECTOR_PREFIX: GRAPHRAG_S3_VECTOR_PREFIX,
  };
}

/** S3 Vectors actions a workload needs to build, query and trim GraphRAG indexes. */
const READ_WRITE_ACTIONS = [
  "s3vectors:GetVectorBucket",
  "s3vectors:CreateIndex",
  "s3vectors:GetIndex",
  "s3vectors:ListIndexes",
  "s3vectors:PutVectors",
  "s3vectors:GetVectors",
  "s3vectors:QueryVectors",
  "s3vectors:DeleteVectors",
  "s3vectors:ListVectors",
];

/** The serve runtime only queries existing indexes. */
const READ_ACTIONS = [
  "s3vectors:GetVectorBucket",
  "s3vectors:GetIndex",
  "s3vectors:QueryVectors",
  "s3vectors:GetVectors",
];

/** Namespace deletion drops whole indexes (`DeleteIndex`) and probes them first. */
const DELETE_ACTIONS = ["s3vectors:GetIndex", "s3vectors:DeleteIndex"];

/**
 * Grants S3 Vectors access on the platform bucket and its `graphrag.*` indexes only.
 * `build` serves the KG-build and graph-cleanup task, `read` the serve runtime (query only); `delete` is
 * the namespace-deletion Lambda. Callers apply it only when {@link isCkgPathEnabled}.
 */
export function grantGraphragS3Vectors(
  scope: Construct,
  grantee: iam.IGrantable,
  access: "build" | "read" | "delete",
): void {
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions:
        access === "build"
          ? READ_WRITE_ACTIONS
          : access === "read"
            ? READ_ACTIONS
            : DELETE_ACTIONS,
      resources: [
        graphragVectorBucketArn(scope),
        graphragVectorIndexArn(scope),
      ],
    }),
  );
}
