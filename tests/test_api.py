"""本地 API 必须保护源规则文件，并拒绝跨站写入。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('app'), '需要实现本地服务')
        from app import create_app
        from fastapi.testclient import TestClient
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = TestClient(create_app(self.root), base_url='http://127.0.0.1:18765')
        self.token = self.client.get('/api/config').json()['token']

    def test_writes_without_session_token_are_rejected(self):
        response = self.client.post('/api/config', json={'rules_path':'x'})
        self.assertEqual(response.status_code, 403)

    def test_cross_origin_even_with_token_rejected(self):
        response = self.client.post('/api/search', json={'keyword':'动画'}, headers={'X-Local-Token':self.token, 'Origin':'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    def test_saving_rule_path_never_changes_source_json(self):
        source = self.root / '我的规则.json'
        original = json.dumps([{'name':'测试站','baseURL':'https://example.com','searchURL':'https://example.com/?q=@keyword'}], ensure_ascii=False)
        source.write_text(original, encoding='utf-8')
        response = self.client.post('/api/config', json={'rules_path':str(source)}, headers={'X-Local-Token':self.token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(source.read_text(encoding='utf-8'), original)
        self.assertEqual(self.client.get('/api/config').json()['rules'][0]['name'], '测试站')

    def test_unknown_job_is_404(self):
        self.assertEqual(self.client.get('/api/jobs/missing').status_code, 404)

    def test_diagnostics_and_current_job_can_be_read_after_refresh(self):
        config=self.client.get('/api/config').json()
        self.assertIn('active_job', config)
        self.assertEqual(self.client.get('/api/diagnostics').status_code,200)
        recent=self.client.get('/api/recent').json()
        self.assertIn('job',recent)

    def test_unknown_analysis_mode_is_rejected(self):
        response=self.client.post('/api/analyze',json={'search_job_id':'x','candidate_ids':['a','b'],'mode':'unknown'},
                                  headers={'X-Local-Token':self.token})
        self.assertEqual(response.status_code,422)

    def test_analysis_defaults_to_smart_and_keeps_legacy_modes(self):
        from helper.jobs import Job
        manager = self.client.app.state.manager
        source = Job('search')
        source.rules = [{'name':'甲'}, {'name':'乙'}]
        source.update(status='completed', searched_rule_ids=[0, 1], results=[
            {'id':'a', 'rule_id':0, 'site':'甲'}, {'id':'b', 'rule_id':1, 'site':'乙'},
        ])
        manager.jobs[source.id] = source
        def prepared(job, all_rules, candidates, episode, mode):
            job.update(mode=mode)
        for supplied, expected in ((None, 'smart'), ('smart', 'smart'), ('fast', 'fast'), ('full', 'full')):
            with self.subTest(mode=supplied), patch.object(manager, '_analyze', side_effect=prepared):
                body = {'search_job_id':source.id, 'candidate_ids':['a', 'b']}
                if supplied is not None:
                    body['mode'] = supplied
                response = self.client.post('/api/analyze', json=body, headers={'X-Local-Token':self.token})
                self.assertEqual(response.status_code, 200, response.text)
                result = manager.wait(response.json()['job_id'], timeout=3)
                self.assertEqual(result['mode'], expected)

    def test_default_analysis_requires_searching_all_rules(self):
        from helper.jobs import Job
        manager = self.client.app.state.manager
        source = Job('search')
        source.rules = [{'name':'甲'}, {'name':'乙'}, {'name':'丙'}]
        source.update(status='completed', searched_rule_ids=[0, 1], results=[
            {'id':'a', 'rule_id':0, 'site':'甲'}, {'id':'b', 'rule_id':1, 'site':'乙'},
        ])
        manager.jobs[source.id] = source
        with patch.object(manager, '_analyze', side_effect=lambda *args: None):
            response = self.client.post('/api/analyze', json={
                'search_job_id':source.id, 'candidate_ids':['a', 'b'],
            }, headers={'X-Local-Token':self.token})
            if response.status_code == 200:
                manager.wait(response.json()['job_id'], timeout=3)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('重新搜索全部规则', response.json()['detail'])

    def test_restart_recovers_interrupted_record_as_interrupted(self):
        from helper.jobs import Job
        from helper.diagnostics import Diagnostics
        job=Job('analyze')
        job.update(results=[{'site':'测试站','status':'解析中'}])
        Diagnostics(self.root).save(job.snapshot(),[])
        recent=self.client.get('/api/recent').json()
        self.assertFalse(recent['live'])
        self.assertEqual(recent['job']['status'],'interrupted')
        self.assertEqual(recent['job']['results'][0]['status'],'已中断')


if __name__ == '__main__':
    unittest.main()
