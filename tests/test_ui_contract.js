'use strict';

// 使用 Node 内置模块与最小 DOM 替身验证前端契约，不依赖浏览器或服务进程。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class NodeStub {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.nodes = new Map(); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector, new NodeStub()); return this.nodes.get(selector); }
  removeAttribute(name) { delete this[name]; }
  focus() { this.focused = true; }
}

const nodes = new Map();
const getNode = (id) => { if (!nodes.has(id)) nodes.set(id, new NodeStub()); return nodes.get(id); };
const config = { token: '测试令牌', rules_path: 'C:\\规则.json', rules: [{ id: 0, name: '网站甲' }, { id: 1, name: '网站乙' }], discovered_paths: [], model_ready: true, busy: false };
const calls = [];
const extraResponses = new Map();
let failure = null;
const context = vm.createContext({
  console, URL, AbortController, setTimeout: () => 1, clearTimeout: () => {},
  document: { getElementById: getNode, createElement: (tag) => new NodeStub(tag), createDocumentFragment: () => new NodeStub('fragment'), querySelectorAll: () => [] },
  Option: class extends NodeStub { constructor(text, value) { super('option'); this.textContent = text; this.value = value; } },
  navigator: { clipboard: { writeText: async () => {} } },
  fetch: async (url, options) => {
    calls.push({ url, options });
    if (failure && (!failure.url || failure.url === url)) return { ok: false, status: failure.status, json: async () => failure.body };
    const data = extraResponses.has(url) ? extraResponses.get(url) : url === '/api/config' ? config : url.startsWith('/api/jobs/') ? { status: 'completed', results: [], errors: [], progress: 100 } : { job_id: '测试任务' };
    return { ok: true, status: 200, json: async () => data };
  },
});
const script = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
vm.runInContext(script + '\nglobalThis.ui = { state, request, updateControls, selectedSiteCount, selectedRuleIds, renderAnalysis, safeUrl, pollJob, restoreSelection };', context);
const ui = context.ui;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const visibleText = (node) => [node.textContent, ...node.children.map(visibleText)].join(' ');

async function run() {
  await settle();
  assert.equal(ui.state.token, config.token, '读取服务令牌');

  failure = { status: 409, body: { detail: '请先等待当前任务完成' } };
  await assert.rejects(ui.request('/api/search', {}), /请先等待当前任务完成/, '显示 FastAPI 中文 detail');
  assert.equal(calls.at(-1).options.headers['X-Local-Token'], config.token, 'POST 带会话令牌');

  failure = { status: 422, body: { detail: [
    { loc: ['body', 'episode'], type: 'greater_than_equal', msg: 'Input should be greater than or equal to 0', ctx: { ge: 0 } },
    { loc: ['body', 'candidate_ids'], type: 'too_short', msg: 'List should have at least 2 items', ctx: { min_length: 2 } },
  ] } };
  await assert.rejects(ui.request('/api/analyze', {}), /检测集数必须大于或等于 0；所选来源至少需要 2 项/, '验证数组转换为中文');
  failure = { status: 422, body: { errors: [{ loc: ['body', 'keyword'], type: 'missing' }] } };
  await assert.rejects(ui.request('/api/search', {}), /请填写番剧名称/, '兼容 errors 数组');
  failure = null;

  ui.state.token = '已过期的令牌';
  failure = { url: '/api/search', status: 403, body: { detail: '会话已失效，请刷新页面' } };
  await assert.rejects(ui.request('/api/search', {}), /会话已更新，请重试此操作/, '令牌过期时恢复会话且不重放请求');
  assert.equal(ui.state.token, config.token);
  failure = null;

  ui.state.candidates = [{ id: 'a', site: '网站甲' }, { id: 'b', site: '网站乙' }, { id: 'c', site: '网站甲' }];
  ui.state.searchJobId = '搜索任务';
  ui.state.selected = new Set(['a']);
  ui.updateControls();
  assert.equal(getNode('analyze-button').disabled, true, '单个网站不能检测');
  ui.state.selected = new Set(['a', 'c']);
  ui.updateControls();
  assert.equal(getNode('analyze-button').disabled, true, '同站两个条目不能检测');
  ui.state.selected = new Set(['a', 'b']);
  ui.updateControls();
  assert.equal(getNode('analyze-button').disabled, false, '两个不同网站可以检测');
  ui.state.selectedRules = new Set(['0', '1']);
  assert.equal(JSON.stringify(ui.selectedRuleIds()), '[0,1]', '网站规则编号为数值');

  for (const input of ['0', '12.5', '']) {
    ui.state.busy = false; ui.state.requesting = false; ui.state.searchJobId = '搜索任务';
    getNode('episode').value = input;
    getNode('mode').value = input === '0' ? 'full' : 'fast';
    await getNode('analyze-button').listeners.click();
    const post = calls.filter((call) => call.url === '/api/analyze' && call.options.method === 'POST').at(-1);
    const body = JSON.parse(post.options.body);
    assert.equal(body.mode, getNode('mode').value, '检测模式传到后端');
    if (input) assert.equal(body.episode, Number(input), '支持零集与小数集号');
    else assert.equal('episode' in body, false, '自动集数省略 episode');
    await settle();
  }

  ui.renderAnalysis({ status: 'completed', episode: 12.5, results: [{ site: '<script>网站</script>', status: '已完成', score: null, rank: null, page_url: 'javascript:alert(1)' }] });
  const text = visibleText(getNode('results-body'));
  assert.match(text, /已完成/, '展示后端中文状态');
  assert.match(text, /未评分/, '缺失分数不填零');
  assert.match(text, /无排名/, '缺失排名明确显示');
  assert.match(text, /链接不可用/, '禁止脚本链接');
  assert.match(getNode('results-summary').textContent, /第 12.5 集/, '保留后端小数集号');
  assert.equal(ui.safeUrl('javascript:alert(1)'), null);
  assert.equal(ui.safeUrl('file:///C:/秘密.txt'), null);
  assert.equal(ui.safeUrl('https://example.com/play'), 'https://example.com/play');
  assert.equal(script.includes('innerHTML'), false, '不使用 HTML 字符串渲染');

  ui.state.jobId = '过期任务'; ui.state.searchJobId = '旧搜索'; ui.state.busy = true;
  failure = { url: '/api/jobs/' + encodeURIComponent('过期任务'), status: 404, body: { detail: '任务不存在或已过期' } };
  await ui.pollJob('过期任务', ui.state.pollVersion);
  assert.equal(ui.state.jobId, null, '404 清理当前任务');
  assert.equal(ui.state.searchJobId, null, '404 清理过期搜索上下文');
  assert.equal(ui.state.busy, false, '404 解除界面忙碌锁定');
  assert.equal(getNode('search-button').disabled, false, '任务过期后可重新搜索');
  assert.match(getNode('notice').textContent, /任务已过期/);
  ui.renderAnalysis({status:'completed', mode:'fast',elapsed_seconds:65.5,results:[{
    site:'测试站',status:'已完成',score:70,rank:1,bitrate:4000000,timings:{'取样与对齐':12.3,'评分':4.1},samples:3
  }]});
  assert.match(visibleText(getNode('results-body')), /4\.00 Mbps/, '显示可用码率');
  assert.match(visibleText(getNode('results-body')), /取样与对齐.*12\.3/, '显示分阶段耗时');
  assert.match(getNode('results-summary').textContent, /快速.*65\.5/, '显示模式及实际总耗时');
  failure = null;
  ui.state.searchJobId=null;
  extraResponses.set('/api/jobs/original', {id:'original',kind:'search',status:'completed',results:[
    {id:'a',site:'甲',title:'第一季',selected:true}, {id:'b',site:'甲',title:'第二季',selected:false},
    {id:'c',site:'乙',title:'第二季',selected:false}
  ]});
  await ui.restoreSelection({search_job_id:'original',selected_candidate_ids:['b','c'],mode:'full',episode:2});
  assert.deepEqual([...ui.state.selected].sort(),['b','c'],'刷新后恢复用户实际选择，而非原自动预选');
  assert.equal(ui.state.searchJobId,'original');
  assert.equal(getNode('mode').value,'full');

  // 直接调用页面事件和渲染函数：规格预选不可伪造成模型排名或零分。
  const smartFailures = [];
  const verifySmart = async (name, callback) => {
    try { await callback(); }
    catch (error) { smartFailures.push(`${name}：${error.message}`); }
  };
  await verifySmart('未设置模式时默认提交智能比较', async () => {
    ui.state.busy = false; ui.state.requesting = false;
    getNode('mode').value = ''; getNode('episode').value = '1';
    await getNode('analyze-button').listeners.click();
    const body = JSON.parse(calls.filter(call => call.url === '/api/analyze' && call.options.method === 'POST').at(-1).options.body);
    assert.equal(body.mode, 'smart');
    await settle();
  });
  await verifySmart('智能搜索覆盖全部规则且旧模式允许按勾选搜索', async () => {
    ui.state.busy = false; ui.state.requesting = false;
    ui.state.selectedRules = new Set(['1']);
    getNode('mode').value = 'smart'; getNode('keyword').value = '测试番剧';
    ui.updateControls();
    assert.equal(getNode('toggle-rules').disabled, true);
    await getNode('search-form').listeners.submit({ preventDefault() {} });
    let body = JSON.parse(calls.filter(call => call.url === '/api/search' && call.options.method === 'POST').at(-1).options.body);
    assert.deepEqual(body.rule_ids, [0, 1]);
    await settle();
    ui.state.busy = false; ui.state.requesting = false;
    getNode('mode').value = 'fast'; ui.state.selectedRules = new Set(['1']);
    ui.updateControls();
    assert.equal(getNode('toggle-rules').disabled, false);
    await getNode('search-form').listeners.submit({ preventDefault() {} });
    body = JSON.parse(calls.filter(call => call.url === '/api/search' && call.options.method === 'POST').at(-1).options.body);
    assert.deepEqual(body.rule_ids, [1]);
    await settle();
  });
  await verifySmart('智能比较仍然需要本地模型', () => {
    getNode('mode').value = 'smart'; ui.state.busy = false; ui.state.requesting = false;
    ui.state.searchJobId = '智能搜索';
    ui.state.candidates = [{ id: 'a', site: '甲' }, { id: 'b', site: '乙' }];
    ui.state.selected = new Set(['a', 'b']); ui.state.config.model_ready = false;
    ui.updateControls();
    assert.equal(getNode('analyze-button').disabled, true);
    ui.state.config.model_ready = true; ui.updateControls();
    assert.equal(getNode('analyze-button').disabled, false);
  });
  const smartJob = { status: 'running', mode: 'smart', highest_resolution: '1920×1080', shortlist_count: 2,
    selection_summary: '最高分辨率保留 2 个网站，其他线路已排除。', results: [
      { site: '甲', road: '线路一', status: '取样中', rank: null, score: null, resolution: '1920×1080', codec: 'h264', fps: 23.976,
        bitrate: null, average_bitrate: 4800000, bitrate_scope: 'total', size_bytes: 800000000, size_kind: 'estimated',
        size_source: '码率与时长推算', size_note: '包含音频', shortlist_rank: 1, selection_status: 'selected', selection_reason: '最高档中规格预选入围' },
      { site: '乙', road: '线路二', status: 'skipped', rank: null, score: null, resolution: '1280×720', codec: 'hevc', fps: null,
        bitrate: null, average_bitrate: null, size_bytes: null, size_kind: 'unknown', selection_status: 'lower_resolution', selection_reason: '低于本次最高分辨率' },
      { site: '甲', road: '线路三', status: 'skipped', rank: null, score: null, resolution: '1920×1080', codec: 'h264', bitrate: 4000000,
        size_bytes: 500000000, size_kind: 'exact', size_source: 'HTTP Content-Length', selection_status: 'same_site', selection_reason: '同站仅保留一条线路' },
    ] };
  await verifySmart('展示智能筛选范围与各线路排除原因', () => {
    ui.renderAnalysis(smartJob);
    const rendered = visibleText(getNode('results-body'));
    assert.match(getNode('results-summary').textContent, /智能/);
    assert.match(visibleText(getNode('shortlist-summary')), /1920×1080/);
    assert.match(visibleText(getNode('shortlist-summary')), /最高分辨率保留 2 个网站/);
    assert.match(rendered, /预选第 1/);
    assert.match(rendered, /较低分辨率已排除/);
    assert.match(rendered, /同站其他线路/);
    assert.match(rendered, /同站仅保留一条线路/);
    assert.equal(getNode('metric-ranked').textContent, '0', '规格入选不能自动获得模型排名');
    const first = getNode('results-body').children[0].children[0];
    assert.equal(visibleText(first.children[0]).trim(), '—', '最终名次保持空缺');
    assert.match(visibleText(first.children[3]), /未评分/);
  });
  await verifySmart('码率作用范围、帧率和大小可信度准确展示', () => {
    ui.renderAnalysis(smartJob);
    const rows = getNode('results-body').children[0].children;
    const first = visibleText(rows[0].children[2]);
    assert.match(first, /23\.98 fps/);
    assert.match(first, /视频码率未知/);
    assert.match(first, /总码率 4\.80 Mbps/);
    assert.match(first, /估算.*800\.0 MB/);
    assert.match(first, /码率与时长推算/);
    assert.match(first, /包含音频/);
    const second = visibleText(rows[1].children[2]);
    assert.match(second, /帧率未知/);
    assert.match(second, /体积未知/);
    assert.doesNotMatch(second, /0(?:\.00)? (?:Mbps|fps|MB)/);
    const third = visibleText(rows[2].children[2]);
    assert.match(third, /视频码率 4\.00 Mbps/);
    assert.match(third, /已知.*500\.0 MB/);
    assert.match(third, /HTTP Content-Length/);
  });
  await verifySmart('旧模式结果不残留智能筛选面板', () => {
    ui.renderAnalysis({ status: 'completed', mode: 'full', results: [] });
    assert.equal(getNode('shortlist-summary').hidden, true);
  });
  await verifySmart('切入智能模式后不能复用旧模式的部分规则搜索', () => {
    ui.state.busy = false; ui.state.requesting = false; ui.state.searchJobId = '仅部分网站的旧搜索';
    getNode('mode').value = 'smart';
    getNode('mode').listeners.change();
    assert.equal(ui.state.searchJobId, null);
    assert.equal(ui.state.candidates.length, 0);
    assert.equal(getNode('analyze-button').disabled, true);
    assert.match(getNode('notice').textContent, /重新搜索/);
  });
  await verifySmart('来源代码转换为中文，体积未知时仍解释原因', () => {
    ui.renderAnalysis({ mode: 'smart', results: [
      { site: '甲', size_bytes: 800000000, size_kind: 'estimated', size_source: 'hls_average' },
      { site: '乙', size_bytes: null, size_kind: 'unknown', size_source: 'unknown', size_note: '源站未提供可靠大小' },
      { site: '丙', size_bytes: 500000000, size_kind: 'exact', size_source: 'content_range', duration: 1370.18 },
    ] });
    const rows = getNode('results-body').children[0].children;
    assert.match(visibleText(rows[0].children[2]), /播放清单平均码率/);
    assert.doesNotMatch(visibleText(rows[0].children[2]), /hls_average/);
    assert.match(visibleText(rows[1].children[2]), /体积未知.*源站未提供可靠大小/);
    assert.match(visibleText(rows[2].children[2]), /范围响应头/);
    assert.match(visibleText(rows[2].children[2]), /时长 22:50/);
  });
  if (smartFailures.length) throw new Error(smartFailures.join('\n'));
  console.log('前端契约验证通过：中文错误、令牌恢复、全规则智能搜索、模式提交、模型门槛、预选状态、规格可信度、评分空值、安全链接与任务恢复。');
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
