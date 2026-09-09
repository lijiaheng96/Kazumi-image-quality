"""任务隔离与集数选择，避免把缺集或不同季悄悄当同一来源。"""
import importlib.util
import unittest


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
