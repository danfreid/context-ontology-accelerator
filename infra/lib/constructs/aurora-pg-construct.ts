// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as crypto from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Provider } from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";

import { resolveContext } from "../context";
import { SCLConstruct } from "./scl-construct";

/** Database roles created by aurora-init.sql. Names are fixed because the SQL grants reference them. */
const SEARCH_RW_ROLE = "coa_search_rw";
const SEARCH_RO_ROLE = "coa_search_ro";

export interface AuroraPgProps {
  /** VPC that holds the cluster. Needs PRIVATE_ISOLATED subnets. */
  readonly vpc: ec2.IVpc;
  /** Security group attached to the cluster (ingress 5432 from workloads). */
  readonly securityGroup: ec2.ISecurityGroup;
  /**
   * Embedding dimension for the HNSW index on search_doc. The migrator creates the index as
   * the table owner so the least-privilege rw role never needs to. Must equal the
   * dimension the embedders use (AURORA_VECTOR_DIMENSIONS / OSS_DIMENSIONS). Default 1024.
   */
  readonly vectorDimensions?: number;
}

/**
 * Aurora Serverless v2 (PostgreSQL 18) cluster that replaces Neptune, OpenSearch
 * Serverless and the DynamoDB metadata tables on the cost-optimised path.
 *
 * - pgvector: vector similarity search (replaces the AOSS VECTORSEARCH collection).
 * - tsvector / ts_rank (built in): keyword search, merged with pgvector results
 *   by reciprocal rank fusion in SQL for hybrid search. ParadeDB pg_search is
 *   not in Aurora's supported-extension list, so it is not used here.
 * - pgrouting: shortest-path / Dijkstra traversal.
 * - Data API enabled, so Lambda reaches the cluster without a VPC-bound JDBC pool.
 *
 * Extensions, tables, roles and grants come from `stacks/foundation/migrations/aurora-init.sql`,
 * applied through the Data API by a Lambda-backed custom resource that re-runs when the file changes.
 * Search workloads connect as `coa_search_rw` or `coa_search_ro` (own generated secrets), not as admin.
 *
 * TODO: pgRouting handles shortest-path/Dijkstra queries; for 4+ hop traversals
 * on 500M+ nodes Neptune retains a performance advantage. Benchmark deep
 * traversal queries before committing to an aurora-only graph backend.
 */
export class AuroraPg extends SCLConstruct {
  /** Cluster writer endpoint. */
  public readonly clusterEndpoint: rds.Endpoint;

  /** Cluster ARN (RDS Data API `resourceArn`). */
  public readonly clusterArn: string;

  /** ARN of the generated admin credential secret. */
  public readonly secretArn: string;
  /** ARN of the secret for the read-write search role ("coa_search_rw"). */
  public readonly searchRwSecretArn: string;
  /** ARN of the secret for the read-only search role ("coa_search_ro"). */
  public readonly searchRoSecretArn: string;

  constructor(scope: Construct, id: string, props: AuroraPgProps) {
    super(scope, id);

    const { ssmPrefix, envName } = resolveContext(this.node);
    const isProd = envName === "prod";

    const parameterGroup = new rds.ParameterGroup(this, "ParameterGroup", {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_18_4,
      }),
      description: "SemanticContext Aurora PostgreSQL 18 parameters",
      parameters: {
        // Only libraries that need preloading. vector and pgrouting are plain
        // CREATE EXTENSION; an unknown name here stops the instance starting.
        shared_preload_libraries: "pg_stat_statements",
        // Serverless v2 at min ACU 0 starts with few slots; 200 is a safe ceiling.
        max_connections: "200",
      },
    });

    const cluster = new rds.DatabaseCluster(this, "Cluster", {
      clusterIdentifier: this.prefixed("aurora"),
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_18_4,
      }),
      parameterGroup,
      serverlessV2MinCapacity: 0,
      serverlessV2MaxCapacity: 8,
      writer: rds.ClusterInstance.serverlessV2("Writer"),
      enableDataApi: true,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [props.securityGroup],
      storageEncrypted: true,
      deletionProtection: isProd,
      removalPolicy: isProd
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      credentials: rds.Credentials.fromGeneratedSecret(
        this.prefixed("aurora-admin"),
        { secretName: this.prefixed("aurora-secret") },
      ),
    });

    this.clusterEndpoint = cluster.clusterEndpoint;
    this.clusterArn = cluster.clusterArn;
    this.secretArn = cluster.secret!.secretArn;

    const removalPolicy = isProd
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;
    const roleSecret = (id: string, suffix: string, username: string) =>
      new secretsmanager.Secret(this, id, {
        secretName: this.prefixed(`aurora-${suffix}`),
        description: `Aurora search role ${username}`,
        generateSecretString: {
          secretStringTemplate: JSON.stringify({ username }),
          generateStringKey: "password",
          passwordLength: 32,
          excludePunctuation: true,
        },
        removalPolicy,
      });
    const rwSecret = roleSecret("SearchRwSecret", "search-rw", SEARCH_RW_ROLE);
    const roSecret = roleSecret("SearchRoSecret", "search-ro", SEARCH_RO_ROLE);
    this.searchRwSecretArn = rwSecret.secretArn;
    this.searchRoSecretArn = roSecret.secretArn;

    this.addMigrator(cluster, props.vectorDimensions ?? 1024, [
      { name: SEARCH_RW_ROLE, secret: rwSecret },
      { name: SEARCH_RO_ROLE, secret: roSecret },
    ]);

    new ssm.StringParameter(this, "SsmAuroraClusterEndpoint", {
      parameterName: `${ssmPrefix}/aurora/cluster-endpoint`,
      stringValue: this.clusterEndpoint.hostname,
      description: "Aurora PostgreSQL cluster writer endpoint",
    });

    new ssm.StringParameter(this, "SsmAuroraClusterArn", {
      parameterName: `${ssmPrefix}/aurora/cluster-arn`,
      stringValue: this.clusterArn,
      description: "Aurora PostgreSQL cluster ARN (RDS Data API resourceArn)",
    });

    new ssm.StringParameter(this, "SsmAuroraSecretArn", {
      parameterName: `${ssmPrefix}/aurora/secret-arn`,
      stringValue: this.secretArn,
      description: "Aurora PostgreSQL admin credential secret ARN",
    });

    new ssm.StringParameter(this, "SsmAuroraSearchRwSecretArn", {
      parameterName: `${ssmPrefix}/aurora/search-rw-secret-arn`,
      stringValue: this.searchRwSecretArn,
      description: "Secret ARN for the read-write Aurora search role",
    });
    new ssm.StringParameter(this, "SsmAuroraSearchRoSecretArn", {
      parameterName: `${ssmPrefix}/aurora/search-ro-secret-arn`,
      stringValue: this.searchRoSecretArn,
      description: "Secret ARN for the read-only Aurora search role",
    });

    // Parallel to /opensearch/endpoint: tells app code which search path to use.
    new ssm.StringParameter(this, "SsmHybridSearchBackend", {
      parameterName: `${ssmPrefix}/opensearch/hybrid-search-backend`,
      stringValue: "pgvector+tsvector",
      description: "Hybrid search backend (pgvector + tsvector/ts_rank, RRF in SQL)",
    });

    new cdk.CfnOutput(this, "AuroraClusterEndpointOutput", {
      value: this.clusterEndpoint.hostname,
      description: "Aurora PostgreSQL cluster writer endpoint",
    });

    new cdk.CfnOutput(this, "AuroraClusterArnOutput", {
      value: this.clusterArn,
      description: "Aurora PostgreSQL cluster ARN",
    });

    new cdk.CfnOutput(this, "AuroraSecretArnOutput", {
      value: this.secretArn,
      description: "Aurora PostgreSQL admin credential secret ARN",
    });
  }

  /**
   * Applies aurora-init.sql through the Data API and sets the role passwords.
   * The Lambda has no VPC attachment: the Data API needs no network path to the
   * cluster. It holds the admin secret, so it is limited to this cluster and these
   * secrets. Re-runs when the SQL file hash changes.
   */
  private addMigrator(
    cluster: rds.DatabaseCluster,
    vectorDimensions: number,
    roles: { name: string; secret: secretsmanager.ISecret }[],
  ): void {
    const sqlPath = path.join(
      __dirname,
      "../stacks/foundation/migrations/aurora-init.sql",
    );
    const sql = fs.readFileSync(sqlPath);
    const sqlHash = crypto.createHash("sha256").update(sql).digest("hex");

    // Stage the handler and the SQL together so the Lambda asset carries both.
    const stage = fs.mkdtempSync(path.join(os.tmpdir(), "aurora-migrator-"));
    fs.copyFileSync(
      path.join(__dirname, "../lambdas/aurora-migrator/index.py"),
      path.join(stage, "index.py"),
    );
    fs.writeFileSync(path.join(stage, "aurora-init.sql"), sql);

    const onEvent = new lambda.Function(this, "MigratorFn", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.minutes(5),
      code: lambda.Code.fromAsset(stage),
      description: "Applies aurora-init.sql to the Aurora search database",
    });
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["rds-data:ExecuteStatement"],
        resources: [cluster.clusterArn],
      }),
    );
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          cluster.secret!.secretArn,
          ...roles.map((r) => r.secret.secretArn),
        ],
      }),
    );

    const provider = new Provider(this, "MigratorProvider", {
      onEventHandler: onEvent,
    });
    const resource = new cdk.CustomResource(this, "Migration", {
      serviceToken: provider.serviceToken,
      properties: {
        ClusterArn: cluster.clusterArn,
        AdminSecretArn: cluster.secret!.secretArn,
        Database: "postgres",
        SqlHash: sqlHash,
        VectorDimensions: vectorDimensions,
        Roles: roles.map((r) => ({
          Name: r.name,
          SecretArn: r.secret.secretArn,
        })),
      },
    });
    resource.node.addDependency(cluster);
  }
}
