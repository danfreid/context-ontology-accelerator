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
