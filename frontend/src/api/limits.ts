/**
 * Mirror of the response-shaped limits in `backend/app/config.py` `Settings`.
 *
 * These are duplicated deliberately, not imported: the frontend does not trust
 * the backend to have enforced them. `docs/ARCHITECTURE.md` "Security
 * Boundaries" calls the API → browser transition its own validation layer, and
 * it stays one whether or not the server checks too. MAX_NODES / MAX_EDGES are
 * now enforced there as well — ADR-023 hands `build_graph` a `GraphLimits` so
 * it builds a smaller graph rather than the router slicing a large one — which
 * makes this file defence in depth for those two rather than the only depth.
 *
 * Not trusting the server is not the same as diverging from it. A cap here
 * that sits *below* its `Settings` counterpart rejects a response the server
 * was entitled to send, and zod rejects the whole document rather than
 * trimming it, so the symptom is a blank screen — loud, but loud about the
 * wrong thing. Keep them in step.
 */
export const LIMITS = {
  /** Settings.MAX_NODES */
  MAX_NODES: 6000,
  /** Settings.MAX_EDGES */
  MAX_EDGES: 20_000,
  /** Settings.MAX_PATH_LENGTH */
  MAX_PATH_LENGTH: 1024,
  /** Settings.MAX_URL_LENGTH */
  MAX_URL_LENGTH: 300,
  /** Settings.MAX_DESCRIPTION_CHARS */
  MAX_DESCRIPTION_CHARS: 500,
  /** Settings.MAX_SERVICE_ENDPOINTS */
  MAX_SERVICE_ENDPOINTS: 200,
  /** Settings.MAX_ENDPOINT_SUMMARY_CHARS */
  MAX_ENDPOINT_SUMMARY_CHARS: 300,
  /** Settings.MAX_COMPONENT_DIAGRAM_CHARS */
  MAX_COMPONENT_DIAGRAM_CHARS: 20_000,
} as const
