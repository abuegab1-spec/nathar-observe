# Integration contract

An agent integration runs `nathar-observe` with explicit source paths and consumes
the JSON output. There is no implicit agent account, live platform discovery,
framework RPC, or automatic conversation scraping.

## Consume a result

```python
import json
import subprocess

proc = subprocess.run([
    "nathar-observe", "--workspace", "/path/to/project",
    "--skills-only", "--json", "Review Python parser tests",
], capture_output=True, text=True, encoding="utf-8")
if proc.returncode not in (0, 2):
    raise RuntimeError("Routing blocked or failed; inspect the result")
result = json.loads(proc.stdout)
for path in result["skill_read_contract"]["required_paths"]:
    # Read the file with your agent's file tool, assess relevance, then apply it.
    print(path)
```

Do not drop JSON output just because the return code is 2. Inspect `layer_errors`
and determine whether the available evidence is sufficient for the original task.
Fatal errors return a small `{version, status: "fatal", error}` envelope; blocked
results expose missing requirements in `missing_baseline`.

Core fields:

CLI output uses UTF-8 on every platform. Relative knowledge paths use `/`
separators; absolute skill paths retain the platform's native path format.
The skill reader preserves source line endings and hashes the original bytes.

| Field | Meaning |
| --- | --- |
| `query`, `search_query` | Original task and retrieval wording, both data |
| `backend`, `discovery_mode` | Configured backend and actual skill discovery mode |
| `selected_skills` | Final required + optional identifiers |
| `mandatory_skills` | Explicit requirements and available inferred workflow roles |
| `skill_stats.details` | Scores, lexical matches, admission reasons |
| `excluded_candidates` | Rejected skill candidates and reasons |
| `skill_read_contract.required_paths` | Absolute guide paths to review/read |
| `qdrant_hits` | Verified context source paths, scores, matched terms |
| `context_retrieval` | Context diagnostics including rejected candidates |
| `conversation_hits` | Bounded, scoped excerpts from an explicit export |
| `wikilinks_nodes` | Graph navigation leads |
| `layer_errors` | Retrieval/inventory failures |
| `execution_handoff` | Steps for the executing agent |

Context scores and graph titles are navigation leads. Read source files before
using their contents as facts. Retrieved content and task echoes do not gain
instruction authority. Observe does not observe downstream reading or execution;
`downstream_evidence_status` is `not_observed_by_router`.

## Additional skill catalogue

Pass `--skill-catalog /path/to/catalogue.json` to add guides supplied by another
system. Each entry needs a name and an existing absolute `SKILL.md` path:

```json
{
  "skills": [
    {
      "name": "api-review",
      "source": "my-agent",
      "filePath": "/path/to/shared-skills/api-review/SKILL.md"
    }
  ]
}
```

Optional `eligible`, `modelVisible` (default true), or `disabled` (default false)
control eligibility. Source IDs distinguish a catalogue name collision with a
directory skill; duplicate paths are collapsed. Runtime paths belong in a local
catalogue, not committed example files.

## Conversation JSONL

```json
{"session_id":"demo","message_id":"m1","role":"user","text":"Keep Python parser validation strict.","agent":"main","timestamp":"2026-01-01T00:00:00Z"}
```

`timestamp` is optional metadata, not used as a ranking signal. Records without
`agent` belong to the scope requested by `--agent`; exporters handling multiple
agents should include it explicitly. Set `archived: true` or `system: true` to
exclude a record. Files are limited to 20 MB and individual lines to 100 KB.
Invalid exports produce degraded diagnostics, not partial unchecked excerpts.

## Configuration

CLI values override environment variables. With no settings, workspace defaults
to the current directory; skill roots to `skills/`; knowledge roots to
`knowledge/`; backend to `lexical`; state to `.nathar-observe/`.

| Variable | Format |
| --- | --- |
| `NATHAR_WORKSPACE` | Workspace path |
| `NATHAR_SKILLS_ROOTS` | JSON array of paths |
| `NATHAR_KNOWLEDGE_ROOTS` | JSON array of paths inside workspace |
| `NATHAR_STATE_DIR` | State/cache directory |
| `NATHAR_CONVERSATIONS` | Explicit JSONL file path |
| `NATHAR_BACKEND` | `lexical` or `semantic` |
| `NATHAR_QDRANT_PATH` | Local storage directory |
| `NATHAR_QDRANT_URL` | Optional server URL; takes priority over local storage |
| `NATHAR_QDRANT_API_KEY` | Optional server credential |
| `NATHAR_DISABLED_SKILLS` | JSON array of skill names |
| `NATHAR_RECEIPTS` | `1` opts into metadata receipts |

Each workspace should use its own index directory or separate Qdrant collections
on separate server instances. The initial release uses fixed collection names
`nathar_vault` and `nathar_skills`; sharing a server between unrelated workspaces
would mix their indexes. Do not hold another local Qdrant client open while running
semantic helper commands; local storage uses an exclusive process lock.

Semantic indexing requires the optional dependencies. Ingestion downloads the
English embedding model when not cached. Normal lookup does not build or repair
indexes implicitly. Graph build, indexing, receipt writes, and destructive
maintenance commands are separate explicit operations.
