// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Construct } from "constructs";
import { prefixed, resolveBackendFlags, resolveContext } from "../context";
import { isAuroraMetadataEnabled } from "../utils/aurora-search";
import { logicalTable } from "../utils/aurora-metadata";

/** Props for the DynamoDBTable construct. */
export interface DynamoDBTableProps {
  /**
   * Logical table name — will be prefixed automatically via `prefixed()`.
   * Example: `"namespaces"` → `"coa-dev-namespaces"`
   */
  tableName: string;

  /** Partition key definition. */
  partitionKey: dynamodb.Attribute;

  /** Optional sort key definition. */
  sortKey?: dynamodb.Attribute;

  /** Optional DynamoDB stream view type. Defaults to no stream. */
  stream?: dynamodb.StreamViewType;

  /** Optional TTL attribute name. Enables automatic item expiration. */
  timeToLiveAttribute?: string;

  /** Override removal policy. Defaults to RETAIN in prod, DESTROY elsewhere. */
  removalPolicy?: cdk.RemovalPolicy;
}

/**
 * Reusable DynamoDB table construct with secure defaults.
 *
 * Enforces:
 *   - PAY_PER_REQUEST billing
 *   - AWS-managed encryption at rest
 *   - Point-in-time recovery enabled
 *   - RETAIN removal policy in prod, DESTROY elsewhere (overridable)
 *
 * With the full Aurora flag set no table is created: `table` is then a stand-in that
 * carries the physical name, and the metadata lives in Aurora (see `logicalTable`).
 */
export class DynamoDBTable extends Construct {
  public readonly table: dynamodb.ITable;

  private readonly physicalTable?: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DynamoDBTableProps) {
    super(scope, id);

    if (isAuroraMetadataEnabled(resolveBackendFlags(this.node))) {
      this.table = logicalTable(
        this,
        "Table",
        prefixed(this.node, props.tableName),
      );
      return;
    }
    const { envName } = resolveContext(this.node);
    const defaultRemoval =
      envName === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;

    this.physicalTable = new dynamodb.Table(this, "Table", {
      tableName: prefixed(this.node, props.tableName),
      partitionKey: props.partitionKey,
      sortKey: props.sortKey,
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: props.removalPolicy ?? defaultRemoval,
      stream: props.stream,
      timeToLiveAttribute: props.timeToLiveAttribute,
    });
    this.table = this.physicalTable;
  }

  /** Delegate addGlobalSecondaryIndex to the underlying table (no-op on the Aurora path). */
  addGlobalSecondaryIndex(props: dynamodb.GlobalSecondaryIndexProps): void {
    this.physicalTable?.addGlobalSecondaryIndex(props);
  }
}
