"""同内容匹配和本机无参考画质推理；实验结果不等同人工观感。"""
from __future__ import annotations

import importlib.util
import math
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / 'data' / 'models'
os.environ.setdefault('TORCH_HOME', str(MODEL_DIR / 'torch'))
os.environ.setdefault('HF_HOME', str(MODEL_DIR / 'huggingface'))
os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')


def fingerprint(image: Image.Image) -> np.ndarray:
    """仅用于判断是否同内容，不以清晰度或颜色直方图替代画质评分。"""
    gray = np.asarray(ImageOps.grayscale(image).resize((32, 18), Image.Resampling.BILINEAR), dtype=np.float32)
    vector = gray.ravel()
    vector -= vector.mean()
    length = np.linalg.norm(vector)
    return vector / length if length > 1e-6 else vector


def fingerprint_distance(a: Image.Image, b: Image.Image) -> float:
    return float(np.clip((1.0 - np.dot(fingerprint(a), fingerprint(b))) / 2.0, 0, 1))


def usable_frame(image: Image.Image) -> bool:
    data = np.asarray(ImageOps.grayscale(image).resize((64, 36)), dtype=np.float32)
    return bool(data.std() >= 8 and 8 <= data.mean() <= 247)


def best_match(reference: str, candidates: list[dict], max_distance: float = .12) -> dict | None:
    with Image.open(reference) as ref:
        if not usable_frame(ref):
            return None
        reference_vector = fingerprint(ref)
    ranked = []
    for frame in candidates:
        with Image.open(frame['path']) as image:
            if not usable_frame(image):
                continue
            distance = float((1 - np.dot(reference_vector, fingerprint(image))) / 2)
        ranked.append((distance, frame))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0])
    distance, frame = ranked[0]
    if distance > max_distance:
        return None
    return {**frame, 'match_distance': max(0., distance)}


def rank_common(rows: list[dict]) -> list[dict]:
    """仅在相同三个以上取样位置比较；失败结果不参与排序。"""
    output = [{**row, 'score': None, 'rank': None} for row in rows]
    eligible = [row for row in output if len(row.get('scores', {})) >= 3]
    for row in output:
        if row not in eligible:
            row.setdefault('message', '共同内容不足三个位置，未参与排名')
    if len({row['site'] for row in eligible}) < 2:
        for row in eligible:
            row['message'] = '可比较网站不足两个，无法生成跨站排名'
        return output
    common = set.intersection(*(set(row['scores']) for row in eligible))
    if len(common) < 3:
        for row in eligible:
            row['message'] = '所有来源的共同内容不足三个位置，未参与排名'
        return output
    for row in eligible:
        values = [float(row['scores'][key]) for key in sorted(common)]
        if not all(math.isfinite(value) for value in values):
            row['message'] = '模型返回无效分数，未参与排名'
            continue
        row['score'] = round(sum(values) / len(values), 2)
        row['samples'] = len(common)
        row['message'] = f'基于 {len(common)} 组共同画面的实验性估计；分数越高，模型预测画质越好'
    eligible = sorted([row for row in eligible if row['score'] is not None], key=lambda row: -row['score'])
    if len({row['site'] for row in eligible}) < 2:
        for row in eligible:
            row.update(score=None, message='有效评分的网站不足两个')
        return output
    # 同一网站保留所有线路；网站名次由最佳已检测线路决定。
    best_sites = {}
    for row in eligible:
        best_sites.setdefault(row['site'], row['score'])
    ranked_sites = sorted(best_sites, key=lambda site: -best_sites[site])
    site_ranks = {}
    last_score, last_rank = None, None
    for index, site in enumerate(ranked_sites, 1):
        score = best_sites[site]
        rank = last_rank if score == last_score else index
        site_ranks[site] = rank
        last_score, last_rank = score, rank
    for row in eligible:
        row['rank'] = site_ranks[row['site']]
        row['best_road'] = row['score'] == best_sites[row['site']]
    return sorted(output, key=lambda row: (row.get('rank') or 9999, -(row.get('score') or 0)))


def cpu_inference_threads() -> int:
    """根据逻辑 CPU 数选择推理线程，限制低核机器的资源占用。"""
    logical_cpus = os.cpu_count() or 2
    return 8 if logical_cpus >= 12 else max(1, min(4, logical_cpus))


class QualityModel:
    """惰性加载真实 MUSIQ 模型，权重只缓存到本工具目录。"""
    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @staticmethod
    def installed() -> bool:
        return importlib.util.find_spec('pyiqa') is not None and importlib.util.find_spec('torch') is not None

    def load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    if not self.installed():
                        raise RuntimeError('尚未安装画质分析环境，请运行安装依赖脚本')
                    import torch
                    import pyiqa
                    torch.set_num_threads(cpu_inference_threads())
                    self._model = pyiqa.create_metric('musiq', device='cpu')
        return self._model

    def score(self, paths: list[str], cancel: threading.Event) -> list[float]:
        if cancel.is_set():
            raise RuntimeError('检测已取消')
        # 并行取帧可以重叠，共享 CPU 模型需串行推理，避免线程竞争和内存叠加。
        while not self._inference_lock.acquire(timeout=.1):
            if cancel.is_set():
                raise RuntimeError('检测已取消')
        try:
            if cancel.is_set():
                raise RuntimeError('检测已取消')
            model = self.load()
            import torch
            result = []
            batch = []

            def infer_batch():
                if cancel.is_set():
                    raise RuntimeError('检测已取消')
                with torch.inference_mode():
                    values = model(torch.stack(batch)).reshape(-1).tolist()
                if cancel.is_set():
                    raise RuntimeError('检测已取消')
                if len(values) != len(batch):
                    raise RuntimeError('画质模型返回的结果数量与画面数量不一致')
                if not all(math.isfinite(value) for value in values):
                    raise RuntimeError('画质模型返回无效结果')
                result.extend(values)
                batch.clear()

            for path in paths:
                if cancel.is_set():
                    raise RuntimeError('检测已取消')
                with Image.open(path) as original:
                    # 保留既有显示尺寸和全部模型 patch，不按分辨率、码率或预评分筛掉画面。
                    image = original.convert('RGB')
                    width = 960
                    height = max(1, round(image.height * width / image.width))
                    image = image.resize((width, height), Image.Resampling.BICUBIC)
                    tensor = torch.from_numpy(np.asarray(image, dtype=np.float32).copy() / 255).permute(2, 0, 1)
                # 宽高比不同则先完成上一批，不能为了凑批次拉伸、裁剪或填充画面。
                if batch and tensor.shape != batch[0].shape:
                    infer_batch()
                batch.append(tensor)
                # 每位置三帧可一次推理，并限制单次推理的内存和取消等待时间。
                if len(batch) == 3:
                    infer_batch()
            if batch:
                infer_batch()
            return result
        finally:
            self._inference_lock.release()


MODEL = QualityModel()
