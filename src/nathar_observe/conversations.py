"""Search an explicit JSONL export, never an agent's internal storage."""
from __future__ import annotations

import json
from . import config


def search(query, focused, agent, limit, *, tokenize, excluded_words):
    stats = {"method": "jsonl-lexical", "agent": agent, "returned": 0, "inspected": 0}
    path = config.settings.conversations
    if path is None:
        return [], None, {**stats, "signal": "conversations_not_configured"}
    anchors = tokenize(focused) - excluded_words
    if not anchors:
        return [], None, {**stats, "signal": "no_conversation_search_terms"}
    found, seen = [], set()
    try:
        if path.stat().st_size > 20_000_000:
            raise ValueError("conversation file exceeds 20 MB")
        with path.open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                if len(line.encode("utf-8")) > 100_000:
                    raise ValueError(f"conversation line {number} exceeds 100 KB")
                row = json.loads(line)
                if (not isinstance(row, dict) or
                    any(not isinstance(row.get(k), str) or not row[k] for k in ("session_id", "message_id", "text")) or
                    row.get("role") not in {"user", "assistant"}):
                    raise ValueError(f"invalid conversation schema at line {number}")
                stats["inspected"] += 1
                if row.get("agent", agent) != agent or row.get("archived", False) or row.get("system", False):
                    continue
                text = row["text"]
                matched = anchors & tokenize(text)
                if len(matched) < min(2, len(anchors)):
                    continue
                identity = (row["session_id"], row["message_id"])
                if identity in seen:
                    continue
                seen.add(identity)
                # Retain the matching region instead of truncating away the evidence.
                words = text.split()
                first = next((i for i, word in enumerate(words) if tokenize(word) & matched), 0)
                snippet = " ".join(words[max(0, first - 15):first + 65])[:500]
                found.append({"sessionId": row["session_id"], "messageId": row["message_id"],
                              "sessionKey": row["session_id"], "role": row["role"],
                              "timestamp": row.get("timestamp"), "snippet": snippet,
                              "matched_terms": sorted(matched), "score": len(matched) / len(anchors)})
    except (OSError, UnicodeError, ValueError):
        return [], "Conversation file unreadable or invalid; inspect JSONL locally", {**stats, "signal": "conversation_search_degraded"}
    found.sort(key=lambda row: (-row["score"], row["sessionId"], row["messageId"]))
    selected, snippets, counts = [], set(), {}
    for row in found:
        session = row["sessionId"]
        if row["snippet"] in snippets or counts.get(session, 0) >= 2:
            continue
        selected.append(row)
        snippets.add(row["snippet"])
        counts[session] = counts.get(session, 0) + 1
        if len(selected) >= limit:
            break
    return selected, None, {**stats, "returned": len(selected), "signal": "conversation_matches" if selected else "no_matching_conversations"}
