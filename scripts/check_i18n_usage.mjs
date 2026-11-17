#!/usr/bin/env node

/**
 * Closed-world frontend i18n usage gate.
 *
 * TypeScript checks typed imports. This scanner covers the retained JavaScript
 * adapter, HTML attributes, template HTML, and compatibility translator ports
 * so a literal key cannot escape merely by crossing an untyped boundary.
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { readI18nCatalog } from './gen_i18n_contract.mjs';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const frontendRoot = path.join(repositoryRoot, 'frontend/src');
const translationCallNames = new Set([
  't', '_t', '_tt', '_tf', 'tr', 'translate', 'moduleTranslate',
]);
/* Components whose every user-visible string must come from the catalogs: the
 * lead deliverables (reports and files) section and the Coordinator task board
 * (Progress) with its sidebar row and task history. A literal holding a letter
 * in a copy position (text, tooltip, aria-label, placeholder, button label,
 * disclosure summary, appended text) fails; separators and symbols such as
 * ' · ' and '›' pass. */
export const literalCopyFiles = [
  'frontend/src/features/lead-deliverables/list.ts',
  'frontend/src/features/lead-deliverables/open.ts',
  'frontend/src/features/lead-deliverables/model.ts',
  'frontend/src/features/coordinator-progress.ts',
  'frontend/src/features/coordinator-task-history.ts',
  'frontend/src/features/project-coordination-surface.ts',
];
const copyProperties = new Set(['textContent', 'innerText', 'title', 'placeholder', 'ariaLabel', 'label', 'alt']);
const copyAttributes = new Set(['aria-label', 'title', 'placeholder', 'alt', 'aria-description']);
const copyCalls = new Set([
  'createDisclosureSummary', 'createTextNode', 'append', 'prepend', 'replaceChildren', 'before', 'after',
]);
const letterPattern = /\p{L}/u;
const dataAttributePattern = /data-i18n(?:-html|-placeholder|-title)?\s*=\s*["']([^"']+)["']/g;
const sourceExtensions = new Set(['.ts', '.js']);

function relativeToRepository(value) {
  return path.relative(repositoryRoot, value).split(path.sep).join('/');
}

async function sourceFiles(directory) {
  const output = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      output.push(...await sourceFiles(absolute));
      continue;
    }
    if (!entry.isFile() || !sourceExtensions.has(path.extname(entry.name))) continue;
    if (entry.name === 'app-runtime.js' || entry.name.includes('.generated.')) continue;
    output.push(absolute);
  }
  return output;
}

function callName(expression) {
  if (ts.isIdentifier(expression)) return expression.text;
  if (ts.isPropertyAccessExpression(expression)) return expression.name.text;
  if (ts.isElementAccessExpression(expression)
      && expression.argumentExpression
      && ts.isStringLiteral(expression.argumentExpression)) {
    return expression.argumentExpression.text;
  }
  return '';
}

function staticKeys(expression) {
  if (!expression) return null;
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return [expression.text];
  }
  if (ts.isParenthesizedExpression(expression)
      || ts.isAsExpression(expression)
      || ts.isTypeAssertionExpression(expression)
      || ts.isNonNullExpression(expression)) {
    return staticKeys(expression.expression);
  }
  if (ts.isConditionalExpression(expression)) {
    const left = staticKeys(expression.whenTrue);
    const right = staticKeys(expression.whenFalse);
    return left && right ? [...left, ...right] : null;
  }
  return null;
}

function objectLiteralKeys(expression) {
  if (!expression) return null;
  if (ts.isParenthesizedExpression(expression)
      || ts.isAsExpression(expression)
      || ts.isTypeAssertionExpression(expression)
      || ts.isNonNullExpression(expression)) {
    return objectLiteralKeys(expression.expression);
  }
  if (!ts.isObjectLiteralExpression(expression)) return null;
  const keys = [];
  for (const property of expression.properties) {
    if (ts.isSpreadAssignment(property)) return null;
    const name = property.name;
    if (!name) return null;
    if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name)) {
      keys.push(name.text);
      continue;
    }
    return null;
  }
  return keys;
}

function lineOf(sourceFile, node) {
  return sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
}

export function inspectScript(
  filePath,
  source,
  definedKeys,
  problems,
  paramsByKey = new Map(),
) {
  const scriptKind = filePath.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS;
  const sourceFile = ts.createSourceFile(
    filePath, source, ts.ScriptTarget.Latest, true, scriptKind,
  );
  const relative = relativeToRepository(filePath);
  function visit(node) {
    if (ts.isCallExpression(node) && translationCallNames.has(callName(node.expression))) {
      const keys = staticKeys(node.arguments[0]);
      if (keys) {
        for (const key of keys) {
          if (!definedKeys.has(key)) {
            problems.push(`${relative}:${lineOf(sourceFile, node)}: undefined key ${key}`);
          }
        }
        const params = node.arguments.slice(1)
          .map(objectLiteralKeys)
          .find((value) => value !== null);
        if (params) {
          for (const key of keys) {
            const expected = paramsByKey.get(key);
            if (!expected) continue;
            const missingParams = expected.filter((name) => !params.includes(name));
            const extraParams = params.filter((name) => !expected.includes(name));
            if (missingParams.length || extraParams.length) {
              problems.push(
                `${relative}:${lineOf(sourceFile, node)}: placeholder drift for ${key}; `
                + `missing=${JSON.stringify(missingParams)} extra=${JSON.stringify(extraParams)}`,
              );
            }
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  inspectDataAttributes(filePath, source, definedKeys, problems);
}

export function inspectDataAttributes(filePath, source, definedKeys, problems) {
  const relative = relativeToRepository(filePath);
  for (const match of source.matchAll(dataAttributePattern)) {
    const key = match[1];
    if (definedKeys.has(key)) continue;
    const line = source.slice(0, match.index).split('\n').length;
    problems.push(`${relative}:${line}: data-i18n references undefined key ${key}`);
  }
}

function literalCopy(expression) {
  if (!expression) return [];
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return letterPattern.test(expression.text) ? [expression.text] : [];
  }
  if (ts.isTemplateExpression(expression)) {
    const parts = [expression.head.text, ...expression.templateSpans.map((span) => span.literal.text)];
    return parts.filter((part) => letterPattern.test(part));
  }
  if (ts.isParenthesizedExpression(expression) || ts.isAsExpression(expression)
      || ts.isNonNullExpression(expression)) {
    return literalCopy(expression.expression);
  }
  if (ts.isConditionalExpression(expression)) {
    return [...literalCopy(expression.whenTrue), ...literalCopy(expression.whenFalse)];
  }
  if (ts.isBinaryExpression(expression)
      && [ts.SyntaxKind.PlusToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken]
        .includes(expression.operatorToken.kind)) {
    return [...literalCopy(expression.left), ...literalCopy(expression.right)];
  }
  return [];
}

function propertyNameText(name) {
  return name && (ts.isIdentifier(name) || ts.isStringLiteral(name)) ? name.text : '';
}

/** Hard-coded user-visible copy in a component that must route every string through t(). */
export function inspectLiteralCopy(filePath, source, problems) {
  const sourceFile = ts.createSourceFile(filePath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const relative = relativeToRepository(filePath);
  const report = (node, values, where) => {
    for (const value of values) {
      problems.push(`${relative}:${lineOf(sourceFile, node)}: hard-coded copy ${JSON.stringify(value)} in ${where}; use t()`);
    }
  };
  function visit(node) {
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isPropertyAccessExpression(node.left) && copyProperties.has(node.left.name.text)) {
      report(node, literalCopy(node.right), node.left.name.text);
    } else if (ts.isPropertyAssignment(node) && copyProperties.has(propertyNameText(node.name))) {
      report(node, literalCopy(node.initializer), propertyNameText(node.name));
    } else if (ts.isCallExpression(node)) {
      const name = callName(node.expression);
      const [first, second] = node.arguments;
      if (name === 'setAttribute' && first && ts.isStringLiteral(first) && copyAttributes.has(first.text)) {
        report(node, literalCopy(second), first.text);
      } else if (copyCalls.has(name)) {
        for (const argument of node.arguments) report(node, literalCopy(argument), name);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
}

async function optionalHtmlFiles() {
  const files = [path.join(repositoryRoot, 'index.html'), path.join(repositoryRoot, 'static/admin.html')];
  const htmlDirectories = [
    path.join(repositoryRoot, 'static/settings_panels'),
    path.join(repositoryRoot, 'frontend/src/application-shell/fragments'),
  ];
  for (const directory of htmlDirectories) {
    try {
      for (const name of await fs.readdir(directory)) {
        if (name.endsWith('.html')) files.push(path.join(directory, name));
      }
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error;
    }
  }
  return files.sort();
}

export async function checkI18nUsage() {
  const catalog = await readI18nCatalog();
  const definedKeys = new Set(catalog.keys);
  const problems = [];
  for (const filePath of await sourceFiles(frontendRoot)) {
    inspectScript(
      filePath,
      await fs.readFile(filePath, 'utf8'),
      definedKeys,
      problems,
      catalog.paramsByKey,
    );
  }
  for (const relative of literalCopyFiles) {
    const filePath = path.join(repositoryRoot, relative);
    inspectLiteralCopy(filePath, await fs.readFile(filePath, 'utf8'), problems);
  }
  for (const filePath of await optionalHtmlFiles()) {
    try {
      inspectDataAttributes(
        filePath, await fs.readFile(filePath, 'utf8'), definedKeys, problems,
      );
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error;
    }
  }
  return { catalog, problems };
}

async function main() {
  const { catalog, problems } = await checkI18nUsage();
  if (problems.length) {
    process.stderr.write(
      `i18n usage contract failed (${problems.length} problem(s)):\n`
      + `${problems.sort().join('\n')}\n`,
    );
    process.exitCode = 1;
  } else {
    process.stdout.write(
      `i18n usage verified (${catalog.keys.length} declared keys; TS/JS/HTML closed world)\n`,
    );
  }
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (invokedPath === import.meta.url) {
  await main();
}
