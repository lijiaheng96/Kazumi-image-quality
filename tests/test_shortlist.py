"""规格只负责缩小候选；不能将同站多线路或大音轨当作模型结论。"""
import unittest

from helper import shortlist


def item(site, rate=None, *, width=1920, height=1080, codec='h264', fps=24, road='线路1'):
    return {'source':{'rule_id':site},'index':site if isinstance(site,int) else 0,
            'row':{'site':str(site),'road':road},
            'metadata':{'width':width,'height':height,'duration':1200,'codec':codec,'fps':fps,'bitrate':rate}}


class ShortlistTests(unittest.TestCase):
    def test_lower_resolution_is_excluded_even_with_more_bitrate(self):
        low, high = item(0,40_000_000,width=1280,height=720),item(1,1_000_000)
        self.assertEqual(shortlist.highest_resolution([low,high]),[high])

    def test_three_slots_are_for_different_sites_and_best_road_is_used(self):
        sources=[item(0,5_000_000,road='慢'),item(0,10_000_000,road='优'),
                 item(0,8_000_000,road='中'),item(1,4_000_000),item(2,3_000_000),item(3,2_000_000)]
        chosen=shortlist.candidate_order(sources)
        self.assertEqual([x['source']['rule_id'] for x in chosen[:3]],[0,1,2])
        self.assertEqual(chosen[0]['row']['road'],'优')
        self.assertEqual(len(chosen),4)

    def test_file_size_is_normalized_by_duration_and_audio_is_deducted_when_known(self):
        source=item(0)
        source['metadata'].update(duration=100,audio_bitrate=128000,audio_streams=1)
        source['row'].update(size_bytes=26_600_000,size_kind='exact')
        metrics=shortlist.comparison_metrics(source)
        self.assertEqual(metrics['spec_bitrate'],2_000_000)
        self.assertEqual(metrics['spec_bitrate_kind'],'estimated_video')

    def test_multiple_audio_tracks_are_not_subtracted_as_a_single_track(self):
        source=item(0)
        source['metadata'].update(audio_bitrate=128000,audio_streams=2)
        source['row']['average_bitrate']=2_256_000
        metrics=shortlist.comparison_metrics(source)
        self.assertEqual(metrics['spec_bitrate'],2_256_000)
        self.assertEqual(metrics['spec_bitrate_kind'],'container')

    def test_peak_bitrate_is_not_used_as_average(self):
        source=item(0)
        source['row']['peak_bitrate']=20_000_000
        self.assertIsNone(shortlist.comparison_metrics(source)['spec_bitrate'])

    def test_probed_container_bitrate_survives_a_failed_size_estimate(self):
        source=item(0)
        source['metadata'].update(container_bitrate=12_000_000,audio_bitrate=128000,audio_streams=1)
        metrics=shortlist.comparison_metrics(source)
        self.assertEqual(metrics['spec_bitrate'],11_872_000)
        self.assertEqual(metrics['spec_bitrate_kind'],'estimated_video')
        self.assertIs(shortlist.candidate_order([item(1,5_000_000),source])[0],source)

    def test_missing_metrics_remain_unknown_without_fake_model_scores(self):
        source=item(0,fps=None)
        metrics=shortlist.comparison_metrics(source)
        self.assertIsNone(metrics['spec_bitrate'])
        self.assertIsNone(metrics['bits_per_frame'])
        self.assertNotIn('score',metrics)
        self.assertEqual(shortlist.candidate_order([source]),[source])

    def test_efficient_codec_champion_is_retained_without_a_fake_bitrate_multiplier(self):
        sources=[item(0,10_000_000),item(1,9_000_000),item(2,8_000_000),item(3,3_000_000,codec='hevc')]
        chosen=shortlist.candidate_order(sources)[:3]
        self.assertEqual({x['source']['rule_id'] for x in chosen},{0,1,3})

    def test_high_fps_is_not_an_automatic_quality_bonus(self):
        sources=[item(0,10_000_000,fps=24),item(1,9_000_000,fps=25),item(2,5_000_000,fps=60)]
        chosen=shortlist.candidate_order(sources)
        self.assertEqual(chosen[0]['source']['rule_id'],0)
        self.assertEqual(chosen[1]['source']['rule_id'],2,'保留明显不同帧率的候选，由模型统一比较')


if __name__=='__main__':
    unittest.main()
