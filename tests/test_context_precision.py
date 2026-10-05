import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nathar_observe import router as observe


class ContextPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = patch.object(observe, 'WORKSPACE', self.root)
        self.workspace.start()

    def tearDown(self):
        self.workspace.stop()
        self.tmp.cleanup()

    def hit(self, path, score, text='', rank=1):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return {'path': path, 'score': score, 'rank': rank}

    def test_skill_noise_cannot_displace_local_context(self):
        skill = self.hit('skills/figma/SKILL.md', .95, 'prompt review')
        note = self.hit('memory/decision.md', .72, 'prompt clarity and conflicting instructions', 2)
        rows, stats = observe.rank_context_candidates('prompt clarity conflicting instructions', [skill, note])
        self.assertEqual([x['path'] for x in rows], ['memory/decision.md'])
        self.assertEqual(rows[0]['score'], .72)
        self.assertEqual(rows[0]['semantic_rank'], 2)
        self.assertIn('skill document', stats['excluded'][0]['reason'])

    def test_unknown_query_abstains_and_preserves_diagnostics(self):
        hit = self.hit('memory/old.md', .68, 'Unrelated old decision')
        rows, stats = observe.rank_context_candidates('zqxvplm blorptastic', [hit])
        self.assertEqual(rows, [])
        self.assertEqual(stats['signal'], 'no_supported_context')
        self.assertEqual(stats['excluded'][0]['path'], hit['path'])

    def test_high_semantic_score_without_subject_evidence_abstains(self):
        hit = self.hit('memory/2026-01-01.md', .87, 'We chose the alternate approach')
        rows, _ = observe.rank_context_candidates('historical architectural rationale', [hit])
        self.assertEqual(rows, [])

    def test_missing_indexed_source_is_not_returned(self):
        rows, stats = observe.rank_context_candidates('prompt', [{'path':'gone.md','score':.9,'rank':1}])
        self.assertFalse(rows)
        self.assertIn('missing', stats['excluded'][0]['reason'])

    def test_outside_workspace_and_symlink_are_not_read(self):
        with tempfile.TemporaryDirectory() as outside:
            p = Path(outside) / 'private.md'; p.write_text('prompt secret')
            try:
                (self.root / 'link.md').symlink_to(p)
            except OSError:
                self.skipTest('Symlinks unavailable for this user')
            for path in [str(p), 'link.md']:
                rows, stats = observe.rank_context_candidates('prompt', [{'path':path,'score':.99,'rank':1}])
                self.assertFalse(rows)
                self.assertIn('outside', stats['excluded'][0]['reason'])

    def test_alias_paths_are_deduplicated(self):
        hit = self.hit('memory/note.md', .8, 'prompt')
        rows, _ = observe.rank_context_candidates('prompt', [hit,dict(hit,path=str(self.root/'memory/note.md'))])
        self.assertEqual(len(rows), 1)

    def test_source_content_breaks_close_semantic_tie(self):
        unrelated = self.hit('misc.md', .77, 'gardening advice')
        relevant = self.hit('notes.md', .73, 'prompt clarity conflicting instructions', 2)
        rows, _ = observe.rank_context_candidates('prompt clarity conflicting instructions',[unrelated,relevant])
        self.assertEqual(rows[0]['path'], 'notes.md')

    def test_skill_supporting_assets_use_skill_layer(self):
        hit = self.hit('skills/word/references/guide.md', .99, 'word document')
        rows, stats = observe.rank_context_candidates('word document', [hit])
        self.assertFalse(rows)
        self.assertIn('supporting asset', stats['excluded'][0]['reason'])

    def test_imported_source_versions_deduplicate_without_losing_new_editions(self):
        body = '---\nurl: https://example.test/article\npublished: 2026-10-01\n---\nDocker restart logs'
        one = self.hit('sources/a.md', .8, body)
        two = self.hit('sources/b.md', .79, body, 2)
        newer = self.hit('sources/c.md', .78, body.replace('2026-10-01','2026-10-02') + '\nNew Docker restart recovery findings', 3)
        rows, stats = observe.rank_context_candidates('Docker restart logs', [one,two,newer])
        self.assertEqual([x['path'] for x in rows], ['sources/a.md','sources/c.md'])
        self.assertIn('duplicate source URL', stats['excluded'][0]['reason'])

    def test_retrieval_error_remains_error_not_empty_success(self):
        with patch.object(observe,'vault_qdrant_search',return_value=([], 'offline')):
            rows,error,stats=observe.retrieve_context('prompt')
        self.assertEqual(error,'offline')
        self.assertEqual(stats['signal'],'context_unavailable')


class SkillPrecisionTests(unittest.TestCase):
    def test_test_inflections_share_one_token(self):
        self.assertEqual(observe._tokens('test tests testing subtests'), {'test'})

    def test_local_recall_does_not_select_web_provider_but_public_comparison_can(self):
        with patch.object(observe,'_load_skill_metadata',return_value={'name':'exa-search','description':'Search the public web'}):
            self.assertIsNotNone(observe._scope_conflict('Find the previous decision about Observe','exa-search'))
            self.assertIsNone(observe._scope_conflict('Find the previous decision about Observe and compare public web sources','exa-search'))

    def test_debugger_requires_debugging_context(self):
        with patch.object(observe,'_load_skill_metadata',return_value={'name':'python-debugpy','description':'Python debugging'}):
            self.assertIsNotNone(observe._scope_conflict('Review Python tests','python-debugpy'))
            self.assertIsNone(observe._scope_conflict('Debug failing Python tests with breakpoints','python-debugpy'))

class StructuredVaultTests(unittest.TestCase):
    def test_zero_results_are_valid(self):
        self.assertEqual(observe._parse_qdrant_json('{"results":[]}'), [])

    def test_paths_are_data_even_when_they_resemble_output(self):
        import json
        path = 'notes/# 2 strange ← name.md'
        rows = observe._parse_qdrant_json(json.dumps({'results':[{'score':.8,'path':path}]}))
        self.assertEqual(rows[0]['path'],path)

    def test_malformed_schema_and_non_finite_scores_are_rejected(self):
        for text in ['{}','{"results":[null]}','{"results":[{"path":"a.md","score":true}]}',
                     '{"results":[{"path":"a.md","score":NaN}]}',
                     '{"results":[{"path":"a.md","score":2}]}']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                observe._parse_qdrant_json(text)

    def test_generic_search_words_do_not_establish_domain(self):
        detail={'matches':[{'word':w,'fields':['description','triggers']} for w in ['find','decision','query','search']]}
        self.assertFalse(observe._lexically_admitted(detail))
