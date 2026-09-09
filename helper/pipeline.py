"""有限并发的媒体检测流程，规格只影响检测顺序，不淘汰来源。"""
from __future__ import annotations

import copy
import tempfile
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import media, quality, rules
from .media_session import MediaSession


class AnalysisPipeline:
    def __init__(self, manager, job, all_rules, candidates, episode, mode):
        self.manager, self.job = manager, job
        self.rules, self.candidates = all_rules, candidates
        self.episode, self.mode = episode, mode
        self.rows, self.errors = [], []
        self.prepared, self.completed = 0, 0
        self.job.update(mode=mode)

    def publish(self, **values):
        with self.job.lock:
            self.job.update(results=self.rows, errors=self.errors, **values)

    def set_row(self, row, **values):
        with self.job.lock:
            row.update(values)
            self.publish()

    @contextmanager
    def stage(self, row, name):
        self.set_row(row, status=name+'中', message='正在'+name,
                     stage_started_seconds=round(time.monotonic()-self.job.started,1))
        self.publish(message=f'{row["site"]} · {row["road"]} 正在{name}；各线路进度见下方')
        try:
            with self.job.stage(name, site=row['site'], road=row['road'], row=row):
                yield
        finally:
            with self.job.lock:
                row.pop('stage_started_seconds',None)
                self.publish()

    def fail(self, row, error):
        from .jobs import error_message
        self.set_row(row, status='已取消' if self.job.cancel.is_set() else '未参与排名', message=error_message(error))

    def chapters(self, candidate):
        if self.job.cancel.is_set():
            return None
        with self.job.stage('获取集数', site=candidate['site']):
            return {**candidate, 'roads':rules.chapters(self.rules[candidate['rule_id']],candidate['url'],20)}

    def prepare(self, item):
        source, row = item
        if self.job.cancel.is_set():
            return None
        try:
            with self.stage(row, '解析'):
                resolved = media.resolve_media(row['page_url'],self.rules[source['rule_id']],self.job.cancel)
                resolved_at = time.monotonic()
            with self.stage(row, '规格探测'):
                metadata = media.probe(resolved,self.job.cancel)
            if metadata['duration'] <= 15:
                raise ValueError('视频过短，可能是广告或无效内容，未参与排名')
            if metadata.get('color_transfer') in ('smpte2084','arib-std-b67'):
                raise ValueError('此来源是 HDR，暂不与 SDR 混合排名')
            self.set_row(row, resolution=f'{metadata["width"]} × {metadata["height"]}',
                         codec=metadata['codec'], bitrate=metadata.get('bitrate'),
                         status='等待取样', message='规格已获取，分辨率和码率不会作为淘汰门槛')
            session = MediaSession(media, row['page_url'], self.rules[source['rule_id']],
                                   resolved, metadata, resolved_at,
                                   stage=lambda: self.job.stage('链接刷新', site=row['site'], road=row['road'], row=row),
                                   on_refresh=lambda count: self.set_row(row, refresh_count=count))
            return {'source':source,'row':row, 'media':resolved, 'metadata':metadata,
                    'resolved_at':resolved_at, 'session':session}
        except Exception as error:
            self.fail(row,error)
            return None
        finally:
            with self.job.lock:
                self.prepared += 1
                self.publish(progress=12+round(self.prepared/max(1,len(self.rows))*25),
                             message=f'已探测 {self.prepared}/{len(self.rows)} 条线路的规格')

    def score(self, row, aligned):
        if len(aligned)<3:
            raise ValueError(f'仅对齐 {len(aligned)} 个正文位置，需要至少三个；可能是片头偏移、不同版本或选错条目')
        with self.stage(row,'评分'):
            scores={}
            for key,frames in aligned.items():
                values=quality.MODEL.score([frame['path'] for frame in frames],self.job.cancel)
                scores[key]=sum(values)/len(values)
        self.set_row(row, scores=scores, status='已检测', message='等待汇总共同样本')

    def sample(self, item, reference, root):
        row, metadata = item['row'], item['metadata']
        if self.job.cancel.is_set():
            return
        trace = []
        try:
            if abs(display_aspect(metadata)/reference['aspect']-1)>.03:
                raise ValueError('画幅比例不同，可能存在裁剪，暂不混排')
            with self.stage(row,'取样与对齐'):
                aligned=self.manager._align_frames(item['session'],item['media'],metadata,reference,
                                                   root/str(item['index']),self.job.cancel,trace=trace)
            self.score(row,aligned)
        except Exception as error:
            self.fail(row,error)
        finally:
            self.set_row(row, alignment=trace)
            self.finish_row()

    def finish_row(self):
        with self.job.lock:
            self.completed += 1
            self.publish(progress=38+round(self.completed/max(1,len(self.rows))*58),
                         message=f'已处理 {self.completed}/{len(self.rows)} 条线路 · 最多两路并发')

    def run(self):
        from .jobs import choose_episode, error_message
        sources=[]
        self.publish(message='并行获取线路和集数')
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending={pool.submit(self.chapters,candidate):candidate for candidate in self.candidates}
            for future in as_completed(pending):
                try:
                    result=future.result()
                    if result:
                        sources.append(result)
                except Exception as error:
                    self.errors.append(dict(site=pending[future]['site'],message=error_message(error)))
                self.publish()
        if self.job.cancel.is_set():
            return
        # 保持用户候选顺序，避免网络返回顺序改变线路编号及参考源选择。
        order={candidate['id']:index for index,candidate in enumerate(self.candidates)}
        sources.sort(key=lambda source:order[source['id']])
        episode=self.episode if self.episode is not None else choose_episode(sources)
        if episode is None:
            raise ValueError('未找到可识别的正片集数，请选择其他条目或规则')
        self.job.update(episode=episode)
        work=[]
        for source in sources:
            found=False
            for road in source['roads']:
                episodes=[ep for ep in road['episodes'] if ep.get('number')==episode]
                if len(episodes)!=1:
                    continue
                found=True
                row=dict(site=source['site'],title=source['title'],road=road['name'],episode=episode,
                         page_url=episodes[0]['url'],status='等待检测',message='等待检测',score=None,rank=None,timings={})
                self.rows.append(row)
                work.append((source,row))
            if not found:
                self.errors.append(dict(site=source['site'],message=f'缺少第 {episode:g} 集，或集数标签有歧义'))
        self.publish(progress=12)
        if len({row['site'] for row in self.rows})<2:
            raise ValueError('拥有该集且标签明确的网站不足两个，无法比较')
        # 同站线路轮转后再探测，避免一个多线路网站占满队列。
        grouped={}
        for item in work:
            grouped.setdefault(item[1]['site'],[]).append(item)
        fair=[]
        while any(grouped.values()):
            for items in grouped.values():
                if items:
                    fair.append(items.pop(0))
        ready=[]
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending={pool.submit(self.prepare,item):self.rows.index(item[1]) for item in fair}
            for future in as_completed(pending):
                item=future.result()
                if item:
                    item['index']=pending[future]
                    ready.append(item)
        if self.job.cancel.is_set():
            return
        if len({item['row']['site'] for item in ready})<2:
            for item in ready:
                self.set_row(item['row'],status='未参与排名',message='可读取视频的网站不足两个，本次未取样')
            raise ValueError('可读取视频的网站不足两个，本次无法比较')
        # 高分辨率仅优先作为内容参考。低分辨率甚至缺码率的来源都保留。
        ready.sort(key=lambda item:(-item['metadata']['width']*item['metadata']['height'],item['index']))
        unique,duplicates={},[]
        for item in ready:
            resolved=item['media']
            specification = tuple(item['metadata'].get(name) for name in
                                  ('width','height','codec','duration','fps','bitrate',
                                   'color_transfer','display_aspect_ratio'))
            key=(resolved['url'],tuple(sorted(resolved.get('headers',{}).items())),specification)
            if key in unique:
                duplicates.append((item,unique[key]))
            else:
                unique[key]=item
        ready=list(unique.values())
        self.completed=len(self.rows)-len(ready)-len(duplicates)
        self.publish(message='加载本地画质模型')
        with self.job.stage('模型加载'):
            quality.MODEL.load()
        if self.job.cancel.is_set():
            return
        cache=self.manager.data_dir/'temp'
        cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='检测-',dir=cache) as temp:
            root=Path(temp)
            reference=None
            while ready and reference is None and not self.job.cancel.is_set():
                item=ready.pop(0)
                row,metadata=item['row'],item['metadata']
                try:
                    with self.stage(row,'参考取样'):
                        refs=self.manager._reference_frames(item['session'],item['media'],metadata,root/str(item['index']),
                                                           self.job.cancel, positions=3 if self.mode=='fast' else 4)
                        if len(refs)<3:
                            raise ValueError('有效正文取样不足三个位置，尝试其他参考来源')
                    self.score(row,refs)
                    reference={'frames':refs,'duration':metadata['duration'],'aspect':display_aspect(metadata)}
                except Exception as error:
                    self.fail(row,error)
                finally:
                    self.finish_row()
            if reference and not self.job.cancel.is_set():
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending=[pool.submit(self.sample,item,reference,root) for item in ready]
                    for future in as_completed(pending):
                        future.result()
            for item,original in duplicates:
                original_row=original['row']
                if original['session'].refresh_count:
                    # 刷新后不能沿用最初的同链假设；已有并发任务结束后逐路独立核验。
                    if reference and not self.job.cancel.is_set():
                        self.sample(item, reference, root)
                        continue
                    self.set_row(item['row'],status='未参与排名',
                                 message='原线路已刷新链接，本次无法独立核验，未复用相同媒体的评分')
                elif original_row.get('scores'):
                    self.set_row(item['row'],scores=copy.deepcopy(original_row['scores']),status='已检测',cached=True,
                                 message='同一媒体及请求上下文，复用本次已检测样本')
                else:
                    self.set_row(item['row'],status='未参与排名',message='相同媒体本次检测未成功，无法复用评分')
                self.finish_row()
        with self.job.lock:
            self.rows=quality.rank_common(self.rows)
            for row in self.rows:
                if row.get('rank'):
                    row['status']='已完成'
            self.publish(progress=98)


def display_aspect(metadata):
    return metadata.get('display_aspect_ratio') or metadata['width']/metadata['height']
