"""对齐必须使用真实时间及完整三帧，不让黑帧和重复镜头误导定位。"""
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
from PIL import Image
from helper.jobs import JobManager
from helper import media


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()
        self.paths = []
        for index in range(6):
            path = self.root / f'{index}.png'
            data = np.random.default_rng(index).integers(10, 240, (18, 32, 3), dtype=np.uint8)
            Image.fromarray(data).resize((320, 180)).save(path)
            self.paths.append(str(path))
        black = self.root / '黑帧.png'
        Image.new('RGB', (320, 180)).save(black)
        self.black = str(black)

    def frames(self, start, paths=None, actual=None):
        return [dict(time=start+i*.5, actual_time=(actual if actual is not None else start)+i*.5,
                     path=path) for i, path in enumerate(paths or self.paths[:3])]

    def test_reference_requires_all_three_frames_usable(self):
        backend = Mock()
        backend.capture_frames.side_effect = lambda resolved,times,*args: self.frames(times[0], [self.paths[0],self.black,self.paths[2]])
        refs = JobManager._reference_frames(backend, {}, {'duration':100}, self.root, self.cancel)
        self.assertEqual(refs, {}, '中间黑帧不可作为所有来源必须匹配的参考')

    def test_direct_alignment_uses_actual_reference_times(self):
        backend = Mock()
        backend.capture_frames.side_effect = lambda resolved,times,*args: self.frames(times[0])
        refs = self.frames(10, actual=10.125)
        aligned = JobManager._align_frames(backend, {}, {'duration':100,'fps':24}, {'frames':{0:refs}}, self.root, self.cancel)
        self.assertIn(0,aligned)
        self.assertEqual(backend.capture_frames.call_args.args[1], [10.125,10.625,11.125])

    def test_repeated_first_frame_does_not_hide_valid_sequence(self):
        backend = Mock()
        def capture(resolved,times,*args):
            return self.frames(times[0], self.paths[:3] if abs(times[0]-11)<.05 else self.paths[3:])
        backend.capture_frames.side_effect = capture
        # 10秒首帧同样完美匹配，但只有11秒开始的完整三帧才属于相同片段。
        candidates = [dict(time=t,actual_time=t,path=self.paths[p]) for t,p in
                      [(9.5,3),(10,0),(10.5,4),(11,0),(11.5,1),(12,2),(12.5,5)]]
        backend.capture_sequence.return_value = candidates
        result = JobManager._align_frames(backend, {}, {'duration':100,'fps':24},
                                         {'frames':{0:self.frames(10)}}, self.root,self.cancel)
        self.assertIn(0,result)
        self.assertAlmostEqual(result[0][0]['actual_time'],11)

    def test_three_future_copies_cannot_count_as_three_moments(self):
        refs = self.frames(10, [self.paths[0]]*3)
        frames = self.frames(10, [self.paths[0]]*3)
        for frame in frames:
            frame['actual_time'] = 12
        self.assertFalse(JobManager._sequence_matches(refs,frames))

    def test_unrelated_content_remains_unmatched(self):
        backend = Mock()
        backend.capture_frames.side_effect = lambda resolved,times,*args: self.frames(times[0],self.paths[3:])
        backend.capture_sequence.return_value = self.frames(10,self.paths[3:])
        result = JobManager._align_frames(backend, {}, {'duration':100,'fps':24},
                                         {'frames':{0:self.frames(10)}},self.root,self.cancel)
        self.assertEqual(result,{})

    def rapid_cut_fixture(self, padding_frames):
        """每六帧换独特画面，避免缓慢变化的测试图掩盖一帧定位偏差。"""
        raw = self.root / '快速切镜.rgb'
        source, target = self.root / '原片.mkv', self.root / '加片头.mkv'
        with raw.open('wb') as stream:
            for index in range(48):
                pixels = np.random.default_rng(index).integers(10, 240, (18, 32, 3), dtype=np.uint8)
                image = Image.fromarray(pixels).resize((96, 54), Image.Resampling.BICUBIC)
                for _ in range(6):
                    stream.write(image.tobytes())
        ffmpeg = media.imageio_ffmpeg.get_ffmpeg_exe()
        commands = [
            [ffmpeg, '-v', 'error', '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', '96x54',
             '-framerate', '24', '-i', str(raw), '-c:v', 'ffv1', str(source)],
            [ffmpeg, '-v', 'error', '-i', str(source), '-vf', f'tpad=start={padding_frames}:start_mode=clone',
             '-c:v', 'ffv1', str(target)],
        ]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        references = {'frames': {}}
        for index, start in enumerate((2, 4, 6, 8)):
            references['frames'][index] = media.capture_frames(
                {'url': str(source)}, [start, start+.5, start+1], self.root/f'真实参考-{index}', self.cancel)
        return {'url': str(target)}, references

    def test_real_24fps_rapid_cuts_with_seven_frame_intro_align_at_least_three_positions(self):
        target, references = self.rapid_cut_fixture(7)
        result = JobManager._align_frames(media, target, media.probe(target, self.cancel), references,
                                         self.root/'七帧片头对齐', self.cancel)
        self.assertGreaterEqual(len(result), 3, '同一内容增加七帧片头后仍应通过至少三个位置的原阈值')
        for key, frames in result.items():
            self.assertTrue(JobManager._sequence_matches(references['frames'][key], frames))

    def test_short_shots_between_coarse_grid_points_still_align_same_content(self):
        target, references = self.rapid_cut_fixture(121)
        # 固定的 .5 秒粗网格会跳过这组 .25 秒镜头；先独立证明四处确有同内容。
        for key, refs in references['frames'].items():
            start = refs[0]['actual_time'] + 5.125
            exact = media.capture_frames(target, [start, start+.5, start+1],
                                         self.root/f'错相已知同内容-{key}', self.cancel)
            self.assertTrue(JobManager._sequence_matches(refs, exact))
        result = JobManager._align_frames(media, target, media.probe(target, self.cancel), references,
                                         self.root/'错相短镜头对齐', self.cancel)
        self.assertGreaterEqual(len(result), 3, '粗网格漏过镜头时应有限补充错相采样')


if __name__ == '__main__':
    unittest.main()
