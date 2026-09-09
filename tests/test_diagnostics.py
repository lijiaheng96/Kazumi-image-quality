"""本地诊断必须可追溯且不保留请求凭据或媒体链接。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from helper.jobs import Job, JobManager


class DiagnosticsTests(unittest.TestCase):
    def test_finished_job_is_saved_with_stage_time_and_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = JobManager(Path(temp))
            ready = threading.Event()
            def target(job):
                with job.stage('评分', site='测试站', road='线路1'):
                    job.update(results=[{'site':'测试站', 'score':70,
                                         'page_url':'https://example.test/?token=secret',
                                         'headers':{'Cookie':'secret'}, 'scores':{0:70}}])
                ready.set()
            job = manager.launch('analyze', target)
            self.assertTrue(ready.wait(3))
            manager.wait(job.id, timeout=3)
            files = list((Path(temp)/'diagnostics').glob('*.json'))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding='utf-8')
            report = json.loads(text)
            self.assertNotIn('secret', text)
            self.assertNotIn('https://', text)
            self.assertNotIn('scores', report['job']['results'][0])
            self.assertEqual(report['job']['status'], 'completed')
            self.assertGreaterEqual(report['job']['elapsed_seconds'], 0)
            self.assertEqual(report['events'][0]['stage'], '评分')
            self.assertGreaterEqual(report['events'][0]['seconds'], 0)

    def test_failed_stage_is_recorded_and_original_failure_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = JobManager(Path(temp))
            def target(job):
                job.update(mode='smart', selection_summary='最高分辨率已筛选')
                with job.stage('解析', site='测试站'):
                    raise ValueError('无法打开 https://example.test/?signature=private')
            job = manager.launch('analyze', target)
            manager.wait(job.id, timeout=3)
            self.assertEqual(job.snapshot()['status'], 'failed')
            self.assertIn('无法打开', job.snapshot()['message'])
            self.assertNotIn('最高分辨率已筛选', job.snapshot()['message'])
            report = manager.diagnostics.latest()
            self.assertEqual(report['events'][0]['status'], 'failed')
            self.assertNotIn('private', json.dumps(report))

    def test_live_job_reports_elapsed_time(self):
        job = Job('search')
        self.assertGreaterEqual(job.snapshot()['elapsed_seconds'], 0)

    def test_diagnostic_redaction_handles_uppercase_urls_and_credentials(self):
        from helper.diagnostics import clean
        result=clean({'message':'HTTPS://example.test/?token=private\nCookie: private\nAuthorization: Bearer private'})
        self.assertNotIn('private',result['message'])

    def test_smart_diagnostics_keep_selection_evidence_without_media_hints_or_credentials(self):
        from helper.diagnostics import Diagnostics
        with tempfile.TemporaryDirectory() as temp:
            job = Job('analyze')
            job.update(mode='smart', selection_summary='最高档选入三个网站', highest_resolution='1920×1080',
                       shortlist_count=3, searched_rule_ids=[0, 1, 2, 3], backup_attempts=1, results=[{
                           'site':'甲', 'fps':23.976, 'duration':1440, 'size_bytes':450000000,
                           'size_kind':'estimated', 'size_source':'segment_sample', 'size_confidence':'medium',
                           'size_note':'依据分片长度估算 HTTPS://example.test/?token=private',
                           'average_bitrate':2500000, 'bitrate_scope':'video', 'peak_bitrate':5000000,
                           'sampled_segments':3, 'spec_bitrate':2500000, 'spec_bitrate_kind':'average',
                           'bits_per_frame':104166, 'shortlist_rank':1, 'selection_status':'shortlisted',
                           'selection_reason':'最高分辨率，规格优先',
                           'hint':{'size_bytes':999, 'url':'https://example.test/?token=private'},
                           'selected_media':{'headers':{'Cookie':'private'}, 'url':'https://example.test/private'},
                           'headers':{'Authorization':'private'}, 'url':'https://example.test/private',
                       }])
            recorder = Diagnostics(Path(temp))
            recorder.save(job.snapshot(), [])
            report = recorder.latest()['job']
            row = report['results'][0]
            self.assertEqual(report['mode'], 'smart')
            self.assertEqual(report.get('searched_rule_ids'), [0, 1, 2, 3])
            self.assertEqual(report.get('selection_summary'), '最高档选入三个网站')
            self.assertEqual(report.get('highest_resolution'), '1920×1080')
            self.assertEqual(report.get('shortlist_count'), 3)
            self.assertEqual(report.get('backup_attempts'), 1)
            for field, expected in {
                'fps':23.976, 'duration':1440, 'size_bytes':450000000, 'size_kind':'estimated',
                'size_source':'segment_sample', 'size_confidence':'medium', 'average_bitrate':2500000,
                'bitrate_scope':'video', 'peak_bitrate':5000000, 'sampled_segments':3,
                'spec_bitrate':2500000, 'spec_bitrate_kind':'average', 'bits_per_frame':104166,
                'shortlist_rank':1, 'selection_status':'shortlisted', 'selection_reason':'最高分辨率，规格优先',
            }.items():
                self.assertEqual(row.get(field), expected, field)
            self.assertIn('依据分片长度估算', row.get('size_note', ''))
            serialized = json.dumps(report, ensure_ascii=False)
            for secret in ('private', 'https://', 'HTTPS://', 'selected_media', 'hint', 'headers'):
                self.assertNotIn(secret, serialized)

    def test_slow_old_checkpoint_cannot_overwrite_newer_state(self):
        from helper.diagnostics import Diagnostics
        with tempfile.TemporaryDirectory() as temp:
            recorder=Diagnostics(Path(temp))
            save=recorder.save
            entered,release=threading.Event(),threading.Event()
            def delayed(snapshot,events):
                if snapshot['progress']==1:
                    entered.set()
                    release.wait(2)
                save(snapshot,events)
            recorder.save=delayed
            job=Job('search')
            job.recorder=recorder
            job.update(progress=1)
            first=threading.Thread(target=job.persist)
            first.start()
            self.assertTrue(entered.wait(1))
            job.update(progress=2)
            second=threading.Thread(target=job.persist)
            second.start()
            second.join(.1)
            release.set()
            first.join(2)
            second.join(2)
            self.assertEqual(recorder.latest()['job']['progress'],2)


if __name__ == '__main__':
    unittest.main()
