"""搜索和检测任务具有独立状态，不接触 Kazumi 正在播放的会话。"""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from . import quality
from .diagnostics import Diagnostics, redact
from .matching import same_title


def choose_episode(sources: list[dict]) -> float | None:
    by_site = {}
    for source in sources:
        numbers = by_site.setdefault(source['site'], set())
        for road in source['roads']:
            numbers.update(episode['number'] for episode in road['episodes'] if episode.get('number') is not None)
    counts = Counter(number for numbers in by_site.values() for number in numbers)
    return min(counts, key=lambda number: (-counts[number], number)) if counts else None


def error_message(error: Exception) -> str:
    # 不把视频签名地址、Cookie 或完整响应体暴露到长期日志。
    message = redact(str(error))
    return (message or type(error).__name__)[:240]


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.persist_lock = threading.Lock()
        self.started = time.monotonic()
        self.ended = None
        self.done = threading.Event()
        self.events = []
        self.recorder = None
        self.data = dict(id=self.id, kind=kind, status='running', message='正在准备', progress=0, results=[], errors=[],
                         started_at=datetime.now(timezone.utc).isoformat())
        self.rules = []

    def update(self, **values):
        with self.lock:
            self.data.update(copy.deepcopy(values))
            if values.get('status') in ('completed','failed','cancelled') and self.ended is None:
                self.ended = time.monotonic()
                self.data['finished_at'] = datetime.now(timezone.utc).isoformat()

    @contextmanager
    def stage(self, name, *, site='', road='', row=None):
        started = time.monotonic()
        status, message = 'completed', ''
        try:
            yield
        except Exception as error:
            status = 'cancelled' if self.cancel.is_set() else 'failed'
            message = error_message(error)
            raise
        finally:
            seconds = round(time.monotonic()-started, 3)
            if self.cancel.is_set():
                status='cancelled'
            with self.lock:
                self.events.append(dict(stage=name, site=site, road=road, seconds=seconds,
                                        at_seconds=round(started-self.started,3), status=status, message=message))
                if row is not None:
                    timings = row.setdefault('timings', {})
                    timings[name] = round(timings.get(name,0)+seconds,3)
            self.persist()

    def persist(self):
        if self.recorder is not None:
            try:
                with self.persist_lock:
                    with self.lock:
                        snapshot, events = self.snapshot(), copy.deepcopy(self.events)
                    self.recorder.save(snapshot, events)
            except OSError:
                self.update(diagnostic_error='本地诊断记录写入失败，请检查剩余空间或目录权限')

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.data)
            result['elapsed_seconds'] = round((self.ended or time.monotonic())-self.started, 1)
        for row in result['results']:
            row.pop('scores', None)
            if row.get('stage_started_seconds') is not None:
                row['stage_elapsed_seconds']=round(result['elapsed_seconds']-row['stage_started_seconds'],1)
        return result

    def finish(self):
        if self.cancel.is_set():
            if self.data['kind']=='analyze':
                with self.lock:
                    rows = copy.deepcopy(self.data['results'])
                for row in rows:
                    if not row.get('scores'):
                        row.update(status='已取消',message='本次检测已取消')
                rows = quality.rank_common(rows)
                for row in rows:
                    if row.get('rank'):
                        row['status']='已完成'
                self.update(results=rows)
            self.update(status='cancelled', message='已取消，已完成的结果保留')
        else:
            message='搜索完成，请确认同一部番剧的来源'
            if self.data['kind']=='analyze':
                message='检测完成' if any(row.get('rank') for row in self.data['results']) else '检测结束，未形成跨站排名，请查看各线路原因'
            self.update(status='completed', progress=100, message=message)


class JobManager:
    def __init__(self, data_dir: Path):
        self.jobs = {}
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self.diagnostics = Diagnostics(data_dir)

    def wait(self, job_id, timeout=None):
        """供命令行诊断等待后台任务完成，超时不终止用户任务。"""
        job = self.jobs[job_id]
        if not job.done.wait(timeout):
            raise TimeoutError('等待任务完成超时')
        return job.snapshot()

    def current(self):
        with self.lock:
            active = [job for job in self.jobs.values() if job.snapshot()['status']=='running']
            return active[0].snapshot() if active else None

    def busy(self):
        with self.lock:
            return any(job.snapshot()['status'] == 'running' for job in self.jobs.values())

    def launch(self, kind, target, *args):
        with self.lock:
            if self.busy():
                raise ValueError('已有任务正在运行，请等待完成或先取消')
            job = Job(kind)
            job.recorder = self.diagnostics
            if len(self.jobs) >= 20:
                del self.jobs[next(iter(self.jobs))]
            self.jobs[job.id] = job
        def run():
            try:
                target(job, *args)
                job.finish()
            except Exception as error:
                if job.cancel.is_set():
                    job.finish()
                else:
                    job.update(status='failed', message=error_message(error))
            finally:
                try:
                    job.persist()
                finally:
                    job.done.set()
        threading.Thread(target=run, name=f'quality-{kind}', daemon=True).start()
        return job

    def search(self, rules, keyword, rule_ids):
        return self.launch('search', self._search, copy.deepcopy(rules), keyword, rule_ids)

    def _search(self, job, all_rules, keyword, ids):
        from . import rules
        job.rules = all_rules
        results, errors = [], []
        selected_rules = [(index, all_rules[index]) for index in ids]
        with ThreadPoolExecutor(max_workers=3) as pool:
            def search_one(rule):
                if job.cancel.is_set():
                    return []
                with job.stage('搜索', site=rule.get('name','未命名网站')):
                    return rules.search(rule, keyword, 18)
            pending = {pool.submit(search_one, rule): (index, rule) for index, rule in selected_rules}
            for completed, future in enumerate(as_completed(pending), 1):
                if job.cancel.is_set():
                    for waiting in pending:
                        waiting.cancel()
                    break
                index, rule = pending[future]
                site = rule.get('name', f'规则 {index + 1}')
                try:
                    matches = future.result()
                    exact_count = sum(same_title(item['title'], keyword) for item in matches)
                    for item in sorted(matches, key=lambda item: not same_title(item['title'],keyword)):
                        results.append(dict(id=uuid.uuid4().hex, rule_id=index, site=site,
                                            title=item['title'], url=item['url'],
                                            selected=exact_count==1 and same_title(item['title'], keyword)))
                    if not matches:
                        errors.append(dict(site=site, message='没有找到匹配条目'))
                except Exception as error:
                    errors.append(dict(site=site, message=error_message(error)))
                job.update(results=results, errors=errors, progress=round(completed / len(pending)*100), message=f'已搜索 {completed}/{len(pending)} 个网站')

    def analyze(self, search_job, candidate_ids, episode, mode='fast'):
        candidates = [row for row in search_job.snapshot()['results'] if row['id'] in candidate_ids]
        if len({row['site'] for row in candidates}) != len(candidates):
            raise ValueError('每个网站只能选择同一部番剧的一个条目')
        if len({row['site'] for row in candidates}) < 2:
            raise ValueError('请至少选择两个网站中同一部番剧的来源')
        if mode not in ('fast','full'):
            raise ValueError('请选择快速或完整检测模式')
        job = self.launch('analyze', self._analyze, search_job.rules, candidates, episode, mode)
        job.update(search_job_id=search_job.id, selected_candidate_ids=[row['id'] for row in candidates])
        return job

    def _analyze(self, job, all_rules, candidates, requested_episode, mode='fast'):
        from .pipeline import AnalysisPipeline
        AnalysisPipeline(self, job, all_rules, candidates, requested_episode, mode).run()

    @staticmethod
    def _reference_frames(media, resolved, metadata, directory, cancel, positions=4):
        refs = {}
        for key, fraction in enumerate((.22, .42, .62, .80)):
            start = metadata['duration'] * fraction
            frames = media.capture_frames(resolved, [start, start+.5, start+1], directory/f'ref-{key}', cancel)
            if len(frames) == 3:
                with Image.open(frames[0]['path']) as image:
                    if quality.usable_frame(image):
                        refs[key] = frames
                        if len(refs) >= positions:
                            break
        return refs

    @staticmethod
    def _align_frames(media, resolved, metadata, reference, directory, cancel):
        aligned = {}
        offset = 0.
        for key, refs in reference['frames'].items():
            base = refs[0]['time']
            estimated = max(0., min(metadata['duration']-2, base+offset))
            frames = media.capture_frames(resolved, [estimated,estimated+.5,estimated+1], directory/f'direct-{key}', cancel)
            if not JobManager._sequence_matches(refs, frames):
                # 在局部窗口中找同一场景，逐段更新偏移；不把时长比例当内容对齐。
                start = max(0., estimated-40)
                duration = min(82., metadata['duration']-start-1)
                coarse = media.capture_sequence(resolved, start, duration, 1., directory/f'coarse-{key}', cancel, width=192)
                match = quality.best_match(refs[0]['path'], coarse)
                if match is None:
                    continue
                fine_start = max(0., match['time']-.75)
                fine = media.capture_sequence(resolved, fine_start, 1.75, .125, directory/f'fine-{key}', cancel, width=192)
                match = quality.best_match(refs[0]['path'], fine)
                if match is None:
                    continue
                found = match['time']
                frames = media.capture_frames(resolved, [found,found+.5,found+1], directory/f'aligned-{key}', cancel)
                if not JobManager._sequence_matches(refs, frames):
                    continue
            offset = frames[0]['time'] - base
            aligned[key] = frames
        return aligned

    @staticmethod
    def _sequence_matches(refs, frames):
        if len(refs) != 3 or len(frames) != 3:
            return False
        distances = []
        for ref, frame in zip(refs, frames):
            with Image.open(ref['path']) as a, Image.open(frame['path']) as b:
                if not quality.usable_frame(a) or not quality.usable_frame(b):
                    return False
                distances.append(quality.fingerprint_distance(a, b))
        return max(distances) <= .12 and sum(distances)/3 <= .08
