/**
 * Wire graph -> Cytoscape elements.
 *
 * `GraphNode.parent` (ADR-006) still becomes `data.parent` with no transform
 * beyond a rename — Cytoscape still groups a directory's children under it
 * for layout purposes (fCoSE reads `parent` the same way `cose` did). What
 * changed under ADR-026 is only how that hierarchy is *drawn*: directories no
 * longer render as filled compound boxes, so this module also derives
 * `tint` — a color key from the node's top-level path segment — for the
 * file-node coloring that replaced them. Nothing here re-derives structure
 * beyond that; the response is already sorted, deduplicated, and
 * parent-before-child (ADR-018).
 *
 * Two defensive drops, because this is a trust boundary and not an internal
 * call: a node naming a parent that is not in the node set, and an edge with
 * an endpoint that is not in the node set. Both are impossible per the
 * backend's own invariants; both make Cytoscape throw on `cy.add()` rather
 * than degrade, which would take the whole canvas down.
 */
import type { ElementDefinition } from 'cytoscape'

import type { GraphEdge, GraphNode, NodeType } from '../api/schema.ts'
import { tintColor, tintKey } from '../scene/directoryColors.ts'

export type GraphElements = {
  elements: ElementDefinition[]
  /** Nodes dropped because their `parent` names no known node. */
  droppedParents: number
  /** Edges dropped because an endpoint names no known node. */
  droppedEdges: number
}

export type NodeElementData = {
  id: string
  /** Cytoscape renders this on the canvas, not into the DOM. */
  label: string
  kind: NodeType
  parent?: string
  /** Top-level path segment, or `''` at the repository root (ADR-026). */
  tint: string
  /** `tint`'s resolved hex color — what the stylesheet actually paints. */
  color: string
  /**
   * `imports + importedBy` (0 on directories, which carry neither field).
   * Purely a rendering input — `style.ts` maps it to node size so busier
   * files read as mildly more prominent — never re-derived from the edge
   * set, since the wire response already counts it per node.
   */
  degree: number
}

export function toElements(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): GraphElements {
  const ids = new Set(nodes.map((node) => node.id))
  const elements: ElementDefinition[] = []
  let droppedParents = 0
  let droppedEdges = 0

  for (const node of nodes) {
    const tint = tintKey(node.path)
    const data: NodeElementData = {
      id: node.id,
      label: node.name,
      kind: node.type,
      tint,
      color: tintColor(tint),
      degree: (node.imports ?? 0) + (node.importedBy ?? 0),
    }
    if (node.parent !== null) {
      if (ids.has(node.parent)) {
        data.parent = node.parent
      } else {
        droppedParents += 1
      }
    }
    elements.push({ data, classes: node.type })
  }

  edges.forEach((edge, index) => {
    if (!ids.has(edge.source) || !ids.has(edge.target)) {
      droppedEdges += 1
      return
    }
    // Index-based, because a repository path may legally contain any byte a
    // composite "source<sep>target" id would need as a separator. Edges are
    // already deduplicated and deterministically ordered upstream, so the
    // index is stable for a given commit.
    elements.push({ data: { id: `e${index}`, source: edge.source, target: edge.target } })
  })

  return { elements, droppedParents, droppedEdges }
}
