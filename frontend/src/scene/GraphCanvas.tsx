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
import { separateOverlaps } from './overlap.ts'
import { buildGraphLayout, buildGraphStyle, FIT_PADDING, INITIAL_ZOOM } from './style.ts'

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

    // The busiest file is the one the opening camera aims at — on a real
    // repository it is the entry point (`index.js` and friends), which is where
    // someone reading the graph for the first time wants to start.
    let maxDegree = 0
    let hubId: string | null = null
    for (const node of nodes) {
      const degree = (node.imports ?? 0) + (node.importedBy ?? 0)
      if (degree > maxDegree) {
        maxDegree = degree
        hubId = node.id
      }
    }
    const layout = buildGraphLayout(nodes.length, edges.length)

    const cy = cytoscape({
      container,
      elements,
      style: buildGraphStyle(edges.length, maxDegree),
      maxZoom: 4,
      minZoom: 0.05,
      // Cytoscape's default is 1. 0.3 was chosen for a smooth analog feel and
      // overshot: crossing the useful zoom range took an unreasonable amount of
      // scrolling. 0.8 keeps the wheel finer-grained than the default without
      // making the range a chore to traverse.
      wheelSensitivity: 0.8,
      // Marquee-selects a region and drags it. Nothing in this app consumes a
      // multi-node selection — `selectedId` is a single id — so the gesture
      // only ever produced accidental bulk moves.
      boxSelectionEnabled: false,
    })
    cyRef.current = cy

    // Belt to the `events: 'no'` braces in `style.ts`. That rule stops the
    // pointer reaching a directory box at all, which is the real guard; this
    // clears the grabbable flag too, so the "dragging invisible empty canvas
    // moves a whole subtree" behaviour cannot come back through a stylesheet
    // edit alone.
    cy.nodes('.directory').ungrabify()

    cy.on('tap', 'node', (event) => onSelectRef.current(event.target.id()))
    cy.on('tap', (event) => {
      if (event.target === cy) onSelectRef.current(null)
    })

    // Hover is pointer feedback only. It never touches `selected` / `neighbor`
    // / `faded` — those are recomputed from scratch by the selection effect
    // below — so a hover in progress can never leave stale state behind after
    // a selection change, and a selection change can never fight a hover.
    //
    // `hovered` is kept a singleton by clearing it everywhere before setting
    // it, and cleared again when the pointer leaves the container: `mouseout`
    // is not guaranteed to fire for every `mouseover` (a fast drag off the
    // canvas, a node moving out from under a stationary pointer), and a missed
    // one left a node wearing the blue hover halo permanently.
    cy.on('mouseover', 'node', (event) => {
      cy.nodes('.hovered').removeClass('hovered')
      event.target.addClass('hovered')
    })
    cy.on('mouseout', 'node', (event) => event.target.removeClass('hovered'))
    const clearHover = () => cy.nodes('.hovered').removeClass('hovered')
    container.addEventListener('pointerleave', clearHover)

    // Fit first to learn the zoom the whole graph would need, then decide
    // whether that zoom is legible. See `INITIAL_ZOOM` in `style.ts`.
    const openCamera = () => {
      cy.fit(undefined, FIT_PADDING)
      const hub = hubId === null ? null : cy.getElementById(hubId)
      if (hub === null || hub.empty()) return

      const zoom = Math.min(Math.max(cy.zoom(), INITIAL_ZOOM.MIN), INITIAL_ZOOM.MAX)
      cy.zoom(zoom)
      cy.center(hub)
    }

    // Runs once, between the layout settling and the camera being aimed, so
    // the opening frame is already separated — see `overlap.ts` for why fCoSE
    // cannot be tuned into guaranteeing this on its own.
    //
    // Deliberately synchronous. An earlier version deferred this through
    // `requestAnimationFrame`, which is not delivered at all while the document
    // is hidden — so a graph that finished laying out in a background tab was
    // never separated and never aimed.
    const settle = () => {
      const remaining = separateOverlaps(cy)
      if (remaining > 0) {
        // Counts only; a node id is a repository path and does not go to a log.
        console.warn(`graph: ${remaining} node pair(s) still overlapping after separation`)
      }
      openCamera()
    }

    // The layout is deliberately not passed to the constructor. React commits
    // this subtree and runs the effect before the flex chain above it has a
    // resolved height, so Cytoscape would size its canvases to 0x0 and lay the
    // graph out against an empty viewport.
    //
    // It is equally deliberate that the `ResizeObserver` below is *not* the
    // only thing that can start it. It used to be, and that was a real bug with
    // an ugly presentation: **a hidden document delivers no `ResizeObserver`
    // callbacks at all**, not even the initial one it fires on `observe()`. Load
    // a repository, switch tabs while the 7 s analysis runs, and the canvas
    // mounts hidden — the observer never fires, fCoSE never runs, and what
    // renders is Cytoscape's *default* layout, a mechanical grid lattice. It
    // does not self-correct on return, because by then the size has not changed
    // and the observer has nothing to report. Verified directly:
    // `document.hidden === true` with a 368x767 container produced zero
    // observer callbacks in 1.5 s.
    //
    // So: if the container already has a box when the effect runs — the common
    // case, since React has committed the whole tree by then — lay out
    // immediately and synchronously, with no dependency on any frame or
    // observer callback being delivered. The observer remains the fallback for
    // the genuinely-unsized case that motivated it, and owns refits after that.
    //
    // Two guards keep that refit from turning into the "camera feels random"
    // behavior this used to have. First, `lastWidth`/`lastHeight` and
    // `RESIZE_EPSILON`: a `ResizeObserver` fires for sub-pixel jitter (a
    // scrollbar appearing, a font metric settling) that is not a real size
    // change, and calling `cy.fit()` on every firing turned that jitter into
    // a visible camera nudge. Only a change bigger than the epsilon re-fits.
    // Second, the fit is deliberately not synchronous with the observer
    // callback: `ResizeObserver` can fire while the browser is still
    // mid-reflow, so `cy.resize()` — which reads the container's current box
    // — and `cy.fit()` — which computes a pan/zoom transform from what
    // `resize()` just read — can end up computed against a box the browser
    // hasn't finished committing. Deferring one frame gives the browser a
    // chance to finish committing before Cytoscape recomputes the transform.
    let laidOut = false
    let lastWidth = 0
    let lastHeight = 0
    const RESIZE_EPSILON = 2

    const runInitialLayout = (width: number, height: number) => {
      if (laidOut) return
      laidOut = true
      lastWidth = width
      lastHeight = height
      cy.resize()
      const run = cy.layout(layout)
      run.one('layoutstop', settle)
      run.run()
    }

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry === undefined) return
      const { width, height } = entry.contentRect
      if (width === 0 || height === 0) return

      if (!laidOut) {
        runInitialLayout(width, height)
        return
      }

      const changed = Math.abs(width - lastWidth) > RESIZE_EPSILON || Math.abs(height - lastHeight) > RESIZE_EPSILON
      if (!changed) return
      lastWidth = width
      lastHeight = height

      requestAnimationFrame(() => {
        cy.resize()
        cy.fit(undefined, FIT_PADDING)
      })
    })
    observer.observe(container)

    // Not `else` — `observe()` may already have queued a callback. Whichever
    // path gets there first wins; `laidOut` makes the other a no-op.
    if (container.clientWidth > 0 && container.clientHeight > 0) {
      runInitialLayout(container.clientWidth, container.clientHeight)
    }

    return () => {
      observer.disconnect()
      container.removeEventListener('pointerleave', clearHover)
      cy.destroy()
      cyRef.current = null
    }
  }, [nodes, edges])

  // `selectedId` is the one source of truth for selection-derived styling.
  // Every run clears all four classes first and then re-derives them from the
  // current selection alone — never by toggling a class on top of whatever
  // was already there — so there is no path for a node to be left in a
  // stale, half-updated state (the "colors disappear after clicking around"
  // bug this used to have). Base node color (`data(color)`, the directory
  // tint) is never touched here; only classes layer on top of it.
  useEffect(() => {
    const cy = cyRef.current
    if (cy === null) return

    cy.batch(() => {
      cy.elements().removeClass('selected neighbor context faded')
      if (selectedId === null) return

      const node = cy.getElementById(selectedId)
      if (node.empty()) return

      // Directory nodes are excluded from all four classes. They are layout
      // hooks that ADR-026 stopped drawing, and `node.neighbor` /
      // `node.selected` set `border-width` and `overlay-opacity` — so classing
      // an ancestor (which is what the old `.union(node.ancestors())` here did)
      // brought its invisible compound box back as a bordered, blue-washed
      // rectangle straddling half the canvas. `style.ts` now also zeroes those
      // properties in a final rule; both are needed, because the fix here keeps
      // the *classes* honest and the fix there keeps the *rendering* honest.
      const drawable = cy.elements().difference(cy.nodes('.directory'))

      // Direct dependencies and dependents.
      const direct = node.closedNeighborhood().intersection(drawable)
      // One further hop out — kept visible but subdued, so the selection
      // keeps its surrounding context instead of dropping straight to faded.
      const context = direct.neighborhood().intersection(drawable).difference(direct)

      drawable.difference(direct).difference(context).addClass('faded')
      context.addClass('context')
      direct.addClass('neighbor')
      node.removeClass('neighbor').addClass('selected')
    })
  }, [selectedId])

  return <div ref={containerRef} className="h-full w-full" />
}
