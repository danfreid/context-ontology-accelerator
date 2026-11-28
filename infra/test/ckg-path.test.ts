// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Annotations, Match } from "aws-cdk-lib/assertions";
import { resolveBackendFlags } from "../lib/context";
import {
  ckgGraphragEnv,
  graphragVectorBucketName,
  isCkgPathEnabled,
  warnIfCkgPathIncomplete,
} from "../lib/utils/graphrag-s3vectors";
import {
  buildAllTemplates,
  normalizeVolatile,
} from "./helpers/default-templates";

jest.mock("../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

type Json = Record<string, unknown>;

const AURORA = {
  graph_backend: "aurora",
  vector_backend: "pgvector",
  metadata_backend: "aurora",
};
const CKG = { ...AURORA, graphrag_vector_backend: "s3vectors" };

function flagsFor(context: Record<string, unknown>) {
  return resolveBackendFlags(new cdk.App({ context }).node);
}

describe("isCkgPathEnabled", () => {
  test("is false by default, for s3vectors alone and for the Aurora set alone", () => {
    expect(isCkgPathEnabled(flagsFor({}))).toBe(false);
    expect(
      isCkgPathEnabled(flagsFor({ graphrag_vector_backend: "s3vectors" })),
    ).toBe(false);
    expect(isCkgPathEnabled(flagsFor(AURORA))).toBe(false);
  });

  test("is false when any Aurora flag is missing", () => {
    for (const missing of [
      { graph_backend: "neptune" },
      { vector_backend: "aoss" },
      { metadata_backend: "dynamodb" },
    ]) {
      expect(isCkgPathEnabled(flagsFor({ ...CKG, ...missing }))).toBe(false);
    }
  });

  test("is true for the Aurora set with s3vectors", () => {
    expect(isCkgPathEnabled(flagsFor(CKG))).toBe(true);
  });

  test("warns once for s3vectors without the Aurora set, never otherwise", () => {
    const warnings = (context: Record<string, unknown>) => {
      const app = new cdk.App({ context });
      const stack = new cdk.Stack(app, "S");
      warnIfCkgPathIncomplete(stack, resolveBackendFlags(app.node));
      return Annotations.fromStack(stack).findWarning("*", Match.anyValue());
    };
    expect(
      warnings({
        graphrag_vector_backend: "s3vectors",
        graph_backend: "aurora",
      }),
    ).toHaveLength(1);
    expect(warnings({})).toHaveLength(0);
    expect(warnings(AURORA)).toHaveLength(0);
    expect(warnings(CKG)).toHaveLength(0);
  });

  test("env is empty off the ckg path and names the bucket on it", () => {
    const stack = new cdk.Stack(new cdk.App(), "S", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    expect(ckgGraphragEnv(stack, flagsFor(AURORA))).toEqual({});
    expect(ckgGraphragEnv(stack, flagsFor(CKG))).toEqual({
      GRAPHRAG_VECTOR_BACKEND: "s3vectors",
      GRAPHRAG_S3_VECTOR_BUCKET: graphragVectorBucketName(stack),
      GRAPHRAG_S3_VECTOR_PREFIX: "graphrag",
    });
  });
});

describe("default flags are unchanged by the ckg path", () => {
  const fixture = JSON.parse(
    fs.readFileSync(
      path.join(__dirname, "__fixtures__", "default-template-hashes.json"),
      "utf-8",
    ),
  ) as Record<string, string>;
  const hash = (json: string) =>
    crypto
      .createHash("sha256")
      .update(normalizeVolatile(json.trim()))
      .digest("hex");
  const defaults = buildAllTemplates();
  // graphrag_vector_backend=s3vectors without the Aurora set must not change anything.
  const partial = buildAllTemplates({
    context: { graphrag_vector_backend: "s3vectors" },
  });

  // The fixture holds the sha256 of every stack's template (volatile values masked)
  // synthesized from the base commit 8e55283 with default flags, before FEAT-005.
  test("the fixture covers the stacks", () => {
    expect(Object.keys(fixture).length).toBeGreaterThan(10);
  });

  test.each(Object.keys(fixture))("%s matches the base commit", (name) => {
    expect(defaults[name]).toBeDefined();
    expect(hash(defaults[name])).toEqual(fixture[name]);
  });

  test.each(Object.keys(fixture))(
    "%s ignores graphrag_vector_backend without the Aurora set",
    (name) => {
      expect(hash(partial[name])).toEqual(fixture[name]);
    },
  );
});

function resources(json: string): Array<Json & { Type: string }> {
  return Object.values(
    (JSON.parse(json) as { Resources?: Record<string, Json> }).Resources ?? {},
  ) as Array<Json & { Type: string }>;
}

describe("ckg path: S3 Vectors instead of AOSS", () => {
  const ckg = buildAllTemplates({ context: CKG });
  const aurora = buildAllTemplates({ context: AURORA });
  const names = Object.keys(ckg);
  const props = (r: Json): Json => (r.Properties ?? {}) as Json;

  test("no stack has an OpenSearch Serverless resource, the proxy Lambda or an aoss: action", () => {
    for (const name of names) {
      const types = resources(ckg[name]).map((r) => r.Type);
      expect({
        name,
        aoss: types.filter((t) => t.startsWith("AWS::OpenSearchServerless")),
      }).toEqual({ name, aoss: [] });
      expect({ name, proxy: ckg[name].includes("aoss-search-proxy") }).toEqual({
        name,
        proxy: false,
      });
      expect({ name, aossAction: ckg[name].includes("aoss:") }).toEqual({
        name,
        aossAction: false,
      });
      expect({
        name,
        proxyArn: ckg[name].includes("OPENSEARCH_PROXY_LAMBDA_ARN"),
      }).toEqual({ name, proxyArn: false });
    }
    // The guard is not vacuous: the Aurora path without s3vectors has all of them.
    const auroraAll = Object.values(aurora).join("\n");
    expect(auroraAll).toContain("AWS::OpenSearchServerless::Collection");
    expect(auroraAll).toContain("aoss-search-proxy");
    expect(auroraAll).toContain("aoss:APIAccessAll");
  });

  test("no stack reads the OpenSearch SSM parameters or grants Neptune", () => {
    for (const name of names) {
      expect({ name, ssm: ckg[name].includes("/opensearch/endpoint") }).toEqual(
        { name, ssm: false },
      );
      expect({ name, neptune: ckg[name].includes("neptune-db:") }).toEqual({
        name,
        neptune: false,
      });
    }
  });

  test("storage creates the vector bucket and no AOSS endpoint exists in network", () => {
    const buckets = resources(ckg["storage"]).filter(
      (r) => r.Type === "AWS::S3Vectors::VectorBucket",
    );
    expect(buckets).toHaveLength(1);
    expect(props(buckets[0]).VectorBucketName).toMatch(/graphrag-vec/);
    expect(ckg["network"]).not.toContain("aoss-data");
    expect(aurora["network"]).toContain("aoss-data");
  });

  test("network has a private-DNS s3vectors endpoint scoped to the bucket, with SG ingress", () => {
    const endpoints = resources(ckg["network"]).filter(
      (r) =>
        r.Type === "AWS::EC2::VPCEndpoint" &&
        JSON.stringify(props(r).ServiceName).includes("s3vectors"),
    );
    expect(endpoints).toHaveLength(1);
    const endpoint = props(endpoints[0]);
    expect(endpoint.VpcEndpointType).toBe("Interface");
    expect(endpoint.PrivateDnsEnabled).toBe(true);
    const policy = JSON.stringify(endpoint.PolicyDocument);
    expect(policy).toContain("graphrag-vec");
    expect(policy).toContain("/index/graphrag.*");
    expect(policy).not.toContain('"Resource":"*"');

    const sg = Object.entries(
      (JSON.parse(ckg["network"]) as { Resources: Record<string, Json> })
        .Resources,
    ).find(
      ([, r]) =>
        r.Type === "AWS::EC2::SecurityGroup" &&
        JSON.stringify(r).includes("s3vectors-vpce-sg"),
    );
    expect(sg).toBeDefined();
    const ingress = resources(ckg["network"]).filter(
      (r) =>
        r.Type === "AWS::EC2::SecurityGroupIngress" &&
        JSON.stringify(props(r).GroupId).includes(sg![0]),
    );
    // ECS tasks (KG build, graph cleanup) and Lambda functions (namespace deletion).
    expect(ingress).toHaveLength(2);
    for (const rule of ingress) {
      expect(props(rule).FromPort).toBe(443);
      expect(props(rule).SourceSecurityGroupId).toBeDefined();
    }
    expect(aurora["network"]).not.toContain("s3vectors");
  });

  test("serve lets the AgentCore SG reach the s3vectors endpoint", () => {
    const ingress = resources(ckg["serve"]).filter(
      (r) =>
        r.Type === "AWS::EC2::SecurityGroupIngress" &&
        JSON.stringify(props(r).Description).includes("S3 Vectors"),
    );
    expect(ingress).toHaveLength(1);
    expect(props(ingress[0]).FromPort).toBe(443);
    expect(ckg["serve"]).not.toContain("AossVpceIngress");
  });

  /** Statements that name s3vectors actions, from every policy in a template. */
  function s3vectorsStatements(json: string): Json[] {
    const found: Json[] = [];
    const walk = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(walk);
      else if (node !== null && typeof node === "object") {
        const o = node as Json;
        const actions = ([] as unknown[]).concat(o.Action ?? []);
        if (actions.some((a) => String(a).startsWith("s3vectors:"))) {
          found.push(o);
        }
        Object.values(o).forEach(walk);
      }
    };
    walk(JSON.parse(json));
    return found;
  }
  const actionsOf = (s: Json): string[] =>
    ([] as string[]).concat((s.Action ?? []) as string[]);

  test("KG build, serve and namespace deletion get S3 Vectors access scoped to the bucket", () => {
    for (const name of ["sources", "serve", "namespace"]) {
      const statements = s3vectorsStatements(ckg[name]);
      expect({ name, count: statements.length > 0 }).toEqual({
        name,
        count: true,
      });
      for (const s of statements) {
        const resource = JSON.stringify(s.Resource);
        expect(resource).toContain("graphrag-vec");
        expect(resource).not.toContain('"*"');
        expect(actionsOf(s).some((a) => a === "s3vectors:*")).toBe(false);
      }
    }
    const actions = (name: string) =>
      s3vectorsStatements(ckg[name]).flatMap(actionsOf);
    for (const name of ["sources", "serve"]) {
      for (const a of [
        "CreateIndex",
        "PutVectors",
        "QueryVectors",
        "GetVectors",
        "DeleteVectors",
        "GetIndex",
        "ListIndexes",
      ]) {
        expect(actions(name)).toContain(`s3vectors:${a}`);
      }
    }
    expect(actions("namespace")).toEqual(
      expect.arrayContaining(["s3vectors:DeleteIndex", "s3vectors:GetIndex"]),
    );
    // Namespace deletion only deletes indexes; it cannot write vectors.
    expect(actions("namespace")).not.toContain("s3vectors:PutVectors");
    // Aurora Data API stays granted for the deletion Lambda.
    expect(ckg["namespace"]).toContain("rds-data:ExecuteStatement");
    // The default and plain Aurora paths grant no S3 Vectors action.
    for (const j of Object.values(aurora)) {
      expect(j).not.toContain("s3vectors:");
    }
  });

  test("workloads get the S3 Vectors and Aurora env, and no AOSS batch override", () => {
    for (const name of ["sources", "serve", "namespace", "ontology"]) {
      expect(ckg[name]).toContain("GRAPHRAG_VECTOR_BACKEND");
      expect(ckg[name]).toContain("s3vectors");
      expect(ckg[name]).toContain("GRAPHRAG_S3_VECTOR_BUCKET");
      expect(ckg[name]).toContain("GRAPH_BACKEND");
    }
    expect(ckg["sources"]).not.toContain("BUILD_BATCH_WRITE_SIZE");
    expect(aurora["sources"]).toContain("BUILD_BATCH_WRITE_SIZE");
    expect(aurora["serve"]).not.toContain("GRAPHRAG_VECTOR_BACKEND");
  });
});
