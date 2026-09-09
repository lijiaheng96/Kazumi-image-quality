"""本地 HTTP 真实媒体抽帧基准；仅监听随机回环端口，不接触运行中的服务。"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg
from PIL import Image, ImageChops

from helper import media


def _old_capture(source, times, directory, cancel):
    """优化前的逐帧独立 seek，保留同样的解码与缩放参数作为对照。"""
    directory.mkdir(parents=True)
    frames = []
    for index, stamp in enumerate(times):
        path = directory / f"{index}.png"
        args = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-threads", "2", "-ss", f"{stamp:.6f}"] + media._input_args(source)
        args += ["-map", "0:v:0", "-an", "-sn", "-vf", media._SCALE,
                 "-frames:v", "1", "-threads", "2", str(path)]
        result = media._run_process(args, cancel, timeout=45)
        if result.returncode or not path.is_file():
            raise RuntimeError("基准逐帧解码失败")
        frames.append({"time": stamp, "path": str(path)})
    return frames


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("repeat 必须为 1 至 10")
    cancel = threading.Event()
    with tempfile.TemporaryDirectory(prefix="画质助手性能基准_") as temporary:
        folder = Path(temporary)
        video = folder / "基准视频.mp4"
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24000/1001:duration=88",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-threads", "2",
             "-g", "48", "-movflags", "+faststart", str(video)],
            check=True, capture_output=True, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_GET(self):
                size = video.stat().st_size
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                start = int(match.group(1)) if match else 0
                end = min(int(match.group(2)), size - 1) if match and match.group(2) else size - 1
                entry = {"range": [start, end], "bytes": 0, "finished": threading.Event()}
                requests.append(entry)
                self.send_response(206 if match else 200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                if match:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with video.open("rb") as stream:
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = stream.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            entry["bytes"] += len(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    entry["finished"].set()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            source = {"url": f"http://127.0.0.1:{server.server_port}/fixture.mp4"}
            times = [30.031, 30.531, 31.031]
            samples = {"逐帧独立读取": [], "相邻帧合并读取": [], "粗匹配1280": [], "粗匹配192": []}
            reference_frames = None
            pixels_match = True
            actual_times = []
            for iteration in range(args.repeat):
                for name in samples:
                    directory = folder / f"{iteration}_{name}"
                    begin = len(requests)
                    started = time.perf_counter()
                    with patch.object(media, "_run_process", wraps=media._run_process) as processes:
                        if name == "逐帧独立读取":
                            frames = _old_capture(source, times, directory, cancel)
                            reference_frames = frames
                        elif name == "相邻帧合并读取":
                            frames = media.capture_frames(source, times, directory, cancel)
                            actual_times = [frame["actual_time"] for frame in frames]
                        else:
                            width = 1280 if name == "粗匹配1280" else 192
                            frames = media.capture_sequence(source, 2.031, 82, 1, directory, cancel, width=width)
                    elapsed = time.perf_counter() - started
                    for request in requests[begin:]:
                        request["finished"].wait(timeout=2)
                    samples[name].append({"seconds": round(elapsed, 4), "processes": processes.call_count,
                                          "http_requests": len(requests) - begin,
                                          "http_bytes": sum(item["bytes"] for item in requests[begin:]),
                                          "frames": len(frames),
                                          "png_bytes": sum(Path(frame["path"]).stat().st_size for frame in frames)})
                    if name == "相邻帧合并读取":
                        for before, after in zip(reference_frames, frames):
                            with Image.open(before["path"]) as expected, Image.open(after["path"]) as actual:
                                pixels_match &= ImageChops.difference(expected, actual).getbbox() is None
                    print(json.dumps({"重复": iteration + 1, "模式": name, **samples[name][-1]}, ensure_ascii=False), flush=True)
            report = {"说明": "本机回环 HTTP、支持 Range、无人工网络延迟；结果不能直接代表公网源站速度",
                      "视频": {"分辨率": "1280x720", "帧率": "24000/1001", "时长秒": 88,
                               "文件字节": video.stat().st_size},
                      "请求采样秒": times, "实际采样秒": actual_times, "逐像素一致": pixels_match,
                      "样本": samples,
                      "耗时中位数秒": {name: round(statistics.median(row["seconds"] for row in rows), 4)
                                       for name, rows in samples.items()}}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if not pixels_match:
                raise RuntimeError("合并提帧与逐帧基准像素不一致")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    main()
