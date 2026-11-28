// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";
import * as lambda from "aws-cdk-lib/aws-lambda";
import {
  buildAllTemplates,
  normalizeVolatile,
} from "./helpers/default-templates";

jest.mock("../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

/**
 * Directory with the default-flag templates recorded before the Aurora port
 * (`COA_BASELINE_DIR`, written by default-baseline.test.ts). The baseline lives
 * outside the repository, so the comparison is skipped when it is absent.
 */
const BASELINE_DIR =
  process.env.COA_BASELINE_DIR ??
  path.resolve(
    __dirname,
    "../../../../.agents/tasks/ckg-integration/task-ckg-aurora-port/baseline",
  );
const hasBaseline = fs.existsSync(BASELINE_DIR);

(hasBaseline ? describe : describe.skip)(
  "default-flag templates match the recorded baseline",
  () => {
    const current = buildAllTemplates();
    const names = fs
      .readdirSync(BASELINE_DIR)
      .filter((f) => f.endsWith(".template.json"))
      .map((f) => f.replace(/\.template\.json$/, ""));

    test("the baseline covers the stacks", () => {
      expect(names.length).toBeGreaterThan(10);
    });

    test.each(names)("%s is unchanged", (name) => {
      const recorded = fs.readFileSync(
        path.join(BASELINE_DIR, `${name}.template.json`),
        "utf-8",
      );
      expect(current[name]).toBeDefined();
      expect(normalizeVolatile(current[name].trim())).toEqual(
        normalizeVolatile(recorded.trim()),
      );
    });
  },
);
