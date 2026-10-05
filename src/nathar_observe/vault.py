from __future__ import annotations

import argparse
import json
import hashlib
import sys
import time
from pathlib import Path


def stable_id(s: str) -> int:
    """Stable point ID from path (sha256-based, deterministic across runs)."""
    return int.from_bytes(hashlib.sha256(s.encode()).digest()[:8], "big", signed=True) & 0x7FFFFFFFFFFFFFFF

from .corpus import DEFAULT_PATHS, _collect_docs, _doc_text
from . import config
QDRANT_URL = config.settings.qdrant_url
COLLECTION = "nathar_vault"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"  # 384 dims, ~80 MB, FastEmbed default
EMBED_DIMS = 384
BATCH_SIZE = 64
INFERENCE_BATCH_SIZE = 8


def _client():
    return config.qdrant_client()


def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(config.settings.state_dir / "models"), threads=4)


def ingest_checkpointed(client, docs, batch_size=BATCH_SIZE):
    """Persist each batch; resume by content/model digest, never path alone."""
    from qdrant_client.models import PointStruct
    model = None
    written = skipped = 0
    for offset in range(0, len(docs), batch_size):
        batch = docs[offset:offset + batch_size]
        ids = [stable_id(doc.path) for doc in batch]
        existing = {point.id: point.payload or {} for point in client.retrieve(
            COLLECTION, ids=ids, with_payload=True, with_vectors=False)}
        pending = []
        for pid, doc in zip(ids, batch):
            text = _doc_text(doc)
            digest = hashlib.sha256(f'{EMBED_MODEL}:{EMBED_DIMS}\0{text}'.encode()).hexdigest()
            if existing.get(pid, {}).get('embedding_digest') == digest:
                skipped += 1
                continue
            pending.append((pid, doc, text, digest))
        if not pending:
            continue
        if model is None:
            model = _model()
        # Keep durable checkpoints large, but bound inference padding and memory.
        vectors = list(model.embed([item[2] for item in pending],
                                  batch_size=min(batch_size, INFERENCE_BATCH_SIZE)))
        if len(vectors) != len(pending):
            raise ValueError('Embedding count mismatch; batch not committed')
        points = [PointStruct(id=pid, vector=list(vector), payload={
            'path': doc.path, 'name': doc.name, 'description': doc.description,
            'mtime': doc.mtime, 'tokens': doc.tokens,
            'frontmatter_keys': list(doc.frontmatter) if isinstance(doc.frontmatter, dict) else [],
            'embedding_digest': digest,
        }) for (pid, doc, _, digest), vector in zip(pending, vectors)]
        client.upsert(COLLECTION, points=points, wait=True)
        written += len(points)
        print(f'Checkpoint: {written} written, {skipped} unchanged', flush=True)
    return {'written': written, 'skipped': skipped}


def cmd_ingest(args) -> int:
    from qdrant_client.models import Distance, VectorParams, PointStruct

    client = _client()
    # Ensure collection exists
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIMS, distance=Distance.COSINE),
        )
        print(f"✓ Created collection: {COLLECTION}")
    else:
        print(f"Collection exists: {COLLECTION} (existing points preserved)")

    # Collect docs
    roots = args.roots or list(config.settings.knowledge_roots)
    print(f"Collecting docs from {len(roots)} roots...")
    docs = _collect_docs(roots)
    print(f"  collected {len(docs)} documents")

    if not args.legacy_path_only:
        print(ingest_checkpointed(client, docs), flush=True)
        return 0

    # Filter: skip if point_id (hash of path) already exists?
    # For simplicity, compute point_id = int(abs(hash(path)) % 2**63)
    existing = set()
    if args.incremental and not args.force:
        try:
            existing_points, _ = client.scroll(COLLECTION, limit=10_000, with_payload=False)
            existing = {p.id for p in existing_points}
            print(f"  incremental: {len(existing)} existing points")
        except Exception:
            pass

    # Get embedding model
    print(f"Loading embedding model: {EMBED_MODEL}...")
    t0 = time.time()
    model = _model()
    print(f"  loaded in {time.time() - t0:.1f}s")

    # Embed in batches
    points = []
    new_docs = []
    for i, doc in enumerate(docs):
        pid = stable_id(doc.path)
        if pid in existing:
            continue
        new_docs.append((pid, doc))
    print(f"  {len(new_docs)} new docs to embed (skipping {len(docs) - len(new_docs)} existing)")

    if not new_docs:
        print("Nothing new to ingest.")
        return 0

    texts = [_doc_text(d) for _, d in new_docs]
    print(f"Embedding {len(texts)} docs (batch={BATCH_SIZE})...")
    t0 = time.time()
    embeddings = list(model.embed(texts, batch_size=BATCH_SIZE))
    embed_time = time.time() - t0
    print(f"  embedded in {embed_time:.1f}s ({len(texts) / embed_time:.1f} docs/sec)")

    # Build points
    for (pid, doc), vec in zip(new_docs, embeddings):
        payload = {
            "path": doc.path,
            "name": doc.name,
            "description": doc.description,
            "mtime": doc.mtime,
            "tokens": doc.tokens,
            "frontmatter_keys": list(doc.frontmatter.keys()) if isinstance(doc.frontmatter, dict) else [],
        }
        points.append(PointStruct(id=pid, vector=list(vec), payload=payload))

    # Upsert in batches
    print(f"Upserting {len(points)} points to Qdrant...")
    client.upsert(COLLECTION, points=points, wait=True)
    total_time = time.time() - t0
    print(f"✓ Done: {len(points)} new points in {total_time:.1f}s")
    return 0


def cmd_search(args) -> int:
    client = _client()
    model = _model()
    query_vec = list(model.embed([args.query]))[0]
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    search_filter = Filter(must_not=[
        FieldCondition(key="index_active", match=MatchValue(value=False))
    ])
    if args.filter:
        search_filter.should = [
            FieldCondition(key="path", match=MatchValue(value=args.filter))
        ]
    # qdrant-client 1.18+ uses query_points() instead of search()
    # Group in Qdrant rather than trimming a fixed prefix of chunk hits:
    # one long source must not consume the candidate budget for other paths.
    response = client.query_points_groups(
        collection_name=COLLECTION,
        query=list(query_vec),
        group_by="path",
        group_size=1,
        limit=args.limit,
        query_filter=search_filter,
        with_payload=True,
    )
    results = [group.hits[0] for group in response.groups if group.hits]
    if getattr(args, "json", False):
        print(json.dumps({"results": [
            {"rank": rank, "score": hit.score, "path": (hit.payload or {}).get("path")}
            for rank, hit in enumerate(results, 1)
        ]}, ensure_ascii=False))
        return 0
    print(f"Query: {args.query!r}")
    if args.filter:
        print(f"Filter: {args.filter}")
    count = client.count(COLLECTION).count
    print(f"Top {len(results)} of {count} points:\n")
    for rank, hit in enumerate(results, 1):
        p = hit.payload or {}
        desc = (p.get("description") or "")[:120]
        print(f"  #{rank:2d}  {hit.score:.3f}  {p.get('path')}")
        if desc:
            print(f"        {desc}")
        print()
    return 0


def cmd_stats(args) -> int:
    client = _client()
    if not client.collection_exists(COLLECTION):
        print(f"No collection: {COLLECTION}. Run: nathar-vault ingest")
        return 1
    info = client.get_collection(COLLECTION)
    print(f"Collection: {COLLECTION}")
    print(f"Qdrant URL: {QDRANT_URL}")
    print(f"Model: {EMBED_MODEL} ({EMBED_DIMS} dims)")
    print(f"Points: {info.points_count}")
    print(f"Status: {info.status}")
    return 0


def cmd_reindex(args) -> int:
    from qdrant_client.models import Distance, VectorParams
    client = _client()
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
        print(f"✓ Deleted collection: {COLLECTION}")
    client.create_collection(
        COLLECTION,
        vectors_config=VectorParams(size=EMBED_DIMS, distance=Distance.COSINE),
    )
    print(f"✓ Recreated collection: {COLLECTION}")
    # Now ingest
    args.legacy_path_only = False
    args.force = True
    args.incremental = False
    return cmd_ingest(args)


def main() -> int:
    config.configure_cli_output()
    p = argparse.ArgumentParser(description="NATHAR vector search via Qdrant + fastembed")
    config.add_arguments(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_i = sub.add_parser("ingest", help="Embed and ingest vault docs")
    p_i.add_argument("--roots", nargs="+", help="Paths to index (default: standard vault)")
    p_i.add_argument("--incremental", action="store_true", default=True, help="Skip already-ingested (default)")
    p_i.add_argument("--force", action="store_true", help="Re-embed everything")
    p_i.add_argument("--checkpointed", action="store_true", help="Compatibility flag; content-digest checkpoints are the default")
    p_i.add_argument("--legacy-path-only", action="store_true", help=argparse.SUPPRESS)
    p_i.set_defaults(func=cmd_ingest)

    p_s = sub.add_parser("search", help="Vector search the vault")
    p_s.add_argument("query")
    p_s.add_argument("--json", action="store_true", help="Structured search results for programmatic consumers")
    p_s.add_argument("--limit", type=int, default=10)
    p_s.add_argument("--filter", help="Substring filter on payload path")
    p_s.set_defaults(func=cmd_search)

    p_st = sub.add_parser("stats", help="Show Qdrant collection stats")
    p_st.set_defaults(func=cmd_stats)

    p_re = sub.add_parser("reindex", help="Wipe + re-ingest from scratch")
    p_re.add_argument("--roots", nargs="+")
    p_re.set_defaults(func=cmd_reindex)

    args = p.parse_args()
    config.apply_arguments(args)
    global QDRANT_URL
    QDRANT_URL = config.settings.qdrant_url
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
