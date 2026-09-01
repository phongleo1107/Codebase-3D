/**
 * Mirror of the response-shaped limits in `backend/app/config.py` `Settings`.
 *
 * These are duplicated deliberately, not imported: the frontend does not trust
 * the backend to have enforced them. `docs/ARCHITECTURE.md` "Security
 * Boundaries" calls the API → browser transition its own validation layer, and
 * `docs/CURRENT_STATE.md` records that MAX_NODES / MAX_EDGES are currently
 * enforced *nowhere* on the server (they belong to the unwritten routing
 * layer). So for those two, this file is the only place they bind anything at
 * all today.
 *
 * If a value here drifts from `Settings`, the symptom is a valid response
 * being rejected — loud, not silent. Keep them in step.
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
