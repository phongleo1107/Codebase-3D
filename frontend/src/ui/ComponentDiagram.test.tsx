/**
 * The automated counterpart to ADR-025's interactive verification.
 *
 * ADR-025 established every claim in `ComponentDiagram.tsx` by driving Chrome
 * by hand on 2026-09-01, and recorded that as a known weakness: "the evidence
 * is real and not repeatable... automating it is now the highest-value frontend
 * test". This file is that automation. It is deliberately written to check the
 * *renderer's actual output* rather than to restate the component's intent,
 * because the thing that would invalidate ADR-025 silently is a `mermaid`
 * version bump, and only output can catch that.
 *
 * ---------------------------------------------------------------------------
 * WHAT IS STUBBED, AND WHY IT DOES NOT WEAKEN THE TEST
 * ---------------------------------------------------------------------------
 *
 * jsdom implements no SVG text metrics, so `getComputedTextLength` and
 * `getBBox` are absent and Mermaid's layout pass throws on the first label.
 * Both are stubbed below. They influence *geometry* only — coordinates, widths,
 * `transform` values — and nothing this file asserts is geometric. The element
 * set, the attribute set, the label text and the namespaces all come from the
 * real Mermaid 11.17.2 pipeline, DOMPurify pass included.
 *
 * jsdom rather than happy-dom, and jsdom was tried first and works. It is also
 * the better fit here: the subject is XML parsing and namespaces, and jsdom
 * parses `image/svg+xml` with a namespace-aware parser that emits a real
 * `<parsererror>` document, which is exactly what ADR-025's step 2 leans on.
 *
 * ---------------------------------------------------------------------------
 * ONE ENVIRONMENT BUG IS PATCHED, AND IT ALMOST PRODUCED A FALSE POSITIVE
 * ---------------------------------------------------------------------------
 *
 * `patchParse5SvgTagNames` below adds a single missing entry to the HTML
 * parser's "adjust SVG tag names" table. Read the comment on it before trusting
 * anything in this file, because it is exactly the kind of stub that can hide a
 * real defect, and the first version of this suite reported a production bug
 * that turned out to be this gap:
 *
 * Mermaid emits `<feDropShadow>` in every `theme: 'dark'` flowchart, inside the
 * filter it uses for node shadows. Its own whole-SVG DOMPurify pass — the one
 * `securityLevel: 'strict'` enables — reparses that string as HTML, and the HTML
 * parser lowercases foreign element names unless the spec's adjustment table
 * restores them. The WHATWG table *does* carry `fedropshadow` → `feDropShadow`
 * (verified against the published spec text, not recalled). parse5 8.0.1, which
 * is jsdom's parser, is missing that one row while carrying all thirty-five
 * others. So under stock jsdom the tag arrives as `fedropshadow`, misses
 * `ALLOWED_ELEMENTS`'s camelCase `'feDropShadow'`, and the panel refuses every
 * diagram — while a real browser, which implements the full table, draws it.
 *
 * The patch makes the environment conform to the specification the component is
 * written against. It is verified rather than assumed: the test below asserts
 * the gap was there and that closing it changes the parse.
 *
 * ---------------------------------------------------------------------------
 * TWO LEVELS, ON PURPOSE
 * ---------------------------------------------------------------------------
 *
 * Tests that mount the panel prove end-to-end behaviour but report only the
 * *first* thing `parseAndCheck` objects to, in document order. Tests that stub
 * `mermaid.render` feed one crafted document at a time and so can pin each
 * refusal *reason* to its intended layer without depending on iteration order.
 * ADR-025 claims both things, so both are tested.
 */
import { act, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import mermaid from 'mermaid'
// jsdom's own HTML parser, reached directly so the table below can be corrected.
// Declared as a devDependency pinned to the version jsdom resolves, so this is
// the same single instance jsdom parses with rather than a second copy.
import { foreignContent } from 'parse5'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { ComponentDiagram } from './ComponentDiagram.tsx'
import { FIXTURE_RESPONSE } from '../graph/fixture.ts'

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined
}

/**
 * Whether stock parse5 was missing the spec's `fedropshadow` row. Captured at
 * module load, before the patch, so the test that asserts it cannot be fooled by
 * its own ordering.
 */
const PARSE5_LACKED_FEDROPSHADOW =
  foreignContent.SVG_TAG_NAMES_ADJUSTMENT_MAP.get('fedropshadow') === undefined

/**
 * Add the one row parse5 8.0.1 is missing from the HTML "adjust SVG tag names"
 * table. See the header. This is a conformance fix to the *environment*, not an
 * accommodation of the component: without it jsdom disagrees with every browser
 * about the name of an element Mermaid emits on every render.
 *
 * Deliberately narrow. It adds one entry, it does not remove or rewrite any, and
 * it is a no-op if a future parse5 fixes the gap.
 */
foreignContent.SVG_TAG_NAMES_ADJUSTMENT_MAP.set('fedropshadow', 'feDropShadow')

beforeAll(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true

  const proto = globalThis.SVGElement.prototype as unknown as Record<string, unknown>
  proto['getComputedTextLength'] = function (this: Element): number {
    return (this.textContent ?? '').length * 7
  }
  proto['getBBox'] = function (this: Element) {
    return { x: 0, y: 0, width: (this.textContent ?? '').length * 7, height: 16 }
  }

  await primeComponentConfig()
})

/**
 * Mounts the panel once so that `configureOnce()` inside `ComponentDiagram.tsx`
 * runs and installs *its* Mermaid config globally. Everything `renderRaw` does
 * afterwards inherits it. See `renderRaw` for why this matters.
 */
async function primeComponentConfig(): Promise<void> {
  await renderPanel('flowchart LR\n  c0["prime"]\n')
  if (mounted !== null) {
    const root = mounted
    mounted = null
    act(() => root.unmount())
  }
  document.body.replaceChildren()
}

/**
 * The fixture's `componentDiagram`, which carries the standing markup probe
 * ADR-025 planted in label `c1`: a `<script>` and an `<img onerror=>`, neither
 * of which the backend could ever emit (`component_diagram._label` strips
 * `< > &`). It is there so this file's layer is observable.
 */
const HOSTILE_SOURCE = FIXTURE_RESPONSE.componentDiagram
if (HOSTILE_SOURCE === null || HOSTILE_SOURCE === undefined) {
  throw new Error('fixture lost its componentDiagram; this suite has nothing to test')
}
expect(HOSTILE_SOURCE).toContain('<script>alert(1)</script>')
expect(HOSTILE_SOURCE).toContain('onerror=alert(1)')

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

let mounted: Root | null = null

afterEach(() => {
  if (mounted !== null) {
    const root = mounted
    mounted = null
    act(() => root.unmount())
  }
  document.body.replaceChildren()
})

type Panel = {
  /** Everything the panel put in the live document. */
  container: HTMLElement
  /**
   * The scrolling div the effect populates — the live-DOM side of the boundary.
   * Falls back to the container for the `source === null` branch, which renders
   * a single `<p>` and no host at all.
   */
  host: HTMLElement
  /** `null` when the panel drew, otherwise the reason it gave. */
  refusal: string | null
  /** The `<svg>` adopted into the live document, if any. */
  svg: SVGSVGElement | null
}

/** Mounts the panel and waits out its async effect. */
async function renderPanel(source: string | null): Promise<Panel> {
  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)
  mounted = root

  await act(async () => {
    root.render(
      <StrictMode>
        <ComponentDiagram source={source} />
      </StrictMode>,
    )
  })
  // The effect awaits `mermaid.render`; one more flush settles the state update
  // that follows it. Mermaid's own layout does not yield beyond microtasks.
  await act(async () => {
    await Promise.resolve()
  })

  const text = container.textContent ?? ''
  const match = /refused — (.*)$/.exec(text)
  // The header row is the only other text, so the host is the last child.
  const host = (container.querySelector('div > div:last-child') ?? container) as HTMLElement

  return {
    container,
    host,
    refusal: match?.[1] ?? null,
    svg: host.querySelector('svg'),
  }
}

/** Every element in a subtree, root included. */
function allElements(root: Element): Element[] {
  return [root, ...Array.from(root.querySelectorAll('*'))]
}

/** Attribute names that can run script, by the same rule the component uses. */
function scriptableAttributes(root: Element): string[] {
  return allElements(root).flatMap((element) =>
    Array.from(element.attributes)
      .map((attribute) => attribute.name)
      .filter((name) => {
        const lower = name.toLowerCase()
        return lower.startsWith('on') || lower === 'href' || lower.endsWith(':href')
      })
      .map((name) => `${element.localName}[${name}]`),
  )
}

/**
 * Renders `source` through the real Mermaid using **the component's own
 * config**, not a copy of it.
 *
 * This deliberately does not call `mermaid.initialize`. Duplicating the
 * component's settings here would mean the raw-output tests kept passing against
 * `htmlLabels: false` after someone flipped it to `true` in
 * `ComponentDiagram.tsx` — testing a config the app no longer uses, which is
 * the exact failure ADR-025 says a version or config change would cause. Instead
 * `primeComponentConfig` mounts the panel once so its `configureOnce` runs, and
 * every raw render after that inherits whatever the component actually set.
 */
async function renderRaw(source: string): Promise<string> {
  const { svg } = await mermaid.render(`raw-${rawSequence++}`, source)
  return svg
}
let rawSequence = 0

// ---------------------------------------------------------------------------
// Layers 1–3: what Mermaid 11.17.2 itself does with hostile labels
// ---------------------------------------------------------------------------

describe("the component's own mermaid config, observed through its effects", () => {
  /**
   * ADR-025's central control is `htmlLabels: false`, because with html labels
   * on Mermaid emits a `foreignObject` full of HTML *and* widens its own
   * DOMPurify pass to preserve it. There is no public getter for the effective
   * config, so this asserts the observable consequence. `renderRaw` runs on the
   * config `ComponentDiagram.tsx` installed, so flipping that flag in the
   * component fails this test.
   */
  it('puts labels in <text>, not in a foreignObject', async () => {
    const svgSource = await renderRaw('flowchart LR\n  c0["plain label"]\n')
    expect(svgSource).not.toContain('foreignObject')
    expect(svgSource).not.toContain('foreignobject')
    expect(svgSource).toContain('<text')
  })

  /**
   * And `securityLevel` must not be `loose`, because that is the branch that
   * skips the whole-SVG DOMPurify pass — the call that actually deletes
   * `<script>` and strips `on*`. Observable the same way: under `loose` the
   * markup below survives into the output.
   */
  it('runs the whole-SVG DOMPurify pass, so a script in a label is deleted', async () => {
    const svgSource = await renderRaw('flowchart LR\n  c0["x <script>window.xss(1)</script> y"]\n')
    expect(svgSource).not.toContain('<script')
    expect(svgSource).not.toContain('window.xss')
  })
})

describe('mermaid 11.17.2, with htmlLabels off, given hostile labels', () => {
  it('emits no script element and no scriptable attribute', async () => {
    const svgSource = await renderRaw(HOSTILE_SOURCE)
    const parsed = new DOMParser().parseFromString(svgSource, 'image/svg+xml')
    expect(parsed.getElementsByTagName('parsererror')).toHaveLength(0)

    const root = parsed.documentElement
    expect(root.getElementsByTagName('script')).toHaveLength(0)
    expect(root.getElementsByTagName('foreignObject')).toHaveLength(0)
    expect(root.getElementsByTagName('foreignobject')).toHaveLength(0)
    expect(scriptableAttributes(root)).toEqual([])
  })

  it('deletes the <script> from the label and keeps the <img> as characters only', async () => {
    const svgSource = await renderRaw(HOSTILE_SOURCE)
    const root = new DOMParser().parseFromString(svgSource, 'image/svg+xml').documentElement

    // ADR-025's recorded observation: the `<script>` disappears entirely and
    // the `<img>` survives only as literal text inside a <text>/<tspan>.
    expect(svgSource).not.toContain('alert(1)')
    expect(root.textContent).toContain('probe')

    const carriers = allElements(root).filter(
      (element) => element.localName === 'text' || element.localName === 'tspan',
    )
    const carriedText = carriers.map((element) => element.textContent ?? '').join(' ')
    expect(carriedText).toContain('probe')
    // Whatever remains of the img is text content of a text node, which has no
    // markup parser. The element itself never exists.
    expect(root.getElementsByTagName('img')).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// Layer 4, end to end: the panel over the real renderer
// ---------------------------------------------------------------------------

describe('the panel, over the real renderer', () => {
  it('puts no script element and no scriptable attribute into the live document', async () => {
    const panel = await renderPanel(HOSTILE_SOURCE)
    expect(panel.host.getElementsByTagName('script')).toHaveLength(0)
    expect(Array.from(panel.host.querySelectorAll('*')).flatMap(scriptableAttributes)).toEqual([])
  })

  it('draws the hostile fixture rather than refusing it', async () => {
    // ADR-025: the golden fixture and a diagram built entirely of hostile
    // labels both rendered. If this fails, the allowlist has drifted from the
    // renderer's output and the panel shows nothing for *every* repository.
    const panel = await renderPanel(HOSTILE_SOURCE)
    expect(panel.refusal).toBeNull()
    expect(panel.svg).not.toBeNull()
  })

  it('draws nothing and calls no renderer when there is no diagram', async () => {
    const spy = vi.spyOn(mermaid, 'render')
    const panel = await renderPanel(null)
    expect(panel.container.textContent).toBe('No component diagram for this repository.')
    expect(panel.svg).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })
})

describe('the two click directives ADR-025 names', () => {
  const CLICK_JAVASCRIPT = 'flowchart LR\n  c0["src"]\n  click c0 "javascript:window.xss(1)"\n'
  const CLICK_HREF = 'flowchart LR\n  c0["src"]\n  click c0 href "https://example.com"\n'

  it('click c0 "javascript:..." makes mermaid emit an <a> with the href already dropped', async () => {
    const svgSource = await renderRaw(CLICK_JAVASCRIPT)
    const root = new DOMParser().parseFromString(svgSource, 'image/svg+xml').documentElement

    const anchors = Array.from(root.querySelectorAll('*')).filter((e) => e.localName === 'a')
    expect(anchors).not.toHaveLength(0)
    // Mermaid's own sanitize-url removed the javascript: URL...
    expect(svgSource).not.toContain('javascript:')
    // ...and `a` is outside ALLOWED_ELEMENTS, so the panel refuses regardless.
    const panel = await renderPanel(CLICK_JAVASCRIPT)
    expect(panel.refusal).toBe('unexpected <a> in the rendered diagram')
    expect(panel.svg).toBeNull()
  })

  /**
   * ADR-025 records this directive as emitting `xlink:href` with the `xlink`
   * prefix undeclared, and says step 2 — the XML parse — is what refuses it.
   * That is not what mermaid 11.17.2 does. It emits a **plain `href`** on an
   * `<a>`, with no `xlink` prefix and no `xmlns:xlink` anywhere in the document;
   * confirmed against the raw serialization under `securityLevel: 'loose'`, so
   * it is mermaid's own output and not something the DOMPurify pass rewrote.
   *
   * The document therefore parses as well-formed XML and step 2 never fires.
   * The panel still refuses, twice over — `a` is outside `ALLOWED_ELEMENTS` and
   * `href` is on the attribute denylist — so this is a defect in the ADR's
   * description of the mechanism, not a hole. Asserted as observed rather than
   * as documented.
   */
  it('click c0 href "https://..." is refused by the element and attribute checks, not the XML parse', async () => {
    const svgSource = await renderRaw(CLICK_HREF)
    const parsed = new DOMParser().parseFromString(svgSource, 'image/svg+xml')

    expect(svgSource).toContain('href="https://example.com')
    expect(svgSource).not.toContain('xlink')
    // Well-formed, so ADR-025's stated layer is not the one that catches this.
    expect(parsed.getElementsByTagName('parsererror')).toHaveLength(0)

    const anchors = Array.from(parsed.documentElement.querySelectorAll('*')).filter(
      (element) => element.localName === 'a',
    )
    expect(anchors).not.toHaveLength(0)
    expect(scriptableAttributes(parsed.documentElement)).not.toEqual([])

    const panel = await renderPanel(CLICK_HREF)
    expect(panel.refusal).toBe('unexpected <a> in the rendered diagram')
    expect(panel.svg).toBeNull()
  })
})

describe('a mermaid failure never reaches the user as its own message', () => {
  // Both sources carry a token standing in for repository-authored text.
  const MALFORMED = 'flowchart LR\n  c0["SENTINEL-REPO-TEXT\n  --> ]]] ((( \n'
  const UNKNOWN_TYPE = 'notADiagramType SENTINEL-REPO-TEXT\n  a --> b\n'

  it('mermaid really does quote the offending source line, so the fixed message matters', async () => {
    await expect(renderRaw(MALFORMED)).rejects.toThrow(/Parse error/)
    await expect(renderRaw(UNKNOWN_TYPE)).rejects.toThrow(/SENTINEL-REPO-TEXT/)
  })

  it('reports the fixed message for malformed source, with no exception text', async () => {
    const panel = await renderPanel(MALFORMED)
    expect(panel.refusal).toBe('the diagram could not be rendered')
    const shown = panel.host.ownerDocument.body.textContent ?? ''
    expect(shown).not.toContain('SENTINEL-REPO-TEXT')
    expect(shown).not.toContain('Parse error')
    expect(shown).not.toContain('Expecting')
  })

  it('reports the fixed message for an unknown diagram type, with no source text', async () => {
    const panel = await renderPanel(UNKNOWN_TYPE)
    expect(panel.refusal).toBe('the diagram could not be rendered')
    const shown = panel.host.ownerDocument.body.textContent ?? ''
    expect(shown).not.toContain('SENTINEL-REPO-TEXT')
    expect(shown).not.toContain('UnknownDiagramError')
  })
})

// ---------------------------------------------------------------------------
// Layer 4, per layer: one crafted document at a time
// ---------------------------------------------------------------------------

describe('each refusal path, isolated by stubbing the renderer', () => {
  /**
   * Replaces `mermaid.render`'s *output* only. The component under test is
   * untouched; what changes is the one input it cannot otherwise be given,
   * which is the point — ADR-025's step 2 and 3 are gates on renderer output,
   * and a gate is tested by what it is handed.
   */
  function renderReturns(svg: string): void {
    vi.spyOn(mermaid, 'render').mockResolvedValue({ svg, diagramType: 'flowchart' })
  }

  const MINIMAL_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">' +
    '<g><rect x="0" y="0" width="4" height="4"/><text><tspan>ok</tspan></text></g></svg>'

  it('accepts a document made only of allowed elements — so "drawn" is reachable', async () => {
    renderReturns(MINIMAL_SVG)
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBeNull()
    expect(panel.svg).not.toBeNull()
    expect(panel.svg?.namespaceURI).toBe('http://www.w3.org/2000/svg')
  })

  it('refuses markup that is not well-formed XML', async () => {
    // Exactly the shape ADR-025 says html labels produce: an unclosed <img>
    // inside a foreignObject.
    renderReturns(
      '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject>' +
        '<div xmlns="http://www.w3.org/1999/xhtml"><img src="x"></div></foreignObject></svg>',
    )
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('the renderer produced markup that is not well-formed XML')
  })

  it('refuses an undeclared namespace prefix, which is how xlink:href would arrive', async () => {
    renderReturns(
      '<svg xmlns="http://www.w3.org/2000/svg"><a xlink:href="https://example.com/">' +
        '<rect width="4" height="4"/></a></svg>',
    )
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('the renderer produced markup that is not well-formed XML')
  })

  it('refuses a root that is not an <svg> in the SVG namespace', async () => {
    renderReturns('<html xmlns="http://www.w3.org/1999/xhtml"><body>no</body></html>')
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('the renderer produced a document that is not an SVG')
  })

  it('refuses an <svg> root that is only namespace-shaped like one', async () => {
    renderReturns('<svg xmlns="http://example.com/not-svg"><rect/></svg>')
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('the renderer produced a document that is not an SVG')
  })

  it('refuses an element outside the allowlist, and names it', async () => {
    renderReturns(
      '<svg xmlns="http://www.w3.org/2000/svg"><a><rect width="4" height="4"/></a></svg>',
    )
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('unexpected <a> in the rendered diagram')
  })

  it('refuses a script element, the one that would execute on import', async () => {
    renderReturns('<svg xmlns="http://www.w3.org/2000/svg"><script>window.xss=1</script></svg>')
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('unexpected <script> in the rendered diagram')
    expect(panel.host.getElementsByTagName('script')).toHaveLength(0)
  })

  it('refuses a foreignObject, the sink htmlLabels would open', async () => {
    renderReturns(
      '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject width="4" height="4"/></svg>',
    )
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('unexpected <foreignObject> in the rendered diagram')
  })

  it('refuses an event-handler attribute on an otherwise allowed element', async () => {
    renderReturns(
      '<svg xmlns="http://www.w3.org/2000/svg">' +
        '<rect width="4" height="4" onload="window.xss=1"/></svg>',
    )
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('unexpected onload attribute in the rendered diagram')
  })

  it('refuses a plain href on an allowed element', async () => {
    renderReturns(
      '<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4" href="#x"/></svg>',
    )
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('unexpected href attribute in the rendered diagram')
  })

  it('refuses a namespaced xlink:href once the prefix is declared', async () => {
    renderReturns(
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">' +
        '<rect width="4" height="4" xlink:href="https://example.com/"/></svg>',
    )
    const panel = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(panel.refusal).toBe('unexpected xlink:href attribute in the rendered diagram')
  })

  it('takes the previous diagram down when a later render is refused', async () => {
    renderReturns(MINIMAL_SVG)
    const drawn = await renderPanel('flowchart LR\n  c0["x"]\n')
    expect(drawn.svg).not.toBeNull()

    vi.mocked(mermaid.render).mockRejectedValue(new Error('Parse error on line 1'))
    const refused = await renderPanel('flowchart LR\n  c0["y"]\n')
    expect(refused.refusal).toBe('the diagram could not be rendered')
    expect(refused.svg).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The allowlist against the renderer's real output
// ---------------------------------------------------------------------------

/**
 * The environment patch, kept honest. If any of this stops holding, the stub in
 * this file is doing something other than what its comment claims, and the
 * `feDropShadow` assertions elsewhere become meaningless.
 */
describe('the parse5 conformance gap this suite patches', () => {
  it('was really missing, and was the only row missing', () => {
    expect(PARSE5_LACKED_FEDROPSHADOW).toBe(true)
    // The other thirty-five rows are present, which is what makes this a
    // one-row gap rather than a different table than the spec's.
    const map = foreignContent.SVG_TAG_NAMES_ADJUSTMENT_MAP
    expect(map.get('feblend')).toBe('feBlend')
    expect(map.get('fegaussianblur')).toBe('feGaussianBlur')
    expect(map.get('foreignobject')).toBe('foreignObject')
    expect(map.get('lineargradient')).toBe('linearGradient')
  })

  it('now restores feDropShadow through an HTML parse, as a browser does', () => {
    const host = document.createElement('div')
    // Exactly what DOMPurify does to Mermaid's serialized SVG: reparse as HTML.
    host.innerHTML = '<svg><filter><feDropShadow/></filter><linearGradient/></svg>'

    const names = Array.from(host.querySelectorAll('*')).map((element) => element.localName)
    expect(names).toContain('feDropShadow')
    expect(names).toContain('linearGradient')
    expect(names).not.toContain('fedropshadow')
  })

  it('matters because mermaid emits feDropShadow on every render', async () => {
    // Not an edge case reachable only by a hostile fixture: the plainest
    // possible diagram has the shadow filter too.
    const svgSource = await renderRaw('flowchart LR\n  c0["a"]\n  c1["b"]\n  c0 --> c1\n')
    expect(svgSource).toContain('feDropShadow')
  })
})

describe('ALLOWED_ELEMENTS against what mermaid 11.17.2 actually emits', () => {
  it('covers every element in the rendered hostile fixture', async () => {
    const svgSource = await renderRaw(HOSTILE_SOURCE)
    const root = new DOMParser().parseFromString(svgSource, 'image/svg+xml').documentElement

    // Mirrors the set in ComponentDiagram.tsx, which is not exported.
    const allowed = new Set([
      'circle',
      'defs',
      'feDropShadow',
      'filter',
      'g',
      'linearGradient',
      'marker',
      'path',
      'polygon',
      'rect',
      'stop',
      'style',
      'svg',
      'text',
      'tspan',
    ])
    const unexpected = [
      ...new Set(
        allElements(root)
          .map((element) => element.localName)
          .filter((name) => !allowed.has(name)),
      ),
    ].sort()

    expect(unexpected).toEqual([])
  })
})
