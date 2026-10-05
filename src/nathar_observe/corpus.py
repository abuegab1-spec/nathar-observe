"""Bounded Markdown collection and an offline lexical context backend."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config

DEFAULT_PATHS = ["knowledge"]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "archive", ".nathar-observe"}


@dataclass
class Doc:
    path: str
    name: str
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    description: str = ""
    mtime: float = 0.0
    tokens: int = 0


def _collect_docs(roots=None):
    workspace = config.settings.workspace
    found = {}
    for root in roots if roots is not None else config.settings.knowledge_roots:
        root = config.resolve_path(root)
        if not root.is_relative_to(workspace):
            raise ValueError("Knowledge roots must remain inside the configured workspace")
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for candidate in files:
            p = candidate.resolve()
            if not p.is_relative_to(workspace) or p.suffix.lower() != ".md":
                continue
            if any(part in SKIP_DIRS for part in candidate.relative_to(workspace).parts):
                continue
            if any(p.match(pattern) for pattern in ("*.backup*", "*-backup.md", "*-draft.md", "*.draft.md")):
                continue
            if p in found:
                continue
            try:
                if p.stat().st_size > 200_000:
                    continue
                text = p.read_text(encoding="utf-8-sig")
                metadata, body = {}, text
                if text.startswith("---\n"):
                    end = text.find("\n---", 4)
                    if end >= 0:
                        metadata = yaml.safe_load(text[4:end]) or {}
                        body = text[end + 4:].lstrip("\r\n")
                if not isinstance(metadata, dict):
                    metadata = {}
                found[p] = Doc(str(p.relative_to(workspace)), p.stem, metadata, body,
                               str(metadata.get("description", "")), p.stat().st_mtime,
                               len(body.split()))
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
    return list(found.values())


def _doc_text(doc):
    parts = [doc.description, doc.description]
    for key in ("keywords", "tags", "triggers"):
        value = doc.frontmatter.get(key, "")
        parts.append(" ".join(map(str, value)) if isinstance(value, list) else str(value))
    parts.append(doc.body[:10_000])
    return "\n".join(parts)


def search(query, limit=5):
    from .router import _tokens, STOPWORDS, GENERIC_ROUTING_WORDS
    anchors = _tokens(query) - STOPWORDS - GENERIC_ROUTING_WORDS
    if not anchors:
        return []
    ranked = []
    for doc in _collect_docs():
        matched = anchors & _tokens(doc.path + " " + _doc_text(doc))
        if not matched:
            continue
        # This is a heuristic relevance score, not cosine similarity or probability.
        score = 0.65 + 0.35 * len(matched) / len(anchors)
        ranked.append({"score": score, "path": doc.path})
    ranked.sort(key=lambda row: (-row["score"], row["path"]))
    return [{"rank": i + 1, **row} for i, row in enumerate(ranked[:max(limit * 3, limit)])]
