"""用真实时间戳与独立回取验证媒体采样兼容性。"""

import http.server
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageChops

from helper import media


class SequenceTimestampTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="媒体时间戳_")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.cancel = threading.Event()

    def make_video(self, name, rate, filters=None, *, duration=4, size="320x180"):
        path = self.folder / name
        args = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={duration}"]
        if filters:
            args += ["-vf", filters, "-fps_mode", "vfr"]
        codec = "libx264" if path.suffix == ".mp4" else "ffv1"
        subprocess.run(args + ["-c:v", codec, str(path)], check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"url": str(path)}

    def assert_recapture_matches(self, source, frames):
        captured = media.capture_frames(source, [frame["time"] for frame in frames],
                                        self.folder / "按实际时间重新取帧", self.cancel)
        for sample, exact in zip(frames, captured):
            self.assertAlmostEqual(sample["time"], exact["actual_time"], places=5)
            with Image.open(sample["path"]) as expected, Image.open(exact["path"]) as actual:
                self.assertIsNone(ImageChops.difference(expected, actual).getbbox(),
                                  "候选帧公布的时间必须能取回同一画面")

    def test_fractional_start_sequence_reports_pts_and_recaptures_same_frames(self):
        source = self.make_video("24帧.mkv", 24)
        frames = media.capture_sequence(source, .031, .5, .125, self.folder / "序列", self.cancel)
        self.assertEqual([round(frame["time"], 3) for frame in frames], [.042, .167, .292, .417])
        self.assertEqual([frame["actual_time"] for frame in frames], [frame["time"] for frame in frames])
        self.assert_recapture_matches(source, frames)

    def test_vfr_sequence_keeps_real_frames_without_fps_duplicates(self):
        source = self.make_video("变帧率.mkv", 10,
                                 "select='eq(n,0)+eq(n,1)+eq(n,6)+eq(n,7)+eq(n,11)+eq(n,18)+eq(n,22)+eq(n,30)'")
        frames = media.capture_sequence(source, .03, 1.5, .25, self.folder / "变帧率序列", self.cancel)
        self.assertEqual([round(frame["time"], 3) for frame in frames], [.1, .6, 1.1])
        self.assert_recapture_matches(source, frames)

    def test_fractional_rate_sequence_recaptures_same_frames(self):
        source = self.make_video("非整数帧率.mp4", "24000/1001")
        frames = media.capture_sequence(source, .031, .5, .125, self.folder / "非整数序列", self.cancel)
        self.assertEqual(len(frames), 4)
        self.assert_recapture_matches(source, frames)

    def test_long_fractional_seek_recaptures_exact_pts_without_rounding_to_next_frame(self):
        source = self.make_video("较长非整数帧率.mp4", "24000/1001", duration=303, size="80x48")
        frames = media.capture_sequence(source, 301.4396, .5, 1 / 23.976,
                                        self.folder / "较长起点序列", self.cancel)
        self.assertGreaterEqual(len(frames), 10)
        self.assert_recapture_matches(source, frames)

    def test_hls_segment_without_keyframe_uses_preroll_for_precise_capture(self):
        path = self.folder / "分片未从关键帧开始.m3u8"
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=12", "-c:v", "libx264",
             "-preset", "ultrafast", "-g", "120", "-sc_threshold", "0", "-hls_time", "2",
             "-hls_flags", "split_by_time", "-hls_list_size", "0", str(path)],
            check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        source = {"url": str(path)}
        frames = media.capture_frames(source, [6.5, 7, 7.5], self.folder / "分片精确帧", self.cancel)
        for frame in frames:
            self.assertAlmostEqual(frame["actual_time"], frame["time"], places=3)
        sequence = media.capture_sequence(source, 6.5, 1.5, .5, self.folder / "分片序列", self.cancel)
        self.assertEqual([round(frame["time"], 3) for frame in sequence], [6.5, 7, 7.5])
        self.assert_recapture_matches(source, sequence)

    def test_http_access_failures_keep_status_without_exposing_signed_address(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(int(self.path.split("/")[1]))
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for status in (401, 403, 410):
                source = {"url": f"http://127.0.0.1:{server.server_port}/{status}/film.mp4?key=不应泄露的签名"}
                operations = [lambda: media.probe(source, self.cancel),
                              lambda: media.capture_frames(source, [0], self.folder / "拒绝单帧", self.cancel),
                              lambda: media.capture_sequence(source, 0, 1, .5, self.folder / "拒绝序列", self.cancel)]
                for operation in operations:
                    with self.subTest(status=status, operation=operations.index(operation)):
                        with self.assertRaisesRegex(ValueError, str(status)) as error:
                            operation()
                        self.assertEqual(error.exception.status_code, status)
                        self.assertIsInstance(error.exception, media.MediaAccessError)
                        self.assertNotIn("key", str(error.exception))
                        self.assertNotIn("不应泄露", str(error.exception))
            self.assertEqual(list(self.folder.rglob("*.png")), [])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
