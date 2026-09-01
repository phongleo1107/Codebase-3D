/**
 * ADR-025 states a property of the whole frontend, not just of one component:
 * "There is no `dangerouslySetInnerHTML` here and no `innerHTML` either", and
 * `ComponentDiagram.tsx`'s own comment widens it to "no
 * `dangerouslySetInnerHTML`, here or anywhere in `src/`".
 *
 * That is a claim about source text, so it is checked against source text. A
 * test that mounts components can only prove the sinks are absent from the paths
 * it happens to exercise; this proves they are absent full stop, which is the
 * form the ADR asserts it in.
 *
 * Sources are read through Vite's `?raw` glob rather than `node:fs` on purpose:
 * `tsconfig.json` sets `types: ["vite/client"]`, so Node's typings are not in
 * scope and an `import ... from 'node:fs'` fails `npm run typecheck` even though
 * it runs fine. This stays inside the types the project already declares.
 */
import { describe, expect, it } from 'vitest'

const SOURCES: Record<string, string> = import.meta.glob('./**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
})

/**
 * `ComponentDiagram.test.tsx` is exempt because it deliberately uses `innerHTML`
 * to reproduce what Mermaid's DOMPurify pass does to a serialized SVG — the one
 * place the sink is the subject rather than a risk. This file is exempt because
 * it quotes the patterns it searches for.
 */
const EXEMPT = ['./ui/ComponentDiagram.test.tsx', './no-markup-sinks.test.ts']

/**
 * Each pattern matches the sink's *syntactic form* — assigned to, or called —
 * rather than the bare name. `ComponentDiagram.tsx`, `Inspector.tsx` and
 * `ServiceMap.tsx` all discuss these APIs at length in their header comments,
 * and a bare-name match reports every one of those sentences. Requiring `=`, `:`
 * or `(` after the name separates `el.innerHTML = x` and
 * `dangerouslySetInnerHTML={{...}}` from prose about them.
 */
const SINKS: ReadonlyArray<readonly [string, RegExp]> = [
  ['dangerouslySetInnerHTML', /\bdangerouslySetInnerHTML\s*[=:]/],
  ['innerHTML', /\binnerHTML\s*[=:]/],
  ['outerHTML', /\bouterHTML\s*[=:]/],
  ['insertAdjacentHTML', /\binsertAdjacentHTML\s*\(/],
  ['document.write', /\bdocument\s*\.\s*write\s*\(/],
]

describe('src/ contains no markup sink', () => {
  const files = Object.entries(SOURCES).filter(([path]) => !EXEMPT.includes(path))

  it('finds source files to check at all', () => {
    // Guards against the glob silently matching nothing and passing vacuously.
    expect(files.length).toBeGreaterThan(8)
    expect(files.map(([path]) => path)).toContain('./ui/ComponentDiagram.tsx')
    expect(files.map(([path]) => path)).toContain('./ui/Inspector.tsx')
  })

  it.each(SINKS.map(([name, pattern]) => [name, pattern] as const))(
    'uses no %s',
    (_name, pattern) => {
      const offenders = files.filter(([, source]) => pattern.test(source)).map(([path]) => path)
      expect(offenders).toEqual([])
    },
  )

  it('would notice a sink if one were added', () => {
    // The patterns are narrow enough to miss prose; this checks they are not so
    // narrow that they miss code. Without it, all five could be broken regexes
    // and the suite would still be green.
    const written = [
      'el.innerHTML = markup',
      'el.outerHTML=markup',
      '<div dangerouslySetInnerHTML={{ __html: markup }} />',
      'el.insertAdjacentHTML("beforeend", markup)',
      'document.write(markup)',
    ]
    for (const sink of written) {
      expect(SINKS.some(([, pattern]) => pattern.test(sink))).toBe(true)
    }

    // ...and that the prose in the real files still does not trip them.
    const prose = 'There is no `dangerouslySetInnerHTML` here and no `innerHTML` either.'
    expect(SINKS.some(([, pattern]) => pattern.test(prose))).toBe(false)
  })
})
