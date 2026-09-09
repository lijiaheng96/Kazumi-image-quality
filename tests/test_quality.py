"""画质排名使用共同内容，测试不依赖网络或评分权重。"""
import importlib.util
import threading
import unittest
from pathlib import Path
import tempfile

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

    def test_equal_scores_have_equal_rank(self):
        scores = {0:70,1:60,2:80}
        rows = self.q.rank_common([{'site':'甲','scores':scores},{'site':'乙','scores':scores}])
        self.assertEqual([r['rank'] for r in rows], [1,1])

    def test_different_sites_required_for_cross_site_ranking(self):
        scores = {0:70,1:60,2:80}
        rows = self.q.rank_common([{'site':'甲','scores':scores},{'site':'甲','scores':scores}])
        self.assertTrue(all(not r.get('rank') for r in rows))


if __name__ == '__main__':
    unittest.main()
