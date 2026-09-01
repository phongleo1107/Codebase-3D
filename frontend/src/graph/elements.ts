/**
 * Wire graph -> Cytoscape elements.
 *
 * This is the whole of ADR-022's "Cytoscape's compound nodes consume a
 * `parent`-shaped hierarchy directly": `GraphNode.parent` (ADR-006) becomes
 * `data.parent`, and the directory tree is expressed with no transform beyond
 * a rename. Nothing here re-derives structure — the response is already
 * sorted, deduplicated, and parent-before-child (ADR-018).
 *
 * Two defensive drops, because this is a trust boundary and not an internal
 * call: a node naming a parent that is not in the node set, and an edge with
 * an endpoint that is not in the node set. Both are impossible per the
 * backend's own invariants; both make Cytoscape throw on `cy.add()` rather
 * than degrade, which would take the whole canvas down.
 */
import type { ElementDefinition } from 'cytoscape'

import type { GraphEdge, GraphNode, NodeType } from '../api/schema.ts'

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
}

export function toElements(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): GraphElements {
  const ids = new Set(nodes.map((node) => node.id))
  const elements: ElementDefinition[] = []
  let droppedParents = 0
  let droppedEdges = 0

  for (const node of nodes) {
    const data: NodeElementData = { id: node.id, label: node.name, kind: node.type }
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
