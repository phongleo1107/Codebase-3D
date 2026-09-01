/**
 * Directory-tint legend (ADR-026).
 *
 * Directories stopped being drawn as boxes on the canvas, so the "which
 * top-level directory is this file in" read moved here — ordinary React/
 * Tailwind text, not a canvas label, which is also what keeps this legible:
 * canvas labels are capped by `min-zoomed-font-size` and fixed colors, DOM
 * text just inherits normal contrast.
 */
import { useMemo } from 'react'

import type { GraphNode } from '../api/schema.ts'
import { ROOT_TINT_KEY, tintColor, tintKey } from '../scene/directoryColors.ts'

type LegendProps = {
  nodes: readonly GraphNode[]
}

export function Legend({ nodes }: LegendProps) {
  const entries = useMemo(() => {
    const keys = new Set<string>()
    for (const node of nodes) {
      if (node.type === 'file') keys.add(tintKey(node.path))
    }
    return Array.from(keys)
      .sort((a, b) => a.localeCompare(b))
      .map((key) => ({ key, label: key === ROOT_TINT_KEY ? '(root)' : key, color: tintColor(key) }))
  }, [nodes])

  if (entries.length === 0) return null

  return (
    <ul className="pointer-events-none absolute bottom-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-neutral-500">
      {entries.map((entry) => (
        <li key={entry.key} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-2 shrink-0 rounded-full"
            style={{ backgroundColor: entry.color }}
          />
          <span className="truncate">{entry.label}</span>
        </li>
      ))}
    </ul>
  )
}
