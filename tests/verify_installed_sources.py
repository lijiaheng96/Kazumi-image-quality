"""手动集成验证：只读已安装规则，保存本工具的检测记录。"""
import concurrent.futures
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helper import rules

if __name__ == '__main__':
    paths = rules.discover_rule_files()
    loaded = next((rules.load_rules(path) for path in paths if rules.load_rules(path)), [])
    records = []
    def run(index, rule):
        try:
            items = rules.search(rule, '葬送的芙莉莲', timeout=14)
            return {'rule_id':index,'site':rule['name'],'items':items}
        except Exception as error:
            from helper.jobs import error_message
            return {'rule_id':index,'site':rule['name'],'error':error_message(error)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run,index,rule) for index,rule in enumerate(loaded)]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            records.append(result)
            print(json.dumps({'site':result['site'],'titles':[item['title'] for item in result.get('items',[])],'error':result.get('error')},ensure_ascii=False), flush=True)
    target = Path(__file__).resolve().parents[1]/'data'/'搜索验证.json'
    target.write_text(json.dumps(records, ensure_ascii=False,indent=2),encoding='utf-8')
