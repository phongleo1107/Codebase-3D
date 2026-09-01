/**
 * PRD §7's service map: the routes route detection found, grouped by the file
 * that defines them, route -> file -> summary.
 *
 * **Rendering guardrail.** `summary` is repository-authored text quoted out of
 * the comment above the handler (ADR-013), exactly like `GraphNode.description`
 * in `Inspector.tsx`. It is interpolated as a React child, which escapes it,
 * and this file contains no `dangerouslySetInnerHTML` — nor may it ever. Skip
 * the summary line entirely when absent rather than rendering a placeholder.
 */
import type { ServiceEndpoint } from '../api/schema.ts'
import { useGraphStore } from '../store/graphStore.ts'

function groupByFile(endpoints: ServiceEndpoint[]): Map<string, ServiceEndpoint[]> {
  const groups = new Map<string, ServiceEndpoint[]>()
  for (const endpoint of endpoints) {
    const existing = groups.get(endpoint.file)
    if (existing === undefined) {
      groups.set(endpoint.file, [endpoint])
    } else {
      existing.push(endpoint)
    }
  }
  return groups
}

function EndpointRow({ endpoint }: { endpoint: ServiceEndpoint }) {
  return (
    <div className="py-1.5">
      <div className="flex items-baseline gap-2">
        <span className="w-14 shrink-0 font-mono text-[10px] uppercase tracking-wider text-neutral-500">
          {endpoint.method}
        </span>
        <span className="break-all font-mono text-xs text-neutral-100">{endpoint.path}</span>
      </div>
      {endpoint.summary !== null && (
        // Repository content. A text node, always.
        <p className="mt-0.5 pl-16 leading-relaxed text-neutral-400">{endpoint.summary}</p>
      )}
    </div>
  )
}

export function ServiceMap({ serviceMap }: { serviceMap: ServiceEndpoint[] | null }) {
  if (serviceMap === null || serviceMap.length === 0) {
    return <p className="p-4 text-xs text-neutral-600">No routes were detected in this repository.</p>
  }

  const groups = groupByFile(serviceMap)

  return (
    <div className="flex flex-col gap-4 p-4 text-xs">
      {[...groups.entries()].map(([file, endpoints]) => (
        <div key={file}>
          <p className="break-all border-b border-neutral-900 pb-1 font-mono text-[10px] text-neutral-600">
            {file}
          </p>
          <div className="divide-y divide-neutral-900/60">
            {endpoints.map((endpoint) => (
              <EndpointRow key={`${endpoint.method} ${endpoint.path}`} endpoint={endpoint} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

/** Convenience wrapper reading directly from the store, for App.tsx to mount. */
export function ServiceMapPanel() {
  const serviceMap = useGraphStore((state) => state.response?.serviceMap ?? null)
  return <ServiceMap serviceMap={serviceMap} />
}
