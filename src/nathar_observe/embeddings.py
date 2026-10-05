from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import config
from .router import _load_skill_metadata, list_skill_folders
QDRANT_URL = config.settings.qdrant_url
COLLECTION = "nathar_skills"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
VECTOR_DIM = 384


def _client():
    return config.qdrant_client()


def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(config.settings.state_dir / "models"), threads=4)


def _skill_document(meta: dict) -> str:
    """Build the text document for embedding from skill metadata."""
    parts = []
    if meta.get("name"):
        parts.append(meta["name"].replace("-", " ").replace("_", " "))
    if meta.get("description"):
        parts.append(meta["description"])
    if meta.get("keywords"):
        parts.append("Keywords: " + ", ".join(meta["keywords"]))
    if meta.get("tags"):
        parts.append("Tags: " + ", ".join(meta["tags"]))
    if meta.get("triggers"):
        parts.append("Triggers: " + meta["triggers"])
    return "\n".join(parts).strip()


def _stable_id(skill_name: str) -> int:
    return int.from_bytes(
        hashlib.sha256(skill_name.encode()).digest()[:8],
        "big", signed=True,
    ) & 0x7FFFFFFFFFFFFFFF


def _collection_exists(client) -> bool:
    try:
        existing = [c.name for c in client.get_collections().collections]
        return COLLECTION in existing
    except Exception as exc:
        raise RuntimeError(
            f"Cannot reach Qdrant while checking collection {COLLECTION} "
            f"({type(exc).__name__}); collection existence is unknown."
        ) from exc


def cmd_ingest(args) -> int:
    """Refresh metadata with content digests; never drop the collection."""
    from qdrant_client.models import Distance, VectorParams, PointStruct

    # Preflight the complete live inventory before any collection mutation.
    # A provider outage must not retire records or create an empty index.
    skills = list_skill_folders(include_platform=True, agent="main")
    from .router import _INVENTORY_ERRORS
    if _INVENTORY_ERRORS:
        raise RuntimeError("Skill inventory incomplete: " + "; ".join(_INVENTORY_ERRORS))
    documents = {}
    for name in skills:
        meta = _load_skill_metadata(name)
        doc = _skill_document(meta)
        if not doc:
            raise ValueError(f"Empty metadata document: {name}")
        digest = hashlib.sha256(f"{EMBED_MODEL}:{VECTOR_DIM}\0{doc}".encode()).hexdigest()
        documents[name] = (meta, doc, digest)

    client = _client()
    if not _collection_exists(client):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
    else:
        vectors = client.get_collection(COLLECTION).config.params.vectors
        if not hasattr(vectors, "size") or vectors.size != VECTOR_DIM or str(vectors.distance).lower() != "cosine":
            raise ValueError("Existing skill vector schema differs; collection preserved")

    model = None
    written = unchanged = activated = 0
    for offset in range(0, len(skills), 32):
        batch = skills[offset:offset + 32]
        ids = [_stable_id(name) for name in batch]
        old = {p.id: p.payload or {} for p in client.retrieve(
            collection_name=COLLECTION, ids=ids, with_payload=True, with_vectors=False)}
        pending = []
        activate_ids = []
        for name, pid in zip(batch, ids):
            meta, doc, digest = documents[name]
            if old.get(pid, {}).get("embedding_digest") == digest:
                if old[pid].get("index_active") is not True:
                    activate_ids.append(pid)
                unchanged += 1
                continue
            pending.append((pid, name, meta, doc, digest))
        if activate_ids:
            client.set_payload(COLLECTION, payload={"index_active": True}, points=activate_ids, wait=True)
            activated += len(activate_ids)
        if not pending:
            continue
        if model is None:
            model = _model()
        vectors = list(model.embed([row[3] for row in pending], batch_size=8))
        if len(vectors) != len(pending):
            raise ValueError("Embedding count mismatch; batch not written")
        points = [PointStruct(id=pid, vector=list(vector), payload={
            "skill": name, "name": meta.get("name", name),
            "description": meta.get("description") or "",
            "keywords": meta.get("keywords", []), "embedding_digest": digest,
            "index_active": True,
        }) for (pid, name, meta, doc, digest), vector in zip(pending, vectors)]
        client.upsert(collection_name=COLLECTION, points=points, wait=True)
        written += len(points)
        print(f"[checkpoint] {written} updated, {unchanged} unchanged", file=sys.stderr)
    # Retain obsolete managed records for rollback, but exclude them from search.
    # Do not alter foreign points merely because they have a skill-like payload.
    active_names = set(skills)
    retired = 0
    cursor = None
    while True:
        page, cursor = client.scroll(COLLECTION, limit=128, offset=cursor,
                                     with_payload=True, with_vectors=False)
        retire_ids = []
        for point in page:
            payload = point.payload or {}
            name = payload.get("skill")
            if (isinstance(name, str) and point.id == _stable_id(name)
                    and name not in active_names and payload.get("index_active") is not False):
                retire_ids.append(point.id)
        if retire_ids:
            client.set_payload(COLLECTION, payload={"index_active": False}, points=retire_ids, wait=True)
            retired += len(retire_ids)
        if cursor is None:
            break
    print(json.dumps({"collection": COLLECTION, "active_skills": len(skills),
                      "updated": written, "unchanged": unchanged, "activated": activated,
                      "retired_preserved": retired, "collection_recreated": False}))
    return 0


def cmd_search(args) -> int:
    """Embed query, search by cosine similarity, return ranked skills."""
    client = _client()
    if not _collection_exists(client):
        print(json.dumps({"query": args.query, "results": [], "error": "collection not found — run ingest first"}))
        return 1

    model = _model()
    query_vec = list(model.embed([args.query]))[0].tolist()

    from qdrant_client.models import Filter, FieldCondition, MatchValue
    search_result = client.query_points(
        collection_name=COLLECTION,
        query=query_vec,
        limit=args.limit,
        query_filter=Filter(must_not=[FieldCondition(key="index_active", match=MatchValue(value=False))]),
        with_payload=True,
    )

    results = []
    for hit in search_result.points:
        results.append({
            "skill": hit.payload.get("skill"),
            "similarity": float(hit.score),
            "name": hit.payload.get("name"),
            "description_preview": (hit.payload.get("description") or "")[:120],
        })

    if args.json:
        print(json.dumps({"query": args.query, "results": results}, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(f"  {r['similarity']:.3f}  {r['skill']:<32}  {r['description_preview']}")
    return 0


def cmd_stats(args) -> int:
    client = _client()
    if not _collection_exists(client):
        print(f"Collection {COLLECTION}: does not exist (run `ingest`)")
        return 1
    info = client.get_collection(COLLECTION)
    print(f"Collection: {COLLECTION}")
    print(f"Points: {info.points_count}")
    print(f"Status: {info.status}")
    return 0


def cmd_clear(args) -> int:
    client = _client()
    if _collection_exists(client):
        client.delete_collection(COLLECTION)
        print(f"[cleared] {COLLECTION}")
    else:
        print(f"[noop] {COLLECTION} does not exist")
    return 0


def main():
    p = argparse.ArgumentParser(description="Semantic skill discovery (Qdrant + fastembed)")
    config.add_arguments(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ingest", help="Incrementally upsert active skill metadata; preserve collection")

    ps = sub.add_parser("search", help="Search by cosine similarity")
    ps.add_argument("query")
    ps.add_argument("--limit", type=int, default=18)
    ps.add_argument("--json", action="store_true")

    sub.add_parser("stats", help="Show collection stats")
    sub.add_parser("clear", help="Drop the skills collection")

    args = p.parse_args()
    config.apply_arguments(args)
    return {
        "ingest": cmd_ingest,
        "search": cmd_search,
        "stats": cmd_stats,
        "clear": cmd_clear,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
