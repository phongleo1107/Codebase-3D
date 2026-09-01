# MVP SPEC — CODEBASE VISUALIZER

> **2026-09-01 — visualization is 2D, not 3D (ADR-022 in [docs/DECISIONS.md](docs/DECISIONS.md), supersedes ADR-002 and ADR-004).** The owner's goal shifted from matching this document's original "3D" framing to shipping a small, learnable, minimally-bespoke frontend. The graph renders with Cytoscape.js in 2D. Sections below are updated in place to match; the rest of this document (analysis, data model, security) is unaffected.

Build a polished web application that visualizes a GitHub repository as an interactive dependency graph.

Working name: **Codebase 3D** *(name predates the 2D pivot; not renamed)*

Core promise:

> Paste a public GitHub repository URL and understand its structure visually within seconds.

This is an engineering MVP, not a prototype/mockup. Prioritize correctness, security, maintainability, and a polished UI over feature count.

---

# 1. MVP SCOPE

### Input

User enters:

```text
https://github.com/owner/repository
```

The application analyzes the repository and produces a 2D visualization.

### Supported language

V1 should support **TypeScript / JavaScript only**.

Do not attempt to support every programming language.

### Visualization

Display:

* Directories/modules
* Files
* Import/dependency relationships
* Module boundaries
* Basic file metadata

Users can:

* Zoom/pan
* Hover nodes
* Click nodes
* Search for files/modules
* Focus camera on a node
* Trace direct dependencies
* Open source code for a selected file
* Collapse/expand directory/module levels

---

# 2. CORE USER FLOW

```text
Landing Page
    ↓
Paste GitHub URL
    ↓
Validate URL
    ↓
Fetch repository
    ↓
Analyze repository
    ↓
Build dependency graph
    ↓
Render visualization
    ↓
Explore codebase
```

The experience should feel fast and intentional.

---

# 3. ARCHITECTURE

Use a clear separation between:

```text
Frontend
    ↓
Backend API
    ↓
Repository Analyzer
    ↓
Graph Model
```

Suggested stack:

### Frontend

* React
* TypeScript
* Cytoscape.js (2D graph rendering/layout)
* Modern CSS/Tailwind

### Backend

* Python
* FastAPI

### Analysis

Use deterministic parsing wherever possible.

Do NOT use an LLM to determine basic imports/dependencies.

The graph should come from actual repository source code.

---

# 4. DATA MODEL

Create an internal graph representation.

### Node

```typescript
type GraphNode = {
    id: string
    name: string
    path: string
    type: "directory" | "file"
    language?: string
}
```

### Edge

```typescript
type GraphEdge = {
    source: string
    target: string
    relationship: "imports"
}
```

Keep the model independent from the visualization layer.

The frontend should receive structured graph data rather than raw parser output.

---

# 5. REPOSITORY ANALYSIS

For V1:

1. Validate GitHub URL.
2. Clone/download the repository into an isolated temporary directory.
3. Enforce strict limits:

   * maximum repository size
   * maximum file count
   * maximum individual file size
   * maximum analysis time
4. Ignore:

   * `.git`
   * `node_modules`
   * build artifacts
   * binaries
   * images
   * archives
   * secrets/config files where appropriate
5. Parse JS/TS source files.
6. Extract imports.
7. Resolve local imports where possible.
8. Build the dependency graph.
9. Delete temporary repository data after analysis.

If parsing fails for a file, skip that file and report it rather than crashing the entire analysis.

---

# 6. VISUAL DESIGN

The visualization is the primary product.

Do NOT build a generic SaaS dashboard.

Avoid:

* excessive cards
* unnecessary statistics
* fake AI insights
* gradients everywhere
* excessive animations
* meaningless glowing effects
* giant hero sections inside the application

Aim for:

* dark, minimal interface
* high information density
* excellent typography
* subtle depth
* smooth camera movement
* restrained animation
* clear hierarchy

### Visual hierarchy

Directory/module:

> larger visual grouping

File:

> individual node

Dependency:

> thin connection

Selected node:

> visually highlighted

Related nodes:

> subtly highlighted

Unrelated nodes:

> visually de-emphasized

---

# 7. INTERACTION

### Hover

Show:

```text
File
src/auth/AuthService.ts

Type: File
Imports: 4
Imported by: 7
```

### Click

Open an inspector panel containing:

* file path
* file type
* imports
* imported by
* source preview

### Search

User searches:

```text
AuthService
```

Camera smoothly moves to the matching node.

### Trace

When a node is selected:

```text
Selected File
    ↓
Direct Dependencies
    ↓
Direct Dependents
```

Highlight only the relevant relationships.

---

# 8. SOURCE CODE VIEW

Show source code in a read-only viewer.

Never execute repository code.

Never install or execute repository dependencies.

Never run:

```text
npm install
npm run
node
python
bash
make
```

The repository is treated strictly as **untrusted data**.

---

# 9. API DESIGN

Example:

```text
POST /api/analyze
```

Request:

```json
{
  "repository_url": "https://github.com/owner/repository"
}
```

Response:

```json
{
  "repository": {
    "owner": "owner",
    "name": "repository"
  },
  "nodes": [],
  "edges": [],
  "stats": {
    "files": 0,
    "directories": 0,
    "dependencies": 0
  }
}
```

Keep API responses deterministic and schema-validated.

---

# 10. SECURITY REQUIREMENTS

This is critical.

Treat every repository as hostile input.

### SSRF

Do not allow arbitrary URLs.

Only accept:

```text
https://github.com/<owner>/<repo>
```

Reject:

* localhost
* 127.0.0.1
* private IPs
* internal hostnames
* arbitrary protocols
* redirects to arbitrary domains

Prefer using the GitHub API/archive endpoint instead of blindly fetching user-provided URLs.

### Command injection

Never construct shell commands using unsanitized user input.

Prefer subprocess argument arrays over shell strings.

Never use:

```python
shell=True
```

with user-controlled input.

### Path traversal

Never trust repository paths.

Prevent:

```text
../../etc/passwd
```

and similar traversal attacks.

Resolve paths and verify that they remain inside the temporary repository directory.

### Resource exhaustion

Implement limits for:

* repository size
* compressed download size
* extracted size
* file count
* file size
* graph node count
* graph edge count
* analysis duration
* request body size

Reject repositories exceeding limits.

### Zip bombs / archive attacks

If archives are used:

* validate archive contents
* enforce extracted-size limits
* prevent path traversal
* reject suspicious compression ratios

### Malicious source code

Source code must NEVER be executed.

Do not:

* install dependencies
* run build scripts
* execute package scripts
* invoke interpreters
* execute Makefiles
* execute shell scripts

Parsing only.

### Secrets

Never log:

* repository contents
* source code
* access tokens
* GitHub credentials
* environment variables

Never return `.env`, credentials, private keys, or similar sensitive files to the frontend.

Add explicit secret-file filtering.

---

# 11. PRIVACY

V1 should ideally be:

```text
No login
No database
No persistent repository storage
No source-code retention
```

Process the repository temporarily and delete it when analysis finishes.

Clearly tell users:

> Repository contents are processed temporarily for analysis and are not intentionally stored.

Do not claim stronger privacy guarantees than the implementation actually provides.

---

# 12. RATE LIMITING

Implement basic protection against abuse.

Example:

```text
IP → limited analysis requests / minute
```

Also limit concurrent analysis jobs.

One user should not be able to consume the entire server.

---

# 13. ERROR HANDLING

Never expose:

* stack traces
* filesystem paths
* internal server details
* environment variables
* subprocess output containing secrets

Return clean errors:

```json
{
  "error": {
    "code": "REPOSITORY_TOO_LARGE",
    "message": "Repository exceeds the maximum supported size."
  }
}
```

Log detailed errors server-side without sensitive content.

---

# 14. FRONTEND SECURITY

Implement:

* strict Content Security Policy where practical
* no dangerous `innerHTML`
* sanitize displayed repository/source metadata
* escape source code correctly
* validate API responses
* do not put secrets in frontend code

Never expose GitHub tokens through the frontend.

---

# 15. TESTING

Before calling the MVP complete, test:

### Normal

* small JS repository
* medium TS repository
* repository with circular imports
* missing imports
* malformed source files
* monorepo structure

### Security

* malicious GitHub URL
* localhost URL
* private IP URL
* URL with redirects
* path traversal filenames
* huge repository
* huge individual file
* thousands of files
* deeply nested directories
* malicious archive if archives are used
* source containing HTML/JS payloads
* source containing fake shell commands
* source containing secrets

### Reliability

* parser crash
* network timeout
* GitHub unavailable
* malformed API response
* analysis timeout
* duplicate requests
* concurrent requests

---

# 16. ENGINEERING RULES

Do not:

* build unnecessary authentication
* build payments
* build teams
* build an AI chatbot
* build a database unless required
* support multiple languages prematurely
* execute repository code
* create a microservice architecture
* over-engineer the backend

Do:

* keep components small
* use typed models
* validate inputs at boundaries
* write tests for security-critical code
* separate parsing from visualization
* keep deterministic logic deterministic
* document important security decisions
* make the application easy to run locally

---

# 17. DEFINITION OF DONE

The MVP is complete when:

```text
[ ] User can enter a public GitHub repository
[ ] Repository is safely retrieved
[ ] Repository is treated as untrusted data
[ ] JS/TS imports are correctly extracted
[ ] Dependency graph is generated
[ ] Graph renders (2D)
[ ] Nodes can be explored
[ ] Search works
[ ] Dependency tracing works
[ ] Source preview works
[ ] Large repositories are rejected
[ ] Malicious URLs are rejected
[ ] Repository code is never executed
[ ] Temporary files are deleted
[ ] Rate limiting exists
[ ] Sensitive files are filtered
[ ] Errors don't leak internal information
[ ] Security tests pass
[ ] No hardcoded secrets
[ ] README explains architecture and security model
[ ] Application can be deployed with one command

```

## PRIORITY

If time runs out, prioritize in this order:

1. Security
2. Correct dependency graph
3. Graph visualization
4. Navigation/search
5. Source inspection
6. Polish
7. Everything else

Do not sacrifice security or correctness to add more features.

Build the smallest product that feels genuinely impressive when someone pastes a repository URL and watches their codebase become a navigable graph.
