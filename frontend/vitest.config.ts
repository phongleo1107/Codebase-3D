import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * Separate from `vite.config.ts` on purpose. Vitest prefers this file when it
 * exists and then ignores `vite.config.ts` entirely, which is what we want: the
 * app config carries a dev-server proxy and the Tailwind plugin, neither of
 * which a jsdom test run has any use for. The React plugin is here because the
 * tests are `.tsx`.
 *
 * `environment: 'jsdom'` rather than `happy-dom`. Both were considered; jsdom
 * was tried first and works, and it is the one that matters for
 * `ui/ComponentDiagram.test.tsx`, whose whole subject is XML parsing and
 * namespaces: jsdom parses `image/svg+xml` with a real namespace-aware XML
 * parser and emits a `<parsererror>` document on malformed input, which is the
 * exact behaviour ADR-025's step 2 depends on.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    restoreMocks: true,
  },
})
