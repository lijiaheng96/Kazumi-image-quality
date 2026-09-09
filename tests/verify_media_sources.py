"""手动验证实际规则能否读取视频，输出仅包含站名与媒体参数。"""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from helper import media, rules

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    ruleset = next((rules.load_rules(path) for path in rules.discover_rule_files() if rules.load_rules(path)), [])
    found = json.loads((root/'data'/'搜索验证.json').read_text(encoding='utf-8'))
    results = []
    names = sys.argv[1:] or ['7sefun', 'ezdmw', 'sorani']
    for site in names:
        record = next(record for record in found if record['site']==site)
        rule = ruleset[record['rule_id']]
        item = next(item for item in record['items'] if item['title']=='葬送的芙莉莲')
        try:
            roads = rules.chapters(rule, item['url'])
            ep = next(ep for ep in roads[0]['episodes'] if ep.get('number')==1)
            resolved = media.resolve_media(ep['url'], rule, threading.Event())
            metadata = media.probe(resolved, threading.Event())
            results.append(dict(site=site, rule_id=record['rule_id'], item=item, road=roads[0]['name'], episode=ep, media=resolved, metadata=metadata))
            print(json.dumps({'site':site,'roads':len(roads),'metadata':metadata},ensure_ascii=False),flush=True)
        except Exception as error:
            from helper.jobs import error_message
            print(json.dumps({'site':site,'error':error_message(error)},ensure_ascii=False),flush=True)
    (root/'data'/'媒体验证.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
