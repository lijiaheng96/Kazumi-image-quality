"""显式运行才访问已配置源站，保存新版本阶段计时供本机诊断。"""
import json
import sys
import time
from pathlib import Path

import requests

BASE='http://127.0.0.1:18765'


def wait(job_id):
    previous=None
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        response=requests.get(f'{BASE}/api/jobs/{job_id}',timeout=10)
        response.raise_for_status()
        job=response.json()
        summary=(job['message'],[(row['site'],row['road'],row['status']) for row in job['results']])
        if summary!=previous:
            print(json.dumps({'elapsed':job['elapsed_seconds'],'message':job['message'],'rows':summary[1]},ensure_ascii=False),flush=True)
            previous=summary
        if job['status']!='running':
            return job
        time.sleep(2)
    raise TimeoutError('本次公网验证超过15分钟，请在界面检查任务')


if __name__=='__main__':
    config=requests.get(BASE+'/api/config',timeout=10).json()
    if config['busy']:
        raise SystemExit('已有用户任务，未启动验证')
    headers={'X-Local-Token':config['token']}
    ids=[rule['id'] for rule in config['rules'] if rule['name'] in ('7sefun','moonci')]
    response=requests.post(BASE+'/api/search',json={'keyword':'葬送的芙莉莲','rule_ids':ids},headers=headers,timeout=10)
    response.raise_for_status()
    # 搜索行没有road，因此独立轮询。
    while True:
        found=requests.get(BASE+'/api/jobs/'+response.json()['job_id'],timeout=10).json()
        if found['status']!='running':
            break
        time.sleep(.5)
    chosen=[row['id'] for row in found['results'] if row['selected']]
    response=requests.post(BASE+'/api/analyze',json={'search_job_id':found['id'],'candidate_ids':chosen,'episode':1,'mode':'fast'},headers=headers,timeout=10)
    response.raise_for_status()
    result=wait(response.json()['job_id'])
    root=Path(__file__).resolve().parents[1]
    (root/'data'/'优化后实际验证.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    ranked={row['site'] for row in result['results'] if row.get('rank')}
    print(json.dumps({'status':result['status'],'seconds':result['elapsed_seconds'],'ranked_sites':sorted(ranked)},ensure_ascii=False),flush=True)
    sys.exit(0 if result['status']=='completed' and len(ranked)>=2 else 1)
