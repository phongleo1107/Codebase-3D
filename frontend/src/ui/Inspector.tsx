/**
 * PRD §7's inspector, at scaffold scope: path, type, and the counts that are
 * already in the response. No source preview — `POST /api/source` is deferred
 * post-MVP together with the whole HMAC token mechanism (ADR-007, ADR-013).
 *
 * **Rendering guardrail.** `description` is repository-authored text quoted out
 * of a comment (ADR-013). It is interpolated as a React child, which escapes
 * it, and this file contains no `dangerouslySetInnerHTML` — nor may it ever.
 * The backend caps and strips it too; this is the second of two independent
 * applications of the rule, not the only one.
 */
import type { GraphNode } from '../api/schema.ts'

function Row({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex justify-between gap-4 py-0.5">
      <dt className="text-neutral-500">{label}</dt>
      <dd className="tabular-nums text-neutral-300">{value}</dd>
    </div>
  )
}

export function Inspector({ node }: { node: GraphNode | null }) {
  if (node === null) {
    return (
      <p className="p-4 text-xs text-neutral-600">
        Select a node to trace its direct dependencies.
      </p>
    )
  }

  const isFile = node.type === 'file'

  return (
    <div className="flex flex-col gap-4 p-4 text-xs">
      <div>
        <p className="text-[10px] uppercase tracking-wider text-neutral-600">{node.type}</p>
        <p className="break-all font-mono text-sm text-neutral-100">{node.path}</p>
      </div>

      {node.description !== null && (
        // Repository content. A text node, always.
        <p className="border-l border-neutral-800 pl-3 leading-relaxed text-neutral-400">
          {node.description}
        </p>
      )}

      <dl className="border-t border-neutral-900 pt-3">
        {node.language !== null && <Row label="Language" value={node.language} />}
        {isFile ? (
          <>
            <Row label="Imports" value={node.imports ?? 0} />
            <Row label="Imported by" value={node.importedBy ?? 0} />
            <Row label="External imports" value={node.externalImports ?? 0} />
            <Row label="Unresolved imports" value={node.unresolvedImports ?? 0} />
            <Row label="Bytes" value={node.bytes ?? 0} />
            <Row label="Lines" value={node.loc ?? 0} />
          </>
        ) : (
          <>
            <Row label="Files" value={node.fileCount ?? 0} />
            <Row label="Total bytes" value={node.totalBytes ?? 0} />
          </>
        )}
        <Row label="Depth" value={node.depth} />
      </dl>
    </div>
  )
}
