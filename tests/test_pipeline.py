"""调度提速不能牺牲低分辨率来源或制造并发失控。"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from helper.jobs import Job, JobManager


class PipelineTests(unittest.TestCase):
    def run_sources(self, mode='fast', fail_other_sites=False, duplicate_media=False, cancel_on_resolve=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        manager = JobManager(root)
        search = Job('search')
        search.rules = [{'name':'低分辨率'}, {'name':'高分辨率'}, {'name':'第三站'}]
        search.update(status='completed', results=[dict(id=str(i), site=rule['name'], title='测试动画',
                     rule_id=i, url=f'https://example.test/{i}') for i, rule in enumerate(search.rules)])
        lock = threading.Lock()
        active, peak = 0, 0
        sample_calls = []
        base_image = Image.fromarray(np.random.default_rng(42).integers(20,230,(72,128,3),dtype=np.uint8))
        def chapters(rule, url, timeout):
            return [{'name':'线路1','episodes':[{'number':1,'url':url}]}]
        def resolve(url, rule, cancel):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.06)
            with lock:
                active -= 1
            if fail_other_sites and not url.endswith('/0'):
                raise ValueError('站点暂时不可用')
            if cancel_on_resolve:
                cancel.set()
            if duplicate_media:
                url='https://example.test/shared'
            return {'url':url,'headers':{}}
        def probe(media, cancel):
            low = media['url'].endswith('/0')
            return dict(duration=100, width=1280 if low else 1920, height=720 if low else 1080,
                        codec='h264', bitrate=5_000_000 if low else 900_000, color_transfer=None)
        def capture(media, times, directory, cancel):
            directory.mkdir(parents=True, exist_ok=True)
            frames=[]
            for i, timestamp in enumerate(times):
                path=directory/f'{i}.png'
                # 文件内容只用于匹配；评分替身按来源返回已知质量。
                base_image.save(path)
                frames.append(dict(time=timestamp,path=str(path)))
            sample_calls.append((media['url'], len(times)))
            return frames
        def score(paths, cancel):
            return [85. if '/0/' in str(Path(p)).replace('\\','/') else 60. for p in paths]
        with patch('helper.rules.chapters',side_effect=chapters), patch('helper.media.resolve_media',side_effect=resolve), \
             patch('helper.media.probe',side_effect=probe), patch('helper.media.capture_frames',side_effect=capture), \
             patch('helper.quality.MODEL.load'), patch('helper.quality.MODEL.score',side_effect=score):
            job = manager.analyze(search, {'0','1','2'}, 1, mode=mode)
            manager.wait(job.id, timeout=8)
        return job.snapshot(), peak, sample_calls

    def test_fast_mode_keeps_low_resolution_high_quality_source_and_bounds_parallelism(self):
        result, peak, calls = self.run_sources()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['selected_candidate_ids'],['0','1','2'])
        self.assertTrue(result['search_job_id'])
        self.assertEqual(len(result['results']), 3)
        low = next(row for row in result['results'] if row['site']=='低分辨率')
        self.assertEqual(low['rank'], 1)
        self.assertEqual(low['samples'], 3)
        self.assertEqual(peak, 2, '独立媒体规格应最多两路并发')
        self.assertEqual(len(calls), 9, '三个站各三个位置，每个位置一次提三帧')
        self.assertTrue(all(row.get('timings') for row in result['results']))

    def test_full_mode_still_compares_four_shared_positions(self):
        result, _, calls = self.run_sources('full')
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(all(row.get('samples')==4 for row in result['results']))
        self.assertEqual(len(calls),12)

    def test_insufficient_readable_sites_does_not_leave_pending_rows(self):
        result,_,_=self.run_sources(fail_other_sites=True)
        self.assertEqual(result['status'],'failed')
        self.assertIn('不足两个',result['message'])
        self.assertFalse(any(row['status']=='等待取样' for row in result['results']))

    def test_identical_media_reuses_samples_and_retains_all_site_rows(self):
        result,_,calls=self.run_sources(duplicate_media=True)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(calls),3)
        self.assertEqual(len(result['results']),3)
        self.assertEqual(sum(bool(row.get('cached')) for row in result['results']),2)
        self.assertTrue(all(row.get('rank')==1 for row in result['results']))

    def test_cancel_during_prepare_stops_before_sampling(self):
        result,_,calls=self.run_sources(cancel_on_resolve=True)
        self.assertEqual(result['status'],'cancelled',result)
        self.assertEqual(calls,[])
        self.assertTrue(all(row['status']=='已取消' for row in result['results']))

    def test_invalid_mode_and_duplicate_site_selection_are_rejected(self):
        manager = JobManager(Path('.'))
        search = Job('search')
        search.update(results=[{'id':'a','site':'甲'},{'id':'b','site':'甲'},{'id':'c','site':'乙'}])
        with self.assertRaisesRegex(ValueError,'每个网站'):
            manager.analyze(search, {'a','b','c'},1)


if __name__ == '__main__':
    unittest.main()
