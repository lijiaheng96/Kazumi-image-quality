"""有界读取播放清单和响应头，估算影片体积；不读取视频分片正文。"""

from __future__ import annotations

import math
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from helper import media as media_tools


MAX_MANIFEST_BYTES = 512 * 1024
_REDIRECTS = (301, 302, 303, 307, 308)
_HLS_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl")


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(value):
    text = str(value).strip()
    return int(text) if re.fullmatch(r"[0-9]{1,18}", text) and int(text) > 0 else None


def _unknown(note="源站未提供可用大小，保留为未知", **extra):
    return {"size_bytes": None, "size_kind": "unknown", "average_bitrate": None,
            "bitrate_scope": "unknown", "size_source": "unknown", "size_confidence": "unknown",
            "sampled_segments": 0, "size_note": note, "peak_bitrate": None, **extra}


def _result(size, duration, source, *, exact=False, scope="container", note="", samples=0, peak=None):
    return {"size_bytes": int(round(size)), "size_kind": "exact" if exact else "estimated",
            "average_bitrate": round(size * 8 / float(duration)) if _positive(duration) else None,
            "bitrate_scope": scope, "size_source": source, "size_confidence": "high" if exact else "medium",
            "sampled_segments": samples, "size_note": note, "peak_bitrate": peak}


def _headers(source):
    headers = media_tools._clean_headers(source.get("headers") or {})
    if headers.get("Cookie") or headers.get("Authorization") or source.get("cookies"):
        raise ValueError("大小估算暂不支持 Cookie 或 Authorization 鉴权")
    headers["Accept-Encoding"] = "identity"
    # requests 的 netrc 会隐式添加鉴权；本模块只允许显式传入的公开媒体头。
    return headers


def _is_hls(source):
    return (urlparse(str(source.get("url", ""))).path.lower().endswith(".m3u8")
            or str(source.get("content_type", "")).split(";", 1)[0].lower() in _HLS_TYPES)


def _budget_adapter(track_socket):
    """只替换本会话连接类，让总预算从接收响应头之前就能关闭实际连接。"""
    class BudgetHTTPConnection(HTTPConnection):
        def connect(self):
            super().connect()
            track_socket(self.sock)

    class BudgetHTTPSConnection(HTTPSConnection):
        def connect(self):
            super().connect()
            track_socket(self.sock)

    class BudgetHTTPPool(HTTPConnectionPool):
        ConnectionCls = BudgetHTTPConnection

    class BudgetHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = BudgetHTTPSConnection

    class BudgetAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
            super().init_poolmanager(connections, maxsize, block=block, **kwargs)
            # 新字典只属于本 PoolManager，不修改 urllib3 的全局连接池表。
            self.poolmanager.pool_classes_by_scheme = {"http": BudgetHTTPPool, "https": BudgetHTTPSPool}

    return BudgetAdapter(max_retries=0)


class _Reader:
    def __init__(self, source, cancel, timeout, max_requests):
        if not _positive(timeout) or timeout > 30 or not isinstance(max_requests, int) or not 1 <= max_requests <= 20:
            raise ValueError("大小估算预算无效")
        self.cancel = cancel
        self.deadline = time.monotonic() + timeout
        self.remaining = max_requests
        self.headers = _headers(source)
        self.initial_scheme = urlparse(str(source.get("url", ""))).scheme
        self.session = requests.Session()
        self.session.trust_env = False
        self._sockets = []
        self._socket_lock = threading.Lock()
        self._watch_stop = threading.Event()
        self._watcher = None
        adapter = _budget_adapter(self._track_socket)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def __enter__(self):
        self._watcher = threading.Thread(target=self._guard, name="媒体大小预算守护", daemon=True)
        self._watcher.start()
        return self

    def __exit__(self, *args):
        self._watch_stop.set()
        if self._watcher is not None:
            self._watcher.join()
        self.session.close()
        with self._socket_lock:
            self._sockets.clear()

    @staticmethod
    def _abort(connection_socket):
        try:
            connection_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _track_socket(self, connection_socket):
        with self._socket_lock:
            self._sockets.append(connection_socket)
        # 处理截止恰好发生在连接建立期间的竞争，不能等已退出的守护线程。
        try:
            self.check()
        except BaseException:
            self._abort(connection_socket)
            raise

    def _guard(self):
        while not self._watch_stop.wait(0.02):
            if self.cancel.is_set() or time.monotonic() >= self.deadline:
                with self._socket_lock:
                    active = list(self._sockets)
                for connection_socket in active:
                    self._abort(connection_socket)
                return

    def check(self):
        media_tools._check_cancel(self.cancel)
        if time.monotonic() >= self.deadline:
            raise TimeoutError("大小估算已到时间上限")

    def headers_for(self, url):
        headers = dict(self.headers)
        if self.initial_scheme == "https" and urlparse(url).scheme == "http":
            headers = {key: value for key, value in headers.items() if key.lower() not in ("referer", "origin")}
        return headers

    def request(self, url, method, *, body=False, extra_headers=None):
        headers = {**self.headers_for(url), **(extra_headers or {})}
        for redirect in range(4):
            self.check()
            if not media_tools._http_url(url):
                raise ValueError("大小估算仅支持 HTTP 或 HTTPS")
            if self.remaining <= 0:
                raise ValueError("大小估算已到请求次数上限")
            self.remaining -= 1
            remaining_time = self.deadline - time.monotonic()
            # 不保存源站 Set-Cookie，避免下一清单或重定向请求隐式携带凭据。
            self.session.cookies.clear()
            with self.session.request(method, url, headers=headers, stream=True, allow_redirects=False,
                                      timeout=(min(2, remaining_time), min(3, remaining_time))) as response:
                self.check()
                status = response.status_code
                if status in (401, 403, 410):
                    raise media_tools.MediaAccessError(status)
                if status in _REDIRECTS and response.headers.get("Location"):
                    if redirect == 3:
                        raise ValueError("大小估算重定向次数过多")
                    next_url = urljoin(url, response.headers["Location"])
                    old, new = urlparse(url), urlparse(next_url)
                    if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
                        headers = {key: value for key, value in headers.items() if key.lower() not in ("cookie", "authorization")}
                    if old.scheme == "https" and new.scheme == "http":
                        headers = {key: value for key, value in headers.items() if key.lower() not in ("referer", "origin")}
                    url = next_url
                    continue
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                raw = self.read_manifest(response) if body and 200 <= status < 300 else None
                return status, response_headers, raw, url
        raise ValueError("没有取得媒体响应头")

    def read_manifest(self, response):
        declared = _integer(response.headers.get("Content-Length"))
        if declared and declared > MAX_MANIFEST_BYTES:
            raise ValueError("播放清单超过 512 KiB 限制")
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type.startswith(("video/", "audio/")) and content_type.split(";", 1)[0] not in _HLS_TYPES:
            raise ValueError("该响应是媒体正文，不能当播放清单读取")
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=16384):
            self.check()
            if len(raw) + len(chunk) > MAX_MANIFEST_BYTES:
                raise ValueError("播放清单超过 512 KiB 限制")
            raw.extend(chunk)
        self.check()
        text = bytes(raw).decode("utf-8-sig")
        if not text.lstrip().startswith("#EXTM3U"):
            raise ValueError("响应不是有效的 HLS 播放清单")
        return text

    def playlist(self, url):
        status, _, text, final_url = self.request(url, "GET", body=True)
        if status != 200 or text is None:
            raise ValueError("未取得可用播放清单")
        return text, final_url

    def size(self, url, *, range_fallback=True):
        status, headers, _, final_url = self.request(url, "HEAD")
        size = self.header_size(status, headers)
        if size:
            return size, "content_range" if status == 206 else "content_length"
        if range_fallback and status in (200, 204, 405, 501):
            status, headers, _, _ = self.request(final_url, "GET", extra_headers={"Range": "bytes=0-0"})
            size = self.header_size(status, headers)
            if size:
                return size, "content_range" if status == 206 else "content_length"
        return None, "unknown"

    @staticmethod
    def header_size(status, headers):
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type.startswith("text/") or content_type in _HLS_TYPES or content_type in ("application/json", "application/dash+xml"):
            return None
        if headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            return None
        if status == 206:
            matched = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", headers.get("content-range", "").strip(), re.I)
            if matched:
                start, end, total = map(int, matched.groups())
                if 0 <= start <= end < total:
                    return total
            return None
        return _integer(headers.get("content-length")) if status == 200 else None


def _attributes(text):
    return {match[1]: match[2][1:-1] if match[2].startswith('"') else match[2]
            for match in re.finditer(r'([A-Z0-9-]+)=("[^"\r\n]*"|[^,\r\n]+)(?:,|$)', text)}


def _variants(text):
    variants, pending, audio_groups = [], None, set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            attributes = _attributes(line.split(":", 1)[1])
            if attributes.get("TYPE") == "AUDIO" and attributes.get("URI"):
                audio_groups.add(attributes.get("GROUP-ID"))
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attributes(line.split(":", 1)[1])
        elif line and not line.startswith("#") and pending is not None:
            codecs = pending.get("CODECS", "")
            if pending.get("RESOLUTION") or not codecs or re.search(r"(?:avc|hev|hvc|av01|vp09|vp9)", codecs, re.I):
                variants.append((line, pending))
            pending = None
    return variants, audio_groups


def _variant_key(variant):
    attributes = variant[1]
    size = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", attributes.get("RESOLUTION", ""))
    width, height = map(int, size.groups()) if size else (0, 0)
    return width * height, height, _positive(attributes.get("AVERAGE-BANDWIDTH")) or _positive(attributes.get("BANDWIDTH")) or 0


def _select(reader, source, text=None, url=None):
    current = url or source["url"]
    attributes, external_audio = {}, False
    for depth in range(3):
        if text is None:
            text, current = reader.playlist(current)
        variants, audio_groups = _variants(text)
        if not variants:
            return {**source, "url": current, "headers": reader.headers_for(current)}, {"url": current, "text": text, "attributes": attributes,
                                                "external_audio": external_audio}
        if depth == 2:
            raise ValueError("播放清单嵌套层数超过限制")
        path, attributes = max(variants, key=_variant_key)
        external_audio = external_audio or attributes.get("AUDIO") in audio_groups
        current, text = urljoin(current, path), None
    raise ValueError("未取得媒体子清单")


def select_variant(resolved, cancel, timeout=6, max_requests=5):
    """主清单先选择最高分辨率版本；非 HLS 来源不产生网络请求。"""
    media_tools._check_cancel(cancel)
    if not _is_hls(resolved):
        return resolved, {}
    try:
        with _Reader(resolved, cancel, timeout, max_requests) as reader:
            return _select(reader, resolved)
    except (media_tools.Cancelled, media_tools.MediaAccessError):
        raise
    except (requests.RequestException, ValueError, TimeoutError, UnicodeError, OSError):
        media_tools._check_cancel(cancel)
        return resolved, {"selection_note": "未能读取清单版本，使用媒体实际探测规格"}


def _segments(text, base):
    segments, maps = [], []
    duration, byte_range, current_map, previous_range = None, None, None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = _positive(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            byte_range = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MAP:"):
            attributes = _attributes(line.split(":", 1)[1])
            if not attributes.get("URI"):
                raise ValueError("播放清单初始化段缺少地址")
            current_map = (urljoin(base, attributes["URI"]), attributes.get("BYTERANGE"))
            if current_map not in maps:
                maps.append(current_map)
        elif line and not line.startswith("#"):
            if duration is None:
                raise ValueError("播放清单分片缺少有效时长")
            url, size = urljoin(base, line), None
            if byte_range is not None:
                match = re.fullmatch(r"([1-9]\d*)(?:@(\d+))?", byte_range)
                if not match:
                    raise ValueError("播放清单字节范围无效")
                size = int(match[1])
                if match[2] is None and (previous_range is None or previous_range[0] != url):
                    raise ValueError("播放清单字节范围缺少起始位置")
                offset = int(match[2]) if match[2] is not None else previous_range[1]
                previous_range = (url, offset + size)
            else:
                previous_range = None
            segments.append({"url": url, "duration": duration, "size": size})
            duration, byte_range = None, None
    if not segments or len(segments) > 10000:
        raise ValueError("播放清单分片数量无效")
    return segments, maps


def _hls_estimate(reader, selected, hint):
    text, url = hint["text"], hint["url"]
    attributes = hint.get("attributes", {})
    peak = _integer(attributes.get("BANDWIDTH"))
    if "#EXT-X-ENDLIST" not in text and not re.search(r"^#EXT-X-PLAYLIST-TYPE:VOD\s*$", text, re.M):
        return _unknown("直播或未完成清单，无法估算整集大小", peak_bitrate=peak)
    if "#EXT-X-I-FRAMES-ONLY" in text:
        return _unknown("仅关键帧预览清单，不能代表正片大小", peak_bitrate=peak)
    segments, maps = _segments(text, url)
    duration = sum(item["duration"] for item in segments)
    external_audio = hint.get("external_audio", False)
    average = _integer(attributes.get("AVERAGE-BANDWIDTH"))
    complete_ranges = all(item["size"] is not None for item in segments)
    if average and (not complete_ranges or any(not item[1] for item in maps)):
        return _result(average * duration / 8, duration, "hls_average", scope="rendition",
                       note="按清单声明的平均码率估算，含对应音轨组合，非纯视频码率" if external_audio else "按清单声明的音视频平均码率估算，非纯视频码率", peak=peak)
    map_bytes = 0
    for map_url, byte_range in maps:
        if byte_range:
            matched = re.fullmatch(r"([1-9]\d*)(?:@\d+)?", byte_range)
            if not matched:
                raise ValueError("初始化段字节范围无效")
            map_bytes += int(matched[1])
        else:
            size, _ = reader.size(map_url, range_fallback=False)
            if size is None:
                return _unknown("初始化段大小未知，无法可靠合计", peak_bitrate=peak)
            map_bytes += size
    note = "当前视频清单分片与初始化段体积；不含外部音轨" if external_audio else "媒体分片与初始化段体积，含封装开销，非纯视频码率"
    if complete_ranges:
        size = sum(item["size"] for item in segments) + map_bytes
        return _result(size, duration, "hls_byterange", exact=True, note=note, peak=peak)
    # 按时长分层，每层取中间分片；短清单直接覆盖全部，最多三个分片。
    if len(segments) <= 3:
        choices = segments
    else:
        choices = []
        for fraction in (0.2, 0.5, 0.8):
            elapsed = 0
            for segment in segments:
                elapsed += segment["duration"]
                if elapsed >= duration * fraction:
                    if segment not in choices:
                        choices.append(segment)
                    break
    samples = []
    for segment in choices:
        size = segment["size"]
        if size is None:
            size, _ = reader.size(segment["url"], range_fallback=False)
        if size:
            samples.append((size, segment["duration"]))
    if len(samples) < 2:
        return _unknown("不足两个分片提供大小，无法可靠估算", sampled_segments=len(samples), peak_bitrate=peak)
    size = sum(item[0] for item in samples) / sum(item[1] for item in samples) * duration + map_bytes
    return _result(size, duration, "segment_heads", note="按少量分片响应头估算；" + note,
                   samples=len(samples), peak=peak)


def estimate_size(selected_media, metadata, cancel, manifest_info=None, timeout=8, max_requests=8):
    """失败保留未知；取消或媒体地址过期交给调用方处理，不返回带签名的地址。"""
    media_tools._check_cancel(cancel)
    try:
        with _Reader(selected_media, cancel, timeout, max_requests) as reader:
            hint = manifest_info or {}
            if hint.get("url") == selected_media.get("url") and hint.get("text"):
                return _hls_estimate(reader, selected_media, hint)
            if _is_hls(selected_media):
                selected, hint = _select(reader, selected_media)
                if selected["url"] != selected_media["url"]:
                    return _unknown("主清单尚未确定版本，请先选择版本并探测相同媒体")
                return _hls_estimate(reader, selected, hint)
            size, source = reader.size(selected_media["url"])
            if size:
                return _result(size, metadata.get("duration"), source, exact=True,
                               note="完整媒体文件体积，含音轨及封装开销，非纯视频码率")
            return _unknown()
    except (media_tools.Cancelled, media_tools.MediaAccessError):
        raise
    except (requests.RequestException, ValueError, TimeoutError, UnicodeError, OSError, KeyError):
        media_tools._check_cancel(cancel)
        return _unknown("大小估算未取得可靠数据或已到预算上限")
