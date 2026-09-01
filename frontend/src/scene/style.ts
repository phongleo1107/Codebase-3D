/**
 * Cytoscape stylesheet and layout options.
 *
 * Colors are literals rather than CSS custom properties: Cytoscape paints to a
 * `<canvas>` and never resolves `var(--x)`. The palette matches the Tailwind
 * side of the app by hand — see `src/index.css` and `ui/App.tsx`.
 *
 * PRD §6: dark, minimal, high information density, restrained animation.
 * Selection state is a small set of classes, applied imperatively by the
 * canvas from one source of truth (`selectedId`) and recomputed from scratch
 * on every change — `selected`, `neighbor` (direct dependency/dependent),
 * `context` (second-order — a neighbor of a neighbor), `faded` (everything
 * else), `hovered` (pointer only, independent of selection). Nothing here
 * ever mutates a node's base color; visual state is layered on top of it via
 * class-scoped style rules, so a node always has a well-defined appearance
 * for any combination of selection/hover state. That is PRD §7's "trace"
 * with no extra machinery.
 *
 * Visual hierarchy is deliberate: nodes are the primary read, edges are
 * secondary (low default opacity, thin, and only asserting themselves near a
 * selection), and the canvas background is tertiary — see `ui/App.tsx` for
 * the background color that sits behind this transparent canvas.
 *
 * ADR-026: directories no longer render as filled compound boxes. Import
 * edges, not directory nesting, are the dominant visual signal — files are
 * colored by `data(color)` (see `directoryColors.ts`) as a secondary grouping
 * cue instead. `parent` is still set on file nodes (`elements.ts`), so
 * directory membership still shapes the layout; it is just no longer drawn.
 */
import cytoscape, { type LayoutOptions, type StylesheetJson } from 'cytoscape'
import fcose, { type FcoseLayoutOptions } from 'cytoscape-fcose'

cytoscape.use(fcose)

export const COLORS = {
  // A charcoal, not a near-black: dark enough to read as a developer tool,
  // light enough that node fills, borders, and labels have real contrast to
  // sit against. `ui/App.tsx` paints this behind the (transparent) canvas.
  canvas: '#161b22',
  fileBorder: '#3d444d',
  edge: '#30363d',
  edgeContext: '#39434e',
  label: '#9198a1',
  accent: '#58a6ff',
  accentDim: '#316dca',
  selected: '#e6edf3',
} as const

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function lerp(from: number, to: number, t: number): number {
  return from + (to - from) * clamp(t, 0, 1)
}

/**
 * Edges are secondary information (see file header). At a few hundred edges a
 * flat opacity reads fine; past a few thousand the same opacity turns every
 * dense cluster into a solid wash of lines. Scaling both down by edge count —
 * not node count — targets what actually causes the clutter: how many line
 * segments are competing for the same pixels.
 */
function edgeVisuals(edgeCount: number): { opacity: number; width: number } {
  const t = (edgeCount - 150) / (3000 - 150)
  return { opacity: lerp(0.35, 0.12, t), width: lerp(0.75, 0.5, t) }
}

/**
 * Builds the stylesheet for a specific graph so edge and label density can
 * scale with `edgeCount`/max degree instead of using one constant for every
 * repository size (ADR-026 predates this; the shape of the rules is
 * unchanged, only `edge` and the label threshold are now computed).
 */
export function buildGraphStyle(edgeCount: number, maxDegree: number): StylesheetJson {
  const edge = edgeVisuals(edgeCount)
  // A node with more imports/importers is more likely to be what the user is
  // looking for at a glance, so its label should survive zooming out further
  // than a leaf file's. mapData needs a real (non-zero) upper bound.
  const degreeCeiling = Math.max(maxDegree, 1)

  return [
    {
      selector: 'node',
      style: {
        label: 'data(label)',
        color: COLORS.label,
        'font-family': 'ui-monospace, SFMono-Regular, Menlo, monospace',
        'font-size': 10,
        'font-weight': 500,
        'text-valign': 'center',
        'text-halign': 'center',
        'min-zoomed-font-size': `mapData(degree, 0, ${degreeCeiling}, 9, 4)`,
        'transition-property': 'opacity, border-color, border-width, overlay-opacity',
        'transition-duration': 120,
      },
    },
    {
      // A fixed dot with the name set beside it, rather than a box sized to its
      // label. `width: 'label'` is deprecated in Cytoscape 3.34 with no
      // replacement enum, and a uniform node size is the denser read anyway:
      // the eye picks up the graph's shape instead of filename lengths. Color
      // is `data(color)` — the directory-tint cue (ADR-026) — not a flat
      // constant, so files are the primary read and their directory is a
      // secondary one. Size is a mild function of `data(degree)` (import count,
      // both directions — see `elements.ts`) so busier files read as slightly
      // more prominent without every node becoming huge.
      selector: 'node.file',
      style: {
        shape: 'ellipse',
        width: 'mapData(degree, 0, 14, 9, 20)',
        height: 'mapData(degree, 0, 14, 9, 20)',
        'background-color': 'data(color)',
        'border-width': 1.5,
        'border-color': COLORS.fileBorder,
        'text-halign': 'right',
        'text-margin-x': 6,
        'font-size': 9,
        // A soft, always-on halo in the node's own tint — Obsidian's points
        // read as small light sources rather than flat dots. Kept faint
        // enough to disappear once a few dozen nodes overlap on screen.
        'overlay-opacity': 0.05,
        'overlay-color': 'data(color)',
        'overlay-padding': 3,
        'overlay-shape': 'ellipse',
      },
    },
    {
      // Compound node: still groups its children for fCoSE's layout (ADR-026
      // keeps `parent` flowing through for exactly this), but is not drawn as a
      // box. Kept as a zero-footprint layout hook rather than deleted from the
      // element set, so a directory-tint legend or collapse/expand can attach
      // to it later without another rendering pass.
      selector: 'node.directory',
      style: {
        shape: 'round-rectangle',
        'background-opacity': 0,
        'border-width': 0,
        label: '',
        padding: '10px',
      },
    },
    {
      selector: 'edge',
      style: {
        // Edges are not pointer targets. A hairline at 12-35% opacity is
        // effectively invisible as a hit target, but Cytoscape still routes
        // the grab to it, so a drag that happens to start on one does nothing
        // instead of panning the canvas. Nothing in this app has an edge
        // interaction to lose — selection is driven by nodes only.
        events: 'no',
        'curve-style': 'bezier',
        width: edge.width,
        opacity: edge.opacity,
        'line-color': COLORS.edge,
        'target-arrow-color': COLORS.edge,
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.5,
        'transition-property': 'opacity, line-color, width',
        'transition-duration': 120,
      },
    },
    {
      // Pointer feedback only — independent of selection, so hovering never
      // fights with `selected`/`neighbor`/`faded` for the same property.
      selector: 'node.hovered',
      style: {
        'overlay-opacity': 0.16,
        'overlay-color': COLORS.accent,
        'overlay-padding': 5,
      },
    },
    {
      selector: 'node.context',
      style: { opacity: 0.7 },
    },
    {
      selector: 'edge.context',
      style: { opacity: 0.22, 'line-color': COLORS.edgeContext, 'target-arrow-color': COLORS.edgeContext },
    },
    {
      selector: 'node.neighbor',
      style: {
        'border-color': COLORS.accentDim,
        color: '#c9d1d9',
        'border-width': 2,
        'overlay-opacity': 0.12,
        'overlay-color': COLORS.accentDim,
        'overlay-padding': 5,
      },
    },
    {
      selector: 'edge.neighbor',
      style: {
        'line-color': COLORS.accentDim,
        'target-arrow-color': COLORS.accentDim,
        width: 1.5,
        opacity: 0.9,
        'arrow-scale': 0.7,
      },
    },
    {
      selector: 'node.selected',
      style: {
        'border-color': COLORS.accent,
        'border-width': 2.5,
        color: COLORS.selected,
        'font-size': 11,
        // Always legible regardless of current zoom — the one label that must
        // never disappear while something is selected.
        'min-zoomed-font-size': 0,
        width: 'mapData(degree, 0, 14, 14, 24)',
        height: 'mapData(degree, 0, 14, 14, 24)',
        'overlay-opacity': 0.24,
        'overlay-color': COLORS.accent,
        'overlay-padding': 7,
      },
    },
    {
      selector: 'node.faded',
      style: { opacity: 0.4 },
    },
    {
      selector: 'edge.faded',
      style: { opacity: 0.08 },
    },
    {
      // Last rule wins in Cytoscape, so this is the one that makes ADR-026's
      // "directories are not drawn" actually hold under *every* class
      // combination. It is not redundant with the `node.directory` rule above:
      // that one sits before `node.neighbor`/`node.selected`, which set
      // `border-width` and `overlay-opacity`, so an ancestor of a selected node
      // used to come back as a visible bordered rectangle with a blue wash over
      // it — the "highlight glitched" artifact. The selection effect in
      // `GraphCanvas.tsx` no longer classes directories at all; this keeps the
      // guarantee from depending on that one call site.
      selector: 'node.directory',
      style: {
        'background-opacity': 0,
        'border-width': 0,
        'overlay-opacity': 0,
        label: '',
        // A compound node's body is the whole (invisible) box its children sit
        // in, and dragging it drags every child with it — "moving a whole chunk
        // of the graph" by grabbing what looks like empty canvas. Since ADR-026
        // these boxes are a layout hook with no visual presence, so they should
        // have no pointer presence either: a drag inside one now pans.
        events: 'no',
      },
    },
  ]
}

export const FIT_PADDING = 32

/**
 * Zoom band for the opening camera (`GraphCanvas.tsx`). Fitting the whole graph
 * is the right opening shot only when the whole graph is legible at that zoom;
 * past a few hundred nodes it lands well under `MIN`, and the user opens on a
 * field of unreadable dots with the busiest file somewhere off to one side.
 * Below `MIN` the camera zooms to `MIN` and centers the highest-degree node
 * instead. `MAX` keeps a ten-node repository from opening comically magnified.
 */
export const INITIAL_ZOOM = { MIN: 0.55, MAX: 1.2 } as const

/**
 * `fcose` replaces the built-in `cose` (ADR-026): it treats compound nesting
 * as a soft constraint on an otherwise import-driven layout rather than the
 * dominant signal, and it scales to this project's real ceiling
 * (`MAX_NODES`/`MAX_EDGES` in `api/limits.ts`) rather than a ten-node fixture.
 * `nestingFactor` is well below `cose`'s former 1.1 for the same reason: files
 * should cluster on who imports whom, with directory membership as a mild
 * nudge rather than the layout's main organizing force.
 *
 * The four force/iteration knobs below used to be flat constants tuned by eye
 * against one repository. That is the actual mechanism behind "a 30-node
 * repository and a 2,000-node repository can't share a layout": fixed
 * `nodeRepulsion`/`idealEdgeLength` give every node the same amount of
 * personal space regardless of how many nodes are asking for space, so a
 * small graph looks sparse and a large one looks compressed. `buildGraphLayout`
 * scales both by node count (`Math.sqrt` so the growth is sub-linear — the
 * canvas shouldn't blow up 10x in size for a 10x-larger repository, just
 * enough to keep density roughly constant), trims `numIter` and drops to
 * `draft` quality (spectral placement only, no incremental cooling pass) past
 * a size where the full incremental solve would noticeably delay first paint,
 * and scales `gravity` with node count.
 *
 * `gravity`'s direction was wrong until it was measured (ADR-027's correction).
 * The reasoning above — ease it *down* so a large graph can breathe — sounds
 * right and produced the worst layouts of anything tried. Gravity is what stops
 * a force layout drifting into a sparse, stringy sprawl, and a *small* graph
 * needs more of it, not less: with 141 file nodes, raising it from 0.265 to
 * 0.6-0.9 lifted the fraction of the bounding box the middle 90% of nodes
 * actually occupy from 0.32 to 0.43 and roughly halved the area, while
 * dropping residual overlaps to zero. On a 2,953-node graph gravity barely
 * moves the bounding box at all, but raising it *increases* residual overlaps
 * (9,180 -> 14,088), because it compresses a graph that is already as dense as
 * the separation pass can cope with. So it scales down with node count, from
 * the opposite end of the range it used to.
 */
export function buildGraphLayout(nodeCount: number, edgeCount: number): LayoutOptions {
  const scale = Math.sqrt(Math.max(nodeCount, 1) / 150)
  const density = edgeCount / Math.max(nodeCount, 1)

  const fcoseLayout: FcoseLayoutOptions = {
    name: 'fcose',
    quality: nodeCount > 1200 ? 'draft' : nodeCount > 250 ? 'default' : 'proof',
    animate: false,
    fit: true,
    padding: FIT_PADDING,
    nodeDimensionsIncludeLabels: true,
    tile: true,
    // Measured against `expressjs/express` in a browser, comparing bounding-box
    // span, fit zoom, and post-separation overlaps across settings. Pushing
    // these higher is counter-productive: 12000/110 blew the span from
    // 3385x1726 to 4305x4465 — halving the zoom you can fit the graph at — and
    // still left 14 overlapping pairs, because a too-long ideal edge scatters
    // clusters into each other's space faster than repulsion clears them.
    //
    // These no longer carry sole responsibility for preventing overlap;
    // `separateOverlaps` does that afterwards, deterministically. Their job is
    // the graph's *shape*, which is what force-directed layout is good at.
    idealEdgeLength: () => clamp(90 * Math.max(scale, 1) + density * 8, 80, 260),
    nodeRepulsion: () => clamp(8000 * scale, 6000, 45_000),
    nestingFactor: 0.3,
    gravity: clamp(0.9 - nodeCount / 3000, 0.1, 0.9),
    // Explicit, because the obvious "improvement" here is a trap. fCoSE
    // randomizes its initial placement by default, so the same repository laid
    // out twice gives two different pictures, and they are not equally good:
    // across five runs of `expressjs/express` the bounding-box area varied by
    // 1.9x and the aspect ratio from 1.16 to 2.4.
    //
    // `randomize: false` looks like the fix and is not usable here. It does not
    // mean "seed deterministically", it means "start from the positions the
    // nodes already have" — and this graph is built fresh, so every node is at
    // (0, 0). fCoSE handles that degenerate seed by producing a partial layout
    // and never emitting `layoutstop`, so the one callback that runs overlap
    // separation and aims the camera never fires. Getting determinism this way
    // would require seeding real initial positions first, which is a bigger
    // change than the variance justifies.
    randomize: true,
    // fCoSE packs disconnected components (test fixtures, examples, standalone
    // scripts) against the main graph with a spacing it does not expose as an
    // option, so the only lever on how tightly they read is the repulsion above.
    // Turning packing off was measured and is worse — it raised the aspect
    // ratio to 2.4-2.8 without shrinking the area.
    numIter: nodeCount > 1200 ? 1200 : nodeCount > 400 ? 1800 : 2500,
  }

  // @types/cytoscape-fcose declares `ready`/`stop` as `LayoutHandler | undefined`
  // where cytoscape's own `BaseLayoutOptions` declares them as `LayoutHandler`
  // (no `| undefined`) — neither side is wrong, but `exactOptionalPropertyTypes`
  // treats the two as incompatible even though this object sets neither
  // property. Widening the return to `cytoscape`'s own `LayoutOptions` is the
  // boundary where that third-party type mismatch gets absorbed, so
  // `GraphCanvas.tsx`'s `cy.layout(...)` call needs no cast of its own.
  return fcoseLayout as LayoutOptions
}
