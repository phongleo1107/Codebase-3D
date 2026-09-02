/**
 * Application shell.
 *
 * Landing page (URL input) when nothing has been analyzed yet, the graph
 * view once a response has landed, and a loading/error state in between —
 * `analyzeRepository` (src/api/client.ts) is a real `POST /api/analyze` call,
 * not the `graph/fixture.ts` seed this used to render.
 */
import { useState, type FormEvent } from 'react'

import { analyzeRepository } from '../api/client.ts'
import { GraphCanvas } from '../scene/GraphCanvas.tsx'
import { useGraphStore, useSelectedNode } from '../store/graphStore.ts'
import { Inspector } from './Inspector.tsx'
import { Legend } from './Legend.tsx'

type Status = 'idle' | 'loading' | 'error'

export function App() {
  const response = useGraphStore((state) => state.response)
  const selectedId = useGraphStore((state) => state.selectedId)
  const select = useGraphStore((state) => state.select)
  const setResponse = useGraphStore((state) => state.setResponse)
  const selectedNode = useSelectedNode()

  const [url, setUrl] = useState('')
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const repositoryUrl = url.trim()
    if (repositoryUrl === '' || status === 'loading') return

    setStatus('loading')
    setError(null)
    try {
      const result = await analyzeRepository(repositoryUrl)
      setResponse(result)
      setStatus('idle')
    } catch (err) {
      setStatus('error')
      setError(err instanceof Error ? err.message : 'Analysis failed.')
    }
  }

  if (response === null) {
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-4 bg-[#0d1117] px-4 text-neutral-300">
        <h1 className="font-mono text-lg text-neutral-100">Codebase 2D</h1>
        <p className="max-w-md text-center text-xs text-neutral-500">
          Paste a public GitHub repository URL to graph its dependencies.
        </p>
        <form onSubmit={handleSubmit} className="flex w-full max-w-md gap-2">
          <input
            type="text"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://github.com/owner/repo"
            disabled={status === 'loading'}
            className="flex-1 rounded border border-neutral-800 bg-neutral-950 px-3 py-2 font-mono text-xs text-neutral-100 outline-none transition-colors duration-150 hover:border-neutral-700 focus:border-neutral-600 disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={status === 'loading' || url.trim() === ''}
            className="shrink-0 rounded bg-neutral-100 px-3 py-2 text-xs font-medium text-neutral-900 transition-colors duration-150 hover:bg-white disabled:opacity-40 disabled:hover:bg-neutral-100"
          >
            {status === 'loading' ? 'Analyzing…' : 'Analyze'}
          </button>
        </form>
        {status === 'error' && error !== null && (
          <p className="max-w-md text-center text-xs text-red-400">{error}</p>
        )}
      </div>
    )
  }

  const { repository, stats } = response

  return (
    <div className="flex h-dvh flex-col bg-[#0d1117] text-neutral-300">
      <header className="flex shrink-0 items-baseline gap-3 border-b border-neutral-800 px-4 py-2">
        <span className="font-mono text-sm text-neutral-100">
          {repository.owner}/{repository.name}
        </span>
        <span className="font-mono text-[10px] text-neutral-600">{repository.commitSha}</span>
        <button
          type="button"
          onClick={() => setResponse(null)}
          className="ml-auto text-[10px] uppercase tracking-wider text-neutral-500 transition-colors duration-150 hover:text-neutral-300"
        >
          Analyze another repository
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <main className="relative min-w-0 flex-1 bg-[#161b22]">
          <GraphCanvas
            nodes={response.nodes}
            edges={response.edges}
            selectedId={selectedId}
            onSelect={select}
          />
          <Legend nodes={response.nodes} />
        </main>
        <aside className="w-[min(20rem,38vw)] shrink-0 overflow-y-auto border-l border-neutral-800">
          <Inspector node={selectedNode} />
        </aside>
      </div>

      <footer className="flex shrink-0 gap-4 border-t border-neutral-800 px-4 py-1.5 font-mono text-[10px] text-neutral-600">
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
