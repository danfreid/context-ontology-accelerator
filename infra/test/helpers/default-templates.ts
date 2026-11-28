// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
/**
 * Builds every stack the way bin/app.ts wires them (empty SSM config, no
 * credentials, so no EdgeWafStack and no AZ lookups) and serializes each
 * Template to JSON. Used to record and compare default-flag baselines.
 *
 * The importing test file must mock "../lib/utils/python-bundling" (see
 * default-baseline.test.ts), because bundlePython shells out to uv/docker.
 */
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template } from "aws-cdk-lib/assertions";
import { DEFAULT_ENV, DEFAULT_RESOURCE_PREFIX } from "../../lib/constants";
import { BrandEnvAspect } from "../../lib/utils/brand-env";
import { Paths } from "../../lib/paths";
import { IdpType } from "../../lib/types";
import {
  AuthnzStack,
  EniWarmStack,
  GuardrailStack,
  IdpAuthenticationStack,
  NetworkStack,
  StorageStack,
  WebStack,
} from "../../lib/stacks/foundation";
import {
  ApiStack,
  DataLayerStack,
  McpStack,
  MetricServiceStack,
  NamespaceStack,
  OntologyStack,
  ServeStack,
  SourcesStack,
  VkgStack,
} from "../../lib/stacks/services";

const TEST_ENV = { account: "123456789012", region: "us-east-1" };

export interface BuildOptions {
  /** CDK context merged over the baseline test context (backend flags etc.). */
  context?: Record<string, unknown>;
}

/** Stack id (without the coa-dev- prefix) mapped to its serialized template. */
export function buildAllTemplates(
  options: BuildOptions = {},
): Record<string, string> {
  const prefix = DEFAULT_RESOURCE_PREFIX;
  const envName = DEFAULT_ENV;
  const stackPrefix = `${prefix}-${envName}`;
  const app = new cdk.App({
    context: {
      "aws:cdk:bundling-stacks": [],
      context_manager_image_uri:
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-dev:latest",
      ecr_repository_arn:
        "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
      ecr_repository_name: "coa-test",
      ontology_engine_image_uri:
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:ontology-engine-test",
      ...options.context,
    },
  });
  const allowedOrigin = "*";
  const env = TEST_ENV;
  const authProps = IdpAuthenticationStack.fromSsmConfig({});

  const network = new NetworkStack(app, `${stackPrefix}-network`, { env });
  const eniWarm = new EniWarmStack(app, `${stackPrefix}-eni-warm`, {
    env,
    vpc: network.vpc,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    discoveryOcspSecurityGroup: network.discoveryOcspSecurityGroup,
    ecsSecurityGroup: network.ecsSecurityGroup,
    authorizerSecurityGroup: network.authorizerSecurityGroup,
    sparqlSourceEgressSecurityGroup: network.sparqlSourceEgressSecurityGroup,
  });
  const auth = new IdpAuthenticationStack(app, `${stackPrefix}-auth`, {
    ...authProps,
    env,
  });
  const guardrail = new GuardrailStack(app, `${stackPrefix}-guardrail`, {
    env,
  });
  const storage = new StorageStack(app, `${stackPrefix}-storage`, {
    env,
    network,
    allowedOrigin,
  });
  const authnz = new AuthnzStack(app, `${stackPrefix}-authnz`, {
    env,
    claimsMappings: auth.initialClaimsMappings,
  });
  const vkg = new VkgStack(app, `${stackPrefix}-vkg`, {
    env,
    network,
    serviceNamespace: network.serviceNamespace,
    ontologyBucket: storage.ontologyArtifactsBucket,
  });
  const namespace = new NamespaceStack(app, `${stackPrefix}-namespace`, {
    env,
    vpc: network.vpc,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    rolesTable: authnz.rolesTable,
    resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
    allowedOrigin,
    vkgClusterArn: vkg.cluster.clusterArn,
    cloudMapNamespaceId: network.serviceNamespace.namespaceId,
    ontologyBucketName: storage.ontologyArtifactsBucket.bucketName,
    ontologyEngineEndpoint: `http://ontology-engine.${prefix}-${envName}-services.local:8001`,
  });
  namespace.addDependency(storage);
  namespace.addDependency(authnz);
  const metricService = new MetricServiceStack(
    app,
    `${stackPrefix}-metric-service`,
    {
      env,
      vpc: network.vpc,
      lambdaSecurityGroup: network.lambdaSecurityGroup,
      aossSecurityGroup: network.aossSecurityGroup,
      neptuneEndpoint: storage.neptuneClusterEndpoint,
      neptuneClusterArn: storage.neptuneClusterArn,
      allowedOrigin,
    },
  );
  metricService.addDependency(namespace);
  metricService.addDependency(authnz);
  const ssm = (suffix: string) => `/${prefix}/${suffix}`;
  // ApiStack reads the Smithy-generated OpenAPI specs, which are not built in a plain
  // checkout; without them the api stack is skipped and web gets placeholder values.
  const hasApiSpecs =
    fs.existsSync(Paths.controlPlaneOpenApiSpec) &&
    fs.existsSync(Paths.dataLayerOpenApiSpec);
  const api = hasApiSpecs
    ? new ApiStack(app, `${stackPrefix}-api`, {
        env,
        allowedOrigin,
        vpc: network.vpc,
        lambdaSecurityGroup: network.lambdaSecurityGroup,
        authorizerSecurityGroup: network.authorizerSecurityGroup,
        rolesTable: authnz.rolesTable,
        resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
        cacheInvalidationTable: authnz.cacheInvalidationTable,
        ssmPathHandlers: {
          "/namespaces": ssm("namespace/api-fn-arn"),
          "/namespaces/{namespaceId}/sources": ssm("sources/api-fn-arn"),
          "/namespaces/{namespaceId}/metrics": ssm("metric/api-fn-arn"),
          "/namespaces/{namespaceId}/proposals": ssm(
            "ontology-engine/api-fn-arn",
          ),
          "/namespaces/{namespaceId}/query": ssm("data-layer/api-fn-arn"),
        },
      })
    : undefined;
  const serve = new ServeStack(app, `${stackPrefix}-serve`, {
    env,
    vpc: network.vpc,
    rolesTable: authnz.rolesTable,
    resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
    aossSecurityGroup: network.aossSecurityGroup,
    s3VectorsEndpointSecurityGroup: network.s3VectorsEndpointSecurityGroup,
    neptuneSecurityGroup: network.neptuneSecurityGroup,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    neptuneClusterArn: storage.neptuneClusterArn,
    neptuneEndpoint: storage.neptuneClusterEndpoint,
    ontologyBucketArn: storage.ontologyArtifactsBucket.bucketArn,
    athenaResultsBucketName: storage.athenaResultsBucket.bucketName,
    vkgEndpoint: `http://vkg.${prefix}-${envName}-services.local:${vkg.containerPort}`,
  });
  serve.addDependency(namespace);
  serve.addDependency(auth);
  const sources = new SourcesStack(app, `${stackPrefix}-sources`, {
    env,
    network,
    storage,
    allowedOrigin,
  });
  sources.addDependency(namespace);
  sources.addDependency(storage);
  sources.addDependency(guardrail);
  sources.addDependency(authnz);
  api?.addDependency(sources);
  api?.addDependency(metricService);
  api?.addDependency(auth);
  api?.addDependency(namespace);
  metricService.addDependency(sources);
  const dataLayer = new DataLayerStack(app, `${stackPrefix}-data-layer`, {
    env,
    vpc: network.vpc,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    allowedOrigin,
  });
  dataLayer.addDependency(serve);
  api?.addDependency(dataLayer);
  const web = new WebStack(app, `${stackPrefix}-web`, {
    env,
    isCognitoMode: true,
    federatedIdentityProviderNames:
      IdpAuthenticationStack.federatedProviderNames(authProps),
    apiEndpoint: api ? api.api.url : "https://api.example.com/prod/",
    apiRestApiId: api ? api.api.restApiId : "placeholder",
    apiStageName: api ? api.api.deploymentStage.stageName : "prod",
    serveRuntimeArn: serve.queryEndpoint,
  });
  web.addDependency(auth);
  if (api) web.addDependency(api);
  web.addDependency(serve);
  const ontology = new OntologyStack(app, `${stackPrefix}-ontology`, {
    env,
    network,
    serviceNamespace: network.serviceNamespace,
    neptuneClusterArn: storage.neptuneClusterArn,
    neptuneClusterEndpoint: storage.neptuneClusterEndpoint,
    ontologyArtifactsBucket: storage.ontologyArtifactsBucket,
    smusDomainId: namespace.domainId,
    allowedOrigin,
  });
  ontology.addDependency(namespace);
  ontology.addDependency(sources);
  ontology.addDependency(authnz);
  api?.addDependency(ontology);
  dataLayer.addDependency(ontology);
  const mcp = new McpStack(app, `${stackPrefix}-mcp`, {
    env,
    vpc: network.vpc,
    rolesTable: authnz.rolesTable,
    resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
  });
  mcp.addDependency(serve);
  mcp.addDependency(auth);
  web.addDependency(mcp);
  for (const stack of app.node.children) {
    if (!(stack instanceof cdk.Stack) || stack === eniWarm || stack === network)
      continue;
    if (
      stack.node
        .findAll()
        .some((c) => c instanceof lambda.Function && c.isBoundToVpc)
    ) {
      stack.addDependency(eniWarm);
    }
  }
  cdk.Aspects.of(app).add(new BrandEnvAspect());

  const out: Record<string, string> = {};
  for (const stack of app.node.children) {
    if (stack instanceof cdk.Stack) {
      out[stack.node.id.slice(stackPrefix.length + 1)] = JSON.stringify(
        Template.fromStack(stack).toJSON(),
        null,
        2,
      );
    }
  }
  return out;
}

/**
 * Masks values that change without a template change: seed-role timestamps (authnz), the
 * api-redeploy-<epoch ms> physical id (web) and the 64-hex content hashes of Docker image assets,
 * which change whenever a bundled source file does. Use it on both sides of a comparison.
 */
export function normalizeVolatile(json: string): string {
  return json
    .replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z/g, "<timestamp>")
    .replace(/api-redeploy-\d+/g, "api-redeploy-<epoch>")
    .replace(/[0-9a-f]{64}/g, "<asset-hash>");
}

/** Writes one <stack>.template.json per stack into dir. Returns the stack names. */
export function writeTemplates(
  dir: string,
  templates: Record<string, string>,
): string[] {
  fs.mkdirSync(dir, { recursive: true });
  for (const [name, json] of Object.entries(templates)) {
    fs.writeFileSync(path.join(dir, `${name}.template.json`), json + "\n");
  }
  return Object.keys(templates);
}

export { IdpType, Paths };
