/**
 * The component-diagram panel — `AnalyzeResponse.componentDiagram` drawn.
 *
 * Not wired into `App.tsx` yet, deliberately. This is the render, not the
 * layout decision.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS FILE IS THE MOST DANGEROUS RENDER IN THE PROJECT
 * ---------------------------------------------------------------------------
 *
 * `GraphNode.description` and `ServiceEndpoint.summary` are repository-authored
 * strings that sit *beside* a format: `Inspector.tsx` drops them in as React
 * children and React escapes them. `componentDiagram` is different in kind —
 * repository text (directory names, route paths, handler comments) sits
 * *inside* Mermaid source, and that source goes to a third-party renderer whose
 * output is an SVG **string**. Every other sink in the app can be satisfied by
 * "render it as a text node". This one cannot, because the thing being rendered
 * is markup by the time we get it back.
 *
 * docs/SECURITY.md, "Repository comment rendered as HTML (XSS)", is the row
 * this file answers.
 *
 * ---------------------------------------------------------------------------
 * THE FOUR LAYERS, AND WHICH ONES WE CONTROL
 * ---------------------------------------------------------------------------
 *
 * 1. **The generator removes the injection site** (ADR-024, backend). Node
 *    identifiers in the Mermaid source are synthetic — `c0`, `r0`, `ext` — so
 *    no repository string is ever concatenated into an identifier, an arrow or
 *    a subgraph name. Repository text occupies exactly one position in the
 *    document: inside a double-quoted label. `component_diagram._label` then
 *    drops non-printables and removes `" # & < > % \ ` { } | ;`.
 *
 * 2. **Mermaid sanitizes each label.** Verified in the pinned source, not
 *    assumed — see the next section.
 *
 * 3. **Mermaid DOMPurifies the serialized SVG**, because `securityLevel` is
 *    not `loose`. Verified in the pinned source.
 *
 * 4. **This file refuses anything outside a known-good shape** before the
 *    markup reaches the live document. Layer 4 is the only one we own, so it
 *    is written to hold even if 1–3 are wrong.
 *
 * ---------------------------------------------------------------------------
 * WHAT MERMAID 11.17.2 ACTUALLY ESCAPES (read from the installed package, then
 * confirmed against a real browser render on 2026-09-01)
 * ---------------------------------------------------------------------------
 *
 * Label text — `sanitizeText` in `dist/chunks/mermaid.core/chunk-DU6HZSFF.mjs`:
 *
 *     sanitizeText = (text, config) =>
 *       DOMPurify.sanitize(sanitizeMore(text, config), { FORBID_TAGS: ['style'] })
 *
 *     sanitizeMore = (text, config) => {
 *       if (getEffectiveHtmlLabels(config)) { ...escape < > = / removeScript... }
 *       return text
 *     }
 *
 * Read that second function twice. `sanitizeMore` — the half that does the
 * `<` → `&lt;` escaping — is **gated on html labels being ON**. With
 * `htmlLabels: false` it is a no-op. What still runs unconditionally is the
 * outer `DOMPurify.sanitize`, and that is the load-bearing call: it deletes
 * `<script>` elements and strips `on*` attributes. So the protection here is
 * DOMPurify, *not* entity-escaping, and it would be wrong to describe this file
 * as relying on Mermaid escaping angle brackets — it does not.
 *
 * The whole-SVG pass — `mermaid.core.mjs`, in `render`'s `serializeSvg`:
 *
 *     } else if (!isLooseSecurityLevel) {
 *       code = DOMPurify.sanitize(code, {
 *         ADD_TAGS: ['foreignobject'], ADD_ATTR: ['dominant-baseline'],
 *         HTML_INTEGRATION_POINTS: { foreignobject: true },
 *       })
 *     }
 *
 * Note `ADD_TAGS: ['foreignobject']` and the HTML integration point: with html
 * labels on, Mermaid *widens* DOMPurify to keep a foreignObject full of HTML.
 * That is the sink we are turning off, which is why `htmlLabels: false` below
 * is a security control and not a styling preference.
 *
 * Observed, in Chrome, at this version, with `htmlLabels: false` and
 * `securityLevel: 'strict'`, feeding labels that the backend could never emit:
 *
 *     "<script>window.xss('x')</script>"      -> label disappears entirely
 *     "<img src=x onerror=window.xss('x')>"   -> literal text `<img src="x">`
 *     "<svg onload=window.xss('x')></svg>"    -> literal text `<svg> </svg>`
 *     "</text><script>...</script>"           -> label disappears entirely
 *     "a < b > c & d"                         -> literal text, no elements
 *
 * Nothing fired; the emitted SVG contained no `<script`, no `on*=`, and no
 * `foreignObject`. Labels land as text content of `<text>`/`<tspan>`, which has
 * no markup parser — the same structural argument `GraphCanvas.tsx` makes about
 * painting labels to a `<canvas>`.
 *
 * (The `<img src="x">` remnant shows as visible characters. That is the
 * double-encoding `component_diagram.py`'s docstring predicts, and it is
 * unreachable from a real response: the backend removes `<`, `>` and `&` from
 * every label. It is a display artifact of a fixture, not a defect.)
 *
 * ---------------------------------------------------------------------------
 * HOW THE SVG GETS INTO THE DOM, AND WHY NOT innerHTML
 * ---------------------------------------------------------------------------
 *
 * `mermaid.render()` returns a string, so *something* has to turn markup into
 * nodes. There is no `dangerouslySetInnerHTML` here and no `innerHTML` either.
 * Both were rejected for a specific reason rather than on principle:
 *
 *   - `innerHTML` runs the **HTML** fragment parser. It happens to neutralize
 *     `<script>` (fragment-parsed scripts are flagged already-started), but it
 *     does **not** neutralize `onload=` / `onbegin=` / `onerror=`, which fire
 *     normally. "innerHTML doesn't run scripts" is true and not the property we
 *     need.
 *   - `DOMParser` + `importNode` is not automatically safer either: an SVG
 *     `<script>` created through the DOM *does* execute on insertion.
 *
 * Neither is safe for arbitrary markup, so the safety cannot come from the
 * injection call. It comes from what is injected. This file therefore:
 *
 *   1. parses the string as `image/svg+xml` into an **inert** document — no
 *     browsing context, so nothing executes and no handler fires while we look
 *     at it;
 *   2. refuses it outright if XML parsing failed, or if the root is not an
 *     `<svg>` in the SVG namespace;
 *   3. walks the inert tree and refuses if it contains any element outside
 *     `ALLOWED_ELEMENTS`, or any attribute in the scriptable classes below;
 *   4. only then imports it into the live document.
 *
 * Step 2 is worth more than it looks. Mermaid's html-label output is **not
 * well-formed XML** — a foreignObject carrying `<img src="x">` fails to parse
 * ("Opening and ending tag mismatch: img"), verified in the browser. So the XML
 * parse is a hard structural gate on `htmlLabels: false`: if that config is
 * ever flipped back on, this panel goes blank and says so, rather than quietly
 * growing an HTML sink. Fail-closed, and it fails on the exact configuration
 * mistake that matters.
 *
 * Step 3 was likewise checked against the feature that could defeat it. A
 * Mermaid `click` directive is the one thing that turns a node into something
 * navigable. Verified at this version:
 *
 *     click c0 "javascript:..."   -> emits an <a>, with the href dropped by
 *                                    Mermaid's own sanitize-url; `a` is not in
 *                                    ALLOWED_ELEMENTS, so we refuse anyway
 *     click c1 call fn()          -> not invoked (securityLevel: 'strict')
 *     click c0 href "https://..." -> emits a plain `href` on an <a>. Refused by
 *                                    step 3, twice: `a` is not in
 *                                    ALLOWED_ELEMENTS and `href` is on the
 *                                    denylist below.
 *
 * That last row was wrong until 2026-09-01 and is worth the correction rather
 * than a silent edit. It read "emits xlink:href with the xlink namespace
 * undeclared, so step 2 already refuses it", and ADR-025 said the same. At
 * mermaid 11.17.2 there is no `xlink:href` and no `xmlns:xlink` anywhere in the
 * output — confirmed against the raw serialization under `securityLevel:
 * 'loose'`, the one branch that skips the whole-SVG DOMPurify pass, so it is
 * mermaid's own output rather than a DOMPurify rewrite. The document is
 * well-formed, so step 2 never fires on it. Security-neutral: the directive is
 * still refused, just one layer later than described. `ComponentDiagram.test.tsx`
 * pins the real behaviour; ADR-025 carries the full correction.
 *
 * Step 2 keeps its value for the case it was actually argued from — html-label
 * output that is not well-formed XML — and the undeclared-prefix path is still
 * exercised, by handing this component a crafted document rather than by
 * relying on mermaid to emit one.
 *
 * Our generator emits no `click`, no `classDef` and no `style` statement. The
 * checks are here because "the generator does not do that today" is a fact
 * about the backend, and this is the browser's own boundary (CLAUDE.md:
 * validate at every boundary).
 *
 * Refuse rather than scrub, for `limits.ts`'s stated reason: the failure is a
 * blank panel with a message, which is loud, instead of a silently altered
 * picture, which is not.
 *
 * ---------------------------------------------------------------------------
 * RESIDUAL, STATED PLAINLY
 * ---------------------------------------------------------------------------
 *
 * `<style>` is on the allowlist — Mermaid scopes its generated CSS with `#id`
 * selectors and the panel is unreadable without it. Its content is derived from
 * the theme config we pass plus any `classDef`/`style` statement in the source;
 * repository text cannot reach either, because ADR-024 confines repository text
 * to quoted labels. CSS cannot execute script in any current browser, so the
 * residual is exfiltration via `url()` in a crafted stylesheet — which needs a
 * response our own backend does not generate, and which the planned CSP
 * (`default-src 'self'`) closes. Not closed here; recorded, not hidden.
 */
import mermaid from 'mermaid'
import { useEffect, useRef, useState } from 'react'

const SVG_NAMESPACE = 'http://www.w3.org/2000/svg'

/**
 * Every element Mermaid 11.17.2 emits for a `flowchart LR` with html labels
 * off. Not a guess and not a general-purpose SVG allowlist: it is the observed
 * output for the golden fixture in `backend/tests/fixtures/`, for a diagram
 * built entirely of hostile labels, and for subgraphs, arrow labels and edge
 * markers. The version is exact-pinned, so this set can only drift on a
 * deliberate upgrade — at which point the panel refuses and says so, which is
 * the intended way to find out.
 *
 * Absent on purpose, and two of the absences are load-bearing rather than
 * tidy: `foreignObject` is reachable through Mermaid *config* (`htmlLabels`)
 * and `a` through Mermaid *syntax* (a `click` directive) — both were driven and
 * both are refused. `script`, `use`, `image` and `animate` are not reachable by
 * any route found at this version; they are excluded because an allowlist that
 * admits what it has not seen is not an allowlist.
 */
const ALLOWED_ELEMENTS: ReadonlySet<string> = new Set([
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

/**
 * Attributes refused wherever they appear. A denylist rather than an allowlist,
 * and the asymmetry with `ALLOWED_ELEMENTS` is deliberate: the scriptable
 * attribute space in SVG is small and well characterized (event handlers, and
 * URL-bearing attributes that accept `javascript:`), while the decorative space
 * is large and grows across patch releases. An allowlist here would blank the
 * panel over a new `stroke-linejoin`; this refuses only what can run.
 *
 * `style` is not here: it is CSS, which cannot execute, and Mermaid uses it for
 * layout on nearly every node.
 */
const isForbiddenAttribute = (name: string): boolean => {
  const lower = name.toLowerCase()
  return lower.startsWith('on') || lower === 'href' || lower.endsWith(':href') || lower === 'base'
}

/** Mermaid scopes its stylesheet by this id, so it must be unique per render. */
let renderSequence = 0

let configured = false

function configureOnce(): void {
  if (configured) return
  configured = true
  mermaid.initialize({
    startOnLoad: false,
    // Explicit, though it is also the default. It gates the whole-SVG DOMPurify
    // pass quoted in the header; a default is not a control.
    securityLevel: 'strict',
    // The security control. Top-level, not `flowchart.htmlLabels` — that key is
    // deprecated at this version and logs a warning. With this off, labels are
    // SVG text content; with it on, they are HTML inside a foreignObject.
    htmlLabels: false,
    // Mermaid otherwise draws its own error diagram into the document on a
    // parse failure. We would rather it write nothing and let us report it.
    suppressErrorRendering: true,
    theme: 'dark',
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    flowchart: { useMaxWidth: true },
  })
}

type Refusal = { reason: string }

/**
 * The string from `mermaid.render()`, parsed inert and checked, or a refusal.
 *
 * Nothing here touches the live document: the returned element belongs to a
 * document with no browsing context, where scripts do not run and handlers do
 * not fire. That is what makes inspecting it before adopting it meaningful.
 */
function parseAndCheck(svgSource: string): SVGSVGElement | Refusal {
  const parsed = new DOMParser().parseFromString(svgSource, 'image/svg+xml')

  // XML well-formedness, which html labels would break. See the header.
  if (parsed.getElementsByTagName('parsererror').length > 0) {
    return { reason: 'the renderer produced markup that is not well-formed XML' }
  }

  const root = parsed.documentElement
  if (root.namespaceURI !== SVG_NAMESPACE || root.localName !== 'svg') {
    return { reason: 'the renderer produced a document that is not an SVG' }
  }

  for (const element of [root, ...Array.from(root.querySelectorAll('*'))]) {
    if (element.namespaceURI !== SVG_NAMESPACE || !ALLOWED_ELEMENTS.has(element.localName)) {
      // The offending name is renderer output, not repository text, so naming
      // it is safe — and it is the one thing that makes an upgrade debuggable.
      return { reason: `unexpected <${element.localName}> in the rendered diagram` }
    }
    for (const attribute of Array.from(element.attributes)) {
      if (isForbiddenAttribute(attribute.localName) || isForbiddenAttribute(attribute.name)) {
        return { reason: `unexpected ${attribute.name} attribute in the rendered diagram` }
      }
    }
  }

  return root as unknown as SVGSVGElement
}

type Status = { state: 'pending' } | { state: 'drawn' } | { state: 'refused'; reason: string }

export function ComponentDiagram({ source }: { source: string | null }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState<Status>({ state: 'pending' })

  useEffect(() => {
    const host = hostRef.current
    if (host === null || source === null) return

    // React 19 StrictMode runs effects twice, and `render` is async — without
    // this, the first render's SVG can land after the second's.
    let live = true
    setStatus({ state: 'pending' })

    // A refusal must take the previous diagram down with it. Leaving the old
    // SVG on screen under a "refused" message shows one repository's picture
    // labelled with another's failure, which is worse than showing nothing.
    const refuse = (reason: string) => {
      if (!live) return
      host.replaceChildren()
      setStatus({ state: 'refused', reason })
    }

    void (async () => {
      configureOnce()
      renderSequence += 1
      let svgSource: string
      try {
        ;({ svg: svgSource } = await mermaid.render(`component-diagram-${renderSequence}`, source))
      } catch {
        // The exception is deliberately not shown. Mermaid's parse errors quote
        // the offending line, and that line contains repository text; the same
        // rule `app/errors.py` applies to the backend's own error bodies.
        refuse('the diagram could not be rendered')
        return
      }
      if (!live) return

      const checked = parseAndCheck(svgSource)
      if ('reason' in checked) {
        refuse(checked.reason)
        return
      }

      host.replaceChildren(document.importNode(checked, true))
      setStatus({ state: 'drawn' })
    })()

    return () => {
      live = false
    }
  }, [source])

  if (source === null) {
    return <p className="p-4 text-xs text-neutral-600">No component diagram for this repository.</p>
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-baseline gap-3 border-b border-neutral-900 px-4 py-2">
        <span className="text-[10px] uppercase tracking-wider text-neutral-500">
          Component diagram
        </span>
        {status.state === 'refused' && (
          // A refusal is a text node like everything else in this app.
          <span className="text-[10px] text-amber-600/80">refused — {status.reason}</span>
        )}
      </div>
      <div
        ref={hostRef}
        className="min-h-0 flex-1 overflow-auto p-4 [&>svg]:h-auto [&>svg]:max-w-full"
        // Populated by the effect above, from nodes imported out of an inert
        // document. No `dangerouslySetInnerHTML`, here or anywhere in `src/`.
      />
    </div>
  )
}
