import { defineConfig } from 'vitest/config';

// Vitest owns co-located unit tests for pure TypeScript modules under
// frontend/src (new behavior lands as *.test.ts next to its owner). The
// legacy node --test *.test.cjs harnesses keep their own npm scripts; this
// runner deliberately does not pick them up.

// Wall-clock limits belong to the repository's per-test hang guard, the same
// one pytest applies (pyproject.toml `timeout`; docs/TESTING_STRATEGY.md "Tests
// never encode host speed"). Vitest's 5 s default failed healthy specs whose
// module imports ran on a loaded host.
const PER_TEST_HANG_GUARD_MS = 300_000;

export default defineConfig({
  test: {
    include: ['frontend/src/**/*.test.ts'],
    environment: 'node',
    watch: false,
    testTimeout: PER_TEST_HANG_GUARD_MS,
    hookTimeout: PER_TEST_HANG_GUARD_MS,
  },
});
