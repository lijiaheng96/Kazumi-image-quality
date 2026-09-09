"""画质排名使用共同内容，测试不依赖网络或评分权重。"""
import importlib.util
import threading
import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('helper.quality'), '需要实现独立画质模块')
        from helper import quality
        self.q = quality

    def test_fingerprint_matches_compression_but_rejects_different_content(self):
        rng = np.random.default_rng(2026)
        a = Image.fromarray(rng.integers(0, 256, (128, 224, 3), dtype=np.uint8)).filter(ImageFilter.GaussianBlur(5))
        b = a.resize((112, 64)).resize(a.size)
        c = Image.fromarray(rng.integers(0, 256, (128, 224, 3), dtype=np.uint8)).filter(ImageFilter.GaussianBlur(5))
        self.assertLess(self.q.fingerprint_distance(a, b), 0.10)
        self.assertGreater(self.q.fingerprint_distance(a, c), 0.18)

    def test_black_and_flat_frames_cannot_verify_content(self):
        self.assertFalse(self.q.usable_frame(Image.new('RGB', (100, 60), 'black')))
        self.assertFalse(self.q.usable_frame(Image.new('RGB', (100, 60), '#929292')))

    def test_ranking_never_compares_different_sample_sets(self):
        rows = [
            {'site':'甲','scores':{0:60,1:70,2:80,3:10}},
            {'site':'乙','scores':{0:59,1:69,2:79}},
            {'site':'丙','scores':{0:95}},
        ]
        result = self.q.rank_common(rows)
        ranked = [r for r in result if r.get('rank')]
        self.assertEqual([r['site'] for r in ranked], ['甲', '乙'])
        self.assertEqual(ranked[0]['score'], 70)
        self.assertEqual(ranked[0]['samples'], 3)
        self.assertIsNone(next(r for r in result if r['site']=='丙').get('score'))

    def test_no_winner_with_only_one_eligible_source(self):
        result = self.q.rank_common([{'site':'甲','scores':{0:70,1:70,2:70}}])
        self.assertIsNone(result[0].get('rank'))
        self.assertIn('不足', result[0]['message'])

    def test_one_incompatible_sample_set_does_not_remove_a_valid_cross_site_group(self):
        rows = [
            {'site': '甲', 'scores': {0: 70, 1: 70, 2: 70, 3: 10}},
            {'site': '乙', 'scores': {0: 60, 1: 60, 2: 60}},
            {'site': '丙', 'scores': {1: 95, 2: 95, 3: 95}},
        ]
        for ordered in (rows, list(reversed(rows))):
            with self.subTest(order=[row['site'] for row in ordered]):
                result = self.q.rank_common(ordered)
                self.assertEqual([(row['site'], row['score'], row['samples'])
                                  for row in result if row['rank']], [('甲', 70, 3), ('乙', 60, 3)])
                excluded = next(row for row in result if row['site'] == '丙')
                self.assertIsNone(excluded['score'])
                self.assertIn('位置', excluded['message'])
                self.assertIn('不一致', excluded['message'])

    def test_comparison_group_prioritizes_site_coverage_over_scores_and_road_count(self):
        rows = [
            {'site': '甲', 'scores': {0: 70, 1: 70, 2: 70, 3: 70}},
            *[{'site': '乙', 'scores': {0: 99, 1: 99, 2: 99}} for _ in range(4)],
            {'site': '丙', 'scores': {1: 60, 2: 60, 3: 60}},
            {'site': '丁', 'scores': {1: 50, 2: 50, 3: 50}},
        ]
        ranked = [row for row in self.q.rank_common(rows) if row['rank']]
        self.assertEqual([row['site'] for row in ranked], ['甲', '丙', '丁'])
        self.assertTrue(all(row['samples'] == 3 for row in ranked))

    def test_three_common_positions_keep_a_better_low_resolution_road_in_the_same_site(self):
        rows = [
            {'site': '甲', 'road': '全部', 'scores': {0: 70, 1: 70, 2: 70, 3: 10}},
            {'site': '乙', 'road': '全部', 'scores': {0: 60, 1: 60, 2: 60, 3: 20}},
            {'site': '甲', 'road': '低分辨率', 'width': 640, 'height': 360,
             'scores': {0: 99, 1: 99, 2: 99}},
        ]
        result = self.q.rank_common(rows)
        ranked = [row for row in result if row['rank']]
        self.assertEqual([(row['site'], row['score'], row['samples']) for row in ranked],
                         [('甲', 99, 3), ('甲', 70, 3), ('乙', 60, 3)])
        self.assertTrue(next(row for row in result if row['road'] == '低分辨率')['best_road'])

    def test_all_four_positions_are_used_when_every_eligible_road_supports_them(self):
        result = self.q.rank_common([
            {'site': '甲', 'scores': {0: 70, 1: 70, 2: 70, 3: 10}},
            {'site': '乙', 'scores': {0: 60, 1: 60, 2: 60, 3: 20}},
        ])
        self.assertEqual([(row['site'], row['score'], row['samples']) for row in result],
                         [('甲', 55, 4), ('乙', 50, 4)])

    def test_equal_site_and_position_coverage_prefers_more_valid_roads(self):
        rows = [
            {'site': '甲', 'scores': {0: 70, 1: 70, 2: 70, 3: 70}},
            {'site': '乙', 'scores': {0: 99, 1: 99, 2: 99}},
            {'site': '丙', 'road': '一', 'scores': {1: 60, 2: 60, 3: 60}},
            {'site': '丙', 'road': '二', 'scores': {1: 50, 2: 50, 3: 50}},
        ]
        ranked = [row for row in self.q.rank_common(rows) if row['rank']]
        self.assertEqual([row['site'] for row in ranked], ['甲', '丙', '丙'])
        self.assertEqual([row['rank'] for row in ranked], [1, 2, 2])

    def test_invalid_model_scores_do_not_change_the_valid_comparison_group(self):
        for invalid in (float('nan'), float('inf'), None, '无效'):
            with self.subTest(invalid=invalid):
                rows = [
                    {'site': '甲', 'scores': {0: 70, 1: 70, 2: 70, 3: 10}},
                    {'site': '乙', 'scores': {0: 60, 1: 60, 2: 60, 3: 20}},
                    {'site': '丙', 'scores': {1: 50, 2: 50, 3: invalid}},
                ]
                result = self.q.rank_common(rows)
                self.assertEqual([(row['site'], row['samples']) for row in result if row['rank']],
                                 [('甲', 4), ('乙', 4)])
                excluded = next(row for row in result if row['site'] == '丙')
                self.assertIsNone(excluded['rank'])
                self.assertIn('无效分数', excluded['message'])

    def test_equal_scores_have_equal_rank(self):
        scores = {0:70,1:60,2:80}
        rows = self.q.rank_common([{'site':'甲','scores':scores},{'site':'乙','scores':scores}])
        self.assertEqual([r['rank'] for r in rows], [1,1])

    def test_different_sites_required_for_cross_site_ranking(self):
        scores = {0:70,1:60,2:80}
        rows = self.q.rank_common([{'site':'甲','scores':scores},{'site':'甲','scores':scores}])
        self.assertTrue(all(not r.get('rank') for r in rows))

    def test_low_resolution_and_bitrate_never_exclude_a_better_model_score(self):
        result = self.q.rank_common([
            {'site': '低分辨率', 'width': 640, 'height': 360, 'bit_rate': 250_000,
             'scores': {0: 76, 1: 75, 2: 74}},
            {'site': '高分辨率', 'width': 3840, 'height': 2160, 'bit_rate': 12_000_000,
             'scores': {0: 61, 1: 60, 2: 59}},
        ])
        self.assertEqual([(row['site'], row['rank'], row['score']) for row in result],
                         [('低分辨率', 1, 75), ('高分辨率', 2, 60)])

    def test_cpu_threads_increase_only_on_machines_with_at_least_twelve_logical_cpus(self):
        for logical_cpus, expected in ((14, 8), (8, 4), (2, 2), (None, 2)):
            with self.subTest(logical_cpus=logical_cpus), patch.object(self.q.os, 'cpu_count', return_value=logical_cpus):
                self.assertEqual(self.q.cpu_inference_threads(), expected)

    def test_model_loader_applies_cpu_thread_selection(self):
        set_threads = Mock()
        metric = object()
        modules = {'torch': SimpleNamespace(set_num_threads=set_threads),
                   'pyiqa': SimpleNamespace(create_metric=lambda *args, **kwargs: metric)}
        with patch.dict('sys.modules', modules), patch.object(self.q.os, 'cpu_count', return_value=14), \
                patch.object(self.q.QualityModel, 'installed', return_value=True):
            self.assertIs(self.q.QualityModel().load(), metric)
        set_threads.assert_called_once_with(8)


@unittest.skipUnless(importlib.util.find_spec('torch'), '未安装可选的本地推理环境')
class QualityInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import torch
        cls.torch = torch
        cls.addClassCleanup(torch.set_num_threads, torch.get_num_threads())
        torch.set_num_threads(1)

    def setUp(self):
        from helper.quality import QualityModel
        self.model = QualityModel()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = []
        for index, value in enumerate((32, 96, 192)):
            path = Path(self.temp.name) / f'{index}.png'
            Image.new('RGB', (160, 90), (value, value, value)).save(path)
            self.paths.append(str(path))

    def test_same_size_frames_are_batched_with_scores_in_original_order(self):
        batch_sizes = []

        def inference(tensor):
            batch_sizes.append(tensor.shape[0])
            return tensor[:, 0, 0, 0] * 255

        self.model._model = inference
        scores = self.model.score(self.paths + self.paths[:1], threading.Event())
        self.assertEqual(scores, [32., 96., 192., 32.])
        self.assertEqual(batch_sizes, [3, 1], '每位置三帧应合并推理，尾批保持原有顺序')

    def test_different_aspect_ratios_preserve_geometry_and_score_order(self):
        Image.new('RGB', (120, 90), (96, 96, 96)).save(self.paths[1])
        dimensions = []

        def inference(tensor):
            dimensions.append(tuple(tensor.shape))
            return tensor[:, 0, 0, 0] * 255

        self.model._model = inference
        self.assertEqual(self.model.score(self.paths, threading.Event()), [32., 96., 192.])
        self.assertEqual(dimensions, [(1, 3, 540, 960), (1, 3, 720, 960), (1, 3, 540, 960)])

    def test_cancelled_request_does_not_load_model(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(self.model, 'load', side_effect=AssertionError('不应加载模型')):
            with self.assertRaisesRegex(RuntimeError, '取消'):
                self.model.score(self.paths, cancel)

    def test_cancel_during_last_inference_discards_results(self):
        cancel = threading.Event()

        def inference(tensor):
            cancel.set()
            return self.torch.ones(tensor.shape[0])

        self.model._model = inference
        with self.assertRaisesRegex(RuntimeError, '取消'):
            self.model.score(self.paths[:1], cancel)

    def test_nonfinite_result_in_batch_is_rejected(self):
        self.model._model = lambda tensor: self.torch.full((tensor.shape[0],), float('nan'))
        with self.assertRaisesRegex(RuntimeError, '无效'):
            self.model.score(self.paths, threading.Event())

    def test_missing_score_in_batch_is_rejected(self):
        self.model._model = lambda tensor: self.torch.ones(1)
        with self.assertRaisesRegex(RuntimeError, '结果数量'):
            self.model.score(self.paths, threading.Event())

    def test_concurrent_requests_serialize_inference_and_cancel_waiter(self):
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        waiter_done = threading.Event()
        results = []

        def inference(tensor):
            if entered.is_set():
                second_entered.set()
            entered.set()
            if not release.wait(3):
                raise AssertionError('测试未及时放行模型')
            return self.torch.ones(tensor.shape[0])

        self.model._model = inference

        def score(cancel, done=None):
            try:
                results.append(self.model.score(self.paths[:1], cancel))
            except Exception as error:
                results.append(error)
            finally:
                if done:
                    done.set()

        first = threading.Thread(target=score, args=(threading.Event(),))
        cancel = threading.Event()
        waiter = threading.Thread(target=score, args=(cancel, waiter_done))
        first.start()
        try:
            self.assertTrue(entered.wait(2), '第一个请求应开始推理')
            waiter.start()
            self.assertFalse(second_entered.wait(.2), '共享模型不允许重叠推理')
            cancel.set()
            self.assertTrue(waiter_done.wait(1), '等待模型锁时取消应及时返回')
            self.assertTrue(any(isinstance(result, RuntimeError) and '取消' in str(result)
                                for result in results))
        finally:
            release.set()
            first.join(3)
            if waiter.ident is not None:
                waiter.join(3)


if __name__ == '__main__':
    unittest.main()
