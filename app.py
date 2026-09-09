"""本地画质助手入口，只监听本机，不依赖 Kazumi 源码。"""
from __future__ import annotations

import argparse
import json
import hashlib
import os
import secrets
import socket
import threading
import webbrowser
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from helper import rules
from helper.jobs import JobManager, error_message
from helper.quality import QualityModel

ROOT = Path(__file__).resolve().parent


class ConfigInput(BaseModel):
    rules_path: str = Field(min_length=1, max_length=4096)


class SearchInput(BaseModel):
    keyword: str = Field(min_length=1, max_length=150)
    rule_ids: list[int] | None = None


class AnalyzeInput(BaseModel):
    search_job_id: str
    candidate_ids: list[str] = Field(min_length=2, max_length=100)
    episode: float | None = Field(default=None, ge=0, le=100000)
    mode: Literal['fast', 'full'] = 'fast'


def create_app(data_dir: Path | None = None):
    directory = data_dir or ROOT / 'data'
    directory.mkdir(parents=True, exist_ok=True)
    config_file = directory / 'config.json'
    token = secrets.token_urlsafe(32)
    manager = JobManager(directory)
    state = {'rules_path':'', 'rules':[]}

    def load_config():
        candidates = []
        if config_file.exists():
            try:
                candidates.append(json.loads(config_file.read_text(encoding='utf-8'))['rules_path'])
            except (ValueError, KeyError, OSError):
                pass
        candidates.extend(rules.discover_rule_files())
        for path in dict.fromkeys(candidates):
            try:
                loaded = rules.load_rules(path)
                if loaded:
                    state.update(rules_path=path, rules=loaded)
                    return
            except (ValueError, OSError):
                continue
    load_config()
    application = FastAPI(title='本地画质助手', docs_url=None, redoc_url=None, openapi_url=None)
    application.state.manager = manager

    @application.middleware('http')
    async def local_only(request: Request, call_next):
        if request.url.hostname not in ('127.0.0.1','localhost','::1'):
            return JSONResponse({'detail':'仅允许本机访问'}, status_code=403)
        if request.method in ('POST','PUT','DELETE','PATCH'):
            origin = request.headers.get('origin')
            expected = f'{request.url.scheme}://{request.url.netloc}'
            if origin is not None and origin != expected:
                return JSONResponse({'detail':'拒绝跨站请求'}, status_code=403)
            if not secrets.compare_digest(request.headers.get('x-local-token',''), token):
                return JSONResponse({'detail':'会话已失效，请刷新页面'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @application.get('/api/config')
    def get_config():
        installed = QualityModel.installed()
        return dict(rules_path=state['rules_path'], discovered_paths=rules.discover_rule_files(),
                    rules=[dict(id=index,name=rule.get('name', f'规则 {index+1}')) for index,rule in enumerate(state['rules'])],
                    model_ready=installed, model_message='已安装本地模型环境，首次检测会加载权重' if installed else '尚未安装分析环境',
                    busy=manager.busy(), token=token, active_job=manager.current())

    @application.get('/api/diagnostics')
    def get_diagnostics():
        return manager.diagnostics.summary()

    @application.get('/api/recent')
    def get_recent():
        with manager.lock:
            if manager.jobs:
                job=next(reversed(manager.jobs.values()))
                return {'job':job.snapshot(),'live':True}
        report=manager.diagnostics.latest()
        if report and report['job']['status']=='running':
            report['job'].update(status='interrupted',message='上次服务退出时任务尚未完成；以下是中断前的最近记录，请重新搜索后检测')
            for row in report['job']['results']:
                if not row.get('rank') and row.get('status')!='未参与排名':
                    row.update(status='已中断',message='服务退出前尚未形成完整结果，无法续跑旧媒体会话')
        return {'job':report['job'] if report else None,'live':False}

    @application.post('/api/config')
    def save_config(body: ConfigInput):
        if manager.busy():
            raise HTTPException(409, '请先等待当前任务完成')
        path = Path(body.rules_path).expanduser().resolve()
        if path == config_file.resolve():
            raise HTTPException(400, '不能把工具配置文件当作规则文件')
        try:
            loaded = rules.load_rules(str(path))
        except Exception as error:
            raise HTTPException(400, error_message(error)) from error
        state.update(rules_path=str(path), rules=loaded)
        config_file.write_text(json.dumps({'rules_path':str(path)},ensure_ascii=False,indent=2), encoding='utf-8')
        return {'ok':True, 'count':len(loaded)}

    @application.post('/api/search')
    def start_search(body: SearchInput):
        keyword = body.keyword.strip()
        if not keyword:
            raise HTTPException(400, '请输入番剧名称')
        # 每次搜索读取一次最新规则快照，只读取，不修改原文件。
        try:
            if state['rules_path']:
                state['rules'] = rules.load_rules(state['rules_path'])
            ids = body.rule_ids if body.rule_ids is not None else list(range(len(state['rules'])))
            ids = list(dict.fromkeys(ids))
            if not ids or any(index < 0 or index >= len(state['rules']) for index in ids):
                raise ValueError('请选择有效的网站规则')
            job = manager.search(state['rules'], keyword, ids)
            return {'job_id':job.id}
        except Exception as error:
            raise HTTPException(409 if manager.busy() else 400, error_message(error)) from error

    @application.get('/api/jobs/{job_id}')
    def get_job(job_id: str):
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, '任务不存在或已过期')
        return job.snapshot()

    @application.post('/api/jobs/{job_id}/cancel')
    def cancel_job(job_id: str):
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, '任务不存在')
        job.cancel.set()
        if job.snapshot()['status']=='running':
            job.update(message='正在取消并释放资源，请稍候')
        return {'ok':True}

    @application.post('/api/analyze')
    def start_analyze(body: AnalyzeInput):
        source = manager.jobs.get(body.search_job_id)
        if source is None or source.snapshot()['kind']!='search':
            raise HTTPException(400, '请先搜索番剧')
        if source.snapshot()['status']=='running':
            raise HTTPException(409, '请等待搜索完成')
        known_ids = {row['id'] for row in source.snapshot()['results']}
        if any(candidate_id not in known_ids for candidate_id in body.candidate_ids):
            raise HTTPException(400, '选择包含无效来源，请重新搜索')
        try:
            job = manager.analyze(source, set(body.candidate_ids), body.episode, body.mode)
            return {'job_id':job.id}
        except ValueError as error:
            raise HTTPException(409 if manager.busy() else 400, str(error)) from error

    @application.get('/api/health')
    def health():
        return {'app':'kazumi-quality-helper','ok':True,'instance':hashlib.sha256(str(ROOT).encode('utf-8')).hexdigest()[:12]}

    @application.get('/')
    def index():
        return FileResponse(ROOT/'web'/'index.html', media_type='text/html; charset=utf-8')

    application.mount('/static', StaticFiles(directory=ROOT/'web'), name='static')
    # 同时兼容 HTML 中的相对资源链接。
    @application.get('/style.css')
    def style():
        return FileResponse(ROOT/'web'/'style.css',media_type='text/css; charset=utf-8')
    @application.get('/app.js')
    def script():
        return FileResponse(ROOT/'web'/'app.js',media_type='application/javascript; charset=utf-8')
    return application


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='本机画质助手')
    parser.add_argument('--port',type=int,default=18765)
    parser.add_argument('--no-browser',action='store_true')
    args = parser.parse_args()
    import uvicorn
    url = f'http://127.0.0.1:{args.port}'
    if not args.no_browser:
        threading.Timer(1.5, lambda:webbrowser.open(url)).start()
    print(f'本地画质助手：{url}\n关闭此进程即可停止服务。', flush=True)
    uvicorn.run(create_app(), host='127.0.0.1', port=args.port, log_level='warning', access_log=False)
