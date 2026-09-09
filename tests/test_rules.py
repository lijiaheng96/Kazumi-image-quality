"""规则兼容层固定响应测试；不访问真实网站。"""
import json
import gzip
import http.server
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from helper import rules


XPATH = {
    "name": "中文站点", "baseURL": "https://anime.example",
    "searchURL": "https://anime.example/search?wd=@keyword",
    "searchList": "//li", "searchName": ".//a", "searchResult": ".//a",
    "chapterRoads": "//div[@class='road']", "chapterResult": ".//a",
}


class RuleTests(unittest.TestCase):
    def test_search_duplicate_entries_do_not_create_ambiguous_matches(self):
        raw = '<ul><li><a href="/1">葬送的芙莉莲</a></li><li><a href="/1">葬送的芙莉莲</a></li><li><a href="/2">葬送的芙莉莲 第二季</a></li></ul>'
        self.assertEqual(rules.parse_search(XPATH, raw), [
            {"title": "葬送的芙莉莲", "url": "https://anime.example/1"},
            {"title": "葬送的芙莉莲 第二季", "url": "https://anime.example/2"}])

    def test_xpath_episode_whitespace_does_not_join_separate_numbers(self):
        raw = '<div class="road"><a href="/1">1 2</a><a href="/2">第 12 集</a></div>'
        episodes = rules.parse_chapters(XPATH, raw)[0]["episodes"]
        self.assertEqual([item["number"] for item in episodes], [None, 12])

    def test_duplicate_episode_links_do_not_make_episode_ambiguous(self):
        raw = '<div class="road"><a href="/1">第1集</a><a href="/1">第1集</a><a href="/1-other">第1集</a></div>'
        episodes = rules.parse_chapters(XPATH, raw)[0]["episodes"]
        self.assertEqual([item["url"] for item in episodes], [
            "https://anime.example/1", "https://anime.example/1-other"])

    def test_api_invalid_episode_does_not_discard_valid_episodes(self):
        rule = dict(XPATH, chapterMode="api")
        raw = json.dumps({"data": {"roads": [{"episodes": [
            {"name": "第1集", "url": "javascript:void(0)"},
            {"name": "第2集", "url": "/2"}, {"name": "第2集", "url": "/2"}]}]}})
        self.assertEqual(rules.parse_chapters(rule, raw)[0]["episodes"], [
            {"name": "第2集", "url": "https://anime.example/2", "number": 2}])

    def test_xpath_search_chinese_relative_and_empty(self):
        raw = '<ul><li><a href="/show/1"> 葬送的芙莉莲 </a></li><li>无链接</li></ul>'
        self.assertEqual(rules.parse_search(XPATH, raw), [
            {"title": "葬送的芙莉莲", "url": "https://anime.example/show/1"}])

    def test_xpath_episode_labels_not_positions(self):
        raw = '<div class="road"><a href="/1">第十二集</a><a href="/2">12.5</a><a href="/3">OVA 1</a><a href="/4"></a><a href="/5">最终话</a></div>'
        episodes = rules.parse_chapters(XPATH, raw)[0]["episodes"]
        self.assertEqual([x["number"] for x in episodes], [12, 12.5, None, None, None])
        self.assertEqual(episodes[0]["url"], "https://anime.example/1")

    def test_xpath_legacy_double_slash_stays_inside_each_result(self):
        rule = dict(XPATH, searchName="//a/text()", searchResult="//a")
        raw = '<ul><li><a href="/1">甲</a></li><li><a href="/2">乙</a></li></ul>'
        self.assertEqual([item["title"] for item in rules.parse_search(rule, raw)], ["甲", "乙"])

    def test_xpath_bare_double_slash_keeps_one_current_root(self):
        # xpath_selector 3.0.2 将裸 // 解析为空选择器列表，结果保留当前根节点。
        rule = dict(XPATH, chapterRoads="//", chapterResult="//div[@class='road']/a")
        raw = '<div class="road"><a href="/1">第1集</a></div><div class="road"><a href="/2">第2集</a><a href="/ova">OVA1</a></div>'
        roads = rules.parse_chapters(rule, raw)
        self.assertEqual(len(roads), 1)
        self.assertEqual([item["number"] for item in roads[0]["episodes"]], [1, 2, None])
        self.assertEqual([item["url"] for item in roads[0]["episodes"]], [
            "https://anime.example/1", "https://anime.example/2", "https://anime.example/ova"])

    def test_xpath_bare_double_slash_keeps_subnode_scope(self):
        rule = dict(XPATH, searchList="//a", searchName="//", searchResult=" // ")
        raw = '<ul><li><a href="/1">甲</a></li><li><a href="/2">乙</a></li></ul>'
        self.assertEqual(rules.parse_search(rule, raw), [
            {"title": "甲", "url": "https://anime.example/1"},
            {"title": "乙", "url": "https://anime.example/2"}])

    def test_ambiguous_and_special_episode_numbers(self):
        for label in ["SP01", "第1集 预告", "剧场版2", "1-12全集", "2024版", "第一季第2集", "第1集 上", "1 / 2"]:
            with self.subTest(label=label):
                self.assertIsNone(rules.episode_number(label))
        for label, number in [("第01话", 1), ("EP.03", 3), ("十二", 12), ("第两百零一集", 201), ("第１２集", 12)]:
            self.assertEqual(rules.episode_number(label), number)

    def test_api_search_preserves_source_id(self):
        rule = dict(XPATH, searchMode="api", searchApiConfig={"listPath": "$.data[*]", "namePath": "$.title", "sourcePath": "$.id"})
        self.assertEqual(rules.parse_search(rule, '{"data":[{"title":"中文番剧","id":42}]}'), [{"title": "中文番剧", "url": "42"}])

    def test_api_nested_template_indices_not_episode_numbers(self):
        rule = dict(XPATH, chapterMode="api", chapterApiConfig={
            "variables": {"id": "$.id"}, "episodeUrlPath": "", "episodePage": {
                "url": "/play/@id/@roadNumber/@episodeNumber", "query": {"source": "@source"}}})
        raw = json.dumps({"id": 8, "data": {"roads": [{"name": "高清", "episodes": [{"name": "第3集"}, {"name": "特别篇1"}, {"name": ""}]}]}}, ensure_ascii=False)
        road = rules.parse_chapters(rule, raw, "条目42")[0]
        self.assertEqual(road["name"], "高清")
        self.assertEqual([e["number"] for e in road["episodes"]], [3, None, None])
        self.assertIn("/play/8/1/1?source=", road["episodes"][0]["url"])

    def test_api_delimited_url_keeps_field_separator(self):
        rule = dict(XPATH, chapterMode="api", chapterApiConfig={"format": "delimited", "roadNamesPath": "$.from", "roadEpisodesPath": "$.urls"})
        raw = json.dumps({"from": "线路甲$$$线路乙", "urls": "第2集$/p?q=a$b#OVA 1$/o$$$第2集$/q"})
        roads = rules.parse_chapters(rule, raw)
        self.assertEqual(len(roads), 2)
        self.assertEqual(roads[0]["episodes"][0]["url"], "https://anime.example/p?q=a$b")
        self.assertIsNone(roads[0]["episodes"][1]["number"])

    def test_captcha_raises_explicit_error(self):
        for config, raw in [({"captchaDetectType": 2, "captchaDetectValue": "请输入验证码"}, "请输入验证码"), ({"captchaDetectType": 1, "captchaDetectValue": "//input[@id='captcha']"}, '<input id="captcha">')]:
            rule = dict(XPATH, antiCrawlerConfig=dict(config, enabled=True))
            with self.assertRaisesRegex(rules.CaptchaRequiredError, "验证"):
                rules.parse_search(rule, raw)

    def test_load_wrapped_rules_utf8_bom_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "规则.json"
            path.write_text(json.dumps({"plugins": [XPATH]}, ensure_ascii=False), encoding="utf-8-sig")
            before = path.read_bytes()
            self.assertEqual(rules.load_rules(str(path))[0]["name"], "中文站点")
            self.assertEqual(before, path.read_bytes())

    def test_invalid_rule_and_unsupported_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text('[{"name":"坏规则"}]', encoding="utf-8")
            with self.assertRaises(rules.RuleError):
                rules.load_rules(str(path))
        with self.assertRaises(rules.RuleError):
            rules.parse_search(dict(XPATH, searchList="[bad"), "<html></html>")
        with self.assertRaises(rules.RuleError):
            rules.parse_search(dict(XPATH, searchMode="unknown"), "")

    def test_xpath_post_request_encoding_headers(self):
        rule = dict(XPATH, usePost=True, userAgent="test-browser", referer="https://anime.example/")
        with patch.object(rules, "_fetch", return_value="<html></html>") as fetch:
            rules.search(rule, "中文 空格", timeout=3)
        request = fetch.call_args.args[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "https://anime.example/search")
        self.assertEqual(request["data"], {"wd": "中文 空格"})
        self.assertEqual(request["headers"]["User-Agent"], "test-browser")

    def test_api_request_json_template_native_values(self):
        config = {"method": "POST", "url": "https://api.example/@source", "query": {"enabled": True}, "bodyType": "json", "body": {"id": "@number", "list": ["@keyword"]}}
        request = rules.prepare_api_request(config, {"source": "a/b", "number": 9, "keyword": "中文"})
        self.assertEqual(request["url"], "https://api.example/a%2Fb")
        self.assertEqual(request["json"], {"id": 9, "list": ["中文"]})
        self.assertEqual(request["params"]["enabled"], "true")
        with self.assertRaisesRegex(rules.RuleError, "变量"):
            rules.prepare_api_request(config, {})

    def test_non_http_request_blocked(self):
        with self.assertRaisesRegex(rules.RuleError, "HTTP"):
            rules.chapters(XPATH, "file:///C:/secret.txt")

    def test_response_decodes_utf8_and_declared_gb18030(self):
        self.assertEqual(rules._decode("中文".encode("utf-8"), {}), "中文")
        self.assertEqual(rules._decode("中文".encode("gb18030"), {"Content-Type": "text/html; charset=GB18030"}), "中文")

    def _response(self, content=b"", headers=None, status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = headers or {}
        response.status_code = status
        response.iter_content.return_value = iter([content])
        return response

    def test_fetch_response_limit_and_forbidden_redirect(self):
        request = {"method": "GET", "url": "https://anime.example/"}
        for response, error in [(self._response(headers={"Content-Length": str(rules.MAX_RESPONSE_BYTES + 1)}), "限制"),
                                (self._response(headers={"Location": "file:///C:/secret.txt"}, status=302), "HTTP")]:
            with patch.object(rules.requests, "Session") as factory:
                factory.return_value.__enter__.return_value.request.return_value = response
                with self.assertRaisesRegex(rules.RuleError, error):
                    rules._fetch(request, 2)

    def test_fetch_stream_limit_without_content_length(self):
        with patch.object(rules, "MAX_RESPONSE_BYTES", 3), patch.object(rules.requests, "Session") as factory:
            factory.return_value.__enter__.return_value.request.return_value = self._response(b"1234")
            with self.assertRaisesRegex(rules.RuleError, "限制"):
                rules._fetch({"method": "GET", "url": "https://anime.example/"})

    def test_fetch_slow_response_obeys_total_deadline(self):
        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "20")
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(rules.RuleError, "超时"):
                rules._fetch({"method": "GET", "url": f"http://127.0.0.1:{server.server_port}/"}, timeout=0.3)
            self.assertLess(time.monotonic() - started, 1.0, "少量持续到达的数据不应绕过总超时")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_fetch_large_page_does_not_spend_deadline_on_byte_processing(self):
        content = ("中文正文" * 65536).encode("utf-8")
        self.assertEqual(self._fetch_local_response(content, timeout=1.5), content.decode("utf-8"))

    def test_fetch_gzip_page_and_decoded_size_limit(self):
        content = "中文正文" * 1000
        compressed = gzip.compress(content.encode("utf-8"))
        self.assertEqual(self._fetch_local_response(compressed, encoding="gzip"), content)
        with patch.object(rules, "MAX_RESPONSE_BYTES", 2000):
            with self.assertRaisesRegex(rules.RuleError, "限制"):
                self._fetch_local_response(compressed, encoding="gzip")

    def test_fetch_slow_gzip_header_cannot_bypass_deadline(self):
        # FNAME 标记后的文件名可以一直没有结束符，解压器因此一直没有正文输出。
        content = b"\x1f\x8b\x08\x08\x00\x00\x00\x00\x00\xff" + b"x" * 35
        started = time.monotonic()
        with self.assertRaisesRegex(rules.RuleError, "超时"):
            self._fetch_local_response(content, encoding="gzip", interval=0.05, timeout=0.25)
        self.assertLess(time.monotonic() - started, 1.2)

    def _fetch_local_response(self, content, encoding="", interval=0, timeout=3):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                if encoding:
                    self.send_header("Content-Encoding", encoding)
                self.end_headers()
                try:
                    if interval:
                        for byte in content:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(interval)
                    else:
                        self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            return rules._fetch({"method": "GET", "url": f"http://127.0.0.1:{server.server_port}/"}, timeout)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_fetch_captcha_even_when_http_forbidden(self):
        response = self._response("请输入验证码".encode("utf-8"), status=403)
        with patch.object(rules.requests, "Session") as factory:
            factory.return_value.__enter__.return_value.request.return_value = response
            with self.assertRaises(rules.CaptchaRequiredError):
                rules._fetch({"method": "GET", "url": "https://anime.example/"})
        response.raise_for_status.assert_not_called()

    def test_discover_normal_and_msix(self):
        with tempfile.TemporaryDirectory() as folder:
            roaming = Path(folder) / "Roaming"
            local = Path(folder) / "Local"
            first = roaming / "com.example" / "kazumi" / "plugins" / "v2" / "plugins.json"
            second = local / "Packages" / "Predidit.Kazumi_abc" / "LocalCache" / "Roaming" / "com.example" / "kazumi" / "plugins" / "v2" / "plugins.json"
            for path in [first, second]:
                path.parent.mkdir(parents=True)
                path.write_text("[]", encoding="utf-8")
            with patch.dict(os.environ, {"APPDATA": str(roaming), "LOCALAPPDATA": str(local)}):
                found = rules.discover_rule_files()
            self.assertIn(str(first), found)
            self.assertIn(str(second), found)


if __name__ == "__main__":
    unittest.main()
