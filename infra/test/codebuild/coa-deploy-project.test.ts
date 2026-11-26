// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { execFileSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { CfnInclude } from "aws-cdk-lib/cloudformation-include";
import { Match, Template } from "aws-cdk-lib/assertions";

/**
 * The optional CodeBuild deploy project (infra/codebuild/coa-deploy-project.yaml) is a
 * standalone CloudFormation template. These tests pin what makes it safe to offer: an
 * arm64 host with Docker, no administrator rights, and no connection to the normal deploy.
 */
const REPO_ROOT = path.join(__dirname, "..", "..", "..");
const TEMPLATE_PATH = path.join(
  REPO_ROOT,
  "infra",
  "codebuild",
  "coa-deploy-project.yaml",
);

const read = (...p: string[]): string =>
  fs.readFileSync(path.join(REPO_ROOT, ...p), "utf-8");

function loadTemplate(): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, "Test", {
    env: { account: "123456789012", region: "eu-west-1" },
  });
  new CfnInclude(stack, "Included", { templateFile: TEMPLATE_PATH });
  return Template.fromStack(stack);
}

interface Statement {
  Sid: string;
  Effect: string;
  Action: string | string[];
  Resource: unknown;
  Condition?: unknown;
}

function policyStatements(template: Template): Statement[] {
  const roles = Object.values(template.findResources("AWS::IAM::Role"));
  expect(roles).toHaveLength(1);
  const policies = roles[0].Properties.Policies;
  expect(policies).toHaveLength(1);
  return policies[0].PolicyDocument.Statement as Statement[];
}

const actionsOf = (s: Statement): string[] =>
  Array.isArray(s.Action) ? s.Action : [s.Action];

/** The only actions the build role may be granted. Each one is explained in deploying.md. */
const DOCUMENTED_ACTIONS = [
  "sts:AssumeRole",
  "cloudformation:DescribeStacks",
  "cloudformation:ListStackResources",
  "ssm:GetParameter",
  "ec2:DescribeAddresses",
  "ec2:DescribeAvailabilityZones",
  "ec2:DescribeInternetGateways",
  "ec2:DescribeSecurityGroups",
  "ec2:DescribeVpcEndpoints",
  "ec2:DescribeVpcEndpointServices",
  "ec2:DescribeVpcs",
  "servicequotas:GetServiceQuota",
  "lambda:GetAccountSettings",
  "bedrock:ListFoundationModels",
  "bedrock:ListInferenceProfiles",
  "iam:GetRole",
  "ecr-public:GetAuthorizationToken",
  "sts:GetServiceBearerToken",
  "logs:CreateLogStream",
  "logs:PutLogEvents",
];

describe("CodeBuild project", () => {
  const template = loadTemplate();

  it("builds on a native arm64 host with Docker (privileged mode)", () => {
    template.hasResourceProperties("AWS::CodeBuild::Project", {
      Environment: {
        Type: "ARM_CONTAINER",
        Image: Match.stringLikeRegexp("amazonlinux-aarch64-standard"),
        PrivilegedMode: true,
      },
    });
  });

  it("defaults to a large compute size and a 120-minute timeout, both configurable", () => {
    template.hasResourceProperties("AWS::CodeBuild::Project", {
      TimeoutInMinutes: { Ref: "TimeoutInMinutes" },
      Environment: { ComputeType: { Ref: "ComputeType" } },
    });
    const params = template.toJSON().Parameters;
    expect(params.TimeoutInMinutes.Default).toBe(120);
    expect(params.TimeoutInMinutes.MaxValue).toBe(480);
    expect(params.ComputeType.Default).toBe("BUILD_GENERAL1_LARGE");
  });

  it("logs to a CloudWatch log group with a retention period", () => {
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      RetentionInDays: { Ref: "LogRetentionInDays" },
    });
    expect(template.toJSON().Parameters.LogRetentionInDays.Default).toBe(30);
    template.hasResourceProperties("AWS::CodeBuild::Project", {
      LogsConfig: { CloudWatchLogs: { Status: "ENABLED" } },
    });
  });

  it("takes the repository from a parameter with no default and no hard-coded fork", () => {
    const params = template.toJSON().Parameters;
    expect(params.RepoUrl.Default).toBeUndefined();
    expect(params.RepoUrl.AllowedPattern).toBe("^https://[A-Za-z0-9._~/:-]+$");
    // No credentials can be part of the URL: '@' is not an allowed character.
    expect(
      new RegExp(params.RepoUrl.AllowedPattern).test("https://u:t@h/r"),
    ).toBe(false);
    expect(read("infra", "codebuild", "coa-deploy-project.yaml")).not.toMatch(
      /github\.com\/|danfreid/,
    );
    template.hasResourceProperties("AWS::CodeBuild::Project", {
      Source: { Type: "NO_SOURCE" },
    });
  });

  it("passes only plain settings as environment variables, no secrets", () => {
    const project = Object.values(
      template.findResources("AWS::CodeBuild::Project"),
    )[0];
    const vars = project.Properties.Environment.EnvironmentVariables;
    expect(vars.map((v: { Name: string }) => v.Name).sort()).toEqual([
      "COA_DEPLOY_ENV",
      "COA_REPO_REF",
      "COA_REPO_URL",
      "SCL_PREFIX",
    ]);
    for (const v of vars) {
      expect(v.Type).toBe("PLAINTEXT");
    }
  });

  it("runs make setup and then the platform deploy", () => {
    const project = Object.values(
      template.findResources("AWS::CodeBuild::Project"),
    )[0];
    const spec: string = project.Properties.Source.BuildSpec;
    expect(spec).toContain("scripts/codebuild/install-tools.sh");
    expect(spec.indexOf("make setup")).toBeGreaterThan(-1);
    expect(
      spec.indexOf('./scripts/deploy.sh "$COA_DEPLOY_ENV"'),
    ).toBeGreaterThan(spec.indexOf("make setup"));
    expect(spec).not.toContain("deploy-all.sh");
  });
});

describe("CodeBuild build role", () => {
  const template = loadTemplate();
  const statements = policyStatements(template);

  it("has no managed policies, so no AdministratorAccess", () => {
    const role = Object.values(template.findResources("AWS::IAM::Role"))[0];
    expect(role.Properties.ManagedPolicyArns).toBeUndefined();
    expect(JSON.stringify(template.toJSON())).not.toMatch(
      /AdministratorAccess|PowerUserAccess/,
    );
  });

  it("is trusted only by CodeBuild in this account", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: "codebuild.amazonaws.com" },
            Condition: {
              StringEquals: { "aws:SourceAccount": { Ref: "AWS::AccountId" } },
            },
          }),
        ],
      },
    });
  });

  it("allows only the documented actions, with no wildcard action", () => {
    const granted = statements.flatMap(actionsOf);
    for (const a of granted) {
      expect(a).not.toContain("*");
    }
    expect(statements.every((s) => s.Effect === "Allow")).toBe(true);
    expect([...new Set(granted)].sort()).toEqual(
      [...DOCUMENTED_ACTIONS].sort(),
    );
  });

  it("can assume only the CDK bootstrap roles, for the deploy region and us-east-1", () => {
    const assume = statements.filter((s) =>
      actionsOf(s).includes("sts:AssumeRole"),
    );
    expect(assume).toHaveLength(1);
    expect(actionsOf(assume[0])).toEqual(["sts:AssumeRole"]);
    const arns = (assume[0].Resource as { "Fn::Sub": string }[]).map(
      (r) => r["Fn::Sub"],
    );
    const expected: string[] = [];
    for (const region of ["${AWS::Region}", "us-east-1"]) {
      for (const role of [
        "deploy-role",
        "file-publishing-role",
        "image-publishing-role",
        "lookup-role",
      ]) {
        expected.push(
          `arn:\${AWS::Partition}:iam::\${AWS::AccountId}:role/cdk-\${BootstrapQualifier}-${role}-\${AWS::AccountId}-${region}`,
        );
      }
    }
    expect([...arns].sort()).toEqual(expected.sort());
  });

  it("uses Resource * only for actions that cannot be scoped", () => {
    const wildcard = statements
      .filter((s) => s.Resource === "*")
      .flatMap(actionsOf)
      .sort();
    expect(wildcard).toEqual(
      [
        "ec2:DescribeAddresses",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeInternetGateways",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeVpcEndpoints",
        "ec2:DescribeVpcEndpointServices",
        "ec2:DescribeVpcs",
        "servicequotas:GetServiceQuota",
        "lambda:GetAccountSettings",
        "bedrock:ListFoundationModels",
        "bedrock:ListInferenceProfiles",
        "ecr-public:GetAuthorizationToken",
        "sts:GetServiceBearerToken",
      ].sort(),
    );
    // Everything wildcarded except ECR Public login is limited to the deploy region.
    for (const s of statements.filter((x) => x.Resource === "*")) {
      if (actionsOf(s).includes("ecr-public:GetAuthorizationToken")) continue;
      expect(s.Condition).toEqual({
        StringEquals: { "aws:RequestedRegion": { Ref: "AWS::Region" } },
      });
    }
  });

  it("writes logs only to its own log group", () => {
    const logs = statements.find((s) =>
      actionsOf(s).includes("logs:PutLogEvents"),
    );
    expect(logs?.Resource).toEqual([
      { "Fn::GetAtt": "LogGroup.Arn" },
      { "Fn::Sub": "${LogGroup.Arn}:*" },
    ]);
  });

  it("is documented: every action and statement appears in deploying.md", () => {
    const doc = read("external-docs", "content", "deploying.md");
    for (const a of DOCUMENTED_ACTIONS) {
      expect(doc).toContain(a);
    }
    for (const s of statements) {
      expect(doc).toContain(s.Sid);
    }
  });
});

describe("the CodeBuild option stays out of the normal deploy", () => {
  it("is not referenced from the CDK app, the deploy scripts or CI", () => {
    const files = [
      path.join("infra", "bin", "app.ts"),
      path.join("infra", "cdk.json"),
      path.join("scripts", "deploy.sh"),
      path.join("scripts", "deploy-all.sh"),
      path.join("scripts", "preflight-deploy.sh"),
    ];
    for (const f of files) {
      expect(read(f).toLowerCase()).not.toContain("codebuild");
    }
    for (const w of fs.readdirSync(
      path.join(REPO_ROOT, ".github", "workflows"),
    )) {
      expect(read(".github", "workflows", w).toLowerCase()).not.toContain(
        "codebuild",
      );
    }
    const libFiles = fs
      .readdirSync(path.join(REPO_ROOT, "infra", "lib"), { recursive: true })
      .map(String)
      .filter((f) => f.endsWith(".ts"));
    for (const f of libFiles) {
      expect(read("infra", "lib", f)).not.toMatch(/codebuild/i);
    }
  });

  it("keeps make deploy-dev on deploy-all.sh and does not depend on a codebuild target", () => {
    const makefile = read("Makefile").replace(/\r\n/g, "\n");
    expect(makefile).toMatch(
      /^deploy-dev:\n\t\.\/scripts\/deploy-all\.sh dev\n/m,
    );
    const codebuildTargets = makefile.match(/^[a-z-]*codebuild[a-z-]*:.*$/gm);
    expect(codebuildTargets).toEqual([
      "codebuild-setup:",
      "deploy-dev-codebuild:",
      "codebuild-remove:",
    ]);
  });
});

/** Bash able to run the repo's scripts: Git for Windows' bash on Windows, else PATH's. */
function findBash(): string {
  if (process.env.COA_TEST_BASH) return process.env.COA_TEST_BASH;
  const gitBash = "C:\\Program Files\\Git\\bin\\bash.exe";
  return process.platform === "win32" && fs.existsSync(gitBash)
    ? gitBash
    : "bash";
}

describe("scripts/codebuild/run-deploy.sh", () => {
  const SCRIPT = path.join(REPO_ROOT, "scripts", "codebuild", "run-deploy.sh");
  let dir: string;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "coa-codebuild-"));
    // A stand-in for the AWS CLI that records start-build and answers the other calls.
    fs.writeFileSync(
      path.join(dir, "aws"),
      `#!/usr/bin/env bash
case "$1 $2" in
  "--version "*) echo "aws-cli/2.15.0 Python/3.11 Linux/x86_64" ;;
  "cloudformation describe-stacks") echo "coa-deploy" ;;
  "codebuild start-build") printf '%s\\n' "$@" >"$FAKE_AWS_DIR/start-build.args"; echo "coa-deploy:1234" ;;
  "codebuild batch-get-builds")
    case "$*" in
      *buildStatus*) echo "$FAKE_BUILD_STATUS" ;;
      *) echo "None" ;;
    esac ;;
  *) echo "unexpected aws call: $*" >&2; exit 9 ;;
esac
`,
      { mode: 0o755 },
    );
  });

  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  function run(
    status: string,
    env: Record<string, string>,
  ): { code: number; out: string; args: string } {
    const toBashPath = (p: string): string => p.replace(/\\/g, "/");
    let code = 0;
    let out = "";
    try {
      out = execFileSync(findBash(), [toBashPath(SCRIPT)], {
        env: {
          PATH: `${toBashPath(dir)}${path.delimiter}${process.env.PATH ?? ""}`,
          HOME: process.env.HOME ?? dir,
          AWS_REGION: "eu-west-1",
          COA_CODEBUILD_POLL_SECONDS: "0",
          FAKE_AWS_DIR: toBashPath(dir),
          FAKE_BUILD_STATUS: status,
          ...env,
        },
        stdio: ["ignore", "pipe", "pipe"],
      }).toString();
    } catch (e: unknown) {
      const err = e as { status: number; stdout: Buffer; stderr: Buffer };
      code = err.status;
      out = `${err.stdout}${err.stderr}`;
    }
    const argsFile = path.join(dir, "start-build.args");
    return {
      code,
      out,
      args: fs.existsSync(argsFile) ? fs.readFileSync(argsFile, "utf-8") : "",
    };
  }

  it("passes every SCL_* setting and the env name to the build and exits 0 on success", () => {
    const r = run("SUCCEEDED", {
      SCL_LAMBDA_RESERVED_CONCURRENCY: "0",
      SCL_SMUS_ADMIN_ARNS: "arn:aws:iam::123456789012:role/Admin",
      SCL_EMPTY: "",
      COA_DEPLOY_ENV: "stage",
      COA_CODEBUILD_REF: "v1.2.3",
    });
    expect(r.code).toBe(0);
    expect(r.args).toContain("--project-name\ncoa-deploy");
    const json =
      r.args.split("\n")[
        r.args.split("\n").indexOf("--environment-variables-override") + 1
      ];
    const vars = JSON.parse(json) as { name: string; value: string }[];
    expect(Object.fromEntries(vars.map((v) => [v.name, v.value]))).toEqual({
      COA_DEPLOY_ENV: "stage",
      COA_REPO_REF: "v1.2.3",
      SCL_LAMBDA_RESERVED_CONCURRENCY: "0",
      SCL_SMUS_ADMIN_ARNS: "arn:aws:iam::123456789012:role/Admin",
    });
  });

  it("starts the build with the project's own settings when nothing is set", () => {
    const r = run("SUCCEEDED", {});
    expect(r.code).toBe(0);
    expect(r.args).not.toContain("--environment-variables-override");
  });

  it.each(["FAILED", "STOPPED", "TIMED_OUT"])(
    "exits non-zero when the build ends %s",
    (status) => {
      expect(run(status, {}).code).toBe(1);
    },
  );

  it("refuses a malformed ref before starting anything", () => {
    const r = run("SUCCEEDED", { COA_CODEBUILD_REF: "main; rm -rf /" });
    expect(r.code).not.toBe(0);
    expect(r.args).toBe("");
  });
});
