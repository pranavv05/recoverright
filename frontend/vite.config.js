import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    // Run every test file in one forked process rather than spawning a worker
    // per file.
    //
    // Why: spawning workers intermittently blew Vitest's worker-startup
    // handshake timeout on Windows — "[vitest-pool]: Failed to start forks
    // worker" / "[vitest-pool-runner]: Timeout waiting for worker to
    // respond". That failure mode is nastier than an ordinary test failure:
    // the run exits non-zero having silently skipped whole files, so the
    // coverage report then reads ~0.7% against a 95% threshold and the repo
    // looks catastrophically broken to anyone following the README quickstart
    // when nothing is actually wrong. Measured at roughly 2 failures in 8
    // runs — always a startup timeout, never an assertion failure, always
    // passing on an immediate re-run.
    //
    // Capping the pool (`maxForks: 4`) was tried first and did NOT fix it: the
    // flake recurred with 4 forks, which ruled out CPU oversubscription and
    // pointed at the per-worker startup handshake itself. One fork removes
    // that handshake entirely — there is one worker to start, so no race to
    // lose. 10+ consecutive clean runs since.
    //
    // Top-level `singleFork`, not `poolOptions.forks.singleFork`: Vitest 4
    // removed `test.poolOptions` and promoted its contents to top-level
    // options; the nested spelling still runs but prints a DEPRECATED notice.
    //
    // Three ways to serialise were measured before settling here. The two
    // rejected ones are recorded because "just use maxWorkers: 1" is the
    // obvious next suggestion and it is the wrong one:
    //   singleFork: true       ~22-30s   <- this
    //   fileParallelism: false ~84-99s
    //   maxWorkers: 1          ~100-210s
    // The slow two still tear down and re-initialise jsdom per file; only
    // singleFork shares one environment across all 14 files, and jsdom setup
    // (not test execution) dominates this suite's wall clock. So this is both
    // the stable option and the fast one — revisit only if the suite grows
    // enough for serial execution to stop being cheap.
    pool: 'forks',
    singleFork: true,
    // Component and unit tests only. Everything under src/test/ that is not a
    // *.test.jsx is a fixture or a helper, not a suite.
    include: ['src/**/*.test.{js,jsx}'],
    restoreMocks: true,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: ['src/**/*.{js,jsx}'],
      exclude: ['src/main.jsx', 'src/test/**', 'src/**/*.test.{js,jsx}'],
      // The dashboard is the only surface a judge or a merchant ever touches,
      // so the bar here is deliberately not "some coverage exists".
      thresholds: {
        statements: 95,
        branches: 90,
        functions: 95,
        lines: 95,
      },
    },
  },
})
