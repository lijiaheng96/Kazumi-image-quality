"""使用本机合成视频验证媒体工具，不访问公网媒体。"""

import http.server
import importlib.util
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageChops


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="画质助手媒体测试_")
        cls.folder = Path(cls.temporary.name)
        cls.video = cls.folder / "中文示例视频.mp4"
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=1536x864:rate=12:duration=4",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-y", str(cls.video)],
            check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("helper.media"), "尚未实现媒体模块")
        from helper import media
        self.media = media
        self.cancel = threading.Event()

    def test_direct_url_keeps_rule_headers_and_rejects_non_http(self):
        result = self.media.resolve_media("https://example.test/film.M3U8?token=x",
                                          {"userAgent": "测试代理", "referer": "https://origin.test/"}, self.cancel)
        self.assertEqual(result["url"], "https://example.test/film.M3U8?token=x")
        self.assertEqual(result["headers"]["User-Agent"], "测试代理")
        self.assertEqual(result["headers"]["Referer"], "https://origin.test/")
        with self.assertRaises(ValueError):
            self.media.resolve_media(str(self.video), {}, self.cancel)

    def test_metadata_does_not_misuse_audio_or_container_bitrate(self):
        output = """Duration: 00:02:03.45, start: 0.000000, bitrate: 4567 kb/s
  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 1920x1080, 23.976 fps, 24 tbr
  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s
"""
        result = self.media._parse_metadata(output)
        self.assertAlmostEqual(result["duration"], 123.45)
        self.assertEqual((result["width"], result["height"]), (1920, 1080))
        self.assertEqual(result["codec"], "hevc")
        self.assertEqual(result["color_transfer"], "smpte2084")
        self.assertIsNone(result["bitrate"])
        self.assertAlmostEqual(result["fps"], 23.976)

    def test_unknown_metadata_stays_none_and_live_duration_is_rejected(self):
        result = self.media._parse_metadata("Duration: 00:01:00.00\nStream #0:0: Video: h264, yuv420p, 640x360\n")
        for field in ("fps", "bitrate", "color_transfer"):
            self.assertIsNone(result[field])
        result = self.media._parse_metadata("Duration: 00:01:00.00\nStream #0:0: Video: h264, yuv420p, 640x360, 1200 kb/s, 25 fps\n")
        self.assertEqual(result["bitrate"], 1200000)
        with self.assertRaisesRegex(ValueError, "时长"):
            self.media._parse_metadata("Duration: N/A\nStream #0:0: Video: h264, yuv420p, 640x360, 25 fps\n")

    def test_unknown_color_transfer_is_not_reported_as_known(self):
        result = self.media._parse_metadata("Duration: 00:01:00.00\nStream #0:0: Video: hevc, yuv420p(tv, bt2020nc/bt2020/unknown), 640x360\n")
        self.assertIsNone(result["color_transfer"])

    def test_metadata_uses_display_aspect_ratio_for_anamorphic_video(self):
        cases = [
            ("720x480 [SAR 32:27 DAR 16:9]", 16 / 9),
            ("720x576 [SAR 16:15]", 4 / 3),
            ("720x480 [DAR 16:9]", 16 / 9),
            ("1920x1080", 16 / 9),
            ("720x480 [SAR 0:1 DAR 0:0]", 3 / 2),
        ]
        for video_format, expected in cases:
            with self.subTest(video_format=video_format):
                output = f"Duration: 00:02:00.00\nStream #0:0: Video: h264, yuv420p, {video_format}, 23.98 fps"
                result = self.media._parse_metadata(output)
                self.assertAlmostEqual(result.get("display_aspect_ratio", 0), expected)

    def test_probe_real_fixture(self):
        result = self.media.probe({"url": str(self.video), "headers": {}}, self.cancel)
        self.assertAlmostEqual(result["duration"], 4, places=1)
        self.assertEqual((result["width"], result["height"]), (1536, 864))
        self.assertEqual(result["codec"], "h264")
        self.assertEqual(result["fps"], 12)

    def test_frame_capture_preserves_aspect_ratio_in_chinese_directory(self):
        frames = self.media.capture_frames({"url": str(self.video)}, [0.5, 1.5], self.folder / "中文提帧", self.cancel)
        self.assertEqual([frame["time"] for frame in frames], [0.5, 1.5])
        with Image.open(frames[0]["path"]) as first, Image.open(frames[1]["path"]) as second:
            self.assertEqual(first.size, (1280, 720))
            self.assertEqual(first.format, "PNG")
            self.assertIsNotNone(ImageChops.difference(first, second).getbbox())

    def test_sequence_extracts_ordered_sample_times(self):
        frames = self.media.capture_sequence({"url": str(self.video)}, 0.25, 2, 0.5,
                                             self.folder / "批量中文提帧", self.cancel)
        self.assertEqual([frame["time"] for frame in frames], [0.25, 0.75, 1.25, 1.75])
        self.assertTrue(all(Path(frame["path"]).is_file() for frame in frames))

    def test_adjacent_frames_share_one_http_input(self):
        received = []
        video_bytes = self.video.read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(video_bytes)))
                self.end_headers()
                try:
                    self.wfile.write(video_bytes)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            frames = self.media.capture_frames(
                {"url": f"http://127.0.0.1:{server.server_port}/film.mp4"}, [0.5, 1, 1.5],
                self.folder / "合并HTTP读取", self.cancel)
            self.assertEqual(len(frames), 3)
            self.assertEqual(received, ["/film.mp4"], "相邻采样应共用一次媒体读取")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_frame_capture_reports_real_vfr_timestamps_and_preserves_order(self):
        video = self.folder / "变帧率.mkv"
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=4",
             "-vf", "select='eq(n,0)+eq(n,1)+eq(n,6)+eq(n,7)+eq(n,11)+eq(n,18)+eq(n,22)+eq(n,30)'",
             "-fps_mode", "vfr", "-c:v", "ffv1", str(video)],
            check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # 请求间隔为 0.5 秒；实际视频只有这些离散时间戳，不能用 fps 人工补帧。
        frames = self.media.capture_frames({"url": str(video)}, [1.03, 0.03, 0.53, 0.53],
                                          self.folder / "变帧率输出", self.cancel)
        self.assertEqual([frame["time"] for frame in frames], [1.03, 0.03, 0.53, 0.53])
        self.assertEqual([round(frame.get("actual_time", -1), 3) for frame in frames], [1.1, 0.1, 0.6, 0.6])
        for index, source_index in enumerate((4, 1, 2, 2)):
            expected = self.folder / f"变帧率基准_{index}.png"
            subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(video), "-vf", f"select='eq(n,{source_index})'", "-frames:v", "1", str(expected)],
                check=True, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with Image.open(frames[index]["path"]) as actual, Image.open(expected) as reference:
                self.assertEqual(actual.size, (320, 180), "评分帧不得放大低分辨率来源")
                self.assertIsNone(ImageChops.difference(actual, reference).getbbox())

    def test_sequence_fingerprint_width_is_optional_and_preserves_aspect_ratio(self):
        frames = self.media.capture_sequence({"url": str(self.video)}, 0.25, 2, 0.5,
                                             self.folder / "小尺寸指纹", self.cancel, width=192)
        self.assertEqual([frame["time"] for frame in frames], [0.25, 0.75, 1.25, 1.75])
        for frame in frames:
            with Image.open(frame["path"]) as image:
                self.assertEqual(image.size, (192, 108))

    def test_fractional_frame_rate_matches_independent_exact_seeks(self):
        video = self.folder / "非整数帧率.mp4"
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001:duration=4",
             "-c:v", "libx264", "-preset", "ultrafast", str(video)],
            check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        requested = [0.031, 0.531, 1.031]
        frames = self.media.capture_frames({"url": str(video)}, requested,
                                          self.folder / "非整数帧率输出", self.cancel)
        # 23.976 fps 的第 1、13、25 帧；相邻实际 PTS 间隔为 0.5005 秒。
        for index, expected_pts in enumerate((1001 / 24000, 13013 / 24000, 25025 / 24000)):
            self.assertAlmostEqual(frames[index].get("actual_time", -1), expected_pts, places=5)
            expected = self.folder / f"独立seek_{index}.png"
            subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", str(requested[index]), "-i", str(video), "-frames:v", "1", str(expected)],
                check=True, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with Image.open(frames[index]["path"]) as actual, Image.open(expected) as reference:
                self.assertIsNone(ImageChops.difference(actual, reference).getbbox())

    def test_sequence_rejects_invalid_width_before_creating_outputs(self):
        directory = self.folder / "无效指纹宽度"
        for width in (0, -1, 1281, 192.5, True, "192"):
            with self.subTest(width=width):
                with self.assertRaisesRegex(ValueError, "宽度"):
                    self.media.capture_sequence({"url": str(self.video)}, 0, 1, 0.5,
                                                directory, self.cancel, width=width)
        self.assertFalse(directory.exists())

    def test_incomplete_frame_batch_removes_all_outputs(self):
        directory = self.folder / "不完整批次"
        with self.assertRaisesRegex(ValueError, "提帧失败"):
            self.media.capture_frames({"url": str(self.video)}, [3.5, 4.5], directory, self.cancel)
        self.assertEqual(list(directory.glob("*.png")), [])

    def test_cancelled_and_invalid_requests_do_not_start_work(self):
        self.cancel.set()
        with self.assertRaises(self.media.Cancelled):
            self.media.resolve_media("https://example.test/a.mp4", {}, self.cancel)
        with self.assertRaises(self.media.Cancelled):
            self.media.probe({"url": str(self.video)}, self.cancel)
        with self.assertRaises(self.media.Cancelled):
            self.media.capture_frames({"url": str(self.video)}, [0], self.folder / "已取消", self.cancel)
        self.cancel.clear()
        with self.assertRaises(ValueError):
            self.media.capture_frames({"url": str(self.video)}, [-1], self.folder / "无效", self.cancel)
        with self.assertRaises(ValueError):
            self.media.capture_sequence({"url": str(self.video)}, 0, 900, 1, self.folder / "太长", self.cancel)

    def test_running_subprocess_cancellation_drains_large_stderr(self):
        timer = threading.Timer(0.3, self.cancel.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(self.media.Cancelled):
                self.media._run_process([sys.executable, "-c", "import sys,time; sys.stderr.write('x'*200000); sys.stderr.flush(); time.sleep(20)"],
                                        self.cancel, timeout=10)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - start, 3)

    def test_running_subprocess_has_finite_timeout(self):
        with self.assertRaisesRegex(TimeoutError, "超时"):
            self.media._run_process([sys.executable, "-c", "import time; time.sleep(10)"], self.cancel, timeout=0.2)

    def test_browser_resolves_iframe_and_prefers_film_over_advertisement(self):
        video_bytes = self.video.read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/watch":
                    body = b'<iframe src="/player"></iframe>'
                    content_type = "text/html"
                elif self.path == "/player":
                    body = b'<script>fetch("/ads/ad.mp4");fetch("/missing.mp4");</script><video autoplay muted src="/film.mp4"></video>'
                    content_type = "text/html"
                elif self.path == "/manifest":
                    body = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nfilm.ts\n#EXT-X-ENDLIST\n"
                    content_type = "application/vnd.apple.mpegurl"
                else:
                    body = video_bytes
                    content_type = "video/mp4"
                self.send_response(404 if self.path == "/missing.mp4" else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "session=fixture; Path=/; SameSite=Lax")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            result = self.media.resolve_media(base + "/watch", {"userAgent": "Kazumi-Quality-Fixture"}, self.cancel)
            self.assertEqual(result["url"], base + "/film.mp4")
            self.assertEqual(result["headers"]["User-Agent"], "Kazumi-Quality-Fixture")
            self.assertEqual(result["headers"]["Referer"], base + "/player")
            self.assertNotIn("Cookie", result["headers"])
            self.assertTrue(any(item["name"] == "session" and item["value"] == "fixture" for item in result["cookies"]))
            self.assertGreaterEqual(len(result["candidates"]), 2)
            self.assertNotIn(base + "/missing.mp4", [item["url"] for item in result["candidates"]])
            with self.assertRaisesRegex(ValueError, "Cookie"):
                self.media.probe(result, self.cancel)
            public_media = {"url": result["url"], "headers": result["headers"]}
            metadata = self.media.probe(public_media, self.cancel)
            self.assertEqual(metadata["width"], 1536)
            frames = self.media.capture_frames(public_media, [0.5], self.folder / "HTTP输入", self.cancel)
            with Image.open(frames[0]["path"]) as frame:
                self.assertEqual(frame.size, (1280, 720))
            manifest = self.media.resolve_media(base + "/manifest", {}, self.cancel)
            self.assertEqual(manifest["url"], base + "/manifest")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_authorization_sources_are_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, "Authorization"):
            self.media.resolve_media("http://127.0.0.1:1/watch", {"headers": {"Authorization": "Bearer synthetic"}}, self.cancel)
        with self.assertRaisesRegex(ValueError, "Authorization"):
            self.media.probe({"url": "http://127.0.0.1:1/film.mp4", "headers": {"Authorization": "Bearer synthetic"}}, self.cancel)

    def test_ffmpeg_rejects_credentials_before_cross_hostname_redirect(self):
        received = []
        video_bytes = self.video.read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                received.append((self.path, dict(self.headers)))
                if self.path == "/start.mp4":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/target.mp4")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(video_bytes)))
                self.end_headers()
                try:
                    self.wfile.write(video_bytes)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/start.mp4"
            for credentials in ({"headers": {"Cookie": "secret=synthetic"}},
                                {"headers": {"Authorization": "Bearer synthetic"}},
                                {"cookies": [{"name": "secret", "value": "synthetic", "domain": "127.0.0.1", "path": "/", "secure": True}]}):
                with self.subTest(credentials=list(credentials)):
                    with self.assertRaisesRegex(ValueError, "暂不支持"):
                        self.media.probe({"url": url, **credentials}, self.cancel)
            self.assertEqual(received, [])
            # 同一重定向在无鉴权输入下仍能正常读取媒体。
            result = self.media.probe({"url": url}, self.cancel)
            self.assertEqual(result["width"], 1536)
            self.assertTrue(any(path == "/target.mp4" for path, _ in received))
            self.assertFalse(any("Cookie" in headers or "Authorization" in headers for _, headers in received))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_browser_rule_cookie_is_scoped_to_page_hostname(self):
        received = []
        video_bytes = self.video.read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                received.append((self.path, self.headers.get("Cookie", "")))
                if self.path == "/watch":
                    body = f'<iframe src="http://localhost:{self.server.server_port}/foreign"></iframe>'.encode()
                    content_type = "text/html"
                elif self.path == "/foreign":
                    body = b'<video autoplay muted src="/film.mp4"></video>'
                    content_type = "text/html"
                else:
                    body = video_bytes
                    content_type = "video/mp4"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = self.media.resolve_media(f"http://127.0.0.1:{server.server_port}/watch",
                                             {"headers": {"Cookie": "secret=synthetic"}}, self.cancel)
            self.assertTrue(any(path == "/watch" and "secret=synthetic" in cookie for path, cookie in received))
            self.assertFalse(any(path in ("/foreign", "/film.mp4") and "secret=synthetic" in cookie for path, cookie in received))
            self.assertNotIn("Cookie", result["headers"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_browser_can_cancel_even_when_page_javascript_is_busy(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<script>while(true){}</script>")

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        timer = threading.Timer(2, self.cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(self.media.Cancelled):
                self.media.resolve_media(f"http://127.0.0.1:{server.server_port}/", {}, self.cancel)
            self.assertLess(time.monotonic() - started, 6)
        finally:
            timer.cancel()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
