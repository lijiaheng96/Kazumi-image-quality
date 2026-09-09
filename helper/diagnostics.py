"""只在本机保存有限字段的诊断记录，不记录规则、凭据与播放地址。"""
from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from pathlib import Path


_FIELDS = {'id', 'kind', 'status', 'message', 'progress', 'results', 'errors', 'site', 'road',
           'title', 'episode', 'score', 'rank', 'samples', 'best_road', 'resolution', 'codec',
           'bitrate', 'timings', 'elapsed_seconds', 'started_at', 'mode', 'stage', 'seconds',
           'at_seconds', 'events', 'job', 'finished_at', 'cached', 'diagnostic_error', 'refresh_count', 'alignment', 'position', 'method',
           'matched', 'reference_times', 'candidate_times', 'distances',
           'fps', 'duration', 'size_bytes', 'size_kind', 'size_source', 'size_confidence', 'size_note',
           'average_bitrate', 'bitrate_scope', 'peak_bitrate', 'sampled_segments', 'spec_bitrate',
           'spec_bitrate_kind', 'bits_per_frame', 'shortlist_rank', 'selection_status', 'selection_reason',
           'selection_summary', 'highest_resolution', 'shortlist_count', 'searched_rule_ids', 'backup_attempts'}


def redact(value):
    value = re.sub(r'https?://\S+', '[链接]', str(value), flags=re.I)
    return re.sub(r'\b(?:Cookie|Authorization|token|access_token|api[_-]?key|signature|password)\s*[:=]\s*[^\r\n]*',
                  '[凭据已隐藏]',value,flags=re.I)


def clean(value, timing=False):
    if isinstance(value, dict):
        return {str(key): clean(item, key == 'timings') for key, item in value.items()
                if str(key) in _FIELDS or timing}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, str):
        return redact(value)[:1000]
    return value


class Diagnostics:
    def __init__(self, data_dir: Path):
        self.directory = data_dir / 'diagnostics'
        self.lock = threading.Lock()

    def save(self, snapshot, events):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            # 文件名仅接受本工具生成的 UUID，防止路径穿越。
            job_id = snapshot['id']
            if not re.fullmatch(r'[a-f0-9]{32}', job_id):
                raise ValueError('诊断任务编号无效')
            target = self.directory / f'{job_id}.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(clean({'job':snapshot,'events':events}), ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)

    def reports(self, limit=100):
        with self.lock:
            paths = sorted(self.directory.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
            reports = []
            for path in paths:
                try:
                    reports.append(json.loads(path.read_text(encoding='utf-8')))
                except (ValueError, OSError):
                    continue
            return reports

    def latest(self):
        return next(iter(self.reports(1)), None)

    def summary(self):
        reports = self.reports()
        stages, failures = defaultdict(list), defaultdict(int)
        for report in reports:
            for event in report.get('events', []):
                stages[event['stage']].append(event['seconds'])
                if event.get('status') == 'failed':
                    failures[(event.get('site', ''), event['stage'])] += 1
        return {'jobs':len(reports), 'stages':[
            {'stage':stage, 'count':len(values), 'seconds':round(sum(values),2),
             'average_seconds':round(sum(values)/len(values),2)}
            for stage, values in sorted(stages.items(), key=lambda item: -sum(item[1]))],
            'failures':[{'site':site,'stage':stage,'count':count} for (site,stage),count in failures.items()],
            'note':'并发阶段累计耗时可能大于任务墙钟时间；仅汇总最近 100 份本地记录'}
