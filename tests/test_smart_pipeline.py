"""所有网站先探测，只让最高档的不同网站候选进入真实模型流程。"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from helper import media
from helper.jobs import Job, JobManager
from helper.smart_pipeline import SmartPipeline


class SmartPipelineTests(unittest.TestCase):
    def execute(self, *, sites=6, only_one_high=False, fail_sites=(), cancel_size=False, unknown_size=False, hdr_sites=()):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager=JobManager(Path(temporary.name))
        job=Job('analyze')
        rules=[{'name':f'站点{i}'} for i in range(sites)]
        candidates=[{'id':str(i),'site':rule['name'],'rule_id':i,'title':'同一番剧',
                     'url':f'https://example.test/{i}'} for i,rule in enumerate(rules)]
        specs,indices={},{}
        offset=0
        for site in range(sites):
            for road in range(2 if site==1 else 1):
                url=f'https://example.test/{site}/{road}'
                low=site==0 or (only_one_high and site!=1)
                specs[url]={'duration':1200,'width':1280 if low else 1920,'height':720 if low else 1080,
                            'codec':'h264','fps':24,'bitrate':40_000_000 if low else (10-site+road)*1_000_000}
                if site in hdr_sites:
                    specs[url]['color_transfer']='smpte2084'
                indices[str(offset)]=site
                offset+=1
        sampled,size_read,model_loads=[],[],[]
        picture=Image.fromarray(np.random.default_rng(8).integers(20,235,(36,64,3),dtype=np.uint8)).resize((320,180))
        def chapters(rule,url,timeout):
            site=int(url.rsplit('/',1)[1])
            return [{'name':f'线路{road+1}','episodes':[{'number':1,'url':f'{url}/{road}'}]}
                    for road in range(2 if site==1 else 1)]
        def resolve(url,rule,cancel):
            return {'url':url,'headers':{}}
        def probe(resolved,cancel):
            return dict(specs[resolved['url']])
        def estimate(resolved,metadata,cancel,**kwargs):
            size_read.append(resolved['url'])
            if cancel_size:
                cancel.set()
                raise media.Cancelled('任务已取消')
            if unknown_size:
                raise TimeoutError('估算超过预算')
            return {'size_bytes':metadata['duration']*metadata['bitrate']/8,'size_kind':'estimated',
                    'average_bitrate':metadata['bitrate'],'bitrate_scope':'container','size_source':'segment_heads'}
        def capture(resolved,times,directory,cancel):
            site=int(resolved['url'].split('/')[-2])
            sampled.append(site)
            if site in fail_sites:
                raise ValueError('该候选无法读取画面')
            directory.mkdir(parents=True,exist_ok=True)
            frames=[]
            for index,t in enumerate(times):
                path=directory/f'{index}.png'
                picture.save(path)
                frames.append({'time':t,'actual_time':t,'path':str(path)})
            return frames
        def score(paths,cancel):
            site=indices[Path(paths[0]).parents[1].name]
            return [90. if site==3 else 50.+site]*len(paths)
        pipeline=SmartPipeline(manager,job,rules,candidates,1,'smart')
        with patch('helper.rules.chapters',side_effect=chapters),patch('helper.media.resolve_media',side_effect=resolve), \
             patch('helper.media.probe',side_effect=probe), \
             patch('helper.media_size.select_variant',side_effect=lambda resolved,cancel:(resolved,{})), \
             patch('helper.media_size.estimate_size',side_effect=estimate), \
             patch('helper.media.capture_frames',side_effect=capture), \
             patch('helper.quality.MODEL.load',side_effect=lambda:model_loads.append(True)), \
             patch('helper.quality.MODEL.score',side_effect=score):
            pipeline.run()
            job.finish()
        return job.snapshot(),sampled,size_read,model_loads

    def test_only_top_three_distinct_high_resolution_sites_are_sampled_and_model_decides_winner(self):
        job,sampled,size_read,loads=self.execute()
        self.assertEqual(set(sampled),{1,2,3})
        self.assertFalse(any('/0/' in url for url in size_read),'低档连体积估算也跳过')
        rows=job['results']
        self.assertEqual(next(row['site'] for row in rows if row.get('rank')==1),'站点3')
        self.assertEqual(job['shortlist_count'],3)
        self.assertEqual(len([row for row in rows if row.get('rank')]),3)
        self.assertTrue(all(row['samples']==3 for row in rows if row.get('rank')))
        low=next(row for row in rows if row['site']=='站点0')
        self.assertEqual(low['selection_status'],'lower_resolution')
        self.assertIsNone(low['score'])
        best=next(row for row in rows if row['site']=='站点1' and row.get('shortlist_rank'))
        self.assertEqual(best['road'],'线路2')
        self.assertEqual(loads,[True])

    def test_single_highest_resolution_site_is_a_candidate_not_a_fake_cross_site_ranking(self):
        job,sampled,_,loads=self.execute(only_one_high=True)
        self.assertEqual(sampled,[])
        self.assertEqual(loads,[])
        self.assertEqual(job['shortlist_count'],1)
        self.assertFalse(any(row.get('rank') for row in job['results']))
        self.assertIn('一个可评估网站',job['selection_summary'])

    def test_failed_candidate_is_replaced_with_same_highest_resolution_backup(self):
        job,sampled,_,_=self.execute(fail_sites={1})
        self.assertEqual(set(sampled),{1,2,3,4})
        self.assertEqual(job['backup_attempts'],1)
        self.assertEqual({row['site'] for row in job['results'] if row.get('rank')},{'站点2','站点3','站点4'})

    def test_backup_attempts_are_bounded_even_when_all_candidates_fail(self):
        job,sampled,_,_=self.execute(sites=8,fail_sites=set(range(1,8)))
        self.assertEqual(len(set(sampled)),5)
        self.assertEqual(job['backup_attempts'],2)
        self.assertFalse(any(row.get('rank') for row in job['results']))

    def test_unknown_file_size_does_not_discard_a_valid_video_bitrate(self):
        job,sampled,_,_=self.execute(unknown_size=True)
        self.assertEqual(set(sampled),{1,2,3})
        self.assertEqual(len([row for row in job['results'] if row.get('rank')]),3)
        self.assertTrue(all(row.get('size_bytes') is None for row in job['results']))

    def test_cancel_during_size_estimation_does_not_load_or_run_the_model(self):
        job,sampled,_,loads=self.execute(cancel_size=True)
        self.assertEqual(job['status'],'cancelled')
        self.assertEqual(sampled,[])
        self.assertEqual(loads,[])

    def test_hdr_specs_are_retained_but_not_mixed_into_uncalibrated_sdr_model_ranking(self):
        job,sampled,_,_=self.execute(hdr_sites={3})
        self.assertEqual(set(sampled),{1,2,4})
        hdr=next(row for row in job['results'] if row['site']=='站点3')
        self.assertEqual(hdr['selection_status'],'unsupported_hdr')
        self.assertEqual(hdr['resolution'],'1920 × 1080')
        self.assertIsNone(hdr.get('rank'))
        self.assertIsNone(hdr.get('score'))

    def test_highest_hdr_only_does_not_fall_back_to_sampling_lower_resolution_sdr(self):
        job,sampled,_,loads=self.execute(only_one_high=True,hdr_sites={1})
        self.assertEqual(sampled,[])
        self.assertEqual(loads,[])
        self.assertEqual(job['shortlist_count'],0)
        self.assertIn('HDR',job['selection_summary'])
        self.assertFalse(any(row.get('rank') for row in job['results']))


if __name__=='__main__':
    unittest.main()
