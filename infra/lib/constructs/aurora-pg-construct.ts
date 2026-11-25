// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as rds from "aws-cdk-lib/aws-rds";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

import { resolveContext } from "../context";
import { SCLConstruct } from "./scl-construct";

export interface AuroraPgProps {
  /** VPC that holds the cluster. Needs PRIVATE_ISOLATED subnets. */
  readonly vpc: ec2.IVpc;
  /** Security group attached to the cluster (ingress 5432 from workloads). */
  readonly securityGroup: ec2.ISecurityGroup;
}

/**
 * Aurora Serverless v2 (PostgreSQL 16) cluster that replaces Neptune, OpenSearch
 * Serverless and the DynamoDB metadata tables on the cost-optimised path.
 *
 * - pgvector: vector similarity search (replaces the AOSS VECTORSEARCH collection).
 * - tsvector / ts_rank (built in): keyword search, merged with pgvector results
 *   by reciprocal rank fusion in SQL for hybrid search. ParadeDB pg_search is
 *   not in Aurora's supported-extension list, so it is not used here.
 * - pgrouting: shortest-path / Dijkstra traversal.
 * - Data API enabled, so Lambda reaches the cluster without a VPC-bound JDBC pool.
 *
 * Extensions are created by `stacks/foundation/migrations/aurora-init.sql`.
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

  constructor(scope: Construct, id: string, props: AuroraPgProps) {
    super(scope, id);

    const { ssmPrefix, envName } = resolveContext(this.node);
    const isProd = envName === "prod";

    const parameterGroup = new rds.ParameterGroup(this, "ParameterGroup", {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_16_6,
      }),
      description: "SemanticContext Aurora PostgreSQL 16 parameters",
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
        version: rds.AuroraPostgresEngineVersion.VER_16_6,
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
}
