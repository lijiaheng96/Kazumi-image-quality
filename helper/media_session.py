"""每条线路持有独立媒体会话，短效链接只在本次任务中更新。"""
from __future__ import annotations

import math
import time
from contextlib import nullcontext


class MediaSession:
    """兼容提帧接口；重解析后必须核对媒体版本，访问失败最多重试一次。"""

    def __init__(self, backend, page_url, rule, resolved, metadata, resolved_at, *,
                 stage=None, on_refresh=None):
        self.backend = backend
        self.page_url, self.rule = page_url, rule
        self.resolved = resolved
        self.metadata = dict(metadata)
        self.resolved_at = resolved_at
        self.stage = stage or nullcontext
        self.on_refresh = on_refresh
        self.refresh_count = 0

    def _check_cancel(self, cancel):
        if cancel.is_set():
            raise self.backend.Cancelled('任务已取消')

    def _same_version(self, metadata):
        if any(metadata.get(key) != self.metadata.get(key)
               for key in ('width', 'height', 'codec', 'color_transfer')):
            return False
        try:
            duration = float(metadata['duration'])
            original = float(self.metadata['duration'])
            aspect = float(metadata.get('display_aspect_ratio') or metadata['width'] / metadata['height'])
            original_aspect = float(self.metadata.get('display_aspect_ratio')
                                    or self.metadata['width'] / self.metadata['height'])
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            return False
        if not (math.isfinite(aspect) and math.isfinite(original_aspect) and aspect > 0 and original_aspect > 0
                and math.isclose(aspect, original_aspect, rel_tol=.001, abs_tol=.000001)):
            return False
        # 缺失或非有限值代表未知；只比较双方确知的规格，不用零替代未知码率。
        for key, tolerance in (('fps', .01), ('bitrate', .05)):
            before, after = self.metadata.get(key), metadata.get(key)
            if before is None or after is None:
                continue
            try:
                before, after = float(before), float(after)
            except (TypeError, ValueError, OverflowError):
                continue
            if not (math.isfinite(before) and math.isfinite(after) and before > 0 and after > 0):
                continue
            if not math.isclose(before, after, rel_tol=tolerance):
                return False
        # 容纳清单或容器时长的小数舍入；不把片头增删或不同集数当作同一媒体。
        return math.isfinite(duration) and math.isfinite(original) and abs(duration - original) <= .5

    def _refresh(self, cancel):
        self._check_cancel(cancel)
        with self.stage():
            self.refresh_count += 1
            if self.on_refresh is not None:
                self.on_refresh(self.refresh_count)
            resolved = self.backend.resolve_media(self.page_url, self.rule, cancel)
            resolved_at = time.monotonic()
            self._check_cancel(cancel)
            metadata = self.backend.probe(resolved, cancel)
            self._check_cancel(cancel)
            if not self._same_version(metadata):
                raise ValueError('刷新后的媒体版本或规格发生变化，已停止本线路以避免混排不同内容')
            self.resolved, self.resolved_at = resolved, resolved_at

    def _capture(self, method, cancel, *args, **kwargs):
        self._check_cancel(cancel)
        if time.monotonic() - self.resolved_at > 25:
            self._refresh(cancel)
        self._check_cancel(cancel)
        operation = getattr(self.backend, method)
        try:
            result = operation(self.resolved, *args, cancel, **kwargs)
        except self.backend.MediaAccessError as error:
            self._check_cancel(cancel)
            if error.status_code not in (401, 403, 410):
                raise
            self._refresh(cancel)
            self._check_cancel(cancel)
            try:
                result = operation(self.resolved, *args, cancel, **kwargs)
            except self.backend.MediaAccessError as retry_error:
                self._check_cancel(cancel)
                raise ValueError(f'链接刷新后源站仍拒绝访问（HTTP {retry_error.status_code}），已停止本线路') from None
        self._check_cancel(cancel)
        return result

    def capture_frames(self, resolved, times, directory, cancel):
        # 调用方仍持有原始解析字典；取样始终使用会话中最近核验过的地址。
        return self._capture('capture_frames', cancel, times, directory)

    def capture_sequence(self, resolved, start, duration, step, directory, cancel, *, width=1280):
        return self._capture('capture_sequence', cancel, start, duration, step, directory, width=width)
