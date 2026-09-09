"""按真实媒体时间匹配三个连续画面，分级搜索并复用已解码的匹配帧。"""
from bisect import bisect_left
import math

import numpy as np
from PIL import Image

from . import quality


def timestamp(frame):
    return frame.get('actual_time', frame['time'])


def sequence_distances(refs, frames):
    if len(refs) != 3 or len(frames) != 3:
        return None
    # 不把解码器迟到的同一帧冒充三个不同时间点；允许两种帧率间的小幅量化差。
    for index in (1, 2):
        reference_gap = timestamp(refs[index]) - timestamp(refs[0])
        candidate_gap = timestamp(frames[index]) - timestamp(frames[0])
        if reference_gap <= 0 or candidate_gap <= 0 or abs(reference_gap-candidate_gap) > .1:
            return None
    distances = []
    for ref, frame in zip(refs, frames):
        with Image.open(ref['path']) as a, Image.open(frame['path']) as b:
            if not quality.usable_frame(a) or not quality.usable_frame(b):
                return None
            distances.append(quality.fingerprint_distance(a, b))
    return distances


def acceptable(distances):
    return distances is not None and max(distances) <= .12 and sum(distances)/3 <= .08


def sequence_matches(refs, frames):
    return acceptable(sequence_distances(refs, frames))


def _vectors(frames):
    result = []
    for frame in frames:
        with Image.open(frame['path']) as image:
            result.append(quality.fingerprint(image) if quality.usable_frame(image) else None)
    return result


def _distance(a, b):
    return float(np.clip((1-np.dot(a, b))/2, 0, 1)) if a is not None and b is not None else 1.


def sequence_candidates(refs, frames):
    """用完整三帧排序候选，重复出现的首帧不会遮住真正的匹配片段。"""
    frames = sorted(frames, key=timestamp)
    times = [timestamp(frame) for frame in frames]
    vectors, ref_vectors = _vectors(frames), _vectors(refs)
    gaps = [timestamp(ref)-timestamp(refs[0]) for ref in refs]
    candidates = []
    for index, start in enumerate(times):
        if _distance(ref_vectors[0], vectors[index]) > .12:
            continue
        indices = [index]
        for gap in gaps[1:]:
            target = start + gap
            slot = bisect_left(times, target)
            choices = [i for i in (slot-1, slot) if 0 <= i < len(times)]
            selected = min(choices, key=lambda i: abs(times[i]-target))
            if abs(times[selected]-target) > .075:
                break
            indices.append(selected)
        if len(indices) != 3 or len(set(indices)) != 3:
            continue
        distances = [_distance(a, vectors[i]) for a, i in zip(ref_vectors, indices)]
        if acceptable(distances):
            candidates.append((sum(distances), frames[index]))
    return [frame for _, frame in sorted(candidates, key=lambda item:(item[0],timestamp(item[1])))[:3]]


def align_frames(media, resolved, metadata, reference, directory, cancel, trace=None):
    from .media import _check_cancel
    aligned, offset = {}, 0.
    fps = metadata.get('fps') or 24
    fps = min(40., max(24., fps)) if math.isfinite(fps) else 24.
    for key, refs in reference['frames'].items():
        _check_cancel(cancel)
        base = timestamp(refs[0])
        gaps = [timestamp(ref)-base for ref in refs]
        estimated = max(0., min(metadata['duration']-2, base+offset))
        event = dict(position=key, reference_times=[round(timestamp(ref),6) for ref in refs],
                     matched=False, method='直接校验')
        if trace is not None:
            trace.append(event)

        def verify(found, name):
            frames = media.capture_frames(resolved, [found+gap for gap in gaps], directory/name, cancel)
            distances = sequence_distances(refs, frames)
            event.update(candidate_times=[round(timestamp(frame),6) for frame in frames],
                         distances=[round(value,4) for value in distances] if distances else [])
            return frames if acceptable(distances) else None

        def search_window(start, duration, name):
            _check_cancel(cancel)
            duration = min(duration, metadata['duration']-start-.1)
            if duration <= 1:
                return None
            frames = media.capture_sequence(resolved, start, duration, 1/fps, directory/name, cancel, width=192)
            for index, candidate in enumerate(sequence_candidates(refs, frames)):
                # 浮点表示可能落在原PTS前一微秒；向前取整到微秒可避免错到下一帧。
                found = math.floor(timestamp(candidate)*1_000_000)/1_000_000
                verified = verify(found, f'{name}-确认-{index}')
                if verified:
                    return verified
            return None

        frames = verify(estimated, f'direct-{key}')
        if frames is None:
            event['method'] = '邻近三帧搜索'
            frames = search_window(max(0.,estimated-1.25), 3.75, f'near-{key}')
        if frames is None:
            event['method'] = '片段偏移搜索'
            start = max(0., estimated-40)
            duration = min(82., metadata['duration']-start-1)
            ref_vector = _vectors(refs[:1])[0]
            searched = []
            for phase in (0, .25):
                # 首轮无法定位才补半个采样间隔，防止快速切镜头恰好被粗采样全部跳过。
                coarse = media.capture_sequence(resolved, start+phase, duration-phase, .5,
                                                directory/f'coarse-{key}-{phase}', cancel, width=192)
                ranked = sorted(zip(coarse, _vectors(coarse)), key=lambda item:_distance(ref_vector,item[1]))
                anchors = []
                for candidate, vector in ranked:
                    if _distance(ref_vector, vector) > .12:
                        break
                    if all(abs(timestamp(candidate)-anchor) >= .75 for anchor in anchors+searched):
                        anchors.append(timestamp(candidate))
                    if len(anchors) >= 3:
                        break
                for index, anchor in enumerate(anchors):
                    searched.append(anchor)
                    frames = search_window(max(0.,anchor-.75), 2.75, f'fine-{key}-{phase}-{index}')
                    if frames:
                        break
                if frames:
                    break
        if frames:
            offset = timestamp(frames[0])-base
            aligned[key] = frames
            event.update(matched=True,message='三个连续画面校验通过')
        else:
            event['message'] = '在限定窗口内未找到通过三帧校验的同内容片段'
    return aligned
