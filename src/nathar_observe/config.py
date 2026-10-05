"""Explicit, process-scoped configuration; no agent framework discovery."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path


def _paths(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, (list, tuple)) or not value or any(not isinstance(v, (str, Path)) for v in value):
        raise ValueError("Roots must be a nonempty list of paths")
    return value


@dataclass(frozen=True)
class Settings:
    workspace: Path
    skills_roots: tuple[Path, ...]
    knowledge_roots: tuple[Path, ...]
    state_dir: Path
    conversations: Path | None
    backend: str
    qdrant_url: str | None
    qdrant_path: Path
    receipts: bool
    disabled_skills: tuple[str, ...]


def _make(**overrides):
    def value(key, env, default=None):
        v = overrides.get(key)
        return os.environ.get(env, default) if v is None else v

    workspace = Path(value("workspace", "NATHAR_WORKSPACE", Path.cwd())).expanduser().resolve()
    def resolve(path):
        p = Path(path).expanduser()
        return (p if p.is_absolute() else workspace / p).resolve()

    skills = tuple(resolve(p) for p in _paths(value("skills_roots", "NATHAR_SKILLS_ROOTS"), ["skills"]))
    knowledge = tuple(resolve(p) for p in _paths(value("knowledge_roots", "NATHAR_KNOWLEDGE_ROOTS"), ["knowledge"]))
    if any(not p.is_relative_to(workspace) for p in knowledge):
        raise ValueError("Knowledge roots must remain inside the configured workspace")
    backend = value("backend", "NATHAR_BACKEND", "lexical")
    if backend not in {"lexical", "semantic"}:
        raise ValueError("backend must be lexical or semantic")
    state = resolve(value("state_dir", "NATHAR_STATE_DIR", ".nathar-observe"))
    conversations = value("conversations", "NATHAR_CONVERSATIONS")
    disabled = value("disabled_skills", "NATHAR_DISABLED_SKILLS", [])
    if isinstance(disabled, str):
        disabled = json.loads(disabled)
    if not isinstance(disabled, (list, tuple)) or any(not isinstance(v, str) for v in disabled):
        raise ValueError("disabled_skills must be a list of names")
    receipts = value("receipts", "NATHAR_RECEIPTS", False)
    return Settings(workspace, skills, knowledge, state,
                    resolve(conversations) if conversations else None, backend,
                    value("qdrant_url", "NATHAR_QDRANT_URL"),
                    resolve(value("qdrant_path", "NATHAR_QDRANT_PATH", state / "qdrant")),
                    receipts is True or receipts == "1", tuple(disabled))


settings = _make()


def configure(**kwargs) -> Settings:
    """Set configuration for subsequent calls in this process (not thread-local)."""
    global settings
    settings = _make(**kwargs)
    return settings


def resolve_path(path) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else settings.workspace / p).resolve()


def add_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--workspace", type=Path, help="Workspace root; default: current directory")
    parser.add_argument("--skills", action="append", type=Path, help="Skill directory; repeat for multiple roots")
    parser.add_argument("--knowledge", action="append", type=Path, help="Knowledge root inside workspace; repeat as needed")
    parser.add_argument("--conversations", type=Path, help="Optional agent-independent JSONL conversation file")
    parser.add_argument("--state-dir", type=Path, help="Cache/index directory; default: WORKSPACE/.nathar-observe")
    parser.add_argument("--backend", choices=("lexical", "semantic"), help="Lexical is the default; semantic requires the semantic extra")
    parser.add_argument("--qdrant-url", help="Optional Qdrant server URL; otherwise use local storage")
    parser.add_argument("--qdrant-path", type=Path, help="Local Qdrant storage path")
    parser.add_argument("--receipts", action="store_true", default=None, help="Persist routing metadata without task/source text")
    parser.add_argument("--disable-skill", action="append", help="Exclude a skill by folder name")


def apply_arguments(args):
    return configure(workspace=args.workspace, skills_roots=args.skills,
                     knowledge_roots=args.knowledge, conversations=args.conversations,
                     state_dir=args.state_dir, backend=args.backend,
                     qdrant_url=args.qdrant_url, qdrant_path=args.qdrant_path,
                     receipts=args.receipts, disabled_skills=args.disable_skill)


def child_environment():
    env = os.environ.copy()
    values = {
        "NATHAR_WORKSPACE": str(settings.workspace),
        "NATHAR_SKILLS_ROOTS": json.dumps([str(p) for p in settings.skills_roots]),
        "NATHAR_KNOWLEDGE_ROOTS": json.dumps([str(p) for p in settings.knowledge_roots]),
        "NATHAR_STATE_DIR": str(settings.state_dir),
        "NATHAR_BACKEND": settings.backend,
        "NATHAR_QDRANT_PATH": str(settings.qdrant_path),
        "NATHAR_RECEIPTS": "1" if settings.receipts else "0",
        "NATHAR_DISABLED_SKILLS": json.dumps(settings.disabled_skills),
    }
    for key, v in (("NATHAR_CONVERSATIONS", settings.conversations), ("NATHAR_QDRANT_URL", settings.qdrant_url)):
        if v:
            values[key] = str(v)
        else:
            env.pop(key, None)
    env.update(values)
    return env


def qdrant_client():
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        raise RuntimeError("Install nathar-observe[semantic] to use semantic indexing") from None
    if settings.qdrant_url:
        return QdrantClient(url=settings.qdrant_url, api_key=os.environ.get("NATHAR_QDRANT_API_KEY"), timeout=30)
    settings.qdrant_path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(settings.qdrant_path))
