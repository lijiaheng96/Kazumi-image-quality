"""本地 API 必须保护源规则文件，并拒绝跨站写入。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


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


if __name__ == '__main__':
    unittest.main()
