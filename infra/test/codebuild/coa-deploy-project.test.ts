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

  const buildSpec = (): string =>
    Object.values(template.findResources("AWS::CodeBuild::Project"))[0]
      .Properties.Source.BuildSpec;

  it("registers amd64 emulation from the ECR Public mirror first, with a fallback, and checks it", () => {
    const spec = buildSpec();
    const mirror = spec.indexOf(
      "public.ecr.aws/eks-distro-build-tooling/binfmt-misc",
    );
    const hub = spec.indexOf("tonistiigi/binfmt");
    expect(mirror).toBeGreaterThan(-1);
    expect(hub).toBeGreaterThan(mirror);
    expect(spec).toContain("--platform linux/amd64");
    expect(spec).toContain('= "x86_64"');
    // The check runs before setup, so a host without emulation fails fast.
    expect(spec.indexOf("could not be registered")).toBeLessThan(
      spec.indexOf("make setup"),
    );
    // Echoed command text must not look like an error to log scanners.
    expect(spec).not.toMatch(/ERROR:/);
  });

  it("pins the privileged emulation images and the check image by digest and logs in to ECR Public first", () => {
    const spec = buildSpec();
    for (const v of ["MIRROR", "HUB", "PROBE"]) {
      expect(spec).toMatch(new RegExp(`${v}=\\S+@sha256:[0-9a-f]{64}\\n`));
    }
    expect(spec).not.toContain("alpine:latest");
    expect(spec.indexOf("aws ecr-public get-login-password")).toBeGreaterThan(
      -1,
    );
    expect(spec.indexOf("aws ecr-public get-login-password")).toBeLessThan(
      spec.indexOf("docker pull"),
    );
    // A failed pull is reported apart from missing emulation.
    expect(spec).toContain("a registry problem, not missing emulation");
  });

  it("has command blocks that are valid bash", () => {
    const blocks = buildSpec()
      .split(/^\s+- \|\n/m)
      .slice(1);
    expect(blocks).toHaveLength(2); // install and build
    for (const b of blocks) {
      const body = b
        .split("\n")
        .filter((l) => /^\s{8,}/.test(l) || l === "")
        .join("\n");
      expect(() =>
        execFileSync(findBash(), ["-n"], {
          input: body,
          stdio: ["pipe", "pipe", "pipe"],
        }),
      ).not.toThrow();
    }
  });

  it("asks for a local Docker layer cache", () => {
    template.hasResourceProperties("AWS::CodeBuild::Project", {
      Cache: { Type: "LOCAL", Modes: ["LOCAL_DOCKER_LAYER_CACHE"] },
    });
  });

  it("deploys only COA_CDK_STACKS when it is set, validated up front, and everything when unset", () => {
    const spec = buildSpec();
    expect(spec).toContain('STACKS="${COA_CDK_STACKS:-}"');
    // Validated in the install phase with the other inputs, before make setup, and strictly.
    const check = spec.indexOf("COA_CDK_STACKS may only hold stack names");
    expect(check).toBeGreaterThan(-1);
    expect(check).toBeLessThan(spec.indexOf("git fetch"));
    expect(spec).not.toContain("\\ -]");
    const subset = spec.indexOf("./scripts/codebuild/deploy-stacks.sh");
    const all = spec.indexOf('./scripts/deploy.sh "$COA_DEPLOY_ENV"');
    expect(subset).toBeGreaterThan(spec.indexOf("make setup"));
    expect(all).toBeGreaterThan(subset);
  });
});

describe("deploy-stacks.sh (subset deploy for batched builds)", () => {
  const script = read("scripts", "codebuild", "deploy-stacks.sh").replace(
    /\r\n/g,
    "\n",
  );
  const deploySh = read("scripts", "deploy.sh").replace(/\r\n/g, "\n");

  it("deploys the given stacks exclusively with deploy.sh's context and flags", () => {
    expect(script).toContain('cdk deploy "$@" --exclusively "${CONTEXT[@]}"');
    expect(script).toContain('CONTEXT=(--context "env=$ENV")');
    expect(script).toContain("scripts/lib/cdk-context.sh");
    expect(script).toContain("--require-approval=never");
    expect(script).toContain('--concurrency "${SCL_CDK_CONCURRENCY:-3}"');
  });

  it("runs the same checks and build steps as deploy.sh before deploying", () => {
    for (const step of [
      "check-smus-admin-principal.sh",
      "coa_add_jdbc_peering_context",
      "./scripts/preflight-deploy.sh",
      "pnpm install --frozen-lockfile",
      "pnpm nx run-many -t build",
    ]) {
      expect(deploySh).toContain(step);
      expect(script).toContain(step);
      expect(script.indexOf(step)).toBeLessThan(
        script.indexOf("exec cdk deploy"),
      );
    }
  });

  it("rejects invalid stack names", () => {
    expect(script).toMatch(/invalid stack name/);
  });
});

describe("run-deploy.sh batched mode", () => {
  const bash = findBash();
  const dryRun = (env: Record<string, string>): string =>
    execFileSync(
      bash,
      [path.join(REPO_ROOT, "scripts", "codebuild", "run-deploy.sh")],
      {
        // Explicit, minimal environment: nothing the developer has exported (SCL_PREFIX,
        // COA_DEPLOY_ENV, COA_CODEBUILD_BATCHES, ...) leaks into the plan.
        env: {
          PATH: process.env.PATH ?? "",
          HOME: process.env.HOME ?? os.tmpdir(),
          COA_CODEBUILD_BATCHED: "1",
          COA_CODEBUILD_DRY_RUN: "1",
          ...env,
        },
        encoding: "utf8",
      },
    );

  it("plans the platform stacks in dependency order with the prefix and environment", () => {
    const out = dryRun({ SCL_PREFIX: "abc", COA_DEPLOY_ENV: "test" });
    const lines = out.split("\n").filter((l) => l.includes("batch "));
    expect(lines).toHaveLength(5);
    expect(lines[0]).toContain("abc-test-network");
    expect(lines[1]).toContain("abc-test-storage");
    expect(lines[2]).toContain("abc-test-sources");
    expect(lines[3]).toContain("abc-test-ontology");
    expect(lines[4]).toContain("abc-test-web");
    const order = lines.join(" ");
    // Each stack must come after the stacks it depends on.
    for (const [dep, stack] of [
      ["network", "storage"],
      ["storage", "vkg"],
      ["namespace", "sources"],
      ["sources", "ontology"],
      ["serve", "mcp"],
      ["ontology", "api"],
      ["api", "web"],
    ]) {
      expect(order.indexOf(`abc-test-${dep}`)).toBeLessThan(
        order.indexOf(`abc-test-${stack}`),
      );
    }
  });

  it("resumes from a given batch", () => {
    const out = dryRun({ COA_CODEBUILD_FROM_BATCH: "4" });
    expect(out).not.toContain("batch 3:");
    expect(out).toContain(
      "batch 4: coa-dev-ontology coa-dev-serve coa-dev-mcp",
    );
  });

  it("accepts an explicit batch list", () => {
    const out = dryRun({ COA_CODEBUILD_BATCHES: "x-a x-b; x-c" });
    expect(out).toContain("batch 1: x-a x-b");
    expect(out).toContain("batch 2: x-c");
  });

  it("rejects a malformed stack name before any AWS call", () => {
    expect(() =>
      dryRun({ COA_CODEBUILD_BATCHES: "coa-dev-a,coa-dev-b" }),
    ).toThrow();
    expect(() => dryRun({ COA_CODEBUILD_BATCHES: "a_b" })).toThrow();
    expect(() => dryRun({ COA_CODEBUILD_BATCHES: " ; " })).toThrow();
  });

  it("covers every stack the CDK app defines", () => {
    const app = read("infra", "bin", "app.ts");
    const out = dryRun({});
    for (const m of app.matchAll(/`\$\{stackPrefix\}-([a-z-]+)`/g)) {
      expect(out).toContain(`coa-dev-${m[1]}`);
    }
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

  it("is trusted only by CodeBuild, for this account and this project", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: "codebuild.amazonaws.com" },
            Condition: {
              StringEquals: { "aws:SourceAccount": { Ref: "AWS::AccountId" } },
              ArnLike: {
                "aws:SourceArn": {
                  "Fn::Sub":
                    "arn:${AWS::Partition}:codebuild:${AWS::Region}:${AWS::AccountId}:project/${ProjectName}",
                },
              },
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

  it("documents the permissions the caller needs", () => {
    const doc = read("external-docs", "content", "deploying.md");
    for (const a of [
      "cloudformation:DescribeStacks",
      "codebuild:StartBuild",
      "codebuild:BatchGetBuilds",
      "codebuild:BatchGetProjects",
      "codebuild:StopBuild",
      "logs:FilterLogEvents",
    ]) {
      expect(doc).toContain(a);
    }
    // The batched run-deploy.sh calls stay inside that list.
    const script = read("scripts", "codebuild", "run-deploy.sh");
    expect(script).toContain("aws codebuild stop-build");
    expect(script).toContain("batch-get-projects");
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

const FIXTURES = path.join(__dirname, "fixtures");
const toBashPath = (p: string): string => p.replace(/\\/g, "/");
const SHA = "1".repeat(40);
const SHA_TAG = "2".repeat(40);
const SHA_PEELED = "3".repeat(40);

/**
 * Install test doubles (copies with LF endings, executable) into dir. Returns an env
 * for a script run: dir first on PATH, and shell functions (BASH_ENV) that call the
 * doubles, because Git for Windows' bash puts its own git ahead of PATH.
 */
function installFakes(dir: string, names: string[]): Record<string, string> {
  for (const n of names) {
    fs.writeFileSync(
      path.join(dir, n),
      fs.readFileSync(path.join(FIXTURES, n), "utf-8").replace(/\r\n/g, "\n"),
      { mode: 0o755 },
    );
  }
  const fns = names
    .filter((n) => n !== "aws")
    .map((n) => `${n}() { "${toBashPath(dir)}/${n}" "$@"; }\n`)
    .join("");
  fs.writeFileSync(path.join(dir, "bashenv"), fns);
  return {
    PATH: `${toBashPath(dir)}${path.delimiter}${process.env.PATH ?? ""}`,
    BASH_ENV: `${toBashPath(dir)}/bashenv`,
    FAKE_AWS_DIR: toBashPath(dir),
  };
}

describe("scripts/codebuild/run-deploy.sh", () => {
  const SCRIPT = path.join(REPO_ROOT, "scripts", "codebuild", "run-deploy.sh");
  let dir: string;
  let fakeEnv: Record<string, string>;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "coa-codebuild-"));
    // Stand-ins for the AWS CLI and git: they record every call and never reach a network.
    fakeEnv = installFakes(dir, ["aws", "git"]);
    fs.writeFileSync(path.join(dir, "git.refs"), `${SHA}\trefs/heads/main\n`);
  });

  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  interface Options {
    /** One "STATUS:TIMED_OUT_PHASES" line per build, in start order; later builds succeed. */
    outcomes?: string[];
    /** Statuses a stack reports on successive reads, by stack name. */
    stacks?: Record<string, string[]>;
    refs?: string;
  }
  interface Result {
    code: number;
    out: string;
    /** The first build's start-build arguments. */
    args: string;
    /** Per started build: its environment overrides by name. */
    builds: Record<string, string>[];
    calls: string;
  }

  function run(
    status: string,
    env: Record<string, string>,
    opts: Options = {},
  ): Result {
    if (opts.outcomes) {
      fs.writeFileSync(
        path.join(dir, "outcomes"),
        opts.outcomes.join("\n") + "\n",
      );
    }
    for (const [name, seq] of Object.entries(opts.stacks ?? {})) {
      fs.writeFileSync(path.join(dir, `stack.${name}`), seq.join("\n") + "\n");
    }
    if (opts.refs !== undefined) {
      fs.writeFileSync(path.join(dir, "git.refs"), opts.refs);
    }
    let code = 0;
    let out = "";
    try {
      out = execFileSync(findBash(), [toBashPath(SCRIPT)], {
        // Explicit environment: nothing from the developer's shell reaches the script.
        env: {
          ...fakeEnv,
          HOME: process.env.HOME ?? dir,
          AWS_REGION: "eu-west-1",
          COA_CODEBUILD_POLL_SECONDS: "0",
          COA_CODEBUILD_SETTLE_POLL_SECONDS: "0",
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
    const startFiles = fs
      .readdirSync(dir)
      .filter((f) => /^start-build\.\d+\.args$/.test(f))
      .sort((a, b) => parseInt(a.split(".")[1]) - parseInt(b.split(".")[1]));
    const builds = startFiles.map((f) => {
      const lines = fs.readFileSync(path.join(dir, f), "utf-8").split("\n");
      const i = lines.indexOf("--environment-variables-override");
      if (i < 0) return {};
      const vars = JSON.parse(lines[i + 1]) as {
        name: string;
        value: string;
      }[];
      return Object.fromEntries(vars.map((v) => [v.name, v.value]));
    });
    const calls = path.join(dir, "calls.log");
    return {
      code,
      out,
      args: startFiles.length
        ? fs.readFileSync(path.join(dir, startFiles[0]), "utf-8")
        : "",
      builds,
      calls: fs.existsSync(calls) ? fs.readFileSync(calls, "utf-8") : "",
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
    expect(r.builds).toEqual([
      {
        COA_DEPLOY_ENV: "stage",
        COA_REPO_REF: "v1.2.3",
        SCL_LAMBDA_RESERVED_CONCURRENCY: "0",
        SCL_SMUS_ADMIN_ARNS: "arn:aws:iam::123456789012:role/Admin",
      },
    ]);
  });

  it("starts the build with the project's own settings when nothing is set", () => {
    const r = run("SUCCEEDED", {});
    expect(r.code).toBe(0);
    expect(r.args).not.toContain("--environment-variables-override");
  });

  it.each(["FAILED", "STOPPED", "TIMED_OUT"])(
    "exits non-zero when the build ends %s",
    (status) => {
      const r = run(status, {});
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(1); // not batched: no retry
    },
  );

  it("refuses a malformed ref before starting anything", () => {
    const r = run("SUCCEEDED", { COA_CODEBUILD_REF: "main; rm -rf /" });
    expect(r.code).not.toBe(0);
    expect(r.args).toBe("");
  });

  describe("dry run", () => {
    it("starts nothing and makes no AWS call, batched or not", () => {
      const modes: Record<string, string>[] = [
        {},
        { COA_CODEBUILD_BATCHED: "1" },
      ];
      for (const batched of modes) {
        const r = run("SUCCEEDED", {
          COA_CODEBUILD_DRY_RUN: "1",
          SCL_LAMBDA_RESERVED_CONCURRENCY: "0",
          ...batched,
        });
        expect(r.code).toBe(0);
        expect(r.builds).toHaveLength(0);
        expect(r.calls).toBe("");
        expect(r.out).toContain("SCL_LAMBDA_RESERVED_CONCURRENCY=0");
        expect(r.out).toContain("no build is started");
      }
    });

    it("says which prefix and environment it assumed", () => {
      const r = run("SUCCEEDED", {
        COA_CODEBUILD_DRY_RUN: "1",
        COA_CODEBUILD_BATCHED: "1",
      });
      expect(r.out).toContain("assuming prefix coa and environment dev");
    });
  });

  describe("batched mode", () => {
    const BATCHED = { COA_CODEBUILD_BATCHED: "1" };
    const DEFAULT_PLAN = [
      "coa-dev-network coa-dev-eni-warm coa-dev-auth coa-dev-guardrail",
      "coa-dev-storage coa-dev-authnz coa-dev-edge-waf coa-dev-vkg coa-dev-namespace",
      "coa-dev-sources",
      "coa-dev-ontology coa-dev-serve coa-dev-mcp",
      "coa-dev-metric-service coa-dev-data-layer coa-dev-api coa-dev-web",
    ];
    const stacksOf = (r: Result): string[] =>
      r.builds.map((b) => b.COA_CDK_STACKS);

    it("runs one build per batch, in order, all on the same commit", () => {
      const r = run("SUCCEEDED", BATCHED);
      expect(r.code).toBe(0);
      expect(stacksOf(r)).toEqual(DEFAULT_PLAN);
      expect(r.builds.map((b) => b.COA_REPO_REF)).toEqual(Array(5).fill(SHA));
      expect(r.out).toContain("All 5 batches deployed");
    });

    it("starts a batch only after the previous one succeeded", () => {
      const r = run("SUCCEEDED", BATCHED, {
        outcomes: ["SUCCEEDED:0", "FAILED:0"],
      });
      expect(r.code).toBe(1);
      expect(stacksOf(r)).toEqual(DEFAULT_PLAN.slice(0, 2));
    });

    it("reruns a batch that timed out (FAILED with a TIMED_OUT phase), then goes on", () => {
      const r = run("SUCCEEDED", BATCHED, { outcomes: ["FAILED:1"] });
      expect(r.code).toBe(0);
      expect(stacksOf(r)).toEqual([DEFAULT_PLAN[0], ...DEFAULT_PLAN]);
      expect(r.out).toContain("timed out; rerunning (1/2)");
      // The rerun deploys the same commit.
      expect(new Set(r.builds.map((b) => b.COA_REPO_REF))).toEqual(
        new Set([SHA]),
      );
    });

    it("also treats a build status of TIMED_OUT as a timeout", () => {
      const r = run("SUCCEEDED", BATCHED, { outcomes: ["TIMED_OUT:0"] });
      expect(r.code).toBe(0);
      expect(r.builds).toHaveLength(6);
    });

    it("does not rerun a batch that failed for another reason", () => {
      const r = run("SUCCEEDED", BATCHED, { outcomes: ["FAILED:0"] });
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(1);
      expect(r.out).toContain("Batch 1 failed (FAILED)");
      expect(r.out).toContain("Resume at batch 1");
      expect(r.out).toContain(
        `COA_CODEBUILD_FROM_BATCH=1 COA_CODEBUILD_REF=${SHA}`,
      );
    });

    it("does not take an unreadable phase query for a timeout", () => {
      const r = run("SUCCEEDED", BATCHED, { outcomes: ["FAILED:ERR"] });
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(1);
      expect(r.out).toContain("treating FAILED as a failure, not a timeout");
    });

    it("gives up after COA_CODEBUILD_BATCH_RETRIES reruns", () => {
      const r = run("SUCCEEDED", BATCHED, {
        outcomes: ["FAILED:1", "FAILED:1", "FAILED:1", "SUCCEEDED:0"],
      });
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(3); // the first run and 2 reruns
      expect(r.out).toContain("Batch 1 failed (TIMED_OUT)");
    });

    it("does not rerun at all when COA_CODEBUILD_BATCH_RETRIES is 0", () => {
      const r = run(
        "SUCCEEDED",
        { ...BATCHED, COA_CODEBUILD_BATCH_RETRIES: "0" },
        { outcomes: ["FAILED:1"] },
      );
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(1);
    });

    it("waits for the batch's stacks to settle before a rerun", () => {
      const r = run("SUCCEEDED", BATCHED, {
        outcomes: ["FAILED:1"],
        stacks: {
          "coa-dev-network": ["UPDATE_IN_PROGRESS", "UPDATE_COMPLETE"],
        },
      });
      expect(r.code).toBe(0);
      expect(r.out).toContain("still in progress: coa-dev-network");
      expect(r.builds).toHaveLength(6);
    });

    it("does not rerun when a stack is in a state a rerun cannot fix", () => {
      const r = run("SUCCEEDED", BATCHED, {
        outcomes: ["FAILED:1"],
        stacks: { "coa-dev-network": ["ROLLBACK_COMPLETE"] },
      });
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(1);
      expect(r.out).toContain("coa-dev-network is ROLLBACK_COMPLETE");
      expect(r.out).toContain("Resume at batch 1");
    });

    it("stops when it cannot read the stacks' status", () => {
      const r = run("SUCCEEDED", BATCHED, {
        outcomes: ["FAILED:1"],
        stacks: { "coa-dev-network": ["DENIED"] },
      });
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(1);
      expect(r.out).toContain("cannot read the status of the batch");
    });

    it("treats a stack that does not exist as settled", () => {
      const r = run("SUCCEEDED", BATCHED, {
        outcomes: ["FAILED:1"],
        stacks: { "coa-dev-network": ["MISSING"] },
      });
      expect(r.code).toBe(0);
      expect(r.builds).toHaveLength(6);
    });

    it("resumes from COA_CODEBUILD_FROM_BATCH", () => {
      const r = run("SUCCEEDED", { ...BATCHED, COA_CODEBUILD_FROM_BATCH: "4" });
      expect(r.code).toBe(0);
      expect(stacksOf(r)).toEqual(DEFAULT_PLAN.slice(3));
      expect(
        run("SUCCEEDED", { ...BATCHED, COA_CODEBUILD_FROM_BATCH: "6" }).code,
      ).toBe(1);
    });

    it("uses the project's prefix when SCL_PREFIX is not set", () => {
      const r = run("SUCCEEDED", { ...BATCHED, FAKE_PROJECT_PREFIX: "abc" });
      expect(stacksOf(r)[0]).toBe(
        "abc-dev-network abc-dev-eni-warm abc-dev-auth abc-dev-guardrail",
      );
    });

    it("prints the resume command when the build status cannot be read", () => {
      const r = run("SUCCEEDED", { ...BATCHED, FAKE_STATUS_FAIL: "1" });
      expect(r.code).toBe(1);
      expect(r.out).toContain("cannot read the status of build");
      expect(r.out).toContain("Resume at batch 1");
    });

    describe("revision", () => {
      it("resolves a branch to its commit once and passes that SHA", () => {
        const r = run("SUCCEEDED", BATCHED);
        expect(r.calls.match(/^git ls-remote/gm)).toHaveLength(1);
        expect(r.out).toContain(`COA_REPO_REF=${SHA} (resolved from main)`);
      });

      it("uses an annotated tag's commit", () => {
        const r = run(
          "SUCCEEDED",
          { ...BATCHED, COA_CODEBUILD_REF: "v1" },
          {
            refs: `${SHA_TAG}\trefs/tags/v1\n${SHA_PEELED}\trefs/tags/v1^{}\n`,
          },
        );
        expect(r.builds.map((b) => b.COA_REPO_REF)).toEqual(
          Array(5).fill(SHA_PEELED),
        );
      });

      it("takes a full commit SHA as it is, without asking the remote", () => {
        const r = run("SUCCEEDED", { ...BATCHED, COA_CODEBUILD_REF: SHA_TAG });
        expect(r.builds.map((b) => b.COA_REPO_REF)).toEqual(
          Array(5).fill(SHA_TAG),
        );
        expect(r.calls).not.toContain("ls-remote");
      });

      it("starts nothing when the ref cannot be resolved", () => {
        const r = run(
          "SUCCEEDED",
          { ...BATCHED, COA_CODEBUILD_REF: "nope" },
          { refs: "" },
        );
        expect(r.code).toBe(1);
        expect(r.builds).toHaveLength(0);
        expect(r.out).toContain("cannot resolve 'nope'");
      });
    });

    it("refuses a malformed custom batch before starting any build", () => {
      const r = run("SUCCEEDED", {
        ...BATCHED,
        COA_CODEBUILD_BATCHES: "coa-dev-a;coa_dev_b",
      });
      expect(r.code).toBe(1);
      expect(r.builds).toHaveLength(0);
      expect(r.out).toContain("invalid stack name 'coa_dev_b'");
    });
  });
});

describe("scripts/codebuild/deploy-stacks.sh run against stand-in tools", () => {
  let dir: string;
  let repo: string;
  let fakeEnv: Record<string, string>;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "coa-stacks-"));
    repo = path.join(dir, "repo");
    const scripts = path.join(repo, "scripts");
    fs.mkdirSync(path.join(scripts, "codebuild"), { recursive: true });
    fs.mkdirSync(path.join(scripts, "lib"));
    // The script under test, with stand-ins for what it calls inside the repository.
    fs.writeFileSync(
      path.join(scripts, "codebuild", "deploy-stacks.sh"),
      read("scripts", "codebuild", "deploy-stacks.sh").replace(/\r\n/g, "\n"),
      { mode: 0o755 },
    );
    fs.writeFileSync(
      path.join(scripts, "lib", "cdk-context.sh"),
      "coa_add_jdbc_peering_context() { :; }\n",
    );
    for (const n of ["check-smus-admin-principal.sh", "preflight-deploy.sh"]) {
      fs.writeFileSync(path.join(scripts, n), "#!/usr/bin/env bash\nexit 0\n", {
        mode: 0o755,
      });
    }
    fakeEnv = installFakes(dir, ["pnpm"]);
  });

  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  function deploy(
    defined: string[],
    requested: string[],
  ): { code: number; out: string; deployed: string } {
    let code = 0;
    let out = "";
    try {
      out = execFileSync(
        findBash(),
        [
          toBashPath(
            path.join(repo, "scripts", "codebuild", "deploy-stacks.sh"),
          ),
          "dev",
          ...requested,
        ],
        {
          env: {
            ...fakeEnv,
            HOME: process.env.HOME ?? dir,
            FAKE_CDK_LIST: defined.join(" "),
          },
          stdio: ["ignore", "pipe", "pipe"],
        },
      ).toString();
    } catch (e: unknown) {
      const err = e as { status: number; stdout: Buffer; stderr: Buffer };
      code = err.status;
      out = `${err.stdout}${err.stderr}`;
    }
    const f = path.join(dir, "deploy.args");
    return {
      code,
      out,
      deployed: fs.existsSync(f) ? fs.readFileSync(f, "utf-8") : "",
    };
  }

  it("deploys the requested stacks exclusively", () => {
    const r = deploy(
      ["coa-dev-a", "coa-dev-b", "coa-dev-c"],
      ["coa-dev-a", "coa-dev-b"],
    );
    expect(r.code).toBe(0);
    expect(r.deployed).toContain(
      "cdk deploy coa-dev-a coa-dev-b --exclusively",
    );
    expect(r.deployed).not.toContain("coa-dev-c");
  });

  it("fails, and deploys nothing, when a stack is not defined by the app", () => {
    const r = deploy(["coa-dev-a"], ["coa-dev-a", "coa-dev-typo"]);
    expect(r.code).toBe(1);
    expect(r.out).toContain("does not define: coa-dev-typo");
    expect(r.deployed).toBe("");
  });

  it("fails when nothing requested is defined", () => {
    const r = deploy(["coa-dev-a"], ["coa-dev-x"]);
    expect(r.code).toBe(1);
    expect(r.deployed).toBe("");
  });

  it("skips an edge-waf stack the app does not define, and deploys the rest", () => {
    const r = deploy(
      ["coa-dev-storage"],
      ["coa-dev-storage", "coa-dev-edge-waf"],
    );
    expect(r.code).toBe(0);
    expect(r.out).toContain("Skipping coa-dev-edge-waf");
    expect(r.deployed).toContain("cdk deploy coa-dev-storage --exclusively");
  });

  it("succeeds with nothing to deploy when only edge-waf was requested and is absent", () => {
    const r = deploy(["coa-dev-storage"], ["coa-dev-edge-waf"]);
    expect(r.code).toBe(0);
    expect(r.deployed).toBe("");
  });

  it("rejects an invalid stack name", () => {
    expect(deploy(["x"], ["bad;name"]).code).toBe(2);
  });
});
