"""Opt-in routing metadata; one file per call for portable concurrent writes."""
import json
import os
from datetime import datetime, timezone
from uuid import uuid4
from . import config


def record(result):
    directory = config.settings.state_dir / "receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (uuid4().hex + ".json")
    metadata = {"at": datetime.now(timezone.utc).isoformat(), "version": result["version"],
                "status": result["status"], "backend": result["backend"],
                "selected_skills": result["selected_skills"],
                "source_count": len(result["qdrant_hits"])}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False)
    return path
