"""分辨率硬筛选、同编码规格预选；此处绝不产生模型画质分数。"""
import math


def positive(value):
    if isinstance(value,bool):
        return None
    try:
        number=float(value)
        return number if math.isfinite(number) and number>0 else None
    except (ValueError,TypeError,OverflowError):
        return None


def pixels(item):
    metadata=item['metadata']
    width,height=positive(metadata.get('width')),positive(metadata.get('height'))
    return width*height if width and height else 0


def highest_resolution(items):
    maximum=max((pixels(item) for item in items),default=0)
    return [item for item in items if pixels(item)==maximum] if maximum else []


def comparison_metrics(item):
    metadata,row=item['metadata'],item['row']
    rate=positive(metadata.get('bitrate'))
    kind='video' if rate else 'unknown'
    if rate is None:
        average=positive(row.get('average_bitrate'))
        size,duration=positive(row.get('size_bytes')),positive(metadata.get('duration'))
        if average is None and size and duration:
            average=size*8/duration
        if average is None:
            average=positive(metadata.get('container_bitrate'))
        if average:
            audio=positive(metadata.get('audio_bitrate'))
            if audio and metadata.get('audio_streams')==1 and average>audio:
                rate,kind=average-audio,'estimated_video'
            else:
                rate,kind=average,'container'
    fps=positive(metadata.get('fps'))
    return {'spec_bitrate':rate,'spec_bitrate_kind':kind,
            'bits_per_frame':rate/fps if rate and fps else None}


def _key(item):
    metrics=comparison_metrics(item)
    confidence={'video':3,'estimated_video':2,'container':1,'unknown':0}[metrics['spec_bitrate_kind']]
    return (-(metrics['spec_bitrate'] or 0),-confidence,item.get('index',0),item['row'].get('road',''))


def candidate_order(items):
    """同编码、相近帧率按码率粗排；不同组轮转，每个规则只占一个候选。"""
    buckets={}
    for item in items:
        codec=str(item['metadata'].get('codec') or 'unknown').lower()
        codec={'avc':'h264','avc1':'h264','hevc':'h265','hev1':'h265','hvc1':'h265'}.get(codec,codec)
        color=item['metadata'].get('color_transfer')
        hdr='HDR' if color in ('smpte2084','arib-std-b67') else 'SDR'
        buckets.setdefault((codec,hdr),[]).append(item)
    groups=[]
    for codec_items in buckets.values():
        frame_groups=[]
        for item in sorted(codec_items,key=lambda value:positive(value['metadata'].get('fps')) or float('inf')):
            fps=positive(item['metadata'].get('fps'))
            group=next((group for anchor,group in frame_groups
                        if (fps is None and anchor is None) or (fps and anchor and fps/anchor<=1.15)),None)
            if group is None:
                group=[]
                frame_groups.append((fps,group))
            group.append(item)
        groups.extend(sorted(group,key=_key) for _,group in frame_groups)
    # 优先覆盖有多个来源的常见规格，再保留其他编码/帧率的组内优胜者。
    groups.sort(key=lambda group:(-len({item['source']['rule_id'] for item in group}),_key(group[0])))
    result,seen=[],set()
    while any(groups):
        for group in groups:
            while group:
                item=group.pop(0)
                site=item['source']['rule_id']
                if site not in seen:
                    result.append(item)
                    seen.add(site)
                    break
    return result
