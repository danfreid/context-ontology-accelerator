// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../lib/stacks/foundation/network-stack";
import { StorageStack } from "../lib/stacks/foundation/storage-stack";
import { MetricServiceStack } from "../lib/stacks/services/metric-service-stack";
import { OntologyStack } from "../lib/stacks/services/ontology-stack";
import { ServeStack } from "../lib/stacks/services/serve-stack";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../lib/constants";
import { buildAllTemplates } from "./helpers/default-templates";

jest.mock("../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };
const DIMENSIONS = 768;

/** Builds the stacks the way bin/app.ts wires them, with the full Aurora flag set. */
function buildFullAuroraPath() {
  const app = new cdk.App({
    context: {
      graph_backend: "aurora",
      vector_backend: "pgvector",
      metadata_backend: "aurora",
      resource_prefix: DEFAULT_RESOURCE_PREFIX,
      env: DEFAULT_ENV,
      context_manager_image_uri:
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-dev:latest",
      ecr_repository_arn:
        "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
      ecr_repository_name: "coa-test",
      ontology_engine_image_uri:
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:ontology-engine-test",
      "aws:cdk:bundling-stacks": [],
    },
  });
  const network = new NetworkStack(app, "Network", { env: TEST_ENV });
  const storage = new StorageStack(app, "Storage", {
    network,
    env: TEST_ENV,
    bedrockEmbedDimensions: DIMENSIONS,
  });
  const dep = new cdk.Stack(app, "Dep", { env: TEST_ENV });
  const table = (id: string) =>
    new dynamodb.Table(dep, id, {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
  const metric = new MetricServiceStack(app, "Metric", {
    vpc: network.vpc,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    aossSecurityGroup: network.aossSecurityGroup,
    neptuneEndpoint: storage.neptuneClusterEndpoint,
    neptuneClusterArn: storage.neptuneClusterArn,
    bedrockEmbedDimensions: DIMENSIONS,
    env: TEST_ENV,
  });
  const serve = new ServeStack(app, "Serve", {
    env: TEST_ENV,
    vpc: network.vpc,
    aossSecurityGroup: network.aossSecurityGroup,
    neptuneSecurityGroup: network.neptuneSecurityGroup,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    neptuneClusterArn: storage.neptuneClusterArn,
    neptuneEndpoint: storage.neptuneClusterEndpoint,
    ontologyBucketArn: storage.ontologyArtifactsBucket.bucketArn,
    vkgEndpoint: "http://vkg.coa-dev-services.local:8080",
    rolesTable: table("Roles"),
    resourceRoleMappingsTable: table("RRM"),
    bedrockEmbedDimensions: DIMENSIONS,
  });
  const ontology = new OntologyStack(app, "Ontology", {
    network,
    serviceNamespace: network.serviceNamespace,
    neptuneClusterArn: storage.neptuneClusterArn,
    neptuneClusterEndpoint: storage.neptuneClusterEndpoint,
    ontologyArtifactsBucket: storage.ontologyArtifactsBucket,
    smusDomainId: "dzd-test123",
    allowedOrigin: "https://test.example.com",
    bedrockEmbedDimensions: DIMENSIONS,
    env: TEST_ENV,
  });
  const json = (s: cdk.Stack) => JSON.stringify(Template.fromStack(s).toJSON());
  return {
    network: Template.fromStack(network),
    storage: Template.fromStack(storage),
    metric: json(metric),
    serve: json(serve),
    ontology: json(ontology),
  };
}

describe("Full Aurora path with a non-default embedding dimension", () => {
  const built = buildFullAuroraPath();
  const count = (s: string, needle: string) => s.split(needle).length - 1;

  test("the migrator builds the index at the configured dimension", () => {
    built.storage.hasResourceProperties("AWS::CloudFormation::CustomResource", {
      VectorDimensions: DIMENSIONS,
    });
  });

  test("ontology, serve and both metric Lambdas get the same dimension", () => {
    const aurora = `"AURORA_VECTOR_DIMENSIONS":"${DIMENSIONS}"`;
    const oss = `"OSS_DIMENSIONS":"${DIMENSIONS}"`;
    // ECS container environment is a list of {Name, Value} pairs.
    const ecs = (name: string) => `"Name":"${name}","Value":"${DIMENSIONS}"`;
    expect(
      count(built.ontology, ecs("AURORA_VECTOR_DIMENSIONS")),
    ).toBeGreaterThanOrEqual(1);
    expect(count(built.ontology, ecs("OSS_DIMENSIONS"))).toBeGreaterThanOrEqual(
      1,
    );
    expect(count(built.serve, aurora)).toBeGreaterThanOrEqual(1);
    expect(count(built.serve, oss)).toBeGreaterThanOrEqual(1);
    expect(count(built.metric, aurora)).toBe(2);
    expect(count(built.metric, oss)).toBe(2);
  });

  test("no workload sees the 1024 default", () => {
    for (const s of [built.ontology, built.serve, built.metric]) {
      expect(s).not.toContain(`"AURORA_VECTOR_DIMENSIONS":"1024"`);
      expect(s).not.toContain(`"OSS_DIMENSIONS":"1024"`);
      expect(s).not.toMatch(
        /"Name":"(AURORA_VECTOR|OSS)_DIMENSIONS","Value":"1024"/,
      );
    }
  });

  test("network creates the aoss-data endpoint that the storage network policy allows", () => {
    const services = Object.values(
      built.network.findResources("AWS::EC2::VPCEndpoint"),
    ).map((e) => JSON.stringify(e.Properties.ServiceName));
    expect(services.filter((s) => /aoss-data/.test(s))).toHaveLength(1);
    built.storage.hasResourceProperties(
      "AWS::OpenSearchServerless::SecurityPolicy",
      { Type: "network" },
    );
  });

  test("no ontology policy references the admin secret", () => {
    expect(built.ontology).not.toContain("/aurora/secret-arn");
  });
});

type Json = Record<string, unknown>;

/** Walks a parsed template and calls `visit` on every object. */
function walk(node: unknown, visit: (o: Json) => void): void {
  if (Array.isArray(node)) {
    node.forEach((n) => walk(n, visit));
  } else if (node !== null && typeof node === "object") {
    visit(node as Json);
    Object.values(node as Json).forEach((n) => walk(n, visit));
  }
}

const NEPTUNE_ENDPOINT_NAMES = /^(NEPTUNE_ENDPOINT|NDB_ENDPOINT)$/;
/**
 * `NEPTUNE_ENDPOINT` / `NDB_ENDPOINT` values in a serialized template: Lambda and runtime
 * maps, ECS `{Name, Value}` pairs, and the `{"name":..,"value":..}` overrides inside a
 * Step Functions definition string.
 */
function neptuneEndpointValues(templateJson: string): string[] {
  const values: string[] = [];
  walk(JSON.parse(templateJson), (o) => {
    for (const [k, v] of Object.entries(o)) {
      if (NEPTUNE_ENDPOINT_NAMES.test(k) && typeof v !== "object") {
        values.push(JSON.stringify(v));
      }
    }
    if (typeof o.Name === "string" && NEPTUNE_ENDPOINT_NAMES.test(o.Name)) {
      values.push(JSON.stringify(o.Value));
    }
  });
  const sfn =
    /(?:NEPTUNE_ENDPOINT|NDB_ENDPOINT)\\*"\s*,\s*\\*"value\\*"\s*:\s*\\*"([^"\\]*)/g;
  for (const m of templateJson.matchAll(sfn)) {
    values.push(JSON.stringify(m[1]));
  }
  return values;
}

const AURORA_FLAGS = {
  graph_backend: "aurora",
  vector_backend: "pgvector",
  metadata_backend: "aurora",
};

describe("Full Aurora path: metadata and graph in Aurora", () => {
  const aurora = buildAllTemplates({ context: AURORA_FLAGS });
  const defaults = buildAllTemplates();
  const resources = (json: string): Json[] =>
    Object.values(
      (JSON.parse(json) as { Resources?: Record<string, Json> }).Resources ??
        {},
    );
  const ofType = (json: string, type: string): Json[] =>
    resources(json).filter((r) => r.Type === type);

  test("no stack creates a DynamoDB table", () => {
    for (const [name, json] of Object.entries(aurora)) {
      expect({
        name,
        tables: ofType(json, "AWS::DynamoDB::Table").length,
      }).toEqual({ name, tables: 0 });
    }
    // The guard is not vacuous: default flags create tables.
    expect(
      Object.values(defaults).some(
        (j) => ofType(j, "AWS::DynamoDB::Table").length > 0,
      ),
    ).toBe(true);
  });

  test("no DynamoDB stream event source and no neptune-db or dynamodb IAM action", () => {
    for (const [name, json] of Object.entries(aurora)) {
      expect({
        name,
        mappings: ofType(json, "AWS::Lambda::EventSourceMapping").filter((m) =>
          /dynamodb/i.test(JSON.stringify(m)),
        ).length,
        neptune: json.includes("neptune-db:"),
        dynamodb: json.includes("dynamodb:"),
      }).toEqual({ name, mappings: 0, neptune: false, dynamodb: false });
    }
    expect(Object.values(defaults).some((j) => j.includes("neptune-db:"))).toBe(
      true,
    );
  });

  test("no non-empty NEPTUNE_ENDPOINT or NDB_ENDPOINT", () => {
    const defaultValues = Object.values(defaults).flatMap(
      neptuneEndpointValues,
    );
    expect(defaultValues.some((v) => v !== '""')).toBe(true);
    const found = Object.entries(aurora).flatMap(([name, json]) =>
      neptuneEndpointValues(json).map((value) => ({ name, value })),
    );
    expect(found.length).toBeGreaterThan(0);
    expect(found.filter((f) => f.value !== '""')).toEqual([]);
  });

  test("every Lambda, task and runtime that names a metadata table has the Aurora env", () => {
    const missing: string[] = [];
    const check = (where: string, env: Record<string, unknown>): void => {
      if (!Object.keys(env).some((k) => /TABLE/.test(k))) return;
      for (const key of [
        "METADATA_BACKEND",
        "GRAPH_BACKEND",
        "AURORA_SECRET_ARN",
        "AURORA_SSM_PREFIX",
      ]) {
        if (!(key in env)) missing.push(`${where}: ${key}`);
      }
      if (env.METADATA_BACKEND !== "aurora") missing.push(`${where}: value`);
    };
    for (const [name, json] of Object.entries(aurora)) {
      for (const r of resources(json)) {
        const props = (r.Properties ?? {}) as Json;
        if (r.Type === "AWS::Lambda::Function") {
          const vars = (props.Environment as Json | undefined)?.Variables;
          if (vars) check(`${name} lambda`, vars as Record<string, unknown>);
        } else if (r.Type === "AWS::BedrockAgentCore::Runtime") {
          const vars = props.EnvironmentVariables;
          if (vars) check(`${name} runtime`, vars as Record<string, unknown>);
        } else if (r.Type === "AWS::ECS::TaskDefinition") {
          for (const c of (props.ContainerDefinitions ?? []) as Json[]) {
            const env: Record<string, unknown> = {};
            for (const e of (c.Environment ?? []) as Json[]) {
              env[String(e.Name)] = e.Value;
            }
            check(`${name} task`, env);
          }
        }
      }
    }
    expect(missing).toEqual([]);
  });

  test("workloads that use Aurora can call the Data API as the engine role", () => {
    for (const name of ["serve", "sources", "namespace", "ontology", "mcp"]) {
      expect(aurora[name]).toContain("rds-data:ExecuteStatement");
      expect(aurora[name]).toContain("engine-rw-secret-arn");
    }
    expect(aurora["metric-service"]).toContain("rds-data:ExecuteStatement");
  });

  test("the metadata seeds go through the Data API instead of DynamoDB putItem", () => {
    const authnz = aurora["authnz"];
    expect(authnz).toContain("coa_metadata");
    expect(authnz).toContain("executeStatement");
    expect(authnz).not.toContain("putItem");
    // Without a database the Data API connects to one named after the user, which
    // does not exist (seen on the first real deploy).
    expect(authnz).toMatch(/\\"database\\":\\"postgres\\"/);
    expect(aurora["authnz"].match(/seed-role-/g)?.length ?? 0).toBeGreaterThan(
      0,
    );
  });

  test("Step Functions status updates go through the Data API, not DynamoDB", () => {
    expect(aurora["sources"]).toContain("aws-sdk:rdsdata:executeStatement");
    expect(aurora["sources"]).not.toContain("states:::dynamodb");
    expect(defaults["sources"]).toContain("states:::dynamodb:updateItem");
  });
});
