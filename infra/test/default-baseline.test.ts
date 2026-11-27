// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as lambda from "aws-cdk-lib/aws-lambda";
import { buildAllTemplates, writeTemplates } from "./helpers/default-templates";
jest.mock("../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));
/**
 * Records default-flag templates when COA_BASELINE_DIR is set; otherwise only
 * checks that every stack synthesizes with default context.
 */
describe("default-flag templates", () => {
  test("every stack synthesizes and is written to the baseline dir", () => {
    const templates = buildAllTemplates();
    expect(Object.keys(templates).length).toBeGreaterThan(10);
    const dir = process.env.COA_BASELINE_DIR;
    if (dir) writeTemplates(dir, templates);
  });
});
