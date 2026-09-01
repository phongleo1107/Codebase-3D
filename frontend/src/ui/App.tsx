/**
 * Application shell.
 *
 * There is no landing page and no URL input yet, because there is no
 * `POST /api/analyze` to submit to (docs/CURRENT_STATE.md: `backend/app/api/`
 * is an empty package). The store is seeded from `graph/fixture.ts` instead —
 * a hand-written response that goes through the same zod schema a real one
 * will. Replacing the seed with a fetch is the only change this file needs
 * when the endpoint lands.
 */
import { useEffect } from 'react'

import { FIXTURE_RESPONSE } from '../graph/fixture.ts'
import { GraphCanvas } from '../scene/GraphCanvas.tsx'
import { useGraphStore, useSelectedNode } from '../store/graphStore.ts'
import { Inspector } from './Inspector.tsx'

export function App() {
  const response = useGraphStore((state) => state.response)
  const selectedId = useGraphStore((state) => state.selectedId)
  const select = useGraphStore((state) => state.select)
  const setResponse = useGraphStore((state) => state.setResponse)
  const selectedNode = useSelectedNode()

  useEffect(() => {
    setResponse(FIXTURE_RESPONSE)
  }, [setResponse])

  if (response === null) return null

  const { repository, stats } = response

  return (
    <div className="flex h-dvh flex-col bg-[#0b0d10] text-neutral-300">
      <header className="flex shrink-0 items-baseline gap-3 border-b border-neutral-900 px-4 py-2">
        <span className="font-mono text-sm text-neutral-100">
          {repository.owner}/{repository.name}
        </span>
        <span className="font-mono text-[10px] text-neutral-600">{repository.commitSha}</span>
        <span className="ml-auto text-[10px] uppercase tracking-wider text-amber-600/80">
          fixture — no backend endpoint yet
        </span>
      </header>

      <div className="flex min-h-0 flex-1">
        <main className="min-w-0 flex-1">
          <GraphCanvas
            nodes={response.nodes}
            edges={response.edges}
            selectedId={selectedId}
            onSelect={select}
          />
        </main>
        <aside className="w-80 shrink-0 overflow-y-auto border-l border-neutral-900">
          <Inspector node={selectedNode} />
        </aside>
      </div>

      <footer className="flex shrink-0 gap-4 border-t border-neutral-900 px-4 py-1.5 font-mono text-[10px] text-neutral-600">
        <span>{stats.files} files</span>
        <span>{stats.directories} directories</span>
        <span>{stats.dependencies} dependencies</span>
        <span>{stats.externalImports} external</span>
        <span>{stats.unresolvedImports} unresolved</span>
        <span>{stats.skippedFiles} skipped</span>
        {stats.truncated && <span className="text-amber-600/80">truncated</span>}
      </footer>
    </div>
  )
}
