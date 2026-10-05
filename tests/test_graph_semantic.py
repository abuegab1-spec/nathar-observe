import json
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from nathar_observe import config, observe, router


@pytest.fixture
def workspace(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples/workspace"
    root = tmp_path / "project"
    shutil.copytree(source, root)
    previous = config.settings
    config.configure(workspace=root)
    router.WORKSPACE = root
    yield root
    config.settings = previous
    router.WORKSPACE = previous.workspace


def test_graph_roundtrip_and_router_handoff(workspace):
    originals = {str(p): p.read_bytes() for p in (workspace / "knowledge").glob("*.md")}
    build = subprocess.run([sys.executable, "-m", "nathar_observe.graph", "--workspace", str(workspace), "build"],
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    result = observe("Review Python parser validation")
    assert result["status"] == "ok"
    assert "api" in result["wikilinks_nodes"]
    assert originals == {str(p): p.read_bytes() for p in (workspace / "knowledge").glob("*.md")}


def test_local_qdrant_skill_ingest_search_and_retirement(workspace, monkeypatch, capsys):
    qdrant = pytest.importorskip("qdrant_client")
    np = pytest.importorskip("numpy")
    from nathar_observe import embeddings

    class FakeModel:
        def embed(self, texts, **kwargs):
            for text in texts:
                v = np.zeros(384)
                v[0] = 1.0
                v[1] = 1.0 if "python" in text.lower() else 0.0
                yield v

    with closing(qdrant.QdrantClient(location=":memory:")) as client:
        monkeypatch.setattr(embeddings, "_client", lambda: client)
        monkeypatch.setattr(embeddings, "_model", FakeModel)
        assert embeddings.cmd_ingest(SimpleNamespace()) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["updated"] == 3
        assert embeddings.cmd_ingest(SimpleNamespace()) == 0
        assert json.loads(capsys.readouterr().out)["unchanged"] == 3
        assert embeddings.cmd_search(SimpleNamespace(query="Python parser tests", limit=3, json=True)) == 0
        matches = json.loads(capsys.readouterr().out)["results"]
        assert matches[0]["skill"] == "python-testing"
        shutil.rmtree(workspace / "skills/docker-ops")
        assert embeddings.cmd_ingest(SimpleNamespace()) == 0
        assert json.loads(capsys.readouterr().out)["retired_preserved"] == 1
        assert embeddings.cmd_search(SimpleNamespace(query="Docker networking", limit=10, json=True)) == 0
        matches = json.loads(capsys.readouterr().out)["results"]
        assert "docker-ops" not in {m["skill"] for m in matches}


def test_local_qdrant_vault_digest_updates_and_search_contract(workspace, monkeypatch, capsys):
    qdrant = pytest.importorskip("qdrant_client")
    np = pytest.importorskip("numpy")
    from nathar_observe import vault

    class FakeModel:
        def embed(self, texts, **kwargs):
            for text in texts:
                v = np.zeros(384)
                v[0] = 1.0
                yield v

    args = SimpleNamespace(roots=None, legacy_path_only=False, checkpointed=True, force=False, incremental=True)
    with closing(qdrant.QdrantClient(location=":memory:")) as client:
        monkeypatch.setattr(vault, "_client", lambda: client)
        monkeypatch.setattr(vault, "_model", FakeModel)
        assert vault.cmd_ingest(args) == 0
        capsys.readouterr()
        count = client.count(vault.COLLECTION).count
        assert count == 2
        note = workspace / "knowledge/parser.md"
        note.write_text(note.read_text() + "\nPython parser validation now rejects infinity.")
        assert vault.cmd_ingest(args) == 0
        assert client.count(vault.COLLECTION).count == count
        capsys.readouterr()
        assert vault.cmd_search(SimpleNamespace(query="Python parser validation", limit=5, filter=None, json=True)) == 0
        rows = json.loads(capsys.readouterr().out)["results"]
        assert {r["path"] for r in rows} == {"knowledge/parser.md", "knowledge/api.md"}
        note.unlink()
        router.WORKSPACE = workspace
        selected, diagnostics = router.rank_context_candidates("Python parser validation", rows)
        assert "knowledge/parser.md" not in {r["path"] for r in selected}
        assert any("missing" in r["reason"] for r in diagnostics["excluded"])
