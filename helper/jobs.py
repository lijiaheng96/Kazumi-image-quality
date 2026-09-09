"""搜索和检测任务具有独立状态，不接触 Kazumi 正在播放的会话。"""
from __future__ import annotations

import copy
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from . import quality


def same_title(a: str, b: str) -> bool:
    def clean(value):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()
    return clean(a) == clean(b)


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
    message = re.sub(r'https?://\S+', '[链接]', str(error))
    return (message or type(error).__name__)[:240]


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.data = dict(id=self.id, kind=kind, status='running', message='正在准备', progress=0, results=[], errors=[])
        self.rules = []

    def update(self, **values):
        with self.lock:
            self.data.update(copy.deepcopy(values))

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.data)
        for row in result['results']:
            row.pop('scores', None)
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
            self.update(status='completed', progress=100, message='检测完成' if self.data['kind']=='analyze' else '搜索完成，请确认同一部番剧的来源')


class JobManager:
    def __init__(self, data_dir: Path):
        self.jobs = {}
        self.lock = threading.RLock()
        self.data_dir = data_dir

    def busy(self):
        with self.lock:
            return any(job.snapshot()['status'] == 'running' for job in self.jobs.values())

    def launch(self, kind, target, *args):
        with self.lock:
            if self.busy():
                raise ValueError('已有任务正在运行，请等待完成或先取消')
            job = Job(kind)
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
            pending = {pool.submit(rules.search, rule, keyword, 18): (index, rule) for index, rule in selected_rules}
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
                    for item in matches:
                        results.append(dict(id=uuid.uuid4().hex, rule_id=index, site=site,
                                            title=item['title'], url=item['url'],
                                            selected=exact_count==1 and same_title(item['title'], keyword)))
                    if not matches:
                        errors.append(dict(site=site, message='没有找到匹配条目'))
                except Exception as error:
                    errors.append(dict(site=site, message=error_message(error)))
                job.update(results=results, errors=errors, progress=round(completed / len(pending)*100), message=f'已搜索 {completed}/{len(pending)} 个网站')

    def analyze(self, search_job, candidate_ids, episode):
        candidates = [row for row in search_job.snapshot()['results'] if row['id'] in candidate_ids]
        if len({row['site'] for row in candidates}) < 2:
            raise ValueError('请至少选择两个网站中同一部番剧的来源')
        return self.launch('analyze', self._analyze, search_job.rules, candidates, episode)

    def _analyze(self, job, all_rules, candidates, requested_episode):
        from . import media, rules
        sources, errors, rows = [], [], []
        for index, candidate in enumerate(candidates):
            if job.cancel.is_set():
                return
            job.update(message=f'获取 {candidate["site"]} 的线路和集数', progress=round(index/len(candidates)*12))
            try:
                roads = rules.chapters(all_rules[candidate['rule_id']], candidate['url'], 20)
                sources.append({**candidate, 'roads':roads})
            except Exception as error:
                errors.append(dict(site=candidate['site'], message=error_message(error)))
                job.update(errors=errors)
        episode = requested_episode if requested_episode is not None else choose_episode(sources)
        if episode is None:
            raise ValueError('未找到可识别的正片集数，请选择其他条目或规则')
        job.update(episode=episode)
        work = []
        for source in sources:
            found = False
            for road in source['roads']:
                episodes = [ep for ep in road['episodes'] if ep.get('number') == episode]
                if len(episodes) != 1:
                    continue
                found = True
                ep = episodes[0]
                row = dict(site=source['site'], title=source['title'], road=road['name'], episode=episode,
                           page_url=ep['url'], status='等待检测', message='等待检测', score=None, rank=None)
                rows.append(row)
                work.append((source, row))
            if not found:
                errors.append(dict(site=source['site'], message=f'缺少第 {episode:g} 集，或集数标签有歧义'))
        job.update(results=rows, errors=errors, progress=15)
        if len({source['site'] for source, row in work}) < 2:
            raise ValueError('拥有该集且标签明确的网站不足两个，无法比较')
        job.update(message='加载本地画质模型，首次运行可能需要下载权重')
        quality.MODEL.load()
        cache_dir = self.data_dir / 'temp'
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='检测-', dir=cache_dir) as temp:
            reference = None
            seen_media = {}
            for index, (source, row) in enumerate(work):
                if job.cancel.is_set():
                    return
                row.update(status='解析中', message='正在解析实际视频地址')
                job.update(results=rows, message=f'{row["site"]} · {row["road"]}（{index+1}/{len(work)}）', progress=15+round(index/len(work)*80))
                directory = Path(temp) / str(index)
                directory.mkdir()
                try:
                    resolved = media.resolve_media(row['page_url'], all_rules[source['rule_id']], job.cancel)
                    cache_key = (resolved['url'], tuple(sorted(resolved.get('headers', {}).items())))
                    if cache_key in seen_media:
                        previous = seen_media[cache_key]
                        row.update({key:copy.deepcopy(previous[key]) for key in ('scores','resolution','codec') if key in previous})
                        row.update(status='已检测', message='与已检测线路使用相同媒体和请求上下文')
                        continue
                    metadata = media.probe(resolved, job.cancel)
                    if metadata['duration'] <= 15:
                        raise ValueError('视频过短，可能是广告或无效内容，未参与排名')
                    if metadata.get('color_transfer') in ('smpte2084','arib-std-b67'):
                        raise ValueError('此来源是 HDR，首版不与 SDR 混合排名')
                    row.update(resolution=f'{metadata["width"]} × {metadata["height"]}', codec=metadata['codec'], status='取样中', message='抽取共同内容')
                    job.update(results=rows)
                    if reference is None:
                        refs = self._reference_frames(media, resolved, metadata, directory, job.cancel)
                        if len(refs) < 3:
                            raise ValueError('有效正文取样不足三个位置，尝试其他来源建立时间轴')
                        reference = {'frames':refs,'duration':metadata['duration'],'aspect':metadata['width']/metadata['height']}
                        aligned = {key:frames for key, frames in refs.items()}
                    else:
                        aspect = metadata['width']/metadata['height']
                        if abs(aspect/reference['aspect']-1) > .03:
                            raise ValueError('画幅比例不同，可能存在裁剪，首版不混排')
                        aligned = self._align_frames(media, resolved, metadata, reference, directory, job.cancel)
                    if len(aligned) < 3:
                        raise ValueError(f'仅对齐 {len(aligned)} 个正文位置，需要至少三个；可能存在片头偏移、不同版本或规则选错条目')
                    row.update(status='分析中', message=f'评估 {len(aligned)} 个匹配位置')
                    job.update(results=rows)
                    scores = {}
                    for key, frames in aligned.items():
                        values = quality.MODEL.score([frame['path'] for frame in frames], job.cancel)
                        scores[key] = sum(values) / len(values)
                    row.update(scores=scores, status='已检测', message='等待汇总共同样本')
                    seen_media[cache_key] = row
                except Exception as error:
                    if job.cancel.is_set():
                        return
                    row.update(status='未参与排名', message=error_message(error))
                finally:
                    job.update(results=rows)
            rows = quality.rank_common(rows)
            for row in rows:
                if row.get('rank'):
                    row['status'] = '已完成'
            job.update(results=rows, progress=98)

    @staticmethod
    def _reference_frames(media, resolved, metadata, directory, cancel):
        refs = {}
        for key, fraction in enumerate((.22, .42, .62, .80)):
            start = metadata['duration'] * fraction
            frames = media.capture_frames(resolved, [start, start+.5, start+1], directory/f'ref-{key}', cancel)
            if len(frames) == 3:
                with Image.open(frames[0]['path']) as image:
                    if quality.usable_frame(image):
                        refs[key] = frames
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
                coarse = media.capture_sequence(resolved, start, duration, 1., directory/f'coarse-{key}', cancel)
                match = quality.best_match(refs[0]['path'], coarse)
                if match is None:
                    continue
                fine_start = max(0., match['time']-.75)
                fine = media.capture_sequence(resolved, fine_start, 1.75, .125, directory/f'fine-{key}', cancel)
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
