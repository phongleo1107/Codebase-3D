/**
 * The Cytoscape canvas.
 *
 * State discipline (docs/ARCHITECTURE.md, "State discipline", re-read for 2D):
 * React owns *what graph* and *what is selected*; Cytoscape owns what it looks
 * like right now. The graph is built once per response and torn down with it;
 * selection is applied by toggling classes through a ref, so changing the
 * selection never re-mounts the instance or re-runs the layout.
 *
 * Nothing in here renders repository text into the DOM. Node labels go to a
 * `<canvas>` via Cytoscape's `label` style property, which draws glyphs and
 * parses no markup. Descriptions and summaries are the inspector's job, as
 * React text nodes.
 */
import cytoscape, { type Core } from 'cytoscape'
import { useEffect, useRef } from 'react'

import type { GraphEdge, GraphNode } from '../api/schema.ts'
import { toElements } from '../graph/elements.ts'
import { FIT_PADDING, GRAPH_LAYOUT, GRAPH_STYLE } from './style.ts'

type GraphCanvasProps = {
  nodes: readonly GraphNode[]
  edges: readonly GraphEdge[]
  selectedId: string | null
  onSelect: (id: string | null) => void
}

export function GraphCanvas({ nodes, edges, selectedId, onSelect }: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)

  // Held in a ref so a new callback identity does not rebuild the graph.
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect

  useEffect(() => {
    const container = containerRef.current
    if (container === null) return

    const { elements, droppedParents, droppedEdges } = toElements(nodes, edges)
    if (droppedParents > 0 || droppedEdges > 0) {
      // Counts only — a path is repository content and does not go to a log.
      console.warn(
        `graph: dropped ${droppedParents} unknown parent link(s) and ${droppedEdges} dangling edge(s)`,
      )
    }

    const cy = cytoscape({
      container,
      elements,
      style: GRAPH_STYLE,
      maxZoom: 4,
      minZoom: 0.05,
    })
    cyRef.current = cy

    cy.on('tap', 'node', (event) => onSelectRef.current(event.target.id()))
    cy.on('tap', (event) => {
      if (event.target === cy) onSelectRef.current(null)
    })

    // The layout is deliberately not passed to the constructor. React commits
    // this subtree and runs the effect before the flex chain above it has a
    // resolved height, so Cytoscape would size its canvases to 0x0 and lay the
    // graph out against an empty viewport. The observer fires once on
    // `observe()` and again on every resize, so the first run with a real box
    // is what runs the layout; later ones only refit.
    let laidOut = false
    const observer = new ResizeObserver(() => {
      if (container.clientWidth === 0 || container.clientHeight === 0) return
      cy.resize()
      if (laidOut) {
        cy.fit(undefined, FIT_PADDING)
      } else {
        laidOut = true
        cy.layout(GRAPH_LAYOUT).run()
      }
    })
    observer.observe(container)

    return () => {
      observer.disconnect()
      cy.destroy()
      cyRef.current = null
    }
  }, [nodes, edges])

  useEffect(() => {
    const cy = cyRef.current
    if (cy === null) return

    cy.batch(() => {
      cy.elements().removeClass('selected neighbor faded')
      if (selectedId === null) return

      const node = cy.getElementById(selectedId)
      if (node.empty()) return

      // Direct dependencies and dependents, plus the enclosing directories, so
      // a selection never fades the box it is sitting in.
      const related = node.closedNeighborhood().union(node.ancestors())
      cy.elements().difference(related).addClass('faded')
      related.addClass('neighbor')
      node.removeClass('neighbor').addClass('selected')
    })
  }, [selectedId])

  return <div ref={containerRef} className="h-full w-full" />
}
