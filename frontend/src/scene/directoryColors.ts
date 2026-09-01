/**
 * Directory-tint palette (ADR-026).
 *
 * Directories stopped being rendered as compound boxes; this is what replaced
 * them as the "which directory is this file in" cue. A fixed, hand-picked
 * palette rather than a computed HSL-from-hash: a hash can land on a color
 * with poor contrast against `COLORS.canvas` (`#0b0d10`), and a fixed set is
 * reviewable in one glance instead of trusted to a formula.
 */

/** Every entry reads clearly on `#0b0d10` at the small radius file nodes use. */
export const DIRECTORY_PALETTE: readonly string[] = [
  '#58a6ff', // blue
  '#3fb950', // green
  '#d29922', // amber
  '#f778ba', // pink
  '#a371f7', // purple
  '#39c5cf', // cyan
  '#f85149', // red
  '#8b949e', // neutral gray (also the "root" bucket's color, see below)
]

/** No directory segment — a file sitting directly at the repository root. */
export const ROOT_TINT_KEY = ''

/**
 * The first path segment, or `ROOT_TINT_KEY` for a root-level file. Computed
 * from `GraphNode.path` rather than walking `parent` links: the wire format
 * already gives every node its full repository-relative path (ADR-006 keeps
 * hierarchy on `parent`, but `path` is the cheaper read for "top segment").
 */
export function tintKey(path: string): string {
  const separator = path.indexOf('/')
  return separator === -1 ? ROOT_TINT_KEY : path.slice(0, separator)
}

/**
 * Deterministic per-key color. Two different keys can collide on the same
 * color once there are more top-level directories than palette entries —
 * acceptable for a secondary grouping cue, not the primary read.
 */
export function tintColor(key: string): string {
  if (key === ROOT_TINT_KEY) return DIRECTORY_PALETTE[DIRECTORY_PALETTE.length - 1] as string
  let hash = 0
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) | 0
  }
  const index = Math.abs(hash) % (DIRECTORY_PALETTE.length - 1)
  return DIRECTORY_PALETTE[index] as string
}
