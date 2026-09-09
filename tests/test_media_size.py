"""仅用本机 HTTP 服务验证大小估算，不访问真实视频站。"""

import http.server
import importlib.util
import threading
import time
import unittest


class MediaSizeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("helper.media_size"), "尚未实现有界媒体大小估算")
        from helper import media_size, media
        self.module, self.media = media_size, media
        self.cancel = threading.Event()
        self.routes, self.requests = {}, []
        routes, received = self.routes, self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.respond()

            def do_GET(self):
                self.respond()

            def respond(self):
                received.append((self.command, self.path, dict(self.headers)))
                status, headers, body = routes.get((self.command, self.path), (404, {}, b""))
                if "_slow_headers" in headers:
                    try:
                        for char in b"HTTP/1.0 200 OK\r\nContent-Length: 9000000\r\n\r\n":
                            self.wfile.write(bytes([char]))
                            self.wfile.flush()
                            time.sleep(headers["_slow_headers"])
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    return
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, str(value))
                self.end_headers()
                if self.command == "GET" and body:
                    try:
                        if callable(body):
                            body(self)
                        else:
                            self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(timeout=2)

    def playlist(self, path, text):
        body = text.encode("utf-8")
        self.routes["GET", path] = 200, {"Content-Type": "application/vnd.apple.mpegurl", "Content-Length": len(body)}, body

    def source(self, path):
        return {"url": self.base + path, "headers": {"Referer": self.base + "/play"}}

    def estimate(self, path, duration=30, **kwargs):
        return self.module.estimate_size(self.source(path), {"duration": duration}, self.cancel, **kwargs)

    def test_mp4_variant_selection_does_not_access_network(self):
        source = self.source("/video.mp4")
        selected, hint = self.module.select_variant(source, self.cancel)
        self.assertEqual(selected, source)
        self.assertEqual(hint, {})
        self.assertEqual(self.requests, [])

    def test_head_returns_full_size_and_container_rate_without_video_body(self):
        self.routes["HEAD", "/video.mp4"] = 200, {"Content-Length": 3000000, "Content-Type": "video/mp4"}, b""
        result = self.estimate("/video.mp4")
        self.assertEqual(result["size_bytes"], 3000000)
        self.assertEqual(result["average_bitrate"], 800000)
        self.assertEqual(result["size_kind"], "exact")
        self.assertEqual(result["bitrate_scope"], "container")
        self.assertEqual([item[0] for item in self.requests], ["HEAD"])

    def test_range_fallback_uses_content_range_total_not_one_byte_length(self):
        self.routes["HEAD", "/video.mp4"] = 405, {}, b""
        self.routes["GET", "/video.mp4"] = 206, {"Content-Range": "bytes 0-0/9000000", "Content-Length": 1}, b"x"
        result = self.estimate("/video.mp4")
        self.assertEqual(result["size_bytes"], 9000000)
        self.assertEqual(result["size_source"], "content_range")
        self.assertEqual(self.requests[-1][2].get("Range"), "bytes=0-0")

    def test_range_ignored_uses_whole_resource_length_without_waiting_for_body(self):
        self.routes["HEAD", "/video.mp4"] = 200, {}, b""
        self.routes["GET", "/video.mp4"] = 200, {"Content-Length": 900000000}, b""
        started = time.monotonic()
        result = self.estimate("/video.mp4")
        self.assertEqual(result["size_bytes"], 900000000)
        self.assertLess(time.monotonic() - started, 1)

    def test_invalid_and_html_sizes_stay_unknown(self):
        for headers in ({"Content-Length": -1}, {"Content-Length": "NaN"},
                        {"Content-Length": 123, "Content-Type": "text/html"},
                        {"Content-Length": 123, "Content-Encoding": "gzip"}):
            with self.subTest(headers=headers):
                self.routes["HEAD", "/video.mp4"] = 200, headers, b""
                result = self.estimate("/video.mp4")
                self.assertIsNone(result["size_bytes"])
                self.assertIsNone(result["average_bitrate"])

    def test_master_selects_highest_resolution_then_declared_rate_and_reuses_leaf(self):
        self.playlist("/master.m3u8", '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=9999999,RESOLUTION=1280x720\nlow.m3u8\n'
                      '#EXT-X-STREAM-INF:BANDWIDTH=3000000,AVERAGE-BANDWIDTH=2400000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\nhigh.m3u8\n'
                      '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080\nother.m3u8\n')
        self.playlist("/high.m3u8", "#EXTM3U\n#EXTINF:30,\na.ts\n#EXT-X-ENDLIST\n")
        selected, hint = self.module.select_variant(self.source("/master.m3u8"), self.cancel)
        self.assertEqual(selected["url"], self.base + "/high.m3u8")
        result = self.module.estimate_size(selected, {"duration": 30}, self.cancel, manifest_info=hint)
        self.assertEqual(result["size_bytes"], 9000000)
        self.assertEqual(result["average_bitrate"], 2400000)
        self.assertEqual(result["peak_bitrate"], 3000000)
        self.assertEqual(result["size_kind"], "estimated")
        self.assertEqual([item[1] for item in self.requests], ["/master.m3u8", "/high.m3u8"])

    def test_peak_bandwidth_alone_does_not_become_average_or_size(self):
        self.playlist("/master.m3u8", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080\nhigh.m3u8\n")
        self.playlist("/high.m3u8", "#EXTM3U\n#EXTINF:30,\na.ts\n#EXT-X-ENDLIST\n")
        selected, hint = self.module.select_variant(self.source("/master.m3u8"), self.cancel)
        result = self.module.estimate_size(selected, {"duration": 30}, self.cancel, manifest_info=hint)
        self.assertIsNone(result["average_bitrate"])
        self.assertIsNone(result["size_bytes"])
        self.assertEqual(result["peak_bitrate"], 3000000)

    def test_complete_byte_ranges_include_one_initialization_map(self):
        self.playlist("/video.m3u8", '#EXTM3U\n#EXT-X-MAP:URI="whole.mp4",BYTERANGE="100@0"\n'
                      '#EXTINF:10,\n#EXT-X-BYTERANGE:2000@100\nwhole.mp4\n'
                      '#EXTINF:20,\n#EXT-X-BYTERANGE:4000\nwhole.mp4\n#EXT-X-ENDLIST\n')
        result = self.estimate("/video.m3u8")
        self.assertEqual(result["size_bytes"], 6100)
        self.assertEqual(result["size_kind"], "exact")
        self.assertEqual(result["size_source"], "hls_byterange")
        self.assertEqual(len(self.requests), 1)

    def test_segment_heads_are_duration_weighted_and_never_download_segments(self):
        self.playlist("/video.m3u8", "#EXTM3U\n#EXTINF:5,\na.ts\n#EXTINF:10,\nb.ts\n#EXTINF:15,\nc.ts\n#EXT-X-ENDLIST\n")
        for path, size in (("/a.ts", 100000), ("/b.ts", 300000), ("/c.ts", 600000)):
            self.routes["HEAD", path] = 200, {"Content-Length": size}, b""
        result = self.estimate("/video.m3u8")
        self.assertEqual(result["size_bytes"], 1000000)
        self.assertEqual(result["size_kind"], "estimated")
        self.assertEqual(result["sampled_segments"], 3)
        self.assertTrue(all(method == "HEAD" for method, path, _ in self.requests if path.endswith(".ts")))

    def test_live_manifest_and_manifest_file_length_are_not_whole_video_size(self):
        self.playlist("/live.m3u8", "#EXTM3U\n#EXTINF:10,\na.ts\n")
        result = self.estimate("/live.m3u8")
        self.assertIsNone(result["size_bytes"])
        self.assertEqual(len(self.requests), 1)
        self.assertIn("直播", result["size_note"])

    def test_external_audio_scope_is_explicit(self):
        self.playlist("/master.m3u8", '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio.m3u8"\n'
                      '#EXT-X-STREAM-INF:BANDWIDTH=3000000,AVERAGE-BANDWIDTH=2500000,RESOLUTION=1920x1080,AUDIO="audio"\nv.m3u8\n')
        self.playlist("/v.m3u8", "#EXTM3U\n#EXTINF:30,\na.ts\n#EXT-X-ENDLIST\n")
        selected, hint = self.module.select_variant(self.source("/master.m3u8"), self.cancel)
        result = self.module.estimate_size(selected, {"duration": 30}, self.cancel, manifest_info=hint)
        self.assertEqual(result["bitrate_scope"], "rendition")
        self.assertIn("音轨", result["size_note"])

    def test_expired_access_is_typed_and_messages_never_contain_signed_url(self):
        for status in (401, 403, 410):
            self.routes["HEAD", "/video.mp4?secret=abc"] = status, {}, b""
            with self.assertRaises(self.media.MediaAccessError) as caught:
                self.estimate("/video.mp4?secret=abc")
            self.assertEqual(caught.exception.status_code, status)
            self.assertNotIn("secret", str(caught.exception))

    def test_redirects_consume_global_request_budget(self):
        self.routes["HEAD", "/video.mp4"] = 302, {"Location": "/again.mp4"}, b""
        self.routes["HEAD", "/again.mp4"] = 302, {"Location": "/video.mp4"}, b""
        result = self.estimate("/video.mp4", max_requests=3)
        self.assertIsNone(result["size_bytes"])
        self.assertEqual(len(self.requests), 3)

    def test_manifest_limit_and_cancel_stop_without_more_requests(self):
        self.playlist("/large.m3u8", "#EXTM3U\n" + "#" * (512 * 1024))
        result = self.estimate("/large.m3u8")
        self.assertIsNone(result["size_bytes"])
        self.cancel.set()
        with self.assertRaises(self.media.Cancelled):
            self.estimate("/video.mp4")
        self.assertEqual(len(self.requests), 1)

    def test_cookies_and_authorization_are_not_forwarded(self):
        for extra in ({"cookies": [{"name": "secret", "value": "x"}]}, {"headers": {"Authorization": "Bearer secret"}},
                      {"headers": {"Cookie": "secret=x"}}):
            source = {"url": self.base + "/video.mp4", **extra}
            result = self.module.estimate_size(source, {"duration": 30}, self.cancel)
            self.assertIsNone(result["size_bytes"])
        self.assertEqual(self.requests, [])

    def test_redirect_set_cookie_does_not_leak_to_later_request(self):
        self.routes["HEAD", "/video.mp4"] = 302, {"Location": self.base.replace("127.0.0.1", "localhost") + "/final.mp4",
                                                 "Set-Cookie": "secret=token; Path=/"}, b""
        self.routes["HEAD", "/final.mp4"] = 200, {"Content-Length": 3000}, b""
        self.assertEqual(self.estimate("/video.mp4")["size_bytes"], 3000)
        self.assertNotIn("Cookie", self.requests[-1][2])

    def test_invalid_content_ranges_are_not_accepted_as_whole_size(self):
        self.routes["HEAD", "/video.mp4"] = 405, {}, b""
        for value in ("bytes 0-0/*", "bytes 5-1/99", "bytes 0-9/2", "items 0-0/123", "bytes 0-0/0"):
            with self.subTest(value=value):
                self.routes["GET", "/video.mp4"] = 206, {"Content-Range": value, "Content-Length": 1}, b"x"
                self.assertIsNone(self.estimate("/video.mp4")["size_bytes"])

    def test_actual_manifest_limit_stops_missing_content_length(self):
        self.routes["GET", "/video.m3u8"] = 200, {"Content-Type": "application/vnd.apple.mpegurl"}, b"#EXTM3U\n" + b"#" * (600 * 1024)
        self.assertIsNone(self.estimate("/video.m3u8")["size_bytes"])

    def test_dripping_manifest_obeys_total_deadline_and_active_cancellation(self):
        def drip(handler):
            for char in b"#EXTM3U\n" + b"#" * 200:
                handler.wfile.write(bytes([char]))
                handler.wfile.flush()
                time.sleep(0.03)

        self.routes["GET", "/slow.m3u8"] = 200, {"Content-Type": "application/vnd.apple.mpegurl"}, drip
        started = time.monotonic()
        result = self.estimate("/slow.m3u8", timeout=0.15)
        self.assertIsNone(result["size_bytes"])
        self.assertLess(time.monotonic() - started, 0.8)
        timer = threading.Timer(0.1, self.cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(self.media.Cancelled):
                self.estimate("/slow.m3u8")
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            timer.cancel()
            timer.join()

    def test_incomplete_byte_range_offset_is_not_counted_as_exact(self):
        self.playlist("/video.m3u8", "#EXTM3U\n#EXTINF:10,\n#EXT-X-BYTERANGE:2000\na.mp4\n#EXT-X-ENDLIST\n")
        self.assertEqual(self.estimate("/video.m3u8")["size_kind"], "unknown")

    def test_missing_segment_head_is_not_range_downloaded_and_single_sample_stays_unknown(self):
        self.playlist("/video.m3u8", "#EXTM3U\n#EXTINF:10,\na.ts\n#EXTINF:10,\nb.ts\n#EXTINF:10,\nc.ts\n#EXT-X-ENDLIST\n")
        self.routes["HEAD", "/a.ts"] = 200, {"Content-Length": 100000}, b""
        self.routes["HEAD", "/b.ts"] = 405, {}, b""
        result = self.estimate("/video.m3u8")
        self.assertIsNone(result["size_bytes"])
        self.assertEqual(result["sampled_segments"], 1)
        self.assertTrue(all(method == "HEAD" for method, path, _ in self.requests if path.endswith(".ts")))

    def test_master_cannot_combine_high_variant_size_with_unselected_probe(self):
        self.playlist("/master.m3u8", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=9999999,AVERAGE-BANDWIDTH=8000000,RESOLUTION=1920x1080\nhigh.m3u8\n")
        self.playlist("/high.m3u8", "#EXTM3U\n#EXTINF:30,\na.ts\n#EXT-X-ENDLIST\n")
        result = self.module.estimate_size(self.source("/master.m3u8"), {"duration": 30, "height": 720}, self.cancel)
        self.assertIsNone(result["size_bytes"])

    def test_byte_ranges_are_exact_even_when_master_declares_average(self):
        self.playlist("/master.m3u8", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=3000,AVERAGE-BANDWIDTH=2000,RESOLUTION=640x360\nhigh.m3u8\n")
        self.playlist("/high.m3u8", "#EXTM3U\n#EXTINF:30,\n#EXT-X-BYTERANGE:6000@0\nwhole.mp4\n#EXT-X-ENDLIST\n")
        selected, hint = self.module.select_variant(self.source("/master.m3u8"), self.cancel)
        result = self.module.estimate_size(selected, {"duration": 30}, self.cancel, manifest_info=hint)
        self.assertEqual(result["size_bytes"], 6000)
        self.assertEqual(result["size_kind"], "exact")

    def test_https_downgrade_removes_referrer_and_origin_for_selected_media(self):
        source = {"url": "https://example.test/master.m3u8", "headers": {"Referer": "https://example.test/play?secret=x", "Origin": "https://example.test"}}
        with self.module._Reader(source, self.cancel, 2, 2) as reader:
            headers = reader.headers_for("http://example.test/sub.m3u8")
        self.assertNotIn("Referer", headers)
        self.assertNotIn("Origin", headers)

    def test_dripping_response_headers_obey_total_deadline(self):
        self.routes["HEAD", "/slow.mp4"] = 200, {"_slow_headers": 0.025}, b""
        started = time.monotonic()
        result = self.estimate("/slow.mp4", timeout=0.15)
        self.assertIsNone(result["size_bytes"])
        self.assertLess(time.monotonic() - started, 0.65, "收到完整响应头前也必须遵守总预算")
        self.assertEqual(len(self.requests), 1, "超时后不能继续回退 Range")
        self.assertFalse(any(worker.name == "媒体大小预算守护" for worker in threading.enumerate()),
                         "估算退出后不能残留守护线程")

    def test_cancel_during_dripping_response_headers_aborts_active_socket(self):
        self.routes["HEAD", "/slow.mp4"] = 200, {"_slow_headers": 0.025}, b""
        timer = threading.Timer(0.1, self.cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(self.media.Cancelled):
                self.estimate("/slow.mp4")
            self.assertLess(time.monotonic() - started, 0.65)
            self.assertEqual(len(self.requests), 1)
            self.assertFalse(any(worker.name == "媒体大小预算守护" for worker in threading.enumerate()))
        finally:
            timer.cancel()
            timer.join()


if __name__ == "__main__":
    unittest.main()
