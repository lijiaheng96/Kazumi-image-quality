"""汇总已有本地记录；不访问网络，不输出地址、凭据或本机配置路径。"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def safe_text(value):
    text = str(value)
    text = re.sub(r'(?i)(?:https?|ftp|file)://[^\s<>]+', '[地址已省略]', text)
    if re.search(r'(?i)(?:[a-z]:[\\/]|\\\\[^\\]|(?<![\w:])/(?:[^/\s]+/)+)', text):
        return '[已省略含本机路径的文本]'
    if re.search(r'(?i)(?:cookie|authorization|password|passwd|token|api[_-]?key|secret|signature|session|username|密码|令牌|凭据|账号)\s*[:=]|\bbearer\s+', text):
        return '[已省略含凭据信息的文本]'
    return text.replace('|', '／').replace('\r', ' ').replace('\n', ' ')[:1000]


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def seconds(value):
    return f'{value:.3f}' if number(value) else '未记录'


def rows_of(value):
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ('records', 'results', 'sources'):
            if isinstance(value.get(key), list):
                return rows_of(value[key])
    return []


def build_report(directory: Path) -> str:
    lines = ['# 本地检测诊断汇总', '',
             f'生成时间：{datetime.now().astimezone().isoformat(timespec="seconds")}', '',
             '仅汇总已有本地记录，不访问公网。媒体地址、请求头、Cookie、凭据和本机配置路径均不进入本报告。', '']

    def paragraph(text):
        lines.extend([text, ''])

    def table(headers, rows):
        lines.append('| ' + ' | '.join(headers) + ' |')
        lines.append('| ' + ' | '.join('---' for _ in headers) + ' |')
        lines.extend('| ' + ' | '.join(safe_text(cell) for cell in row) + ' |' for row in rows)
        lines.append('')

    def read(path):
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            paragraph(f'{safe_text(path.name)} 无法读取为 JSON，已跳过；不据此推断检测结果。')
            return None

    paragraph('## 旧搜索与媒体记录')
    search_path = directory / '搜索验证.json'
    if search_path.exists():
        search = rows_of(read(search_path))
        failed = [row for row in search if row.get('error')]
        found = [row for row in search if not row.get('error') and row.get('items')]
        empty = [row for row in search if not row.get('error') and not row.get('items')]
        paragraph(f'搜索验证.json 共记录 {len(search)} 个来源：{len(found)} 个返回条目，'
                  f'{len(failed)} 个失败，{len(empty)} 个未返回条目。')
        table(['站点', '状态', '返回条目数', '已记录原因', '耗时／秒'], [
            [row.get('site', '未记录'), '失败' if row.get('error') else '有结果' if row.get('items') else '无结果',
             len(row.get('items', [])) if isinstance(row.get('items', []), list) else '未记录',
             row.get('error', '—'), seconds(row.get('elapsed_seconds'))] for row in search])
        if not any(number(row.get('elapsed_seconds')) or row.get('timings') for row in search):
            paragraph('旧搜索记录未保存总耗时与阶段耗时，无法回推。各站条目数量不能代替搜索耗时。')
    else:
        paragraph('未找到搜索验证.json。')

    media_path = directory / '媒体验证.json'
    if media_path.exists():
        media = rows_of(read(media_path))
        table(['媒体验证站点', '线路', '媒体时长／秒', '分辨率', '编码', '码率／bit/s', '已记录错误'], [
            [row.get('site', '未记录'), row.get('road', '未记录'),
             (row.get('metadata') or {}).get('duration', '未记录'),
             f"{row['metadata']['width']} × {row['metadata']['height']}" if isinstance(row.get('metadata'), dict)
             and 'width' in row['metadata'] and 'height' in row['metadata'] else '未记录',
             (row.get('metadata') or {}).get('codec', '未记录'),
             (row.get('metadata') or {}).get('bitrate') or '未记录', row.get('error', '—')] for row in media])
        paragraph('上表时长是视频内容长度，不是探测或任务耗时。媒体元数据成功只证明这些记录中的线路可探测，'
                  '不能扩展为所有搜索站点可播放、可取帧或可排名。码率缺失不作画质失败判定。')
        if not any(number(row.get('elapsed_seconds')) or row.get('timings') for row in media):
            paragraph('旧媒体验证未保存总耗时与阶段耗时，无法回推。')

    def job_rows(job):
        rows = rows_of(job)
        if rows and job.get('kind') == 'search':
            table(['搜索站点', '条目标题'], [
                [row.get('site', '未记录'), row.get('title', '未记录')] for row in rows])
        elif rows:
            table(['站点', '线路', '状态', '名次', '模型分数', '共同样本数', '已记录原因'], [
                [row.get('site', '未记录'), row.get('road', '未记录'), row.get('status', '未记录'),
                 row.get('rank') if row.get('rank') is not None else '未排名',
                 row.get('score') if number(row.get('score')) else '未评分',
                 row.get('samples', '未记录'), row.get('message', '—')] for row in rows])
        errors = job.get('errors', [])
        if isinstance(errors, list) and errors:
            table(['任务错误站点', '已记录原因'], [
                [error.get('site', '未记录'), error.get('message', error.get('error', '未记录'))]
                if isinstance(error, dict) else ['未记录', error] for error in errors])

    paragraph('## 旧排名记录')
    ranking_paths = [directory / name for name in
                     ('实际排名验证.json', '浏览器排名验证.json', '实际排名.json', '浏览器排名.json')]
    for path in ranking_paths:
        if not path.exists():
            continue
        job = read(path)
        if not isinstance(job, dict):
            continue
        paragraph(f'### {safe_text(path.name)}')
        paragraph(f"任务状态：{safe_text(job.get('status', '未记录'))}；"
                  f"总耗时／秒：{seconds(job.get('elapsed_seconds'))}。")
        job_rows(job)
        if not number(job.get('elapsed_seconds')):
            paragraph('旧记录未保存总耗时与阶段耗时，无法回推；不能用文件修改时间、媒体时长或分数倒推任务速度。')
        ranked_sites = {row.get('site') for row in rows_of(job) if row.get('rank') is not None}
        paragraph(f'这份记录有 {len(ranked_sites)} 个站点取得名次。任务完成状态只表示流程结束，'
                  '各线路是否成功及能否跨站排名以上述行状态为准；不同份记录不混为同一次比较。')

    paragraph('## 新版任务计时')
    diagnostics = []
    for path in sorted((directory / 'diagnostics').glob('*.json')):
        report = read(path)
        if isinstance(report, dict) and isinstance(report.get('job'), dict):
            diagnostics.append((path, report))
    paragraph(f'找到 {len(diagnostics)} 份可读取的新版任务诊断。'
              '并发阶段累计耗时可能大于任务墙钟时间；阶段累计不能相加冒充端到端耗时。')
    if diagnostics:
        table(['记录文件', '任务类型', '任务状态', '开始时间', '总耗时／秒'], [
            [path.name, report['job'].get('kind', '未记录'), report['job'].get('status', '未记录'),
             report['job'].get('started_at', '未记录'), seconds(report['job'].get('elapsed_seconds'))]
            for path, report in diagnostics])
    else:
        paragraph('当前无新版阶段计时数据，无法判定历史运行的时间具体花在请求、取帧还是模型推理。')
    stages = defaultdict(list)
    failures = []
    for path, report in diagnostics:
        paragraph(f'### {safe_text(path.name)}')
        job_rows(report['job'])
        events = rows_of(report.get('events', []))
        if events:
            table(['阶段', '站点', '线路', '耗时／秒', '状态', '已记录原因'], [
                [event.get('stage', '未记录'), event.get('site', '—'), event.get('road', '—'),
                 seconds(event.get('seconds')), event.get('status', '未记录'),
                 event.get('message', '—')] for event in events])
        for event in events:
            if number(event.get('seconds')):
                stages[str(event.get('stage', '未记录'))].append(event['seconds'])
            if event.get('status') == 'failed':
                failures.append([event.get('site', '未记录'), event.get('stage', '未记录'),
                                 event.get('message', '未记录')])
    if stages:
        table(['阶段累计（所有上述任务）', '次数', '累计秒', '平均秒'], [
            [stage, len(values), seconds(sum(values)), seconds(sum(values) / len(values))]
            for stage, values in sorted(stages.items(), key=lambda item: -sum(item[1]))])
    if failures:
        table(['失败站点', '失败阶段', '已记录原因'], failures)

    paragraph('## 本机基准记录')
    benchmarks = sorted(directory.rglob('*benchmark*.json'))
    metric_names = {'seconds': '耗时秒', '秒': '耗时秒', 'elapsed_seconds': '耗时秒',
                    'batch_size': '每批帧数', '批次': '每批帧数', '重复': '重复序号',
                    'frames': '帧数', '帧数': '帧数', 'processes': '进程数',
                    'http_requests': 'HTTP请求数', 'http_bytes': 'HTTP字节数', 'png_bytes': 'PNG字节数',
                    '每帧秒': '每帧秒', '最大分差': '最大分差', 'max_difference': '最大分差',
                    'max_abs_diff': '最大分差', '进程内存MiB': '进程内存MiB',
                    'load_seconds': '加载秒', '加载秒': '加载秒', '线程数': '线程数',
                    'mean_seconds': '均值秒', 'median_seconds': '中位数秒',
                    '计时次数': '计时次数', '最短秒': '最短秒', '最长秒': '最长秒',
                    '逻辑CPU数': '逻辑CPU数', '图片数': '图片数', '可用内存MiB': '可用内存MiB',
                    '系统CPU使用率百分比': '系统CPU使用率百分比'}

    def benchmark_details(value, trail=()):
        if isinstance(value, dict):
            metrics = {metric_names[key]: item for key, item in value.items()
                       if key in metric_names and number(item)}
            if metrics:
                metric_records.append(('／'.join(trail) or '顶层', metrics))
            for key in ('说明', 'note', 'limitations', '局限', '阶段'):
                if isinstance(value.get(key), str):
                    paragraph(safe_text(value[key]))
            for key in ('torch', '开始时间', '结束时间', '转存时间'):
                if isinstance(value.get(key), str):
                    paragraph(f'{key}：{safe_text(value[key])}')
            if isinstance(value.get('耗时中位数秒'), dict):
                table(['基准方案', '记录的耗时中位数／秒'], [
                    [key, item] for key, item in value['耗时中位数秒'].items() if number(item)])
            if isinstance(value.get('逐像素一致'), bool):
                paragraph(f"逐像素一致：{'是' if value['逐像素一致'] else '否'}。")
            for key, item in value.items():
                # 只下钻基准结果容器，不输出任意元数据或未知字符串字段。
                if key in ('samples', 'runs', 'records', 'results', 'measurements', 'environment',
                           '样本', '测量', '计时', '环境', '生产评分', 'benchmarks', '汇总', '补充验证'):
                    if isinstance(item, dict):
                        if any(name in metric_names or name in ('说明', 'torch') for name in item):
                            benchmark_details(item, trail + (safe_text(key),))
                        else:
                            for name, child in item.items():
                                benchmark_details(child, trail + (safe_text(name),))
                    else:
                        benchmark_details(item, trail + (safe_text(key),))
        elif isinstance(value, list):
            for index, item in enumerate(value, 1):
                benchmark_details(item, trail + (str(index),))

    if not benchmarks:
        paragraph('没有已保存的基准 JSON；未写入文件的终端计时不在本报告中重建。')
    for path in benchmarks:
        paragraph(f'### {safe_text(path.name)}')
        metric_records = []
        benchmark_details(read(path))
        metric_columns = list(dict.fromkeys(key for _, values in metric_records for key in values))
        if metric_columns:
            table(['记录位置'] + metric_columns, [[label] + [values.get(key, '—') for key in metric_columns]
                                                 for label, values in metric_records])
    paragraph('基准只代表记录中的输入、采样数、环境与当时机器负载。本机回环 HTTP 数据不能直接代表公网源站速度；'
              '纯推理耗时不包含搜索、解析、探测与取帧。未保存计时结果的终端实验不在本报告中重建。')

    paragraph('## 运行日志与解释边界')
    log = directory / '运行日志.txt'
    if log.exists():
        try:
            text = log.read_text(encoding='utf-8-sig')
            nonempty = [line for line in text.splitlines() if line.strip()]
            if nonempty and all(line.startswith(('本地画质助手：', '关闭此进程即可停止服务。')) for line in nonempty):
                paragraph(f"运行日志.txt 仅记录 {text.count('本地画质助手：')} 次启动提示及停止说明，"
                          '没有检测任务、站点失败或阶段时间，无法回推旧任务总耗时。')
            else:
                paragraph(f'运行日志.txt 有 {len(nonempty)} 行非空文本；没有已知结构化任务计时格式，'
                          '本脚本不将自由文本自动换算为总耗时，也不复制可能含敏感内容的原日志。')
        except OSError:
            paragraph('运行日志.txt 无法读取，未据此推断耗时。')
    else:
        paragraph('未找到运行日志.txt。')
    paragraph('搜索失败、媒体探测失败、提帧失败、共同内容不足与模型评分偏低是不同情况，不能互相替代。'
              '仅比较同一任务的共同内容；失败或缺少共同内容时不伪造排名。')
    paragraph('分辨率和码率仅作为媒体信息，不按其高低淘汰候选来源；模型分数不是正确率，'
              '模型尚未经过动画领域人工校准，细小分差不能保证等同主观观感差异。')
    return '\n'.join(lines)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def record(self, name, value):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_sensitive_text_is_removed(self):
        for text in ('请求失败 https://example.invalid/?token=private',
                     r'读取失败 C:\Users\private\settings.json', 'Cookie: private',
                     'Authorization: Bearer private', '密码=private'):
            self.assertNotIn('private', safe_text(text))
        self.assertIn('请求失败', safe_text('请求失败 https://example.invalid/?token=private'))

    def test_legacy_reports_do_not_invent_elapsed_time(self):
        self.record('搜索验证.json', [{'site': '甲', 'items': [{'title': '测试'}]},
                                       {'site': '乙', 'error': 'HTTP 403'}])
        self.record('实际排名验证.json', {'status': 'completed', 'results': [
            {'site': '甲', 'status': '已检测', 'rank': None,
             'message': '可比较网站不足两个', 'page_url': 'https://private.invalid'}]})
        report = build_report(self.directory)
        self.assertIn('HTTP 403', report)
        self.assertIn('无法回推', report)
        self.assertIn('可比较网站不足两个', report)
        self.assertNotIn('private', report)

    def test_parallel_stage_total_is_not_reported_as_wall_time(self):
        self.record('diagnostics/a.json', {'job': {'kind': 'analyze', 'status': 'completed',
                    'elapsed_seconds': 5, 'results': []}, 'events': [
                        {'stage': '取帧', 'site': '甲', 'seconds': 3, 'status': 'ok'},
                        {'stage': '取帧', 'site': '乙', 'seconds': 4, 'status': 'failed',
                         'message': 'HTTP 403'}]})
        report = build_report(self.directory)
        self.assertIn('5.000', report)
        self.assertIn('7.000', report)
        self.assertIn('并发阶段累计耗时可能大于任务墙钟时间', report)
        self.assertIn('HTTP 403', report)

    def test_benchmark_numbers_and_limitations_are_preserved(self):
        self.record('media-benchmark-test.json', {'说明': '回环 HTTP，无人工网络延迟',
                    '耗时中位数秒': {'独立': .54, '合并': .196}, '逐像素一致': True})
        self.record('quality-benchmark-test.json', {'runs': [
            {'批次': 1, '秒': 6.0379, '最大分差': 0},
            {'批次': 3, '秒': 4.0421, '最大分差': 0}]})
        report = build_report(self.directory)
        for value in ('0.54', '0.196', '6.0379', '4.0421', '回环 HTTP'):
            self.assertIn(value, report)
        self.assertIn('不能直接代表公网', report)

    def test_thread_summary_preserves_background_load_and_repeat_count(self):
        self.record('quality-threads-benchmark.json', {
            '环境': {'torch': '2.14.0+cpu', '逻辑CPU数': 14},
            'runs': [{'线程数': 8, '秒': 1.52, '系统CPU使用率百分比': 85.9}],
            '汇总': [{'线程数': 8, '计时次数': 2, 'median_seconds': 1.483191,
                    '最短秒': 1.446323, '最长秒': 1.520059}]})
        report = build_report(self.directory)
        for value in ('2.14.0+cpu', '85.9', '计时次数', '1.483191', '1.446323', '1.520059'):
            self.assertIn(value, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data', help='本地数据目录')
    parser.add_argument('--self-test', action='store_true', help='运行内置脱敏与统计测试')
    args = parser.parse_args()
    if args.self_test:
        result = unittest.main(argv=[sys.argv[0]], exit=False).result
        if not result.wasSuccessful():
            raise SystemExit(1)
        return
    target = args.data_dir / '本地检测诊断汇总.md'
    target.write_text(build_report(args.data_dir), encoding='utf-8')
    print('已生成本地检测诊断汇总.md')


if __name__ == '__main__':
    main()
