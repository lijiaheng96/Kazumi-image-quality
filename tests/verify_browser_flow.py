"""通过实际页面验证搜索、自动预选、跨站检测及结果展示。显式运行才访问源站。"""
import json
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        jobs=[]
        def response_seen(response):
            if response.url.endswith('/api/analyze') and response.status==200:
                jobs.append(response.json()['job_id'])
        page.on('response',response_seen)
        page.goto('http://127.0.0.1:18765')
        page.wait_for_load_state('networkidle')
        for checkbox in page.locator('#rule-list input').all():
            checkbox.set_checked(checkbox.get_attribute('value') in ('0','2'))
        page.locator('#keyword').fill('葬送的芙莉莲')
        page.locator('#search-button').click()
        deadline=time.monotonic()+90
        while not page.locator('#analyze-button').is_enabled():
            if time.monotonic()>deadline:
                raise RuntimeError('页面未自动选出两个可检测网站：'+page.locator('body').inner_text()[-1000:])
            page.wait_for_timeout(300)
        page.screenshot(path=str(root/'data'/'搜索页面验证.png'),full_page=True)
        print('页面搜索与自动预选通过',flush=True)
        page.locator('#analyze-button').click()
        deadline=time.monotonic()+1200
        previous=None
        while time.monotonic()<deadline:
            page.wait_for_timeout(1500)
            if not jobs:
                continue
            job=requests.get('http://127.0.0.1:18765/api/jobs/'+jobs[0],timeout=10).json()
            message=job['message']+' / '+', '.join(row.get('status','') for row in job['results'])
            if message!=previous:
                print(message,flush=True)
                previous=message
            if job['status']!='running':
                page.wait_for_timeout(2000)
                page.screenshot(path=str(root/'data'/'实际排名页面.png'),full_page=True)
                (root/'data'/'浏览器排名验证.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
                assert not errors,errors
                ranked={row['site'] for row in job['results'] if row.get('rank')}
                print(json.dumps({'status':job['status'],'ranked_sites':sorted(ranked),'rows':[{key:row.get(key) for key in ['site','road','rank','score','samples','message']} for row in job['results']]},ensure_ascii=False),flush=True)
                assert len(ranked)>=2,'未产生两个网站的实际排名'
                print('浏览器端到端验证通过',flush=True)
                break
        else:
            raise TimeoutError('浏览器检测超时')
        browser.close()
