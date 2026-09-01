/**
 * zod mirror of the backend wire contract.
 *
 * Source of truth: `backend/app/models/graph.py` and `backend/app/models/api.py`.
 * Those files say the frontend schema "mirrors this file verbatim, so nothing
 * here may be renamed casually" — so this module is a transcription, not an
 * interpretation. Field names, optionality, and bounds are copied one for one:
 *
 *   - every model sets `extra="forbid"`  ->  `z.strictObject`
 *   - a pydantic field with no default is required here, even when nullable
 *     (`GraphNode.parent` is the deliberate case: `None` means "the root", and
 *     the analyzer must state it)
 *   - a pydantic field with `= None` / `= 0` / `= []` may be absent, so it is
 *     `.nullish().default(...)` here and normalizes to a present value
 *   - `Field(ge=0)` -> `.int().min(0)`, `Field(min_length=1)` -> `.min(1)`,
 *     and each `AfterValidator(_within_*_limit)` -> `.max(LIMITS.*)`
 *
 * Invariants the *analysis* guarantees but the *models* do not — sorting,
 * dedup, `stats.dependencies === edges.length` — are deliberately not asserted
 * here either, for the same reason `app/models/graph.py` does not assert them:
 * this layer validates shape, and inventing a stricter contract than the
 * server publishes would turn a backend counting bug into a blank screen.
 */
import { z } from 'zod'

import { LIMITS } from './limits.ts'

const nonNegativeInt = () => z.number().int().min(0)

/** A pydantic field declared `X | None = None`: absent and null are the same fact. */
const optionalNull = <T extends z.ZodTypeAny>(inner: T) => inner.nullish().default(null)

// models/api.py: _COMMIT_SHA_PATTERN — full or abbreviated lowercase-hex SHA.
const CommitSha = z.string().regex(/^[0-9a-f]{7,40}$/)
// models/api.py: MemberPath.
const MemberPath = z.string().min(1).max(LIMITS.MAX_PATH_LENGTH)
// models/api.py: HttpMethod — a character class, not an enum of known verbs,
// so a router defining an unusual verb is still describable.
const HttpMethod = z.string().regex(/^[A-Z]{1,16}$/)

// models/graph.py: NodeType.
export const NodeTypeSchema = z.enum(['directory', 'file'])

export const GraphNodeSchema = z.strictObject({
  id: z.string().min(1),
  name: z.string().min(1),
  path: z.string().min(1),
  type: NodeTypeSchema,
  // Hierarchy lives here, never on edges (ADR-006). null marks the root.
  // Required, not defaulted — the analyzer states it for every node.
  parent: z.string().nullable(),
  depth: nonNegativeInt(),
  language: optionalNull(z.string()),
  // Repository-authored text. Bounded at extraction and again at the model
  // boundary on the server; bounded a third time here because this layer does
  // not rely on either. Render as a text node — never as HTML.
  description: optionalNull(z.string().min(1).max(LIMITS.MAX_DESCRIPTION_CHARS)),

  // File metadata (null on directory nodes).
  bytes: optionalNull(nonNegativeInt()),
  loc: optionalNull(nonNegativeInt()),
  imports: optionalNull(nonNegativeInt()),
  importedBy: optionalNull(nonNegativeInt()),
  externalImports: optionalNull(nonNegativeInt()),
  unresolvedImports: optionalNull(nonNegativeInt()),
  // ADR-007's HMAC token. Deferred post-MVP: stays in the contract, stays null.
  sourceToken: optionalNull(z.string()),

  // Directory aggregates (null on file nodes).
  fileCount: optionalNull(nonNegativeInt()),
  totalBytes: optionalNull(nonNegativeInt()),
})

export const GraphEdgeSchema = z.strictObject({
  source: z.string().min(1),
  target: z.string().min(1),
  relationship: z.literal('imports'),
})

export const StatsSchema = z.strictObject({
  files: nonNegativeInt(),
  directories: nonNegativeInt(),
  dependencies: nonNegativeInt(),
  externalImports: nonNegativeInt().nullish().default(0),
  unresolvedImports: nonNegativeInt().nullish().default(0),
  skippedFiles: nonNegativeInt().nullish().default(0),
  truncated: z.boolean().nullish().default(false),
})

export const RepositorySchema = z.strictObject({
  owner: z.string().min(1),
  name: z.string().min(1),
  commitSha: CommitSha,
})

export const ServiceEndpointSchema = z.strictObject({
  method: HttpMethod,
  /** The route pattern as written in the source, e.g. "/api/users/:id". */
  path: MemberPath,
  /** The graph node ID of the file that defines the route. */
  file: MemberPath,
  /** 0-indexed, matching the parser; the frontend adds one for display. */
  line: nonNegativeInt(),
  /** The comment above the handler, quoted. Repository content — text only. */
  summary: optionalNull(z.string().min(1).max(LIMITS.MAX_ENDPOINT_SUMMARY_CHARS)),
})

export const AnalyzeResponseSchema = z.strictObject({
  repository: RepositorySchema,
  // MAX_NODES / MAX_EDGES are enforced *nowhere* on the server today
  // (docs/CURRENT_STATE.md, "Broken / Known Issues"), so these two caps are
  // currently the only place those limits bind anything.
  nodes: z.array(GraphNodeSchema).max(LIMITS.MAX_NODES),
  edges: z.array(GraphEdgeSchema).max(LIMITS.MAX_EDGES),
  stats: StatsSchema,
  serviceMap: z
    .array(ServiceEndpointSchema)
    .max(LIMITS.MAX_SERVICE_ENDPOINTS)
    .nullish()
    .default([]),
  // Mermaid source, generated from the graph. Handed to the Mermaid renderer
  // verbatim — never concatenated into markup.
  componentDiagram: optionalNull(z.string().min(1).max(LIMITS.MAX_COMPONENT_DIAGRAM_CHARS)),
})

// models/api.py: AnalyzeRequest. snake_case on the way in, per PRD §9.
export const AnalyzeRequestSchema = z.strictObject({
  repository_url: z.string().min(1).max(LIMITS.MAX_URL_LENGTH),
})

// models/api.py: the error contract — exactly three keys.
export const ApiErrorSchema = z.strictObject({
  error: z.strictObject({
    code: z.string().min(1),
    message: z.string().min(1),
    requestId: z.string().min(1),
  }),
})

export type NodeType = z.output<typeof NodeTypeSchema>
export type GraphNode = z.output<typeof GraphNodeSchema>
export type GraphEdge = z.output<typeof GraphEdgeSchema>
export type Stats = z.output<typeof StatsSchema>
export type Repository = z.output<typeof RepositorySchema>
export type ServiceEndpoint = z.output<typeof ServiceEndpointSchema>
export type AnalyzeResponse = z.output<typeof AnalyzeResponseSchema>
export type AnalyzeRequest = z.input<typeof AnalyzeRequestSchema>
export type ApiError = z.output<typeof ApiErrorSchema>
