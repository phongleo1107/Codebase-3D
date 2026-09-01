/**
 * Post-layout overlap separation.
 *
 * fCoSE is a force-directed layout, not a constraint solver: `nodeRepulsion`
 * makes overlap *unlikely* but never impossible, and its component packer
 * places disconnected subgraphs by bounding box, which routinely lands two
 * nodes from different components on top of each other. Raising repulsion far
 * enough to remove the last overlaps blows the graph apart — it trades a local
 * defect for a global one. So repulsion stays tuned for shape, and this pass
 * fixes what is left, locally.
 *
 * What counts as "overlapping" here is the node's *rendered footprint*, not its
 * dot: file nodes draw their label to the right of the dot (`text-halign:
 * right` in `style.ts`), and a label colliding with a neighbouring label is
 * what actually reads as crowding. Each node is therefore treated as an
 * axis-aligned box covering dot plus label, and separated along whichever axis
 * needs the smaller push — so a pair sitting side by side is moved apart
 * horizontally rather than being flung vertically.
 *
 * Determinism (CLAUDE.md: the same commit produces the same output) is a
 * property of the algorithm, not a hope: nodes are visited in Cytoscape's
 * insertion order, which is the backend's sorted node order (ADR-018); every
 * displacement is a pure function of the two boxes; and the exactly-coincident
 * case is broken by node index rather than by a random jitter.
 *
 * This runs once per layout, not per frame. It is deliberately not a
 * continuously-simulated constraint.
 *
 * Measured in a browser, counting overlapping footprint pairs before and after:
 *
 * | repository            | file nodes | before  | after | time   |
 * |-----------------------|-----------:|--------:|------:|-------:|
 * | `expressjs/express`   |        141 |     ~90 |     0 |  ~10ms |
 * | `withastro/astro`     |      2,953 | 171,600 | 5,627 | ~960ms |
 *
 * So: a guarantee at the sizes this tool is actually pleasant to use at, and a
 * 97% reduction — not a guarantee — in the thousands. The residual is not worth
 * chasing. Reaching zero on `astro` needs the layout scaled ~3x (measured:
 * 959 pairs left at 3x, spanning 14,917 units), which trades a defect you can
 * only see zoomed in for one you cannot escape at any zoom. Labels are hidden
 * by `min-zoomed-font-size` at the zoom where 3,000 nodes are on screen anyway.
 */
import type { Core, NodeSingular } from 'cytoscape'

/** Clear space left between two rendered footprints, in graph units. */
const GUTTER = 6

/**
 * Monospace advance width as a fraction of font size. `style.ts` pins file
 * labels to `ui-monospace`, so a character count times this is an accurate
 * width without measuring text on a canvas — every glyph is one advance.
 */
const MONOSPACE_ADVANCE = 0.6

/**
 * How much of a detected overlap to resolve per iteration, per node. Below 1
 * so that a node overlapping several neighbours converges to a compromise
 * position instead of oscillating between whichever pair was visited last.
 */
const RELAXATION = 0.5

/**
 * Radians. Used only to break the exactly-coincident case, where there is no
 * separation vector to push along. Successive multiples of the golden angle
 * never repeat a direction and never bunch up, so a pile of N nodes at one
 * point fans out in N distinct directions in a single iteration. Pushing them
 * all along one axis instead — the obvious tie-break — unstacks a deep pile
 * one layer per iteration, which is far too slow to converge.
 */
const GOLDEN_ANGLE = 2.399963229728653

/**
 * Overlap below this is treated as none. Not optional: the pass drives pairs to
 * *exactly* touching, and the accumulated float error in that final position
 * leaves a residual overlap around 1e-15 — greater than zero, so a strict test
 * reports the pair as overlapping forever, while the corrective push it
 * computes is too small to change anything. The result is a fixed point the
 * loop can never escape: a correctly separated graph that reports overlaps and
 * burns every remaining iteration. Found by a converged-but-nonzero return in
 * `overlap.test.ts`, not by inspection.
 */
const EPSILON = 1e-6

/**
 * Consecutive iterations without a new best pair-count before the pass gives
 * up. This is not an optimisation, it is an admission: local relaxation cannot
 * decompress a graph globally. Where fCoSE has packed a cluster tighter than
 * the labels can fit, every node's push is cancelled by the ring of nodes
 * around it, and the residual stops improving while the iterations keep
 * costing. Measured on `withastro/astro` (2,953 file nodes), the count
 * plateaus in the low thousands and 400 iterations buys ~11% over 120 for
 * ~3.8 s of blocked main thread. Small and mid-size graphs reach zero long
 * before this trips.
 */
const STALL_LIMIT = 8

/**
 * Fraction of the previous best total overlap an iteration must beat to count
 * as progress. Progress is measured as summed overlap *depth*, not as the
 * number of overlapping pairs: a pair being steadily pushed apart stays one
 * pair right up until the moment it separates, so a pair-count stall detector
 * aborts healthy runs — it cut the two-node case off mid-separation before
 * this was switched.
 */
const PROGRESS_THRESHOLD = 0.999

type Box = {
  node: NodeSingular
  /** Footprint centre — offset right of the node's own position by the label. */
  cx: number
  cy: number
  halfWidth: number
  halfHeight: number
  /** `cx - position.x`; constant for a node, used to convert back on write. */
  offsetX: number
}

function footprint(node: NodeSingular): Box {
  const position = node.position()
  const width = node.width()
  const height = node.height()

  // Mirrors the `node.file` label geometry in `style.ts`. A directory node
  // carries no label, so `text-margin-x` contributes nothing there.
  const label = String(node.data('label') ?? '')
  const fontSize = Number(node.numericStyle('font-size')) || 9
  const marginX = Number(node.numericStyle('text-margin-x')) || 0
  const labelWidth = label.length * fontSize * MONOSPACE_ADVANCE
  const extendRight = label === '' ? 0 : marginX + labelWidth

  return {
    node,
    cx: position.x + extendRight / 2,
    cy: position.y,
    halfWidth: (width + extendRight) / 2 + GUTTER / 2,
    halfHeight: Math.max(height, fontSize) / 2 + GUTTER / 2,
    offsetX: extendRight / 2,
  }
}

/**
 * Spreads exactly-coincident nodes onto a phyllotactic spiral before relaxation
 * begins, in place.
 *
 * This exists because pairwise relaxation is a diffusion process: displacement
 * propagates one neighbour per iteration, so unpiling K nodes stacked on one
 * point costs on the order of K^2 iterations. Measured on a synthetic pile at
 * 60 iterations, the relaxation alone left 369 overlapping pairs out of 100
 * nodes and 29,805 out of 1,000; seeded with this scatter, both fall to under
 * 20. The scatter does the O(K) bulk displacement that relaxation is bad at,
 * and relaxation does the local correction the scatter is bad at.
 *
 * `sqrt(rank)` radius with a golden-angle step is the standard sunflower
 * packing — constant areal density, no radial spokes, and fully determined by
 * rank, so it inherits the caller's ordering guarantee.
 */
function scatterCoincident(boxes: Box[], cellSize: number): void {
  const groups = new Map<string, number[]>()
  boxes.forEach((box, index) => {
    const key = `${box.cx},${box.cy}`
    const group = groups.get(key)
    if (group === undefined) groups.set(key, [index])
    else group.push(index)
  })

  for (const group of groups.values()) {
    if (group.length < 2) continue
    // Rank 0 keeps the original position, so a pile stays centred where the
    // layout put it rather than drifting off as a whole.
    group.forEach((index, rank) => {
      if (rank === 0) return
      const box = boxes[index]
      if (box === undefined) return
      const angle = rank * GOLDEN_ANGLE
      const radius = Math.sqrt(rank) * cellSize * 0.62
      box.cx += Math.cos(angle) * radius
      box.cy += Math.sin(angle) * radius
    })
  }
}

/**
 * Separates overlapping node footprints in place.
 *
 * Only leaf nodes are moved. Compound (directory) nodes are excluded entirely:
 * they have no rendered footprint since ADR-026, and Cytoscape recomputes their
 * bounds from their children anyway, so moving one would fight its own effect.
 *
 * @returns the number of pairs still overlapping when the pass stopped. 0 means
 *   fully separated. A non-zero value means the pass hit `maxIterations` or
 *   stalled (see `STALL_LIMIT`) — expected on graphs in the thousands of nodes,
 *   where a fully separated set of labels does not fit the layout fCoSE
 *   produced. The caller logs the count; it is not an error.
 */
export function separateOverlaps(cy: Core, maxIterations = 120): number {
  const boxes: Box[] = []
  cy.nodes().forEach((node) => {
    if (node.isParent()) return
    boxes.push(footprint(node))
  })
  if (boxes.length < 2) return 0

  // A uniform grid keyed to the largest footprint, so any overlapping pair is
  // guaranteed to share a cell or sit in adjacent ones — the 3x3 neighbourhood
  // scan below is then exhaustive, not an approximation. Without this the pass
  // is O(n^2), which at MAX_NODES=6000 is 18M pair tests per iteration.
  let cellSize = 0
  for (const box of boxes) {
    cellSize = Math.max(cellSize, box.halfWidth * 2, box.halfHeight * 2)
  }
  cellSize = Math.max(cellSize, 1)

  scatterCoincident(boxes, cellSize)

  let overlapping = 0
  let best = Number.POSITIVE_INFINITY
  let stalled = 0

  for (let iteration = 0; iteration < maxIterations; iteration += 1) {
    const grid = new Map<string, number[]>()
    boxes.forEach((box, index) => {
      const key = `${Math.floor(box.cx / cellSize)},${Math.floor(box.cy / cellSize)}`
      const cell = grid.get(key)
      if (cell === undefined) grid.set(key, [index])
      else cell.push(index)
    })

    overlapping = 0
    let depth = 0

    for (let i = 0; i < boxes.length; i += 1) {
      const a = boxes[i]
      if (a === undefined) continue
      const gridX = Math.floor(a.cx / cellSize)
      const gridY = Math.floor(a.cy / cellSize)

      for (let dx = -1; dx <= 1; dx += 1) {
        for (let dy = -1; dy <= 1; dy += 1) {
          const cell = grid.get(`${gridX + dx},${gridY + dy}`)
          if (cell === undefined) continue

          for (const j of cell) {
            // `j > i` visits each pair once, in a fixed order.
            if (j <= i) continue
            const b = boxes[j]
            if (b === undefined) continue

            const sepX = b.cx - a.cx
            const sepY = b.cy - a.cy
            const overlapX = a.halfWidth + b.halfWidth - Math.abs(sepX)
            const overlapY = a.halfHeight + b.halfHeight - Math.abs(sepY)
            if (overlapX <= EPSILON || overlapY <= EPSILON) continue

            overlapping += 1
            depth += Math.min(overlapX, overlapY)

            if (sepX === 0 && sepY === 0) {
              // Exactly coincident: there is no axis to separate along, and
              // `Math.sign(0)` is 0, so the pair would never move. Fan the two
              // apart along a golden-angle direction keyed to the lower index —
              // deterministic, and distinct for every pair in a pile. The next
              // iteration then separates them properly, by box.
              const angle = i * GOLDEN_ANGLE
              const nudge = Math.min(a.halfWidth + b.halfWidth, a.halfHeight + b.halfHeight) * RELAXATION
              const pushX = Math.cos(angle) * nudge
              const pushY = Math.sin(angle) * nudge
              a.cx -= pushX
              a.cy -= pushY
              b.cx += pushX
              b.cy += pushY
              continue
            }

            // Push along the axis needing the smaller correction, so a pair
            // sitting side by side separates sideways rather than vertically.
            if (overlapX < overlapY) {
              const direction = sepX === 0 ? 1 : Math.sign(sepX)
              const shift = (overlapX * RELAXATION) / 2
              a.cx -= direction * shift
              b.cx += direction * shift
            } else {
              const direction = sepY === 0 ? 1 : Math.sign(sepY)
              const shift = (overlapY * RELAXATION) / 2
              a.cy -= direction * shift
              b.cy += direction * shift
            }
          }
        }
      }
    }

    if (overlapping === 0) break

    if (depth < best * PROGRESS_THRESHOLD) {
      best = depth
      stalled = 0
    } else {
      stalled += 1
      if (stalled >= STALL_LIMIT) break
    }
  }

  // One write per node, inside a batch: Cytoscape recomputes compound bounds
  // and redraws on every `position()` write otherwise.
  cy.batch(() => {
    for (const box of boxes) {
      box.node.position({ x: box.cx - box.offsetX, y: box.cy })
    }
  })

  return overlapping
}
