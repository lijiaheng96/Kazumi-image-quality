"""离线比较 MUSIQ 逐帧与小批次推理；不改变取样、尺寸或模型参数。"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', help='本地同尺寸图片，省略时使用 data/取帧预检')
    parser.add_argument('--frames', type=int, default=6, help='循环复用图片组成的比较帧数')
    parser.add_argument('--repeats', type=int, default=2, help='交换顺序进行的重复次数')
    parser.add_argument('--batches', type=int, nargs='+', default=[1, 2, 3])
    args = parser.parse_args()
    if args.frames < 1 or args.repeats < 1 or any(size < 1 for size in args.batches):
        parser.error('帧数、重复次数与批次大小必须是正整数')
    if args.batches[0] != 1:
        parser.error('第一个比较批次必须为 1，以逐帧结果作为分数基准')
    # 缺少缓存即结束，基准本身不会为性能比较下载模型。
    if not list((ROOT / 'data' / 'models').rglob('musiq_koniq_ckpt-e95806b9.pth')):
        parser.error('缺少已缓存的 MUSIQ 权重，请先完成正常环境安装')
    paths = [Path(path) for path in args.paths] or sorted((ROOT / 'data' / '取帧预检').rglob('*.png'))
    if not paths:
        parser.error('没有本地图片，请传入图片路径')

    import numpy as np
    import psutil
    import torch
    from PIL import Image
    from helper.quality import QualityModel

    tensors = []
    for index in range(args.frames):
        with Image.open(paths[index % len(paths)]) as original:
            image = original.convert('RGB')
            image = image.resize((960, max(1, round(image.height * 960 / image.width))), Image.Resampling.BICUBIC)
            tensors.append(torch.from_numpy(np.asarray(image, dtype=np.float32).copy() / 255).permute(2, 0, 1))
    if len({tuple(tensor.shape) for tensor in tensors}) != 1:
        parser.error('本基准要求图片宽高比一致，实际评分需将不同尺寸分批')

    start = time.perf_counter()
    quality = QualityModel()
    model = quality.load()
    load_seconds = time.perf_counter() - start
    process = psutil.Process()
    print(json.dumps({'阶段': '环境', 'torch': torch.__version__, '线程数': torch.get_num_threads(),
                      '帧数': args.frames, '图片数': len(paths), '加载秒': round(load_seconds, 3)}, ensure_ascii=False), flush=True)
    timings = []
    baseline = None
    with torch.inference_mode():
        model(tensors[0].unsqueeze(0))
        for repeat in range(args.repeats):
            order = args.batches if repeat % 2 == 0 else list(reversed(args.batches))
            for batch in order:
                start = time.perf_counter()
                scores = []
                for offset in range(0, len(tensors), batch):
                    scores.extend(model(torch.stack(tensors[offset:offset + batch])).reshape(-1).tolist())
                elapsed = time.perf_counter() - start
                if baseline is None:
                    baseline = scores
                max_difference = max(abs(a - b) for a, b in zip(scores, baseline))
                measurement = {'重复': repeat + 1, '批次': batch, '秒': round(elapsed, 4),
                               '每帧秒': round(elapsed / args.frames, 4), '最大分差': max_difference,
                               '进程内存MiB': round(process.memory_info().rss / 1024 ** 2, 1), '分数': scores}
                timings.append(measurement)
                print(json.dumps(measurement, ensure_ascii=False), flush=True)
                if len(scores) != args.frames or not all(np.isfinite(scores)) or max_difference > .001:
                    raise RuntimeError('批量推理未通过逐帧分数一致性检查')
    start = time.perf_counter()
    actual = quality.score([str(paths[index % len(paths)]) for index in range(args.frames)], threading.Event())
    difference = max(abs(a - b) for a, b in zip(actual, baseline))
    print(json.dumps({'阶段': '生产评分', '秒': round(time.perf_counter() - start, 4),
                      '最大分差': difference, '包含图片读取与尺寸转换': True}, ensure_ascii=False), flush=True)
    if len(actual) != args.frames or not all(np.isfinite(actual)) or difference > .001:
        raise RuntimeError('生产评分未通过逐帧分数一致性检查')
    print(json.dumps({'阶段': '完成', '测量数': len(timings)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
