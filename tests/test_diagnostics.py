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
                with job.stage('解析', site='测试站'):
                    raise ValueError('无法打开 https://example.test/?signature=private')
            job = manager.launch('analyze', target)
            manager.wait(job.id, timeout=3)
            self.assertEqual(job.snapshot()['status'], 'failed')
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
