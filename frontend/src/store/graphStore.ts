/**
 * The one place "what graph" and "what is selected" live.
 *
 * Deliberately small. The response is stored exactly as it was validated —
 * no derived copies, so there is nothing that can drift from it — and
 * `nodesById` is a selector rather than stored state for the same reason.
 */
import { create } from 'zustand'

import type { AnalyzeResponse, GraphNode } from '../api/schema.ts'

type GraphState = {
  response: AnalyzeResponse | null
  selectedId: string | null
  setResponse: (response: AnalyzeResponse | null) => void
  select: (id: string | null) => void
}

export const useGraphStore = create<GraphState>()((set) => ({
  response: null,
  selectedId: null,
  setResponse: (response) => set({ response, selectedId: null }),
  select: (id) => set({ selectedId: id }),
}))

/**
 * A linear scan rather than a memoized index: it returns a reference *into*
 * `response.nodes`, so the selector's output is referentially stable and
 * zustand does not re-render on unrelated updates. An index built here would
 * allocate on every store change to answer one lookup.
 */
export function useSelectedNode(): GraphNode | null {
  return useGraphStore((state) => {
    if (state.response === null || state.selectedId === null) return null
    return state.response.nodes.find((node) => node.id === state.selectedId) ?? null
  })
}
