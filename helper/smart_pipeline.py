"""全规则规格初筛，最高档前三网站才进入模型，失败候选有限补位。"""
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import media, media_size, quality, shortlist
from .media_session import MediaSession
from .pipeline import AnalysisPipeline, display_aspect


class VariantBackend:
    """签名刷新后仍先选最高档，避免重新退回主清单的首个低档视频流。"""
    def __init__(self):
        self.hint={}

    def __getattr__(self,name):
        return getattr(media,name)

    def resolve_media(self,page_url,rule,cancel):
        resolved=media.resolve_media(page_url,rule,cancel)
        selected,self.hint=media_size.select_variant(resolved,cancel)
        return selected


class SmartPipeline(AnalysisPipeline):
    def prepare(self,item):
        source,row=item
        if self.job.cancel.is_set():
            return None
        try:
            backend=VariantBackend()
            with self.stage(row,'解析'):
                resolved=backend.resolve_media(row['page_url'],self.rules[source['rule_id']],self.job.cancel)
                resolved_at=time.monotonic()
            with self.stage(row,'规格探测'):
                metadata=media.probe(resolved,self.job.cancel)
            if metadata['duration']<=15:
                raise ValueError('视频过短，可能为广告或无效内容')
            if not shortlist.pixels({'metadata':metadata}):
                raise ValueError('未能确认有效分辨率，无法进入最高档筛选')
            self.set_row(row,resolution=f'{metadata["width"]} × {metadata["height"]}',codec=metadata['codec'],
                         bitrate=metadata.get('bitrate'),fps=metadata.get('fps'),duration=metadata['duration'],
                         status='等待筛选',message='规格已读取，等待全部网站确认最高分辨率')
            session=MediaSession(backend,row['page_url'],self.rules[source['rule_id']],resolved,metadata,resolved_at,
                                 stage=lambda:self.job.stage('链接刷新',site=row['site'],road=row['road'],row=row),
                                 on_refresh=lambda count:self.set_row(row,refresh_count=count))
            return {'source':source,'row':row,'media':resolved,'metadata':metadata,'session':session,'backend':backend}
        except Exception as error:
            self.set_row(row,selection_status='unavailable',selection_reason='未取得可用于筛选的媒体规格')
            self.fail(row,error)
            return None
        finally:
            with self.job.lock:
                self.prepared+=1
                self.publish(progress=12+round(self.prepared/max(1,len(self.rows))*25))

    def estimate(self,item):
        row,session=item['row'],item['session']
        if self.job.cancel.is_set():
            return
        try:
            with self.stage(row,'体积估算'):
                for attempt in range(2):
                    try:
                        values=media_size.estimate_size(session.resolved,item['metadata'],self.job.cancel,
                                                        manifest_info=item['backend'].hint)
                        break
                    except media.MediaAccessError:
                        if attempt:
                            raise
                        session._refresh(self.job.cancel)
                self.set_row(row,**values)
        except media.Cancelled:
            return
        except Exception as error:
            from .jobs import error_message
            # 非关键估算失败不抹掉已取得的真实分辨率/码率，更不记为零。
            self.set_row(row,size_bytes=None,size_kind='unknown',average_bitrate=None,
                         size_source='unknown',size_note=error_message(error))
        self.set_row(row,**shortlist.comparison_metrics(item),status='等待预选',message='最高档规格已整理')

    def finish_row(self):
        with self.job.lock:
            self.completed+=1
            success=sum(bool(row.get('scores')) for row in self.rows)
            self.publish(progress=min(96,60+round(self.completed*7)),
                         message=f'模型已处理 {self.completed} 条候选，{success} 个网站取得有效样本')

    def choose(self,item,number,backup=False):
        self.set_row(item['row'],shortlist_rank=number,selection_status='selected',
                     selection_reason='候选失效后从最高档备用网站补位' if backup else '最高分辨率，按同编码及相近帧率规格预选',
                     status='等待模型评估',message='已入选，等待共同画面模型评估')

    def run(self):
        ready=self.collect_sources()
        if self.job.cancel.is_set():
            return
        retained=shortlist.highest_resolution(ready)
        if not retained:
            raise ValueError('没有取得可用视频规格，无法筛选最高分辨率')
        highest=retained[0]['row']['resolution']
        retained_ids={id(item) for item in retained}
        for item in ready:
            if id(item) not in retained_ids:
                self.set_row(item['row'],status='按分辨率排除',selection_status='lower_resolution',
                             selection_reason=f'低于本次最高分辨率 {highest}，不再取样',message='较低分辨率，已跳过体积估算与模型')
        self.publish(highest_resolution=highest,selection_summary=f'最高分辨率 {highest}，正在整理同档来源的码率和体积',progress=38)
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in as_completed([pool.submit(self.estimate,item) for item in retained]):
                future.result()
        if self.job.cancel.is_set():
            return
        eligible=[]
        for item in retained:
            if item['metadata'].get('color_transfer') in ('smpte2084','arib-std-b67'):
                self.set_row(item['row'],status='HDR 仅展示规格',selection_status='unsupported_hdr',
                             selection_reason='当前模型未校准 HDR 色调映射，不能与 SDR 直接比较',
                             message='保留最高档规格；本轮不取样，不产生模型分数')
            else:
                eligible.append(item)
        ordered=shortlist.candidate_order(eligible)
        selected,backups=ordered[:3],ordered[3:]
        picked_ids={id(item) for item in selected}
        site_best_ids={id(item) for item in ordered}
        for item in eligible:
            if id(item) not in picked_ids:
                same_site=id(item) not in site_best_ids
                self.set_row(item['row'],status='未进入模型预选',
                             selection_status='same_site' if same_site else 'outside_top3',
                             selection_reason='同一网站已有规格更优的线路候选' if same_site else '未进入前三网站，保留为同档备用',
                             message='本轮不取样，不产生模型分数')
        for number,item in enumerate(selected,1):
            self.choose(item,number)
        self.publish(shortlist_count=len(selected),backup_attempts=0,progress=58,
                     selection_summary=f'全部候选已探测；最高档 {highest}，预选 {len(selected)} 个不同网站进行模型比较')
        if len(selected)<2:
            reason=('仅有一个可评估网站' if selected else '仅有暂不支持模型比较的 HDR 网站')
            self.publish(selection_summary=f'最高分辨率 {highest} {reason}，已列出规格候选；不足两个可评估网站，未做跨站模型排名',progress=98)
            for item in selected:
                self.set_row(item['row'],status='唯一最高档候选',message='已确定规格候选；跨站模型比较需要至少两个网站')
            return
        self.publish(message='只为入选网站加载画质模型')
        with self.job.stage('模型加载'):
            quality.MODEL.load()
        if self.job.cancel.is_set():
            return
        cache=self.manager.data_dir/'temp'
        cache.mkdir(parents=True,exist_ok=True)
        extras=0

        def replacement():
            nonlocal extras
            if extras>=2 or not backups or self.job.cancel.is_set():
                return None
            extras+=1
            item=backups.pop(0)
            self.choose(item,3+extras,backup=True)
            self.publish(backup_attempts=extras)
            return item

        with tempfile.TemporaryDirectory(prefix='智能检测-',dir=cache) as temporary:
            root=Path(temporary)
            reference=None
            waiting=list(selected)
            while waiting and reference is None and not self.job.cancel.is_set():
                item=waiting.pop(0)
                row,metadata=item['row'],item['metadata']
                try:
                    with self.stage(row,'参考取样'):
                        refs=self.manager._reference_frames(item['session'],item['media'],metadata,
                                                           root/str(item['index']),self.job.cancel,positions=3)
                        if len(refs)<3:
                            raise ValueError('参考来源有效正文位置不足三个')
                    self.score(row,refs)
                    reference={'frames':refs,'duration':metadata['duration'],'aspect':display_aspect(metadata)}
                except Exception as error:
                    self.fail(row,error)
                    other=replacement()
                    if other:
                        waiting.append(other)
                finally:
                    self.finish_row()
            if reference and not self.job.cancel.is_set():
                # 参考成功后其他入选网站并行，补位只占同样的最多两路并发。
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending=[pool.submit(self.sample,item,reference,root) for item in waiting]
                    for future in as_completed(pending):
                        future.result()
                while sum(bool(row.get('scores')) for row in self.rows)<min(3,len(ordered)):
                    other=replacement()
                    if other is None:
                        break
                    self.sample(other,reference,root)
        with self.job.lock:
            self.rows=quality.rank_common(self.rows)
            ranked={row['site'] for row in self.rows if row.get('rank')}
            for row in self.rows:
                if row.get('rank'):
                    row['status']='已完成'
            explanation=(f'{len(ranked)} 个网站通过共同画面模型比较，第一名仅代表本次入选来源的模型推荐'
                         if ranked else '入选来源未形成至少两个网站、三个共同位置的模型比较，请查看各线路原因')
            self.publish(progress=98,selection_summary=f'最高档 {highest}，预选最多三站，额外补位 {extras} 次；{explanation}')
