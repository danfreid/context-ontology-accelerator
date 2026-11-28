// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { resolveBackendFlags } from "../lib/context";

function nodeWithContext(context: Record<string, string>): cdk.App {
  return new cdk.App({ context });
}

describe("resolveBackendFlags", () => {
  it("returns defaults when no context is set", () => {
    const app = nodeWithContext({});
    expect(resolveBackendFlags(app.node)).toEqual({
      graphBackend: "neptune",
      vectorBackend: "aoss",
      metadataBackend: "dynamodb",
      graphragVectorBackend: "aoss",
      computeBackend: "fargate",
    });
  });

  it("accepts the aurora path", () => {
    const app = nodeWithContext({
      graph_backend: "aurora",
      vector_backend: "pgvector",
      metadata_backend: "aurora",
      graphrag_vector_backend: "s3vectors",
      compute_backend: "lambda",
    });
    expect(resolveBackendFlags(app.node)).toEqual({
      graphBackend: "aurora",
      vectorBackend: "pgvector",
      metadataBackend: "aurora",
      graphragVectorBackend: "s3vectors",
      computeBackend: "lambda",
    });
  });

  it("throws at synth when graph_backend is unknown", () => {
    const app = nodeWithContext({ graph_backend: "mysql" });
    expect(() => resolveBackendFlags(app.node)).toThrow(
      /graph_backend.*must be.*neptune.*aurora/i,
    );
  });

  it("throws at synth when vector_backend is unknown", () => {
    const app = nodeWithContext({ vector_backend: "elasticsearch" });
    expect(() => resolveBackendFlags(app.node)).toThrow(
      /vector_backend.*must be.*aoss.*pgvector/i,
    );
  });

  it("throws at synth when metadata_backend is unknown", () => {
    const app = nodeWithContext({ metadata_backend: "redis" });
    expect(() => resolveBackendFlags(app.node)).toThrow(
      /metadata_backend.*must be.*dynamodb.*aurora/i,
    );
  });

  it("throws at synth when graphrag_vector_backend is unknown", () => {
    const app = nodeWithContext({ graphrag_vector_backend: "pinecone" });
    expect(() => resolveBackendFlags(app.node)).toThrow(
      /graphrag_vector_backend.*must be.*aoss.*s3vectors/i,
    );
  });
  it("throws at synth when compute_backend is unknown", () => {
    const app = nodeWithContext({ compute_backend: "ec2" });
    expect(() => resolveBackendFlags(app.node)).toThrow(
      /compute_backend.*must be.*fargate.*lambda/i,
    );
  });
});
