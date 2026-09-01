/**
 * A hand-written `AnalyzeResponse` standing in for the server.
 *
 * There is no `POST /api/analyze` yet — `backend/app/api/` is an empty package
 * (docs/CURRENT_STATE.md) — so this fixture is what the canvas renders until
 * there is. It is written to satisfy the *analysis* invariants as well as the
 * schema, so it exercises the real thing rather than a convenient shape:
 *
 *   - nodes are ordered by path *components*, so a parent always precedes its
 *     children (ADR-018)
 *   - the repository root is a node: id ".", `parent: null`, named for the repo
 *   - `sum(imports) === sum(importedBy) === edges.length === stats.dependencies`
 *   - `root.fileCount === stats.files`, and directory aggregates are recursive
 *   - `externalImports` / `unresolvedImports` count statements, not distinct
 *     packages (ADR-018)
 *
 * It is parsed through `AnalyzeResponseSchema` at module load, so a drift
 * between this file and the schema fails immediately and visibly.
 *
 * One description deliberately contains angle brackets. It is there to make
 * the rendering guardrail observable: if it ever shows up as anything other
 * than the literal characters, something built an HTML string.
 */
import { AnalyzeResponseSchema, type AnalyzeResponse } from '../api/schema.ts'

const raw = {
  repository: {
    owner: 'example',
    name: 'demo-service',
    commitSha: '9f1c2ab',
  },
  nodes: [
    {
      id: '.',
      name: 'demo-service',
      path: '.',
      type: 'directory',
      parent: null,
      depth: 0,
      fileCount: 6,
      totalBytes: 6930,
    },
    {
      id: 'src',
      name: 'src',
      path: 'src',
      type: 'directory',
      parent: '.',
      depth: 1,
      fileCount: 6,
      totalBytes: 6930,
    },
    {
      id: 'src/api',
      name: 'api',
      path: 'src/api',
      type: 'directory',
      parent: 'src',
      depth: 2,
      fileCount: 2,
      totalBytes: 3170,
    },
    {
      id: 'src/api/client.ts',
      name: 'client.ts',
      path: 'src/api/client.ts',
      type: 'file',
      parent: 'src/api',
      depth: 3,
      language: 'typescript',
      description: 'Thin fetch wrapper. Adds no auth header of its own.',
      bytes: 860,
      loc: 41,
      imports: 1,
      importedBy: 1,
      externalImports: 1,
      unresolvedImports: 0,
    },
    {
      id: 'src/api/routes.ts',
      name: 'routes.ts',
      path: 'src/api/routes.ts',
      type: 'file',
      parent: 'src/api',
      depth: 3,
      language: 'typescript',
      description: 'HTTP surface for the demo service.',
      bytes: 2310,
      loc: 96,
      imports: 2,
      importedBy: 1,
      externalImports: 1,
      unresolvedImports: 1,
    },
    {
      id: 'src/app.ts',
      name: 'app.ts',
      path: 'src/app.ts',
      type: 'file',
      parent: 'src',
      depth: 2,
      language: 'typescript',
      description: 'Composition root: builds the server and mounts the routes.',
      bytes: 1180,
      loc: 52,
      imports: 2,
      importedBy: 1,
      externalImports: 2,
      unresolvedImports: 0,
    },
    {
      id: 'src/index.ts',
      name: 'index.ts',
      path: 'src/index.ts',
      type: 'file',
      parent: 'src',
      depth: 2,
      language: 'typescript',
      bytes: 420,
      loc: 18,
      imports: 1,
      importedBy: 0,
      externalImports: 1,
      unresolvedImports: 0,
    },
    {
      id: 'src/lib',
      name: 'lib',
      path: 'src/lib',
      type: 'directory',
      parent: 'src',
      depth: 2,
      fileCount: 2,
      totalBytes: 2160,
    },
    {
      id: 'src/lib/logger.ts',
      name: 'logger.ts',
      path: 'src/lib/logger.ts',
      type: 'file',
      parent: 'src/lib',
      depth: 3,
      language: 'typescript',
      bytes: 640,
      loc: 30,
      imports: 1,
      importedBy: 1,
      externalImports: 0,
      unresolvedImports: 0,
    },
    {
      id: 'src/lib/utils.ts',
      name: 'utils.ts',
      path: 'src/lib/utils.ts',
      type: 'file',
      parent: 'src/lib',
      depth: 3,
      language: 'typescript',
      // Repository-authored text containing markup. Renders as characters.
      description: 'Shared helpers. Never emit <script> tags from here.',
      bytes: 1520,
      loc: 74,
      imports: 0,
      importedBy: 3,
      externalImports: 0,
      unresolvedImports: 0,
    },
  ],
  edges: [
    { source: 'src/api/client.ts', target: 'src/lib/utils.ts', relationship: 'imports' },
    { source: 'src/api/routes.ts', target: 'src/api/client.ts', relationship: 'imports' },
    { source: 'src/api/routes.ts', target: 'src/lib/logger.ts', relationship: 'imports' },
    { source: 'src/app.ts', target: 'src/api/routes.ts', relationship: 'imports' },
    { source: 'src/app.ts', target: 'src/lib/utils.ts', relationship: 'imports' },
    { source: 'src/index.ts', target: 'src/app.ts', relationship: 'imports' },
    { source: 'src/lib/logger.ts', target: 'src/lib/utils.ts', relationship: 'imports' },
  ],
  stats: {
    files: 6,
    directories: 4,
    dependencies: 7,
    externalImports: 5,
    unresolvedImports: 1,
    skippedFiles: 3,
    truncated: false,
  },
  serviceMap: [
    {
      method: 'GET',
      path: '/users/:id',
      file: 'src/api/routes.ts',
      line: 41,
      summary: 'Fetch one user by id.',
    },
    {
      method: 'POST',
      path: '/users',
      file: 'src/api/routes.ts',
      line: 58,
    },
  ],
  // Mermaid source, in the shape `analysis/component_diagram.py` actually
  // emits — compare `backend/tests/fixtures/component_diagram_golden.mmd`. It
  // is derived from the numbers above rather than invented: every file sits
  // under `src`, so there is exactly one container holding 6 files; both
  // service-map entries live in it; `stats.externalImports` is 5; and every
  // import edge is within `src`, so all of them collapse to a self-pair and
  // none is drawn.
  //
  // The node ids are synthetic (`c0`, `r0`, `ext`) because that is ADR-024's
  // whole point: repository text reaches the output in exactly one position,
  // inside a quoted label, and never in the syntax. The previous version of
  // this fixture used `src` / `api` / `lib` as ids, which read as though
  // directory names became identifiers. They do not.
  //
  // `c1` is the standing probe, and it is a deliberate deviation from
  // "derivable from the graph above" — the same deviation the `<script>` in
  // `utils.ts`'s description is. It CANNOT occur in a real response:
  // `component_diagram._label` removes `< > & " # % \ ` { } | ;` from every
  // label. It is here so `ui/ComponentDiagram.tsx`'s own layer is observable
  // rather than merely argued: at mermaid 11.17.2 with `htmlLabels: false`,
  // the `<script>` is dropped by mermaid's DOMPurify pass and the `<img>`
  // survives only as literal characters in an SVG `<text>` node. If either
  // ever renders as an element, something built markup.
  componentDiagram: [
    '%% Component diagram, generated from the dependency graph (ADR-013).',
    '%% Containers are top-level directories; arrows are import counts.',
    'flowchart LR',
    '  subgraph repo["demo-service"]',
    '    c0["src · 6 files"]',
    '    c1["probe <script>alert(1)</script> <img src=x onerror=alert(1)>"]',
    '  end',
    '  subgraph api["API surface"]',
    '    r0["GET /users/:id - Fetch one user by id."]',
    '    r1["POST /users"]',
    '  end',
    '  ext["External packages · 5 imports"]',
    '  r0 --> c0',
    '  r1 --> c0',
    '  c0 -->|5| ext',
  ].join('\n'),
}

export const FIXTURE_RESPONSE: AnalyzeResponse = AnalyzeResponseSchema.parse(raw)
