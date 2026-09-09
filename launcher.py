"""双击入口：后台启动本机服务，重复点击只打开已有页面。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:18765'
IDENTITY = hashlib.sha256(str(ROOT).encode('utf-8')).hexdigest()[:12]
PID_FILE = ROOT/'data'/'server.json'


def owns_command(command: list[str]) -> bool:
    """只接受直接执行本目录 app.py 的命令，不接受 -c 内出现相似路径。"""
    if len(command)<2 or '-c' in command or '-m' in command:
        return False
    script = next((value for value in command[1:] if value.lower().endswith('.py')), '')
    return bool(script) and Path(script).resolve()==(ROOT/'app.py').resolve()


def notify(message, error=False):
    if os.name=='nt':
        ctypes.windll.user32.MessageBoxW(None, message, '本地画质助手', 0x10 if error else 0x40)
    else:
        print(message)


def health():
    try:
        # 明确绕过环境代理，本机服务无需代理。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL+'/api/health',timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def stop():
    from filelock import FileLock
    (ROOT/'data').mkdir(exist_ok=True)
    with FileLock(ROOT/'data'/'launcher.lock',timeout=15):
        _stop_locked()


def _stop_locked():
    if not PID_FILE.exists():
        notify('没有找到由此目录启动的服务。')
        return
    import psutil
    try:
        record = json.loads(PID_FILE.read_text(encoding='utf-8'))
        process = psutil.Process(record['pid'])
        if not owns_command(process.cmdline()) or abs(process.create_time()-record['created']) > .01:
            raise ValueError('记录的进程已改变，未停止任何程序')
        children = process.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        process.terminate()
        _, alive = psutil.wait_procs([*children,process],timeout=3)
        for remaining in alive:
            remaining.kill()
        PID_FILE.unlink(missing_ok=True)
        notify('画质助手已停止。浏览器页面可以关闭。')
    except psutil.NoSuchProcess:
        PID_FILE.unlink(missing_ok=True)
        notify('画质助手已经停止。')


def start():
    from filelock import FileLock
    (ROOT/'data').mkdir(exist_ok=True)
    with FileLock(ROOT/'data'/'launcher.lock',timeout=15):
        _start_locked()


def _start_locked():
    existing = health()
    if existing:
        if existing.get('instance') != IDENTITY:
            raise ValueError('18765 端口已有其他服务或其他目录的画质助手。请先关闭它再启动。')
        webbrowser.open(URL)
        return
    python = ROOT/'.venv'/'Scripts'/'python.exe'
    if not python.exists():
        raise ValueError('缺少本工具运行环境，请先运行 setup.ps1 安装依赖。')
    data = ROOT/'data'
    data.mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    flags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (data/'运行日志.txt').open('a',encoding='utf-8') as log:
        process = subprocess.Popen([str(python),'-X','utf8',str(ROOT/'app.py'),'--no-browser'],cwd=ROOT,env=env,
                                   stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags)
    import psutil
    for _ in range(50):
        if process.poll() is not None:
            raise ValueError('服务启动失败，请查看 data/运行日志.txt。')
        status = health()
        if status and status.get('instance')==IDENTITY:
            record = {'pid':process.pid,'created':psutil.Process(process.pid).create_time()}
            pending = PID_FILE.with_suffix('.pending')
            pending.write_text(json.dumps(record),encoding='utf-8')
            os.replace(pending, PID_FILE)
            webbrowser.open(URL)
            return
        time.sleep(.2)
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    raise ValueError('服务启动超时，本次进程已结束。请查看 data/运行日志.txt 后重试。')


if __name__=='__main__':
    try:
        stop() if '--stop' in sys.argv else start()
    except Exception as error:
        notify(str(error),error=True)
        sys.exit(1)
