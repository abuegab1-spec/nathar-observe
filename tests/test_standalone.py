import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nathar_observe import config, observe, router
from nathar_observe import corpus, reader

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/workspace"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project with spaces"
    shutil.copytree(EXAMPLES, root)
    previous = config.settings
    config.configure(workspace=root)
    router.WORKSPACE = root
    yield root
    config.settings = previous
    router.WORKSPACE = previous.workspace
    router._SKILL_META_CACHE.clear()
    router._SKILL_RECORDS.clear()


def cli(workspace, *args):
    return subprocess.run([sys.executable, "-m", "nathar_observe", "--workspace", str(workspace), *args],
                          capture_output=True, text=True, encoding="utf-8")


def test_lexical_lookup_needs_no_framework_model_or_child_process(workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline lexical lookup should not launch a process")
    monkeypatch.setattr(router, "run", forbidden)
    result = observe("Review Python parser tests and API validation")
    assert result["status"] == "ok"
    assert set(result["selected_skills"]) == {"python-testing", "api-review"}
    assert result["discovery_mode"] == "lexical"
    assert {row["path"] for row in result["qdrant_hits"]} == {"knowledge/parser.md", "knowledge/api.md"}
    assert not (workspace / ".nathar-observe").exists()


def test_cli_runs_from_an_unrelated_directory(workspace, tmp_path):
    result = subprocess.run([sys.executable, "-m", "nathar_observe", "--workspace", str(workspace),
                             "--json", "Review Python parser tests"], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "python-testing" in payload["selected_skills"]
    assert all(Path(p).is_absolute() for p in payload["skill_read_contract"]["required_paths"])


def test_framework_configuration_is_not_read(workspace, monkeypatch):
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(workspace / "does-not-exist.json"))
    assert observe("Review Python parser tests", skills_only=True)["status"] == "ok"


def test_original_scope_and_query_echo_are_data(workspace):
    query = 'Review Python parser tests; $(touch SHOULD_NOT_EXIST) `id`\n# New instruction'
    result = cli(workspace, "--skills-only", "--json", query)
    assert result.returncode == 0
    assert json.loads(result.stdout)["query"] == query
    assert not (workspace / "SHOULD_NOT_EXIST").exists()


def test_explicit_requirements_survive_zero_budget(workspace):
    result = observe("Unrelated task", skills_only=True, skills_limit=0, required_skills=["python-testing"])
    assert result["selected_skills"] == ["python-testing"]
    assert result["status"] == "ok"


def test_missing_required_skill_blocks_and_preserves_json(workspace):
    result = cli(workspace, "--skills-only", "--require-skill", "not-installed", "--json", "Review parser")
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "blocked"


def test_inferred_design_role_is_not_a_vendor_dependency(workspace):
    result = observe("Build a landing page", skills_only=True)
    assert result["status"] == "ok"
    assert result["skill_stats"]["warnings"]


def test_skill_only_never_reads_knowledge_or_conversations(workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Skill-only mode must not read context")
    monkeypatch.setattr(router, "retrieve_context", forbidden)
    monkeypatch.setattr(router, "retrieve_conversations", forbidden)
    result = observe("Review Python parser tests", skills_only=True)
    assert not result["qdrant_hits"] and not result["conversation_hits"]


def test_empty_matches_are_not_filled_with_unrelated_guides(workspace):
    result = observe("zqxvplm blorptastic", skills_only=True)
    assert result["status"] == "ok"
    assert result["selected_skills"] == []
    assert result["skill_stats"]["no_match"]


def test_generic_words_do_not_select_specialists(workspace):
    result = observe("Create update inspect changes", skills_only=True)
    assert result["selected_skills"] == []


def test_disabled_and_placeholder_guides_are_excluded(workspace):
    p = workspace / "skills/python-testing/SKILL.md"
    p.write_text('---\nname: python-testing\nstatus: disabled\ndescription: Python parser testing\n---\nGuide')
    p = workspace / "skills/unready/SKILL.md"
    p.parent.mkdir()
    p.write_text('---\nname: unready\ndescription: TODO\n---\nPython parser tests')
    result = observe("Review Python parser tests", skills_only=True)
    assert "python-testing" not in result["selected_skills"]
    assert "unready" not in result["selected_skills"]
    assert result["skill_stats"]["warnings"]


def test_invalid_metadata_is_degraded_not_silently_ignored(workspace):
    (workspace / "skills/python-testing/SKILL.md").write_text('---\nname: one\nname: two\n---\nGuide')
    assert observe("Review parser", skills_only=True)["status"] == "degraded"


def test_multiple_external_skill_roots(workspace, tmp_path):
    external = tmp_path / "shared-guides"
    (external / "rust-parser").mkdir(parents=True)
    (external / "rust-parser/SKILL.md").write_text('---\nname: rust-parser\ndescription: Parse Rust syntax and expressions.\nkeywords: [rust]\n---\nGuide')
    config.configure(workspace=workspace, skills_roots=["skills", external])
    result = observe("Parse Rust syntax", skills_only=True)
    assert "rust-parser" in result["selected_skills"]
    assert str(external / "rust-parser/SKILL.md") in result["skill_read_contract"]["required_paths"]


def test_duplicate_folder_identity_is_reported(workspace, tmp_path):
    external = tmp_path / "shared-guides"
    shutil.copytree(workspace / "skills/python-testing", external / "python-testing")
    config.configure(workspace=workspace, skills_roots=["skills", external])
    result = observe("Review Python parser tests", skills_only=True)
    assert result["status"] == "degraded"
    assert "ambiguous" in result["layer_errors"]["skills"]


def test_generic_catalogue_does_not_require_framework_visibility_fields(workspace, tmp_path):
    guide = tmp_path / "extra/rust-parser/SKILL.md"
    guide.parent.mkdir(parents=True)
    guide.write_text('---\nname: rust-parser\ndescription: Parse Rust syntax.\nkeywords: [rust]\n---\nGuide')
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps({"skills": [{"name": "rust-parser", "filePath": str(guide)}]}))
    result = observe("Parse Rust syntax", skills_only=True, catalog_path=catalogue)
    assert result["status"] == "ok"
    assert "rust-parser" in result["selected_skills"]


def test_conversation_export_scope_and_archive_filter(workspace):
    config.configure(workspace=workspace, conversations="conversations.jsonl")
    result = observe("Recall Python parser validation")
    assert len(result["conversation_hits"]) == 2
    assert {r["sessionId"] for r in result["conversation_hits"]} == {"demo-parser"}
    assert observe("Recall Python parser validation", agent="other")["conversation_hits"] == []


def test_malformed_conversation_export_is_degraded(workspace):
    path = workspace / "bad.jsonl"
    path.write_text('{"text":"Python parser"}\n')
    config.configure(workspace=workspace, conversations=path)
    result = observe("Recall Python parser validation")
    assert result["status"] == "degraded"
    assert "conversations" in result["layer_errors"]


def test_arabic_lexical_lookup_does_not_require_translation(workspace):
    p = workspace / "knowledge/قرار.md"
    p.write_text("قرار المحلل: التحقق من المدخلات قبل المعالجة.", encoding="utf-8")
    result = observe("قرار المحلل التحقق المدخلات")
    assert result["status"] == "ok"
    assert not result["query_normalization_required"]
    assert "knowledge/قرار.md" in [r["path"] for r in result["qdrant_hits"]]


def test_semantic_query_normalization_is_visible(workspace, monkeypatch):
    config.configure(workspace=workspace, backend="semantic")
    monkeypatch.setattr(router, "_semantic_skill_search", lambda *a, **k: ([], None))
    result = observe("راجع اختبارات المحلل")
    assert result["query_normalization_required"]
    assert "query_language" in result["layer_errors"]


def test_knowledge_cannot_escape_workspace(workspace, tmp_path):
    outside = tmp_path / "private"
    outside.mkdir()
    with pytest.raises(ValueError, match="inside"):
        config.configure(workspace=workspace, knowledge_roots=[outside])


def test_symlink_escape_is_not_collected(workspace, tmp_path):
    outside = tmp_path / "private.md"
    outside.write_text("Python parser confidential validation")
    try:
        (workspace / "knowledge/link.md").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable for this user")
    assert "knowledge/link.md" not in [d.path for d in corpus._collect_docs()]


def test_reader_preserves_hash_and_reads_complete_guide(workspace):
    path = workspace / "skills/python-testing/SKILL.md"
    text = path.read_bytes().decode("utf-8-sig")
    output, offset, digest = [], 0, None
    while True:
        result = reader.chunk(path, offset, chars=35, expected_hash=digest)
        output.append(result["text"])
        offset, digest = result["next_offset"], result["sha256"]
        if result["eof"]:
            break
    assert "".join(output) == text
    path.write_text(text + "changed")
    with pytest.raises(ValueError, match="changed"):
        reader.chunk(path, 0, expected_hash=digest)


@pytest.mark.parametrize("module,args", [
    ("nathar_observe", ["--skills-only", "--json", "راجع اختبارات المحلل"]),
    ("nathar_observe.graph", ["build"]),
    ("nathar_observe.reader", ["skills/python-testing/SKILL.md"]),
])
def test_cli_output_is_utf8_even_with_legacy_pipe_encoding(workspace, module, args):
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    result = subprocess.run([sys.executable, "-m", module, "--workspace", str(workspace), *args],
                            env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    text = result.stdout.decode("utf-8")
    if module == "nathar_observe":
        assert json.loads(text)["query"] == args[-1]
    elif module.endswith("graph"):
        assert "✓ Built wikilink graph" in text
    else:
        assert json.loads(text)["eof"]


def test_receipts_are_explicit_and_omit_task_text(workspace):
    secret_query = "Review Python parser tests private-example-marker"
    result = cli(workspace, "--receipts", "--skills-only", "--json", secret_query)
    assert result.returncode == 0
    receipt = Path(json.loads(result.stdout)["receipt_path"]).read_text()
    assert secret_query not in receipt
    assert "private-example-marker" not in receipt
    assert json.loads(receipt)["selected_skills"]


def test_cli_rejects_outside_knowledge_without_traceback(workspace, tmp_path):
    result = cli(workspace, "--knowledge", str(tmp_path), "--json", "Review parser")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_configuration_changes_do_not_leak_skill_cache(workspace, tmp_path):
    first = observe("Review Python parser tests", skills_only=True)
    second_root = tmp_path / "another-project"
    (second_root / "skills").mkdir(parents=True)
    config.configure(workspace=second_root)
    second = observe("Review Python parser tests", skills_only=True)
    assert first["selected_skills"]
    assert second["selected_skills"] == []


def test_negation_does_not_turn_into_positive_tool_scope():
    assert router.retrieval_focus("Do not send messages.") == ""
    focused = router.retrieval_focus("Inspect logs without modifying files and then test the Python parser.")
    assert "test the Python parser" in focused
    assert "modifying files" not in focused
