#!/usr/bin/env node
/**
 * Checks the README's frontend test-count claim against a real Vitest run.
 *
 * The Python-side tests/test_readme_claims.py checks every other README
 * claim it can reach, including two other test counts, by literally
 * re-running the tool that produces the number rather than hand-counting.
 * The frontend's "237 Vitest ... tests" claim (in the repository-structure
 * section) is the one number that check cannot also cover: it would mean
 * shelling out to `vitest` from a Python test file that runs in a backend CI
 * job with no Node.js installed, and whose own docstring promises it runs
 * "offline in well under a second" with no external services. A check that
 * passes on a laptop and fails every time in CI is worse than the drift it
 * would catch.
 *
 * This script is the same check, written where it can actually run: in the
 * frontend's own Node/Vitest environment, wired into the frontend CI job
 * where Node already exists (see .github/workflows/ci.yml). It lives outside
 * src/, so Vitest's own `include: ['src/**\/*.test.{js,jsx}']` glob in
 * vite.config.js never picks it up — it cannot recursively spawn itself.
 *
 * Also deliberately NOT a Vitest test for the same reason: a vitest test
 * that shells out to `vitest run` to count vitest's own tests would either
 * have to exclude itself from its own count (fragile) or recurse into
 * spawning itself forever (dangerous). A plain script sidesteps both.
 */

import { execSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = path.resolve(__dirname, '..')
const PROJECT_ROOT = path.resolve(FRONTEND_ROOT, '..')
const README_PATH = path.join(PROJECT_ROOT, 'README.md')

function actualTestCount() {
  // execSync (command-string, always through a shell) rather than
  // execFileSync: `npx` is `npx.cmd` on Windows, and execFileSync's own
  // `shell: true` + array-args combination is a documented Node
  // deprecation (args aren't escaped when concatenated for the shell).
  // There is no interpolated input in this command line — it is one fixed
  // literal string — so the usual injection risk that warning exists for
  // does not apply here.
  const raw = execSync(
    'npx vitest run --reporter=json',
    { cwd: FRONTEND_ROOT, encoding: 'utf-8', maxBuffer: 1024 * 1024 * 32 },
  )
  const report = JSON.parse(raw)
  if (typeof report.numTotalTests !== 'number') {
    throw new Error(`vitest JSON reporter output had no numTotalTests field:\n${raw.slice(0, 500)}`)
  }
  return report.numTotalTests
}

function claimedTestCount(readme) {
  const match = readme.match(/(\d+)\s+Vitest\s*\+\s*React Testing Library tests/)
  if (!match) {
    throw new Error(
      'README no longer states a Vitest test count in the documented form ' +
      '("<N> Vitest + React Testing Library tests")',
    )
  }
  return parseInt(match[1], 10)
}

function main() {
  const readme = readFileSync(README_PATH, 'utf-8')
  const claimed = claimedTestCount(readme)
  const actual = actualTestCount()

  if (claimed !== actual) {
    console.error(
      `README claims ${claimed} frontend tests, the real Vitest run finds ${actual}. ` +
      'Update the "<N> Vitest + React Testing Library tests" line in README.md ' +
      '(§ Repository Structure) if a test was added or removed.',
    )
    process.exit(1)
  }

  console.log(`OK: README's claimed frontend test count (${claimed}) matches the real Vitest run.`)
}

main()
