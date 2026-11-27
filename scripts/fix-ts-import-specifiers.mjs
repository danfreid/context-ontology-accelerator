#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Repairs relative import specifiers in the Smithy TypeScript clients.
//
//   node scripts/fix-ts-import-specifiers.mjs PACKAGE_DIR [PACKAGE_DIR...]
//
// On Windows the Smithy TypeScript codegen builds some specifiers from native
// paths: they contain backslashes (`"./src\pagination\Interfaces"`, which in a
// TS string literal is `./srcpaginationInterfaces`) and some are written
// relative to the package root instead of the importing file. tsc then fails
// with TS2307. This rewrites backslashes to `/` and re-bases a specifier that
// does not resolve from its file but does resolve from the package root.
//
// A file is only rewritten when one of its specifiers needs it, so the output
// on Linux and macOS (where every specifier already resolves) is unchanged.
// Exits 1, naming each one, if a relative specifier still does not resolve.
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

// `from "x"`, `import "x"`, `import("x")`, `require("x")` with a relative x.
const SPECIFIER = /(\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)(["'])(\.\.?(?:[\\/][^"'\r\n]*)?)\2/g;
const SUFFIXES = ["", ".ts", ".tsx", ".d.ts", ".js", ".json", "/index.ts", "/index.tsx", "/index.d.ts", "/index.js"];

function isFile(p) {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
  }
}

function resolves(fromDir, spec) {
  const base = path.resolve(fromDir, spec);
  if (SUFFIXES.some((s) => isFile(base + s))) return true;
  // ESM-style `./x.js` pointing at `x.ts`.
  return /\.js$/.test(base) && [".ts", ".tsx"].some((s) => isFile(base.slice(0, -3) + s));
}

function toRelative(fromDir, target) {
  const rel = path.relative(fromDir, target).split(path.sep).join("/");
  return rel.startsWith(".") ? rel : `./${rel}`;
}

function sourceFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === "dist-cjs" || entry.name === "dist-es" || entry.name === "dist-types") continue;
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(p));
    else if (/\.tsx?$/.test(entry.name)) out.push(p);
  }
  return out;
}

function fixPackage(pkgDir) {
  const root = path.resolve(pkgDir);
  const changed = [];
  const unresolved = [];
  for (const file of sourceFiles(root)) {
    const dir = path.dirname(file);
    const text = readFileSync(file, "utf8");
    const fixed = text.replace(SPECIFIER, (match, lead, quote, spec) => {
      let next = spec.replace(/\\/g, "/");
      if (!resolves(dir, next) && resolves(root, next)) {
        next = toRelative(dir, path.resolve(root, next));
      }
      if (!resolves(dir, next)) {
        unresolved.push(`${path.relative(root, file).split(path.sep).join("/")}: ${spec}`);
        return match;
      }
      return next === spec ? match : `${lead}${quote}${next}${quote}`;
    });
    if (fixed !== text) {
      writeFileSync(file, fixed);
      changed.push(file);
    }
  }
  return { changed, unresolved };
}

function main(dirs) {
  if (dirs.length === 0) {
    console.error("usage: fix-ts-import-specifiers.mjs PACKAGE_DIR [PACKAGE_DIR...]");
    return 2;
  }
  let failed = false;
  for (const dir of dirs) {
    if (!existsSync(dir)) {
      console.error(`✗ ${dir} does not exist`);
      failed = true;
      continue;
    }
    const { changed, unresolved } = fixPackage(dir);
    if (changed.length > 0) {
      console.log(`  Fixed import specifiers in ${changed.length} file(s) under ${path.basename(path.resolve(dir))}`);
    }
    for (const u of unresolved) {
      console.error(`✗ Unresolved import in ${path.basename(path.resolve(dir))}/${u}`);
      failed = true;
    }
  }
  return failed ? 1 : 0;
}

process.exitCode = main(process.argv.slice(2));
