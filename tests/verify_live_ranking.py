"""显式运行的公网集成验证；通过真实 API 搜索两个来源并检测。"""
import json
import sys
import time
from pathlib import Path
import requests

BASE = 'http://127.0.0.1:18765'

def wait_job(job_id, timeout=1200):
    deadline = time.monotonic()+timeout
    previous = None
    while time.monotonic()<deadline:
        job = requests.get(BASE+f'/api/jobs/{job_id}',timeout=10).json()
        summary = (job['message'], [(row.get('site'),row.get('road'),row.get('status')) for row in job['results']])
        if summary!=previous:
            print(json.dumps({'status':job['status'],'message':job['message'],'progress':job['progress'],'rows':summary[1]},ensure_ascii=False),flush=True)
            previous = summary
        if job['status']!='running':
            return job
        time.sleep(2)
    raise TimeoutError('实际来源验证超时')

if __name__=='__main__':
    config = requests.get(BASE+'/api/config',timeout=10).json()
    headers = {'X-Local-Token':config['token']}
    sites = sys.argv[1:] or ['7sefun','sorani']
    ids = [rule['id'] for rule in config['rules'] if rule['name'] in sites]
    search = requests.post(BASE+'/api/search',json={'keyword':'葬送的芙莉莲','rule_ids':ids},headers=headers,timeout=10)
    search.raise_for_status()
    found = wait_job(search.json()['job_id'],120)
    selected = [row['id'] for row in found['results'] if row['selected']]
    started = requests.post(BASE+'/api/analyze',json={'search_job_id':found['id'],'candidate_ids':selected,'episode':1},headers=headers,timeout=10)
    started.raise_for_status()
    print('分析任务编号 '+started.json()['job_id'],flush=True)
    result = wait_job(started.json()['job_id'])
    path = Path(__file__).resolve().parents[1]/'data'/'实际排名验证.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'results':[{key:row.get(key) for key in ['site','road','status','rank','score','samples','message']} for row in result['results']]},ensure_ascii=False),flush=True)
    sys.exit(0 if result['status']=='completed' and len({row['site'] for row in result['results'] if row.get('rank')})>=2 else 1)
