"""签名链接更新只影响本线路；验证版本、取消和有界重试。"""
import importlib.util
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from helper import media


class SessionBackend:
    Cancelled = media.Cancelled
    MediaAccessError = media.MediaAccessError

    def __init__(self):
        self.metadata = dict(width=1920, height=1080, codec='h264', duration=100.)
        self.refreshes = []
        self.probes = []
        self.captures = []
        self.failures = []
        self.cancel_during_resolve = False

    def resolve_media(self, page_url, rule, cancel):
        self.refreshes.append((page_url, rule))
        if self.cancel_during_resolve:
            cancel.set()
        return {'url': 'https://example.test/new.mp4?signature=secret', 'headers': {}}

    def probe(self, resolved, cancel):
        self.probes.append(resolved)
        return dict(self.metadata)

    def capture_frames(self, resolved, times, directory, cancel):
        self.captures.append(('frames', resolved, times))
        if self.failures:
            raise self.failures.pop(0)
        return [{'time': times[0], 'path': '刷新后.png' if 'new.mp4' in resolved['url'] else '原链接.png'}]

    def capture_sequence(self, resolved, start, duration, step, directory, cancel, *, width=1280):
        self.captures.append(('sequence', resolved, (start, duration, step, width)))
        if self.failures:
            raise self.failures.pop(0)
        return [{'time': start, 'path': '刷新后.png' if 'new.mp4' in resolved['url'] else '原链接.png'}]


class MediaSessionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('helper.media_session'), '需要媒体链接会话代理')
        from helper.media_session import MediaSession
        self.session_type = MediaSession
        self.clock = patch('helper.media_session.time.monotonic', return_value=100.)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.cancel = threading.Event()
        self.backend = SessionBackend()
        self.original = {'url': 'https://example.test/old.mp4?signature=secret', 'headers': {}}
        self.rule = {'name': '测试站'}
        self.row = {}
        self.events = []

    @contextmanager
    def stage(self):
        self.events.append('链接刷新开始')
        try:
            yield
        finally:
            self.events.append('链接刷新结束')

    def session(self, resolved_at=100.):
        return self.session_type(self.backend, 'https://example.test/watch', self.rule,
                                 self.original, dict(self.backend.metadata), resolved_at,
                                 stage=self.stage,
                                 on_refresh=lambda count: self.row.update(refresh_count=count))

    def capture(self, session):
        return session.capture_frames(self.original, [5., 5.5, 6.], Path('unused'), self.cancel)

    def test_fresh_link_is_used_without_an_extra_browser_or_probe(self):
        result = self.capture(self.session())
        self.assertEqual(result[0]['path'], '原链接.png')
        self.assertEqual(self.backend.refreshes, [])
        self.assertEqual(self.backend.probes, [])
        self.assertEqual(self.events, [])

    def test_expired_link_is_refreshed_before_capture_and_reused_while_fresh(self):
        session = self.session(resolved_at=30.)
        self.assertEqual(self.capture(session)[0]['path'], '刷新后.png')
        self.assertEqual(self.capture(session)[0]['path'], '刷新后.png')
        self.assertEqual(self.backend.refreshes, [('https://example.test/watch', self.rule)])
        self.assertEqual(len(self.backend.probes), 1)
        self.assertEqual(self.row['refresh_count'], 1)
        self.assertEqual(self.events, ['链接刷新开始', '链接刷新结束'])
        self.assertNotIn('secret', str(self.row) + str(self.events))

    def test_sequence_proxy_preserves_sampling_arguments_and_refreshes(self):
        result = self.session(resolved_at=0.).capture_sequence(
            self.original, 10., 20., .125, Path('unused'), self.cancel, width=192)
        self.assertEqual(result[0]['path'], '刷新后.png')
        self.assertEqual(self.backend.captures[0][2], (10., 20., .125, 192))
        self.assertEqual(self.row['refresh_count'], 1)

    def test_access_denial_refreshes_and_retries_only_once(self):
        for status in (401, 403, 410):
            with self.subTest(status=status):
                self.backend = SessionBackend()
                self.backend.failures = [media.MediaAccessError(status)]
                result = self.capture(self.session())
                self.assertEqual(result[0]['path'], '刷新后.png')
                self.assertEqual(len(self.backend.captures), 2)
                self.assertEqual(len(self.backend.refreshes), 1)

    def test_second_access_denial_stops_instead_of_refreshing_forever(self):
        self.backend.failures = [media.MediaAccessError(403), media.MediaAccessError(403)]
        with self.assertRaisesRegex(ValueError, '刷新后.*HTTP 403'):
            self.capture(self.session())
        self.assertEqual(len(self.backend.captures), 2)
        self.assertEqual(len(self.backend.refreshes), 1)

    def test_other_media_errors_do_not_trigger_a_browser_refresh(self):
        for failure in (ValueError('无法解码'), media.MediaAccessError(500)):
            with self.subTest(failure=failure):
                self.backend = SessionBackend()
                self.backend.failures = [failure]
                with self.assertRaises(type(failure)):
                    self.capture(self.session())
                self.assertEqual(len(self.backend.captures), 1)
                self.assertEqual(self.backend.refreshes, [])

    def test_changed_media_version_is_rejected_before_collecting_any_frames(self):
        for changed in ({'width': 1280}, {'height': 720}, {'codec': 'hevc'}, {'duration': 102.},
                        {'color_transfer': 'smpte2084'}, {'display_aspect_ratio': 4 / 3}):
            with self.subTest(changed=changed):
                self.backend = SessionBackend()
                session = self.session(resolved_at=0.)
                self.backend.metadata.update(changed)
                with self.assertRaisesRegex(ValueError, '版本.*混排'):
                    self.capture(session)
                self.assertEqual(self.backend.captures, [])
                self.assertEqual(len(self.backend.probes), 1)

    def test_small_duration_rounding_difference_does_not_reject_the_same_version(self):
        session = self.session(resolved_at=0.)
        self.backend.metadata['duration'] = 100.02
        self.assertEqual(self.capture(session)[0]['path'], '刷新后.png')

    def test_changed_reported_transfer_function_rejects_even_equal_resolution_and_duration(self):
        self.backend.metadata['color_transfer'] = 'bt709'
        session = self.session(resolved_at=0.)
        self.backend.metadata['color_transfer'] = 'arib-std-b67'
        with self.assertRaisesRegex(ValueError, '版本.*混排'):
            self.capture(session)
        self.assertEqual(self.backend.captures, [])

    def test_anamorphic_video_must_keep_the_same_display_aspect_after_refresh(self):
        self.backend.metadata.update(width=720, height=480, display_aspect_ratio=16 / 9)
        session = self.session(resolved_at=0.)
        self.backend.metadata['display_aspect_ratio'] = 4 / 3
        with self.assertRaisesRegex(ValueError, '版本.*混排'):
            self.capture(session)
        self.assertEqual(self.backend.captures, [])

    def test_equivalent_rounded_display_aspect_is_allowed(self):
        self.backend.metadata.update(color_transfer='bt709', display_aspect_ratio=16 / 9)
        session = self.session(resolved_at=0.)
        self.backend.metadata['display_aspect_ratio'] = 1.778
        self.assertEqual(self.capture(session)[0]['path'], '刷新后.png')

    def test_conflicting_known_frame_rate_or_bitrate_rejects_refreshed_quality(self):
        for key, original, refreshed in (('fps', 24., 60.), ('bitrate', 1_000_000, 8_000_000)):
            with self.subTest(key=key):
                self.backend = SessionBackend()
                self.backend.metadata[key] = original
                session = self.session(resolved_at=0.)
                self.backend.metadata[key] = refreshed
                with self.assertRaisesRegex(ValueError, '版本.*混排'):
                    self.capture(session)
                self.assertEqual(self.backend.captures, [])

    def test_unknown_frame_rate_or_bitrate_remains_eligible_after_refresh(self):
        for key, known in (('fps', 24.), ('bitrate', 1_000_000)):
            for original, refreshed in ((None, known), (known, None), (None, None), (float('nan'), known)):
                with self.subTest(key=key, original=original, refreshed=refreshed):
                    self.backend = SessionBackend()
                    self.backend.metadata[key] = original
                    session = self.session(resolved_at=0.)
                    self.backend.metadata[key] = refreshed
                    self.assertEqual(self.capture(session)[0]['path'], '刷新后.png')

    def test_small_frame_rate_and_bitrate_reporting_differences_are_allowed(self):
        self.backend.metadata.update(fps=23.976, bitrate=1_000_000)
        session = self.session(resolved_at=0.)
        self.backend.metadata.update(fps=24., bitrate=1_020_000)
        self.assertEqual(self.capture(session)[0]['path'], '刷新后.png')

    def test_cancelled_session_never_refreshes_or_captures(self):
        self.cancel.set()
        with self.assertRaises(media.Cancelled):
            self.capture(self.session(resolved_at=0.))
        self.assertEqual(self.backend.refreshes, [])
        self.assertEqual(self.backend.captures, [])

    def test_cancellation_during_refresh_stops_before_probe_and_capture(self):
        self.backend.cancel_during_resolve = True
        with self.assertRaises(media.Cancelled):
            self.capture(self.session(resolved_at=0.))
        self.assertEqual(len(self.backend.refreshes), 1)
        self.assertEqual(self.backend.probes, [])
        self.assertEqual(self.backend.captures, [])

    def run_pipeline_fixture(self, *, queued=True, shared_media=False, different_bitrates=False,
                             different_scores=False):
        import tempfile
        from PIL import Image, ImageDraw
        from helper.jobs import Job, JobManager
        from helper.pipeline import AnalysisPipeline

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager = JobManager(Path(temporary.name))
        job = Job('analyze')
        rules = [{'name': '甲'}, {'name': '乙'}]
        candidates = [dict(id=str(i), rule_id=i, site=rule['name'], title='同一作品',
                           url=f'https://example.test/{i}') for i, rule in enumerate(rules)]
        resolutions = {}
        contexts = {}
        captured = []
        picture = Image.new('RGB', (128, 72), '#202020')
        ImageDraw.Draw(picture).rectangle((16, 12, 95, 56), fill='#eeeeee')

        def chapters(rule, page_url, timeout):
            return [{'name': '线路一', 'episodes': [{'number': 1, 'url': page_url}]}]

        def resolve(page_url, rule, cancel):
            resolutions[page_url] = resolutions.get(page_url, 0) + 1
            suffix = 'old' if resolutions[page_url] == 1 else 'new'
            url = 'https://example.test/shared.mp4' if shared_media and suffix == 'old' else f'{page_url}/{suffix}.mp4'
            resolved = {'url': url, 'headers': {}}
            contexts[id(resolved)] = page_url
            return resolved

        def probe(resolved, cancel):
            metadata = dict(self.backend.metadata)
            if different_bitrates:
                metadata['bitrate'] = 1_000_000 if contexts[id(resolved)].endswith('/0') else 8_000_000
            return metadata

        def capture(resolved, times, directory, cancel):
            captured.append(resolved['url'])
            if queued and not resolved['url'].endswith('/new.mp4'):
                raise media.MediaAccessError(403)
            directory.mkdir(parents=True, exist_ok=True)
            frames = []
            for index, timestamp in enumerate(times):
                path = directory / f'{index}.png'
                picture.save(path)
                frames.append({'time': timestamp, 'actual_time': timestamp, 'path': str(path)})
            return frames

        def score(paths, cancel):
            if different_scores:
                return [70. if Path(path).parents[1].name == '0' else 50. for path in paths]
            return [60.] * len(paths)

        pipeline = AnalysisPipeline(manager, job, rules, candidates, 1, 'fast')
        with patch('helper.rules.chapters', side_effect=chapters), \
                patch('helper.media.resolve_media', side_effect=resolve), \
                patch('helper.media.probe', side_effect=probe), \
                patch('helper.media.capture_frames', side_effect=capture), \
                patch('helper.quality.MODEL.load', side_effect=lambda: setattr(self.now, 'return_value', 170. if queued else 100.)), \
                patch('helper.quality.MODEL.score', side_effect=score):
            pipeline.run()
        return job.snapshot()['results'], resolutions, captured, job

    def test_pipeline_refreshes_queued_reference_and_alignment_sources(self):
        result, resolutions, captured, job = self.run_pipeline_fixture()
        self.assertEqual([row['rank'] for row in result], [1, 1])
        self.assertEqual([row['refresh_count'] for row in result], [1, 1])
        self.assertEqual(resolutions, {'https://example.test/0': 2, 'https://example.test/1': 2})
        self.assertEqual(len(captured), 6, '两站各三个位置直接使用刷新地址，不先请求过期地址')
        self.assertTrue(all(url.endswith('/new.mp4') for url in captured))
        self.assertEqual(sum(event['stage'] == '链接刷新' for event in job.events), 2)
        self.assertEqual(len(next(row for row in result if row['site'] == '乙')['alignment']), 3)

    def test_initial_shared_link_with_conflicting_specs_is_sampled_independently(self):
        result, resolutions, captured, _ = self.run_pipeline_fixture(
            queued=False, shared_media=True, different_bitrates=True, different_scores=True)
        self.assertEqual([(row['site'], row['score']) for row in result], [('甲', 70.), ('乙', 50.)])
        self.assertTrue(all(not row.get('cached') for row in result))
        self.assertEqual(len(captured), 6, '同链接但初始码率不同，不能复用另一路评分')
        self.assertEqual(resolutions, {'https://example.test/0': 1, 'https://example.test/1': 1})

    def test_refreshed_original_cannot_lend_scores_to_an_initial_url_duplicate(self):
        result, resolutions, captured, _ = self.run_pipeline_fixture(
            shared_media=True, different_scores=True)
        self.assertEqual([(row['site'], row['score']) for row in result], [('甲', 70.), ('乙', 50.)])
        self.assertTrue(all(not row.get('cached') for row in result))
        self.assertEqual([row['refresh_count'] for row in result], [1, 1])
        self.assertEqual(len(captured), 6, '原线路链接刷新后，初始同链接线路必须独立取样评分')
        self.assertEqual(resolutions, {'https://example.test/0': 2, 'https://example.test/1': 2})


if __name__ == '__main__':
    unittest.main()
