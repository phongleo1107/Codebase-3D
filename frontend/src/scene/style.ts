/**
 * Cytoscape stylesheet and layout options.
 *
 * Colors are literals rather than CSS custom properties: Cytoscape paints to a
 * `<canvas>` and never resolves `var(--x)`. The palette matches the Tailwind
 * side of the app by hand — see `src/index.css`.
 *
 * PRD §6: dark, minimal, high information density, restrained animation.
 * Selection state is three classes, applied imperatively by the canvas —
 * `selected`, `neighbor` (a direct dependency or dependent), `faded`
 * (everything else). That is PRD §7's "trace" with no extra machinery.
 */
import type { LayoutOptions, StylesheetJson } from 'cytoscape'

export const COLORS = {
  canvas: '#0b0d10',
  file: '#1c2128',
  fileBorder: '#30363d',
  directory: '#0f1318',
  directoryBorder: '#22282f',
  edge: '#2b3138',
  label: '#8b949e',
  directoryLabel: '#6e7681',
  accent: '#58a6ff',
  accentDim: '#316dca',
} as const

export const GRAPH_STYLE: StylesheetJson = [
  {
    selector: 'node',
    style: {
      label: 'data(label)',
      color: COLORS.label,
      'font-family': 'ui-monospace, SFMono-Regular, Menlo, monospace',
      'font-size': 10,
      'text-valign': 'center',
      'text-halign': 'center',
      'min-zoomed-font-size': 7,
    },
  },
  {
    // A fixed dot with the name set beside it, rather than a box sized to its
    // label. `width: 'label'` is deprecated in Cytoscape 3.34 with no
    // replacement enum, and a uniform node size is the denser read anyway:
    // the eye picks up the graph's shape instead of filename lengths.
    selector: 'node.file',
    style: {
      shape: 'ellipse',
      width: 11,
      height: 11,
      'background-color': COLORS.file,
      'border-width': 1,
      'border-color': COLORS.fileBorder,
      'text-halign': 'right',
      'text-margin-x': 5,
      'font-size': 9,
    },
  },
  {
    // Compound node: a directory drawn as the box its children sit inside.
    selector: 'node.directory',
    style: {
      shape: 'round-rectangle',
      'background-color': COLORS.directory,
      'background-opacity': 0.85,
      'border-width': 1,
      'border-color': COLORS.directoryBorder,
      color: COLORS.directoryLabel,
      'font-size': 11,
      'text-valign': 'top',
      'text-halign': 'center',
      'text-margin-y': -4,
      padding: '14px',
    },
  },
  {
    selector: 'edge',
    style: {
      'curve-style': 'bezier',
      width: 1,
      'line-color': COLORS.edge,
      'target-arrow-color': COLORS.edge,
      'target-arrow-shape': 'triangle',
      'arrow-scale': 0.6,
    },
  },
  {
    selector: '.neighbor',
    style: { 'border-color': COLORS.accentDim, color: COLORS.label },
  },
  {
    selector: 'edge.neighbor',
    style: { 'line-color': COLORS.accentDim, 'target-arrow-color': COLORS.accentDim, width: 1.5 },
  },
  {
    selector: 'node.selected',
    style: { 'border-color': COLORS.accent, 'border-width': 2, color: '#e6edf3' },
  },
  {
    selector: '.faded',
    style: { opacity: 0.18 },
  },
]

/**
 * A built-in layout, chosen for exactly that reason: `cose` is compound-aware
 * and ships inside Cytoscape, so it adds no dependency. ARCHITECTURE.md leaves
 * the final pick open between `cola`, `elk`, and `dagre` — all three are
 * separate packages, and picking one is a dependency decision that belongs
 * with real repository-sized graphs to measure against, not with a
 * ten-node fixture.
 */
export const FIT_PADDING = 24

export const GRAPH_LAYOUT: LayoutOptions = {
  name: 'cose',
  animate: false,
  fit: true,
  padding: FIT_PADDING,
  nodeDimensionsIncludeLabels: true,
  idealEdgeLength: () => 90,
  nodeRepulsion: () => 9000,
  nestingFactor: 1.1,
  gravity: 0.4,
  numIter: 1500,
}
