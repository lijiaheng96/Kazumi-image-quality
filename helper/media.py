"""独立 Edge 媒体解析与本地 FFmpeg 提帧；错误信息不泄露签名地址。"""

from __future__ import annotations

import math
import re
import subprocess
import threading
import time
import uuid
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg


class Cancelled(Exception):
    """用户已取消当前工作。"""


_MEDIA_SUFFIXES = (".m3u8", ".mp4", ".m4v", ".webm", ".mkv", ".mpd", ".mov", ".flv")
_MEDIA_TYPES = ("video/", "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml")
_AD_PATTERN = re.compile(r"(?:^|[./_?&=-])(ads?|advert\w*|preroll|doubleclick)(?:[./_?&=-]|$)", re.I)
# 转为方形像素并保留显示比例，不放大窄视频，最大显示宽度为 1280。
_SCALE = "scale=w='min(1280,iw*sar)':h='max(1,round(ih*min(1,1280/(iw*sar))))',setsar=1"


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("任务已取消")


def _http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
    except (ValueError, TypeError):
        return False


def _direct_media(value: str) -> bool:
    return _http_url(value) and urlparse(value).path.lower().endswith(_MEDIA_SUFFIXES)


def _rule_headers(rule: dict) -> dict:
    headers = {}
    if isinstance(rule.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in rule["headers"].items() if v is not None})
    for target, keys in (("User-Agent", ("userAgent", "user_agent")), ("Referer", ("referer", "referrer"))):
        for key in keys:
            if isinstance(rule.get(key), str) and rule[key].strip():
                headers[target] = rule[key].strip()
                break
    return _clean_headers(headers)


def _clean_headers(headers: dict) -> dict:
    """只传递媒体鉴权所需的头，拒绝换行注入和连接控制头。"""
    allowed = {"user-agent": "User-Agent", "referer": "Referer", "origin": "Origin",
               "cookie": "Cookie", "authorization": "Authorization"}
    result = {}
    for key, value in headers.items():
        name = allowed.get(str(key).lower())
        if name and value is not None:
            value = str(value)
            if "\r" in value or "\n" in value or "\0" in value:
                raise ValueError("媒体请求头包含无效字符")
            result[name] = value
    return result


def _page_cookies(cookie_header: str, page_url: str) -> list[dict]:
    """规则原始 Cookie 仅授权给播放页主机，不设为浏览器全局请求头。"""
    if not cookie_header:
        return []
    parsed = urlparse(page_url)
    try:
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        if not cookies:
            raise ValueError("规则 Cookie 格式无效")
        return [{"name": key, "value": value.value, "url": f"{parsed.scheme}://{parsed.netloc}/"}
                for key, value in cookies.items()]
    except CookieError:
        raise ValueError("规则 Cookie 格式无效") from None


def resolve_media(page_url: str, rule: dict, cancel: threading.Event) -> dict:
    """从直链或播放页监听媒体；不保证解密、验证码及全部站点规则可用。"""
    _check_cancel(cancel)
    if not _http_url(page_url):
        raise ValueError("播放页地址必须为 HTTP 或 HTTPS 地址")
    initial_headers = _rule_headers(rule)
    if initial_headers.get("Authorization"):
        raise ValueError("首版暂不支持携带 Authorization 的媒体来源，无法安全处理跨域鉴权")
    page_cookies = _page_cookies(initial_headers.pop("Cookie", ""), page_url)
    if _direct_media(page_url):
        parsed = urlparse(page_url)
        cookies = [{"name": item["name"], "value": item["value"], "domain": parsed.hostname,
                    "path": "/", "secure": parsed.scheme == "https"} for item in page_cookies]
        return {"url": page_url, "headers": initial_headers, "cookies": cookies}

    from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

    candidates: dict[str, dict] = {}
    first_usable = None
    deadline = time.monotonic() + 35

    def collect(url, request_headers=None, resource_type="", content_type=""):
        nonlocal first_usable
        if not _http_url(url) or not (_direct_media(url) or any(content_type.startswith(t) for t in _MEDIA_TYPES)):
            return
        # 分片不能作为完整影片，交给完整清单处理。
        if urlparse(url).path.lower().endswith((".ts", ".m4s", ".aac", ".vtt")):
            return
        headers = dict(initial_headers)
        headers.update(_clean_headers(request_headers or {}))
        headers.pop("Cookie", None)
        headers.setdefault("Referer", page_url)
        path = urlparse(url).path.lower()
        score = 40 if path.endswith((".m3u8", ".mpd")) else 20
        if resource_type == "media":
            score += 15
        suspected_ad = bool(_AD_PATTERN.search(url))
        if suspected_ad:
            score -= 100
        previous = candidates.get(url)
        if previous is None or score >= previous["priority"]:
            candidates[url] = {"url": url, "headers": headers, "priority": score, "suspected_ad": suspected_ad}
        if not suspected_ad and first_usable is None:
            first_usable = time.monotonic()

    try:
        with sync_playwright() as runtime:
            _check_cancel(cancel)
            browser = runtime.chromium.launch(channel="msedge", headless=True, timeout=8000,
                                              args=["--autoplay-policy=no-user-gesture-required"])
            try:
                _check_cancel(cancel)
                options = {"extra_http_headers": {k: v for k, v in initial_headers.items() if k != "User-Agent"}}
                if initial_headers.get("User-Agent"):
                    options["user_agent"] = initial_headers["User-Agent"]
                context = browser.new_context(**options)
                if page_cookies:
                    context.add_cookies(page_cookies)
                page = context.new_page()

                def on_response(response):
                    content_type = response.headers.get("content-type", "").lower()
                    if 200 <= response.status < 300 and (_direct_media(response.url) or any(content_type.startswith(t) for t in _MEDIA_TYPES)):
                        request = response.request
                        collect(response.url, request.all_headers(), request.resource_type, content_type)

                # 监听整个隔离上下文，自动覆盖 iframe；收到成功响应才加入候选。
                context.on("response", on_response)
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=1200)
                except BrowserTimeout:
                    pass
                while time.monotonic() < deadline:
                    _check_cancel(cancel)
                    now = time.monotonic()
                    if first_usable is not None and now - first_usable >= 2.5:
                        break
                    page.wait_for_timeout(100)
                _check_cancel(cancel)
                if not candidates:
                    raise ValueError("未发现可用媒体地址；该站点可能需要交互或使用了暂不支持的解析规则")
                ordered = sorted(candidates.values(), key=lambda item: item["priority"], reverse=True)
                if ordered[0]["suspected_ad"]:
                    raise ValueError("仅发现疑似广告媒体，未识别到正片")
                for item in ordered:
                    # 只取该媒体地址所属域、路径可用的 Cookie，不读取用户浏览器会话。
                    item["cookies"] = context.cookies([item["url"]])
                primary = ordered[0]
                return {"url": primary["url"], "headers": primary["headers"], "cookies": primary["cookies"],
                        "candidates": ordered}
            finally:
                browser.close()
    except Cancelled:
        raise
    except BrowserError:
        _check_cancel(cancel)
        raise ValueError("独立 Edge 解析失败，请确认已安装 Edge，或尝试其他来源") from None


def _run_process(args: list[str], cancel: threading.Event, timeout: float = 45) -> subprocess.CompletedProcess:
    """同时排空输出管道；取消和超时时仅终止本次创建的进程。"""
    _check_cancel(cancel)
    start = time.monotonic()
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False)
    try:
        while True:
            _check_cancel(cancel)
            if time.monotonic() - start >= timeout:
                raise TimeoutError("媒体处理超时，请尝试其他来源")
            try:
                stdout, stderr = process.communicate(timeout=min(0.15, max(0.01, timeout - (time.monotonic() - start))))
                _check_cancel(cancel)
                return subprocess.CompletedProcess(args, process.returncode,
                                                   stdout.decode("utf-8", errors="replace"),
                                                   stderr.decode("utf-8", errors="replace"))
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=0.8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)


def _input_args(media: dict) -> list[str]:
    url = str(media.get("url", ""))
    if not url or "\0" in url:
        raise ValueError("媒体地址为空或无效")
    headers = _clean_headers(media.get("headers") or {})
    if headers.get("Authorization"):
        raise ValueError("首版暂不支持携带 Authorization 的媒体，无法安全处理跨域重定向和视频分片")
    if headers.get("Cookie") or media.get("cookies"):
        # FFmpeg 7.1 的 -cookies 未完整实现 Secure 和主机边界，不能替代浏览器 Cookie 策略。
        raise ValueError("首版暂不支持携带 Cookie 的媒体，无法安全处理跨域重定向和视频分片，请选择无 Cookie 来源")
    args = []
    if _http_url(url):
        args.extend(["-rw_timeout", "15000000"])
        if headers:
            args.extend(["-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())])
    elif not Path(url).is_file():
        raise ValueError("媒体地址无效或本地测试文件不存在")
    args.extend(["-i", url])
    return args


def _parse_metadata(output: str) -> dict:
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not duration_match:
        raise ValueError("无法获取有限视频时长，直播或该媒体暂不支持画质比较")
    hours, minutes, seconds = map(float, duration_match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("视频时长无效")
    video_line = next((line for line in output.splitlines() if "Video:" in line and "attached pic" not in line), None)
    if video_line is None:
        raise ValueError("媒体不包含可用视频流")
    dimensions = re.search(r"(?:^|[,\s])(\d{2,6})x(\d{2,6})(?=[,\s\[]|$)", video_line)
    codec = re.search(r"Video:\s*([\w-]+)", video_line)
    if dimensions is None or codec is None:
        raise ValueError("无法识别视频分辨率或编码")
    fps = re.search(r"(\d+(?:\.\d+)?)\s+fps\b", video_line)
    bitrate = re.search(r"(\d+(?:\.\d+)?)\s+kb/s\b", video_line)
    color_transfer = None
    triple = re.search(r"\b[\w-]+/[\w-]+/([\w-]+)\b", video_line)
    if triple:
        color_transfer = triple.group(1) if triple.group(1) not in ("unknown", "unspecified", "reserved") else None
    else:
        color = re.search(r"\b(bt709|smpte2084|arib-std-b67|gamma22|gamma28|smpte170m|bt2020-10|bt2020-12)\b", video_line)
        if color:
            color_transfer = color.group(1)
    return {"duration": duration, "width": int(dimensions.group(1)), "height": int(dimensions.group(2)),
            "codec": codec.group(1), "fps": float(fps.group(1)) if fps else None,
            "bitrate": int(float(bitrate.group(1)) * 1000) if bitrate else None, "color_transfer": color_transfer}


def probe(media: dict, cancel: threading.Event) -> dict:
    """使用已安装的 FFmpeg 探测输入；无输出文件时返回码 1 属于正常行为。"""
    _check_cancel(cancel)
    args = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin"] + _input_args(media)
    result = _run_process(args, cancel, timeout=35)
    return _parse_metadata(result.stderr)


def _finite_nonnegative(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def capture_frames(media: dict, times: list[float], directory: Path, cancel: threading.Event) -> list[dict]:
    """精确解码指定时间点，失败时不返回部分结果；输出路径归调用者管理。"""
    _check_cancel(cancel)
    if not all(_finite_nonnegative(value) for value in times):
        raise ValueError("取帧时间必须是有限的非负秒数")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:12]
    frames = []
    created = []
    try:
        for index, timestamp in enumerate(times):
            _check_cancel(cancel)
            path = directory / f"{prefix}_{index:04d}.png"
            created.append(path)
            args = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-threads", "2", "-ss", f"{timestamp:.6f}"] + _input_args(media)
            args += ["-map", "0:v:0", "-an", "-sn", "-vf", _SCALE, "-frames:v", "1", "-threads", "2", str(path)]
            result = _run_process(args, cancel, timeout=45)
            if result.returncode or not path.is_file() or path.stat().st_size == 0:
                raise ValueError("视频提帧失败，该时间点不可用或源站拒绝读取")
            frames.append({"time": float(timestamp), "path": str(path.resolve())})
        return frames
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def capture_sequence(media: dict, start: float, duration: float, step: float, directory: Path,
                     cancel: threading.Event) -> list[dict]:
    """一次解码最多 90 秒的粗匹配帧，最多 180 张；时间以请求采样网格标记。"""
    _check_cancel(cancel)
    if (not _finite_nonnegative(start) or not _finite_nonnegative(duration) or duration <= 0 or duration > 90
            or not _finite_nonnegative(step) or step <= 0 or math.ceil(duration / step) > 180):
        raise ValueError("批量取帧参数无效：时长需为 0 至 90 秒，步长为正且最多 180 帧")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:12]
    pattern = directory / f"{prefix}_%04d.png"
    args = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "2", "-ss", f"{start:.6f}"] + _input_args(media)
    args += ["-t", f"{duration:.6f}", "-map", "0:v:0", "-an", "-sn", "-vf", f"fps=1/{step:.8f}," + _SCALE,
             "-frames:v", str(math.ceil(duration / step)), "-threads", "2", str(pattern)]
    try:
        result = _run_process(args, cancel, timeout=120)
        paths = sorted(directory.glob(f"{prefix}_*.png"))
        if result.returncode or not paths:
            raise ValueError("批量提帧失败，视频可能无法定位或源站拒绝读取")
        return [{"time": float(start + index * step), "path": str(path.resolve())} for index, path in enumerate(paths)]
    except BaseException:
        for path in directory.glob(f"{prefix}_*.png"):
            path.unlink(missing_ok=True)
        raise
