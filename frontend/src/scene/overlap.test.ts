/**
 * Tests for the post-layout overlap separation pass.
 *
 * These run against a real Cytoscape instance in jsdom with no renderer
 * (`headless: true`), which covers the geometry: `separateOverlaps` reads
 * positions and dimensions and writes positions, and none of that needs a
 * canvas.
 *
 * Two limits, both found by running this rather than assumed. **A headless
 * instance does not resolve a stylesheet**: `node.width()`/`height()` return 1
 * regardless of what the style says, and `numericStyle('font-size')` returns
 * `undefined`, so the pass falls back to its own defaults here. That makes
 * these tests exercise the separation algorithm on a known-uniform box size —
 * which is what they are for — but it means they say nothing about whether the
 * footprint matches what `style.ts` actually paints. And as with ADR-025, this
 * is jsdom, so it is not evidence about how the result *looks*.
 */
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import { describe, expect, it } from 'vitest'

import { separateOverlaps } from './overlap.ts'

/**
 * Headless Cytoscape reports every node as 1x1 (see the file header), and the
 * pass falls back to a 9px font, so a label of L characters yields a footprint
 * `1 + L*9*0.6` wide and 9 tall, before the gutter.
 */
const HEADLESS_NODE_SIZE = 1
const FALLBACK_FONT_SIZE = 9

/**
 * `layout: preset` is load-bearing, not decoration. Constructing with
 * `elements` and no `layout` runs Cytoscape's default **grid** layout, which
 * discards the positions each definition carries — headlessly it collapses
 * every node to (0, 0). Two tests here passed vacuously before this was found:
 * they asserted on positions that had already been thrown away.
 */
function build(elements: ElementDefinition[]): Core {
  return cytoscape({ headless: true, elements, layout: { name: 'preset' } })
}

function file(id: string, x: number, y: number, label = id): ElementDefinition {
  return { data: { id, label }, position: { x, y }, classes: 'file' }
}

/** Recomputes the footprint boxes the same way the pass does, for assertions. */
function overlappingPairs(cy: Core): number {
  const boxes = cy
    .nodes()
    .toArray()
    .filter((node) => !node.isParent())
    .map((node) => {
      const label = String(node.data('label') ?? '')
      // No `text-margin-x`: it resolves to 0 without a renderer.
      const extendRight = label === '' ? 0 : label.length * FALLBACK_FONT_SIZE * 0.6
      return {
        cx: node.position('x') + extendRight / 2,
        cy: node.position('y'),
        hw: (HEADLESS_NODE_SIZE + extendRight) / 2,
        hh: Math.max(HEADLESS_NODE_SIZE, FALLBACK_FONT_SIZE) / 2,
      }
    })

  let pairs = 0
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i]
      const b = boxes[j]
      if (a === undefined || b === undefined) continue
      if (Math.abs(b.cx - a.cx) < a.hw + b.hw && Math.abs(b.cy - a.cy) < a.hh + b.hh) pairs += 1
    }
  }
  return pairs
}

describe('separateOverlaps', () => {
  it('separates two nodes stacked exactly on top of each other', () => {
    const cy = build([file('a', 0, 0), file('b', 0, 0)])
    expect(overlappingPairs(cy)).toBe(1)

    expect(separateOverlaps(cy)).toBe(0)
    expect(overlappingPairs(cy)).toBe(0)
  })

  it('leaves already-separated nodes exactly where they are', () => {
    const cy = build([file('a', 0, 0), file('b', 500, 500)])
    const before = cy.nodes().map((node) => ({ ...node.position() }))

    expect(separateOverlaps(cy)).toBe(0)
    expect(cy.nodes().map((node) => ({ ...node.position() }))).toEqual(before)
  })

  it('accounts for the label drawn to the right of the dot, not just the dot', () => {
    // The dots are 30 apart and 1px wide, so they do not touch at all — but
    // `a`'s 23-character label runs ~124px to the right, straight through `b`.
    // That collision is the one the eye actually sees, and a dot-only pass
    // would report nothing to do here.
    const cy = build([file('a', 0, 0, 'a-very-long-filename.ts'), file('b', 30, 0, 'b.ts')])
    expect(overlappingPairs(cy)).toBe(1)

    expect(separateOverlaps(cy)).toBe(0)
    expect(overlappingPairs(cy)).toBe(0)
  })

  it('resolves a dense pile of coincident nodes', () => {
    // The case the spiral pre-scatter exists for. Relaxation alone leaves 46
    // of these overlapping after the default 60 iterations, because unpiling
    // by pairwise pushes is a diffusion process — see `scatterCoincident`.
    const cy = build(Array.from({ length: 30 }, (_, i) => file(`n${i}`, 0, 0, `file${i}.ts`)))

    expect(separateOverlaps(cy)).toBe(0)
    expect(overlappingPairs(cy)).toBe(0)
  })

  it('keeps a scattered pile centred on where the layout put it', () => {
    const cy = build(Array.from({ length: 25 }, (_, i) => file(`n${i}`, 400, 200, `file${i}.ts`)))
    separateOverlaps(cy)

    const xs = cy.nodes().map((n) => n.position('x'))
    const ys = cy.nodes().map((n) => n.position('y'))
    const mean = (values: number[]) => values.reduce((sum, v) => sum + v, 0) / values.length
    // The pile spreads around its origin rather than drifting off as a block.
    expect(mean(xs)).toBeGreaterThan(300)
    expect(mean(xs)).toBeLessThan(500)
    expect(mean(ys)).toBeGreaterThan(100)
    expect(mean(ys)).toBeLessThan(300)
  })

  it('is deterministic — the same input yields byte-identical positions', () => {
    const seed = Array.from({ length: 40 }, (_, i) => file(`n${i}`, (i % 7) * 12, Math.floor(i / 7) * 8, `m${i}.ts`))

    const first = build(seed.map((e) => ({ ...e, position: { ...e.position! } })))
    const second = build(seed.map((e) => ({ ...e, position: { ...e.position! } })))
    separateOverlaps(first)
    separateOverlaps(second)

    expect(first.nodes().map((n) => n.position())).toEqual(second.nodes().map((n) => n.position()))
  })

  it('does not move compound directory nodes directly', () => {
    const cy = build([
      { data: { id: 'dir', label: '' }, classes: 'directory' },
      { data: { id: 'a', label: 'a.ts', parent: 'dir' }, position: { x: 0, y: 0 }, classes: 'file' },
      { data: { id: 'b', label: 'b.ts', parent: 'dir' }, position: { x: 0, y: 0 }, classes: 'file' },
    ])

    expect(separateOverlaps(cy)).toBe(0)
    // The children moved apart; the parent is a derived box, never written to.
    const a = cy.getElementById('a').position()
    const b = cy.getElementById('b').position()
    expect(a).not.toEqual(b)
    expect(overlappingPairs(cy)).toBe(0)
  })

  it('reports the pairs it could not resolve rather than silently claiming success', () => {
    // A single iteration cannot finish a 200-deep pile even after the scatter,
    // so the pass must return a non-zero count for the caller to log.
    const cy = build(Array.from({ length: 200 }, (_, i) => file(`n${i}`, 0, 0, `file${i}.ts`)))
    expect(separateOverlaps(cy, 1)).toBeGreaterThan(0)
  })
})
