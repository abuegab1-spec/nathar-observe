from __future__ import annotations

import argparse
# GraphML is the persistence format; caches contain no executable objects.
import re
import sys
from collections import deque
from pathlib import Path

import networkx as nx
from . import config

# Wikilink regex. Captures the title only (strips alias + section).
# `[[A|B]]` → "A";  `[[A#sec]]` → "A";  `[[A|B#sec]]` → "A"
WIKILINK_RE = re.compile(
    r"\[\[([^\[\]\|#\n]+?)(?:\|[^\]\n]+?)?(?:#[^\]\n]+?)?\]\]"
)

# Agent directives / bot tokens that look like wikilinks but aren't notes.
# Filter these from indexing to keep graph signal high.
DIRECTIVE_TOKENS = {
    "reply_to_current", "audio_as_voice", "embed",
    "approve", "status", "help", "reasoning",
}

# Allow extension variants in wikilinks (e.g. [[SOUL.md]] resolves to "SOUL")
# Wikilinks should not normally include extensions, but some notes do.

# Files / dirs to skip (large, low-signal, or noisy)
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".obsidian",
}

DEFAULT_ROOTS = [str(p) for p in config.settings.knowledge_roots]
ROOT_FILES = []
CACHE_PKL = config.settings.state_dir / "legacy-wikilinks.pkl"
CACHE_GML = config.settings.state_dir / "wikilinks.graphml"


# --------------------------------------------------------------------- helpers


def derive_title(path: Path) -> str:
    """Decide the canonical title for a .md file.

    Default: filename stem (lowercased + dashes).
    Convention: SKILL.md inside a folder → use folder name as title
    so `[[obsidian-vault-patterns]]` resolves to that skill, not a generic "SKILL".
    """
    if path.name == "SKILL.md" and path.parent.name:
        return normalize(path.parent.name)
    return normalize(path.stem)


def normalize(title: str) -> str:
    """Normalize wikilink target for fuzzy matching.

    - lowercase
    - spaces → dashes
    - strip trailing .md / .markdown
    - strip leading/trailing whitespace
    """
    t = title.strip().lower().replace(" ", "-")
    for ext in (".md", ".markdown"):
        if t.endswith(ext):
            t = t[: -len(ext)]
    return t


def is_directive(title: str) -> bool:
    """Skip Agent bot directive tokens that look like wikilinks."""
    norm = normalize(title)
    return norm in DIRECTIVE_TOKENS or norm.startswith("media:")


def find_md_files(roots: list[str], include_protocol: bool = True) -> list[Path]:
    """Find Markdown files inside explicitly configured knowledge roots."""
    files: list[Path] = []
    for root in roots:
        p = config.resolve_path(root)
        if not p.is_relative_to(config.settings.workspace):
            raise ValueError("Knowledge roots must remain inside the configured workspace")
        if not p.exists():
            continue
        if p.is_file() and p.suffix == ".md":
            files.append(p)
        elif p.is_dir():
            for f in p.rglob("*.md"):
                if not f.resolve().is_relative_to(config.settings.workspace):
                    continue
                if any(part in SKIP_DIRS for part in f.parts):
                    continue
                files.append(f)

    # Protocol files at workspace root (only when explicitly included)
    if include_protocol:
        workspace = config.settings.workspace
        for rf in ROOT_FILES:
            candidate = workspace / rf
            if candidate.exists() and candidate.suffix == ".md":
                files.append(candidate)

    # Dedupe (a file should be unique)
    return sorted(set(files))


def extract_wikilinks(text: str) -> list[str]:
    """Return list of wikilink targets (normalized)."""
    targets = []
    for m in WIKILINK_RE.finditer(text):
        title = m.group(1).strip()
        if title:
            targets.append(title)
    return targets


def resolve_target(target: str, known_titles: set[str]) -> str | None:
    """Resolve a wikilink target to an actual note title.

    Wikilinks can be loose:
      [[SOUL.md]]      → resolve to "SOUL.md" (filename stem)
      [[SOUL]]         → fuzzy match to "SOUL"
      [[research/github-curated/INDEX]] → match path stem too
    """
    norm = normalize(target)

    # 1) Exact normalized match
    if norm in known_titles:
        return norm

    # 2) Fuzzy: endswith containment (handles "research/github-curated/INDEX" → "INDEX")
    if "/" in target:
        last = norm.rsplit("/", 1)[-1]
        if last in known_titles:
            return last

    # 3) Substring match — return first hit
    matches = [t for t in known_titles if norm in t or t in norm]
    if matches:
        # Prefer shortest match (more specific)
        return min(matches, key=len)

    return None


# --------------------------------------------------------------------- build


def build_graph(roots: list[str]) -> tuple[nx.DiGraph, dict]:
    """Build adjacency graph. Resolves wikilinks to existing notes."""
    files = find_md_files(roots)
    if not files:
        print(f"No .md files found under roots: {roots}")
        return nx.DiGraph(), {"files": 0, "nodes": 0, "edges": 0}

    # Pass 1: collect all titles (normalized).
    # SKILL.md inside a folder uses the folder name as its title
    # (so `[[obsidian-vault-patterns]]` resolves to that skill, not a generic "SKILL").
    title_to_paths: dict[str, list[Path]] = {}
    for f in files:
        title = derive_title(f)
        title_to_paths.setdefault(title, []).append(f)

    # Common aliases: SOUL.md, AGENTS.md, etc. are recognized as-is
    known_titles = set(title_to_paths.keys())

    # Pass 2: parse wikilinks, resolve to known titles when possible
    G = nx.DiGraph()
    for title in known_titles:
        G.add_node(title)

    raw_edges = 0
    resolved_edges = 0
    unresolved_examples: list[tuple[str, str]] = []

    for title, paths in title_to_paths.items():
        # Use first file found for content; merge all if multiple
        wikilink_set: set[str] = set()
        for p in paths:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for tgt in extract_wikilinks(text):
                wikilink_set.add(tgt)

        for tgt in wikilink_set:
            raw_edges += 1
            if is_directive(tgt):
                continue
            if tgt == title or normalize(tgt) == title:
                continue  # skip self-loops
            resolved = resolve_target(tgt, known_titles)
            if resolved:
                G.add_edge(title, resolved)
                resolved_edges += 1
            else:
                unresolved_examples.append((title, tgt))

    stats = {
        "files": len(files),
        "titles": len(known_titles),
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "raw_edges": raw_edges,
        "resolved_edges": resolved_edges,
        "unresolved": raw_edges - resolved_edges,
        "unresolved_sample": unresolved_examples[:25],
    }
    return G, stats


def save_graph(G: nx.DiGraph) -> None:
    # Persist graph data as GraphML.
    CACHE_GML.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(G, str(CACHE_GML))


def load_graph() -> nx.DiGraph | None:
    if CACHE_GML.exists():
        try:
            return nx.read_graphml(str(CACHE_GML))
        except Exception as e:
            print(f"GraphML cache corrupt ({type(e).__name__}: {e}). Rebuilding.", file=sys.stderr)
            return None
    return None


# --------------------------------------------------------------------- queries


# (search_hops implemented together with shortest_path above for clarity)


def shortest_path(G: nx.DiGraph, src: str, dst: str) -> list[str] | None:
    src_matches = resolve_query(G, src)
    dst_matches = resolve_query(G, dst)
    if not src_matches or not dst_matches:
        return None
    try:
        return nx.shortest_path(G, src_matches[0], dst_matches[0])
    except nx.NetworkXNoPath:
        return None


def resolve_query(G: nx.DiGraph, query: str, broad: bool = False) -> list[str]:
    """Resolve a fuzzy query to node names, tiered by precision.

    Tier 1 — EXACT match (case-insensitive). Returns ONLY the exact match.
    Tier 2 — STARTSWITH match. Returns ONLY startswith matches.
    Tier 3 — SUBSTRING match (broad). Returns ALL substring matches.

    Args:
        broad: if True, force Tier 3 behavior (skip precision tiers).
               Default False — prefer precise matches.
    """
    q = query.lower()
    matches = [n for n in G.nodes() if q in n.lower()]
    if not matches:
        return []

    if broad:
        # Sort: shortest first (most specific), then alphabetical
        return sorted(matches, key=lambda n: (len(n), n))

    # Tier 1: exact match
    exact = [n for n in matches if n.lower() == q]
    if exact:
        return exact

    # Tier 2: startswith
    starts = [n for n in matches if n.lower().startswith(q)]
    if starts:
        return starts

    # Tier 3: substring (broad fallback)
    return matches


def search_hops(G: nx.DiGraph, query: str, hops: int = 2, limit: int = 30, broad: bool = False, skills_only: bool = False) -> tuple[list[tuple], str]:
    """BFS up to N hops from any node matching `query`.

    Args:
        broad: if True, accept substring matches (loose).
               Default False — only exact + startswith.
        skills_only: if True, restrict result set to nodes that are skills
                     (have a corresponding skills/<name>/SKILL.md).
    """
    matches = resolve_query(G, query, broad=broad)
    if not matches:
        return [], (
            f"No node matches '{query}'.\n"
            f"  Total nodes: {G.number_of_nodes()}\n"
            f"  Sample: {sorted(G.nodes())[:10]}"
        )

    seed = matches[0]
    if skills_only:
        skill_root = config.settings.skills_roots[0]
        skill_names = {p.parent.name for p in skill_root.glob("*/SKILL.md")} if skill_root.exists() else set()

    visited = {seed: 0}
    queue = deque([seed])
    results = []

    while queue and len(results) < limit:
        cur = queue.popleft()
        cur_hop = visited[cur]
        if cur_hop >= hops:
            continue
        for direction in ("successors", "predecessors"):
            for nb in getattr(G, direction)(cur):
                if nb not in visited:
                    visited[nb] = cur_hop + 1
                    # Filter to skills if requested
                    if not skills_only or nb in skill_names or nb == seed:
                        results.append((cur, nb, cur_hop + 1, "out" if direction == "successors" else "in"))
                    queue.append(nb)
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break

    extra = f" (matched {len(matches)} other variants)" if len(matches) > 1 else ""
    info = f"Seed: `{seed}`{extra}. Found {len(results)} notes within {hops} hops."
    return results, info


def hub_report(G: nx.DiGraph, top: int = 20) -> list[tuple]:
    rows = []
    for n in sorted(G.nodes(), key=lambda x: G.degree(x), reverse=True)[:top]:
        rows.append((n, G.in_degree(n), G.out_degree(n), G.degree(n)))
    return rows


def orphan_report(G: nx.DiGraph) -> list[str]:
    return sorted([n for n in G.nodes() if G.degree(n) == 0])


# --------------------------------------------------------------------- main


def cmd_build(args) -> int:
    G, stats = build_graph(args.roots)
    if G.number_of_nodes() == 0:
        return 1
    save_graph(G)
    print(f"✓ Built wikilink graph: {stats['nodes']} nodes / {stats['edges']} edges")
    print(f"  Files scanned: {stats['files']}")
    print(f"  Wikilinks found: {stats['raw_edges']} raw → {stats['resolved_edges']} resolved ({stats['unresolved']} unresolved)")
    if stats["unresolved_sample"]:
        print(f"  Unresolved examples (target not found in vault):")
        for src, tgt in stats["unresolved_sample"][:10]:
            print(f"    {src} → {tgt}")
    print(f"  Saved: {CACHE_GML} (GraphML, no pickle)")
    if CACHE_PKL.exists():
        print(f"  WARNING: legacy pickle still present at {CACHE_PKL}", file=sys.stderr)
    print()
    print("TOP 10 HUBS (total degree):")
    for n, _, _, deg in hub_report(G, 10):
        print(f"  {deg:4d}   {n}")
    return 0


def cmd_search(args) -> int:
    G = load_graph()
    if not G:
        print("No graph cached. Run: nathar-graph build")
        return 1
    results, info = search_hops(
        G, args.query, args.hops, args.limit,
        broad=args.broad, skills_only=args.skills_only,
    )
    print(f"# {info}\n")
    if not results:
        print(f"# {info}")
        return 0
    for src, tgt, hop, direction in results:
        arrow = "→" if direction == "out" else "←"
        print(f"  hop {hop}  {src} {arrow} {tgt}")
    return 0


def cmd_path(args) -> int:
    G = load_graph()
    if not G:
        print("No graph cached. Run: nathar-graph build")
        return 1
    path = shortest_path(G, args.src, args.dst)
    if path is None:
        print(f"No path between `{args.src}` and `{args.dst}`.")
        print("  (Either the nodes don't exist or they're in different connected components)")
        return 0
    print(" → ".join(path))
    print(f"({len(path)-1} hops)")
    return 0


def cmd_hubs(args) -> int:
    G = load_graph()
    if not G:
        print("No graph cached.")
        return 1
    print("TOP CONNECTED NODES:")
    print(f"  {'deg':>4}  {'in':>3}  {'out':>3}   note")
    print(f"  {'---':>4}  {'--':>3}  {'---':>3}   ----")
    for n, indeg, outdeg, deg in hub_report(G, args.top):
        print(f"  {deg:4d}  {indeg:3d}  {outdeg:3d}   {n}")
    return 0


def cmd_orphans(args) -> int:
    G = load_graph()
    if not G:
        print("No graph cached.")
        return 1
    orphans = orphan_report(G)
    print(f"ORPHANS (degree 0): {len(orphans)} total")
    for o in orphans[:args.limit]:
        print(f"  - {o}")
    if len(orphans) > args.limit:
        print(f"  ... and {len(orphans)-args.limit} more")
    return 0


def cmd_validate(args) -> int:
    """Per-note wikilink validation.

    Reads a single file, extracts wikilinks, and reports which resolve
    against the graph and which are orphan (point to non-existent notes).

    Returns 0 if all wikilinks resolve, 1 if any orphan links found.
    """
    G = load_graph()
    if not G:
        print("No graph cached. Run `nathar_wikilinks.py build` first.")
        return 1

    path = Path(args.path)
    if not path.exists():
        print(f"File not found: {path}")
        return 1

    text = path.read_text(encoding="utf-8")
    targets = extract_wikilinks(text)
    if not targets:
        print(f"No wikilinks found in {path.name}")
        return 0

    known_titles = set(G.nodes())
    resolved = []
    orphans = []
    for t in targets:
        node = resolve_target(t, known_titles)
        if node is None:
            orphans.append(t)
        else:
            resolved.append((t, node))

    print(f"VALIDATE: {path.name}")
    print(f"  total wikilinks: {len(targets)}")
    print(f"  resolved: {len(resolved)}")
    print(f"  orphan: {len(orphans)}")
    if args.verbose:
        for t, n in resolved:
            print(f"  [✓] [[{t}]] -> {n}")
    if orphans:
        print(f"  ORPHAN LINKS:")
        for o in orphans:
            print(f"  [✗] [[{o}]] -> does not resolve")
        return 1
    print(f"  ✓ ALL WIKILINKS RESOLVE")
    return 0


def cmd_stats(args) -> int:
    G = load_graph()
    if not G:
        print("No graph cached.")
        return 1
    print(f"Nodes: {G.number_of_nodes()}")
    print(f"Edges: {G.number_of_edges()}")
    print(f"Density: {nx.density(G):.6f}")
    scc = list(nx.strongly_connected_components(G))
    wcc = list(nx.weakly_connected_components(G))
    print(f"Strongly connected components: {len(scc)}")
    print(f"Weakly connected components: {len(wcc)}")
    print(f"Largest weakly connected component: {max(len(c) for c in wcc)} nodes")
    orphans = len(orphan_report(G))
    print(f"Orphans (degree 0): {orphans}")
    return 0


def cmd_reset(args) -> int:
    for p in (CACHE_PKL, CACHE_GML):
        if p.exists():
            p.unlink()
            print(f"  removed: {p}")
    return 0


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parse YAML frontmatter as a dict (list fields kept as lists)."""
    import re
    fm: dict[str, object] = {}
    if not text.startswith("---"):
        return fm
    end = text.find("\n---", 3)
    if end == -1:
        return fm
    block = text[3:end]
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        if val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            items = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
            fm[key] = items
        else:
            fm[key] = val
    return fm


def skill_metadata(skill_root: Path) -> list[dict]:
    """Read all SKILL.md files, return list of metadata dicts."""
    out: list[dict] = []
    for p in sorted(skill_root.glob("*/SKILL.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            fm = parse_frontmatter(text)
        except OSError:
            fm = {}
        out.append({
            "folder": p.parent.name,
            "fm_name": fm.get("name", p.parent.name),
            "category": fm.get("category") or fm.get("type") or "",
            "tags": fm.get("tags") if isinstance(fm.get("tags"), list) else [],
            "origin": fm.get("origin") or "",
            "description": fm.get("description", "") if isinstance(fm.get("description"), str) else "",
        })
    return out


def cmd_related(args) -> int:
    """Show skills related to a given skill (same domain/category/origin).

    Tier 1: same `category` or `type`           (+10)
    Tier 2: any tag overlap                     (+3 per shared tag)
    Tier 3: same `origin`                       (+1)

    Result is ranked by score; ties sorted by folder name.
    """
    workspace = config.settings.workspace
    skill_root = workspace / "skills"
    if not skill_root.exists():
        print(f"No skills/ directory found at {skill_root}")
        return 1

    skills = skill_metadata(skill_root)
    q = args.name.lower()

    # Find target (folder OR frontmatter name, tiered like cmd_skill)
    target = None
    for s in skills:
        if s["folder"].lower() == q or s["fm_name"].lower() == q:
            target = s
            break
    if not target:
        starts = [s for s in skills if s["folder"].lower().startswith(q) or s["fm_name"].lower().startswith(q)]
        if len(starts) == 1:
            target = starts[0]
        elif len(starts) > 1:
            print(f"Ambiguous — {len(starts)} candidates:")
            for s in starts[:10]:
                print(f"  {s['folder']} (name: {s['fm_name']})")
            return 1
    if not target:
        subs = [s for s in skills if q in s["folder"].lower() or q in s["fm_name"].lower()]
        if len(subs) == 1:
            target = subs[0]
        elif len(subs) > 1:
            print(f"Ambiguous — {len(subs)} candidates:")
            for s in subs[:10]:
                print(f"  {s['folder']} (name: {s['fm_name']})")
            return 1
    if not target:
        print(f"No skill matches '{args.name}'.")
        print(f"  Total skills: {len(skills)}")
        return 1

    target_folder = target["folder"]
    target_label = target["fm_name"] if target["fm_name"] != target["folder"] else target["folder"]

    print(f"Target: {target_label}")
    if target_label != target_folder:
        print(f"  folder: {target_folder}")
    print(f"  category/type: {target['category'] or '—'}")
    print(f"  tags: {target['tags'] or '—'}")
    print(f"  origin: {target['origin'] or '—'}")
    print()

    target_tags = set(t.lower() for t in target["tags"])
    target_cat = target["category"]
    target_origin = target["origin"]

    related: list[tuple[int, str, dict]] = []
    for s in skills:
        if s["folder"] == target_folder:
            continue
        score = 0
        reasons = []

        # Tier 1: category/type match
        if target_cat and s["category"] and s["category"] == target_cat:
            score += 10
            reasons.append(f"category={s['category']}")

        # Tier 2: tag overlap
        other_tags = set(t.lower() for t in s["tags"])
        overlap = target_tags & other_tags
        if overlap:
            score += 3 * len(overlap)
            reasons.append(f"tags={sorted(overlap)}")

        # Tier 3: origin match
        if target_origin and s["origin"] and s["origin"] == target_origin:
            score += 1
            reasons.append(f"origin={s['origin'][:40]}")

        if score > 0:
            related.append((score, s["folder"], {"name": s["fm_name"], "reasons": reasons}))

    related.sort(key=lambda t: (-t[0], t[1]))

    if not related:
        print(f"No related skills found for {target_label}.")
        print(f"  Tip: maybe this skill lacks category/tags/origin metadata.")
        return 0

    print(f"=== {len(related)} RELATED SKILLS (ranked) ===\n")
    for i, (score, folder, info) in enumerate(related[: args.limit], 1):
        marker = f"  ({info['name']})" if info["name"] != folder else ""
        print(f"  {i:3d}. {folder}{marker}  [score={score}  {', '.join(info['reasons'])}]")
    if len(related) > args.limit:
        print(f"\n  ... and {len(related)-args.limit} more (use --limit N to see more)")
    return 0


def cmd_skill(args) -> int:
    """Direct skill lookup. Resolves name → skills/<name>/SKILL.md.

    Tier 1: exact match on EITHER folder name OR frontmatter `name:`.
    Tier 2: startswith match (only if single match).
    Tier 3: substring match (only if single match — else "ambiguous").

    Accepts both the folder name (e.g. "output-skill") and the frontmatter
    canonical name (e.g. "full-output-enforcement") — resolves to one.
    """
    import re
    workspace = config.settings.workspace
    skill_root = workspace / "skills"
    if not skill_root.exists():
        print(f"No skills/ directory found at {skill_root}")
        return 1

    # Build (folder_name, frontmatter_name) pairs
    all_pairs: list[tuple[str, str]] = []  # (folder, fm_name)
    for p in sorted(skill_root.glob("*/SKILL.md")):
        folder_name = p.parent.name
        fm_name = folder_name  # default = folder name
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            # Parse name from frontmatter
            m = re.search(r"^name:\s*(.+?)\s*$", text, re.MULTILINE)
            if m:
                fm_name = m.group(1).strip()
        except OSError:
            pass
        all_pairs.append((folder_name, fm_name))

    q = args.name.lower()

    def by_field(field_idx: int, predicate):
        """field_idx 0 = folder, 1 = frontmatter name."""
        return [pair for pair in all_pairs if predicate(pair[field_idx].lower())]

    # Tier 1: exact match on either field
    exact = by_field(0, lambda v: v == q)
    if not exact:
        exact = by_field(1, lambda v: v == q)
    if exact:
        candidates = exact

    else:
        # Tier 2: startswith (folder OR frontmatter)
        starts = by_field(0, lambda v: v.startswith(q))
        if not starts:
            starts = by_field(1, lambda v: v.startswith(q))
        if len(starts) == 1:
            candidates = starts
        elif len(starts) > 1:
            print(f"Ambiguous — {len(starts)} skills start with '{args.name}':")
            for folder, fm in starts[:10]:
                marker = f" (folder) → {fm}" if folder != fm else ""
                print(f"  {folder}{marker}")
            if len(starts) > 10:
                print(f"  ... and {len(starts)-10} more")
            return 1
        else:
            # Tier 3: substring (folder OR frontmatter)
            subs = by_field(0, lambda v: q in v)
            if not subs:
                subs = by_field(1, lambda v: q in v)
            if len(subs) == 1:
                candidates = subs
            elif len(subs) > 1:
                print(f"Ambiguous — {len(subs)} skills contain '{args.name}':")
                for folder, fm in subs[:10]:
                    marker = f" (folder) → {fm}" if folder != fm else ""
                    print(f"  {folder}{marker}")
                if len(subs) > 10:
                    print(f"  ... and {len(subs)-10} more")
                return 1
            else:
                print(f"No skill matches '{args.name}'.")
                print(f"  Total skills: {len(all_pairs)}")
                print(f"  Sample: {[p[0] for p in all_pairs[:10]]}")
                return 1

    folder = candidates[0][0]
    fm = candidates[0][1]
    skill_md = skill_root / folder / "SKILL.md"
    if folder != fm:
        print(f"=== {skill_md.relative_to(workspace)} (frontmatter: {fm}) ===\n")
    else:
        print(f"=== {skill_md.relative_to(workspace)} ===\n")
    text = skill_md.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    head = lines[: args.lines]
    for ln in head:
        print(ln)
    if len(lines) > args.lines:
        print(f"\n... ({len(lines) - args.lines} more lines. Use --lines N to see more)")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Wikilink graph index for NATHAR vault",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  nathar-graph build
  nathar-graph search "parser" --hops 2
  nathar-graph path "api" "parser"
  nathar-graph hubs --top 15
  nathar-graph orphans --limit 30
  nathar-graph stats
""",
    )
    config.add_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="Build/persist graph from MD roots")
    p.add_argument("--roots", nargs="+", default=DEFAULT_ROOTS)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("search", help="BFS within N hops of a node")
    p.add_argument("query")
    p.add_argument("--hops", type=int, default=2)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--broad", action="store_true", help="Accept substring matches (loose, default: exact + startswith)")
    p.add_argument("--skills-only", action="store_true", help="Restrict results to nodes that are skills")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("path", help="Shortest path between two notes")
    p.add_argument("src")
    p.add_argument("dst")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("hubs", help="Top connected nodes")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_hubs)

    p = sub.add_parser("orphans", help="Notes with no wikilinks in or out")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_orphans)

    p = sub.add_parser("validate", help="Per-note wikilink validation")
    p.add_argument("path", help="Path to .md file to validate")
    p.add_argument("--verbose", "-v", action="store_true", help="Show all resolved links")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("stats", help="Quick statistics")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("reset", help="Clear cache files")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("skill", help="Direct lookup for a skill (skills/<name>/SKILL.md)")
    p.add_argument("name", help="Skill folder name (e.g. obsidian-vault-patterns)")
    p.add_argument("--lines", type=int, default=40, help="Number of lines to print from SKILL.md (default 40)")
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("related", help="Find skills related to a given skill (same category/type/tags/origin)")
    p.add_argument("name", help="Skill folder or frontmatter name (e.g. frontend-design)")
    p.add_argument("--limit", type=int, default=30, help="Max results to display (default 30)")
    p.set_defaults(func=cmd_related)

    args = parser.parse_args()
    config.apply_arguments(args)
    global CACHE_GML, CACHE_PKL
    CACHE_GML = config.settings.state_dir / "wikilinks.graphml"
    CACHE_PKL = config.settings.state_dir / "legacy-wikilinks.pkl"
    if hasattr(args, "roots") and args.roots == DEFAULT_ROOTS:
        args.roots = [str(p) for p in config.settings.knowledge_roots]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
