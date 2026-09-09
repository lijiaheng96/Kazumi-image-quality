"""任务隔离与集数选择，避免把缺集或不同季悄悄当同一来源。"""
import importlib.util
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch


class JobLogicTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('helper.jobs'), '需要实现任务模块')
        from helper import jobs
        self.j = jobs

    def test_episode_uses_site_coverage_not_road_count(self):
        sources = [
            {'site':'甲','roads':[{'episodes':[{'number':2}]}]*4},
            {'site':'乙','roads':[{'episodes':[{'number':1}]}]},
            {'site':'丙','roads':[{'episodes':[{'number':1}]}]},
        ]
        self.assertEqual(self.j.choose_episode(sources), 1)

    def test_special_episode_is_not_assumed_first_episode(self):
        self.assertIsNone(self.j.choose_episode([{'site':'甲','roads':[{'episodes':[{'number':None}]}]}]))

    def test_exact_match_does_not_remove_season_numbers(self):
        self.assertTrue(self.j.same_title('葬送的芙莉莲', ' 葬送的芙莉莲 '))
        self.assertFalse(self.j.same_title('示例动画', '示例动画 第二季'))

    def test_cancel_keeps_partial_results(self):
        job = self.j.Job('search')
        job.update(results=[{'site':'甲'}])
        job.cancel.set()
        job.finish()
        self.assertEqual(job.snapshot()['status'], 'cancelled')
        self.assertEqual(len(job.snapshot()['results']), 1)

    def test_cancelled_search_never_starts_queued_site_requests(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            manager=self.j.JobManager(Path(temp))
            job=self.j.Job('search')
            job.cancel.set()
            with patch('helper.rules.search',side_effect=AssertionError('已取消，不应联网')):
                manager._search(job,[{'name':str(i)} for i in range(9)],'动画',list(range(9)))
            self.assertEqual(job.snapshot()['results'],[])
            self.assertEqual(job.snapshot().get('searched_rule_ids'), [])

    def test_search_records_failed_and_empty_rule_attempts_without_claiming_unselected_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = self.j.JobManager(Path(temp))
            job = self.j.Job('search')
            def search(rule, keyword, timeout):
                if rule['name'] == '丙':
                    raise ValueError('站点暂时不可用')
                return []
            with patch('helper.rules.search', side_effect=search):
                manager._search(job, [{'name':'甲'}, {'name':'乙'}, {'name':'丙'}], '动画', [0, 2])
            self.assertEqual(job.snapshot().get('searched_rule_ids'), [0, 2])
            self.assertEqual({row['site'] for row in job.snapshot()['errors']}, {'甲', '丙'})

    def test_smart_rejects_missing_partial_and_duplicate_coverage_but_legacy_modes_work(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = self.j.JobManager(Path(temp))
            source = self.j.Job('search')
            source.rules = [{'name':'甲'}, {'name':'乙'}, {'name':'丙'}]
            source.update(status='completed', results=[
                {'id':'a', 'rule_id':0, 'site':'甲'}, {'id':'b', 'rule_id':1, 'site':'乙'},
            ])
            with patch.object(manager, '_analyze', side_effect=lambda *args: None):
                for coverage in (None, [0, 1], [0, 0, 1]):
                    with self.subTest(coverage=coverage):
                        if coverage is not None:
                            source.update(searched_rule_ids=coverage)
                        try:
                            with self.assertRaisesRegex(ValueError, '重新搜索全部规则'):
                                manager.analyze(source, {'a', 'b'}, 1, mode='smart')
                        finally:
                            for job in list(manager.jobs.values()):
                                manager.wait(job.id, timeout=3)
                for mode in ('fast', 'full'):
                    job = manager.analyze(source, {'a', 'b'}, 1, mode=mode)
                    self.assertEqual(manager.wait(job.id, timeout=3)['status'], 'completed')

    def test_smart_default_accepts_complete_coverage_and_preserves_same_site_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = self.j.JobManager(Path(temp))
            source = self.j.Job('search')
            source.rules = [{'name':'甲'}, {'name':'乙'}]
            source.update(status='completed', searched_rule_ids=[1, 0], results=[
                {'id':'a', 'rule_id':0, 'site':'甲'}, {'id':'b', 'rule_id':1, 'site':'乙'},
                {'id':'c', 'rule_id':0, 'site':'甲'},
            ])
            def prepared(job, all_rules, candidates, episode, mode):
                job.update(mode=mode)
            with patch.object(manager, '_analyze', side_effect=prepared):
                job = manager.analyze(source, {'a', 'b'}, 1)
                result = manager.wait(job.id, timeout=3)
                self.assertEqual(result['mode'], 'smart')
                self.assertEqual(result['searched_rule_ids'], [0, 1])
                with self.assertRaisesRegex(ValueError, '每个网站只能选择'):
                    manager.analyze(source, {'a', 'b', 'c'}, 1, mode='smart')

    def test_smart_finish_explains_selection_without_changing_line_failure_or_cancel(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                job = self.j.Job('analyze')
                job.update(mode='smart', selection_summary='最高分辨率仅一个网站，未形成跨站排名', results=[
                    {'site':'甲', 'status':'未参与排名', 'message':'没有足够共同内容'},
                ])
                if cancelled:
                    job.cancel.set()
                job.finish()
                result = job.snapshot()
                if cancelled:
                    self.assertEqual(result['status'], 'cancelled')
                    self.assertIn('已取消', result['message'])
                else:
                    self.assertIn('最高分辨率仅一个网站', result['message'])
                    self.assertEqual(result['results'][0]['message'], '没有足够共同内容')

    def test_cancel_analysis_keeps_completed_scores_and_marks_pending_rows(self):
        job = self.j.Job('analyze')
        job.update(results=[
            {'site':'甲','scores':{0:70,1:70,2:70},'status':'已检测'},
            {'site':'乙','scores':{0:60,1:60,2:60},'status':'已检测'},
            {'site':'丙','status':'解析中','message':'正在解析'},
        ])
        job.cancel.set()
        job.finish()
        rows = job.snapshot()['results']
        self.assertEqual(rows[0].get('score'),70)
        self.assertEqual(rows[0]['rank'],1)
        self.assertEqual(rows[2]['status'],'已取消')


if __name__ == '__main__':
    unittest.main()
