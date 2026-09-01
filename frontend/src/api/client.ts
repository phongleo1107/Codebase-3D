/**
 * `POST /api/analyze` client.
 *
 * This is the first code path that runs `AnalyzeResponseSchema` against a
 * real server response rather than `graph/fixture.ts`'s hand-written stand-in
 * (docs/TODO.md, Day 2). A response the schema rejects is a transcription bug
 * against `backend/app/models/graph.py` / `api.py` — fix `schema.ts`, do not
 * loosen it to make the rejection go away.
 *
 * The base URL is `VITE_API_URL` (docs/ARCHITECTURE.md "Deployment": Vercel
 * points this at the separately hosted backend) and defaults to the empty
 * string — a same-origin, relative request. Locally that is what
 * `vite.config.ts`'s dev-server proxy forwards to `http://localhost:8000`,
 * since the backend has no CORS headers yet (Day 3, docs/TODO.md) and a
 * cross-origin `fetch` with a JSON body would otherwise fail its preflight.
 */
import {
  AnalyzeResponseSchema,
  ApiErrorSchema,
  type AnalyzeRequest,
  type AnalyzeResponse,
} from './schema.ts'

const API_BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? ''

/** One of the 14 `ErrorCode`s in `backend/app/errors.py`, with its static message and request id. */
export class ApiRequestError extends Error {
  readonly code: string
  readonly requestId: string

  constructor(code: string, message: string, requestId: string) {
    super(message)
    this.name = 'ApiRequestError'
    this.code = code
    this.requestId = requestId
  }
}

export async function analyzeRepository(repositoryUrl: string): Promise<AnalyzeResponse> {
  const requestBody: AnalyzeRequest = { repository_url: repositoryUrl }

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/api/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody),
    })
  } catch {
    throw new Error('Could not reach the analysis server.')
  }

  const json: unknown = await response.json().catch(() => null)

  if (!response.ok) {
    const apiError = ApiErrorSchema.safeParse(json)
    if (apiError.success) {
      const { code, message, requestId } = apiError.data.error
      throw new ApiRequestError(code, message, requestId)
    }
    throw new Error(`Analysis failed (HTTP ${response.status}).`)
  }

  const result = AnalyzeResponseSchema.safeParse(json)
  if (!result.success) {
    throw new Error(`Server response did not match the expected schema: ${result.error.message}`)
  }
  return result.data
}
