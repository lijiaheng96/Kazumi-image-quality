"""显式执行才访问全部已配置源站，验证智能筛选和真实模型结果。"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from helper.diagnostics import clean, redact

BASE='http://127.0.0.1:18765'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keyword',required=True,help='完整番剧名称，只使用唯一同名匹配')
    parser.add_argument('--episode',type=float,default=1,help='验证集数')
    args=parser.parse_args()
    with requests.Session() as session:
        session.trust_env=False
        config=session.get(BASE+'/api/config',timeout=10).json()
        if config['busy']:
            raise SystemExit('已有用户任务，未启动验证')
        session.headers['X-Local-Token']=config['token']

        def post(path,body):
            response=session.post(BASE+path,json=body,timeout=10)
            response.raise_for_status()
            return response.json()

        def wait(job_id):
            deadline=time.monotonic()+900
            last_print=0
            while time.monotonic()<deadline:
                response=session.get(BASE+'/api/jobs/'+job_id,timeout=10)
                response.raise_for_status()
                job=response.json()
                if job['status']!='running' or time.monotonic()-last_print>=20:
                    print(json.dumps({'类型':job['kind'],'耗时':job['elapsed_seconds'],
                                      '进度':job['progress'],'提示':redact(job['message'])},ensure_ascii=False),flush=True)
                    last_print=time.monotonic()
                if job['status']!='running':
                    return job
                time.sleep(2)
            post('/api/jobs/'+job_id+'/cancel',{})
            raise TimeoutError('验证达到 15 分钟上限，已请求取消本次验证任务')

        # 省略规则编号，后端重新读取本机配置后搜索全部规则。
        search=wait(post('/api/search',{'keyword':args.keyword})['job_id'])
        if search['status']!='completed':
            raise RuntimeError('搜索未完成，请查看本地诊断')
        chosen=[row for row in search['results'] if row.get('selected')]
        print(json.dumps({'已搜索规则数':len(search.get('searched_rule_ids',[])),
                          '唯一同名匹配':[{key:row[key] for key in ('site','title')} for row in chosen]},ensure_ascii=False),flush=True)
        if len(chosen)<2:
            raise RuntimeError('唯一同名匹配不足两个网站，需要先人工确认条目')
        result=wait(post('/api/analyze',{'search_job_id':search['id'],'candidate_ids':[row['id'] for row in chosen],
                                        'episode':args.episode,'mode':'smart'})['job_id'])
        (ROOT/'data'/'智能比较实测.json').write_text(json.dumps({'search':clean(search),'analysis':clean(result)},
                                                             ensure_ascii=False,indent=2),encoding='utf-8')
        rows=result['results']
        selected=[row for row in rows if row.get('shortlist_rank')]
        low=[row for row in rows if row.get('selection_status')=='lower_resolution']
        ranked=[{key:row.get(key) for key in ('site','road','rank','score','samples','resolution')} for row in rows if row.get('rank')]
        summary={'状态':result['status'],'检测耗时秒':result['elapsed_seconds'],'最高档':result.get('highest_resolution'),
                 '预选网站数':result.get('shortlist_count'),'补位次数':result.get('backup_attempts'),
                 '低档跳过线路数':len(low),'排名':ranked}
        print(json.dumps(summary,ensure_ascii=False),flush=True)
        assert result['shortlist_count']<=3 and result['backup_attempts']<=2
        assert len(selected)<=5 and len({row['site'] for row in selected})==len(selected)
        skipped={'体积估算','参考取样','取样与对齐','评分'}
        assert all(row.get('score') is None and not row.get('samples')
                   and skipped.isdisjoint(row.get('timings',{})) for row in low)
        assert result['status']=='completed' and len({row['site'] for row in ranked})>=2,'未形成跨站排名，请检查实测记录'


if __name__=='__main__':
    main()
