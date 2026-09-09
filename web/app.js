'use strict';

// 所有服务端文本通过 textContent 渲染，播放链接仅允许 HTTP(S)。
const $ = (id) => document.getElementById(id);
const state = { token: '', config: null, busy: false, requesting: false, jobId: null, jobKind: null, searchJobId: null, candidates: [], selected: new Set(), selectedRules: new Set(), pollVersion: 0, cancelPending: false, externalTimer: null };
const statusNames = { pending: '等待处理', queued: '等待处理', running: '正在处理', resolving: '正在解析', downloading: '正在取样', sampling: '正在取样', matching: '正在匹配画面', scoring: '正在估计画质', completed: '检测完成', success: '检测完成', ok: '检测完成', failed: '检测失败', error: '检测失败', cancelled: '已取消', skipped: '未参与比较', unmatched: '内容未匹配', insufficient: '样本不足', incomparable: '未达到比较条件' };

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}

function safeUrl(value) {
  try { const url = new URL(String(value)); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; }
}

function sourceLink(url, label) {
  const href = safeUrl(url);
  if (!href) return element('span', 'cell-detail', '链接不可用');
  const link = element('a', '', label);
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function notify(message, success = false) {
  $('notice').textContent = message;
  $('notice').className = success ? 'notice success' : 'notice';
  $('notice').hidden = !message;
}

function apiErrorMessage(data, status) {
  for (const value of [data?.detail, data?.error, data?.message]) {
    if (typeof value === 'string' && value.trim()) return value;
  }
  const validationErrors = [data?.detail, data?.errors, data?.error].find(Array.isArray);
  if (validationErrors?.length) {
    const names = { keyword: '番剧名称', rules_path: '规则文件路径', rule_ids: '网站规则', candidate_ids: '所选来源', episode: '检测集数', search_job_id: '搜索任务' };
    const messages = validationErrors.map((error) => {
      if (typeof error === 'string') return /[\u3400-\u9fff]/.test(error) ? error : '请检查输入内容';
      if (!error || typeof error !== 'object') return '请检查输入内容';
      const field = Array.isArray(error.loc) ? error.loc.find((part) => names[part]) : error.field;
      const name = names[field] || '请求参数';
      const context = error.ctx || {};
      if (typeof error.msg === 'string' && /[\u3400-\u9fff]/.test(error.msg)) return `${name}：${error.msg}`;
      switch (error.type) {
        case 'greater_than_equal': return `${name}必须大于或等于 ${context.ge}`;
        case 'less_than_equal': return `${name}必须小于或等于 ${context.le}`;
        case 'string_too_short': return `${name}至少需要 ${context.min_length} 个字符`;
        case 'string_too_long': return `${name}不能超过 ${context.max_length} 个字符`;
        case 'too_short': return `${name}至少需要 ${context.min_length} 项`;
        case 'too_long': return `${name}不能超过 ${context.max_length} 项`;
        case 'missing': return `请填写${name}`;
        case 'float_parsing': case 'float_type': case 'finite_number': return `${name}必须是有效数字`;
        case 'int_parsing': case 'int_type': return `${name}必须是整数`;
        default: return `${name}格式不正确，请检查输入`;
      }
    });
    return `请求参数不正确：${[...new Set(messages)].join('；')}。`;
  }
  return `操作未完成（状态码 ${status}），请检查本地服务。`;
}

async function request(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const options = { signal: controller.signal, cache: 'no-store', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-Local-Token'] = state.token;
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let data;
    try { data = await response.json(); } catch { throw new Error('本地服务返回了无法读取的响应，请检查启动窗口。'); }
    if (!response.ok) {
      let message = apiErrorMessage(data, response.status);
      if (response.status === 403 && body !== undefined) {
        // 更新失效令牌，但不自动重放写操作，避免重复创建任务。
        try {
          const refreshed = await request('/api/config');
          if (refreshed.token && refreshed.token !== state.token) {
            state.token = refreshed.token;
            message = '会话已更新，请重试此操作。';
          }
        } catch { message += ' 请刷新页面后重试。'; }
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('本地服务响应超时，请检查启动窗口后重试。');
    if (error instanceof TypeError) throw new Error('无法连接本地服务，请确认启动窗口仍在运行。');
    throw error;
  } finally { clearTimeout(timer); }
}

function selectedSiteCount() {
  return new Set(state.candidates.filter((candidate) => state.selected.has(String(candidate.id))).map((candidate) => candidate.site)).size;
}

function selectedRuleIds() {
  const ids = [...state.selectedRules].map(Number);
  if (ids.some((id) => !Number.isInteger(id) || id < 0)) throw new Error('网站规则编号无效，请重新读取规则文件。');
  return ids;
}

function updateControls() {
  const locked = state.busy || state.requesting;
  for (const id of ['rules-path', 'discovered-paths', 'save-config', 'toggle-rules', 'keyword', 'episode', 'refresh-config']) $(id).disabled = locked;
  $('search-button').disabled = locked || !state.config || !state.selectedRules.size;
  $('analyze-button').disabled = locked || !state.searchJobId || selectedSiteCount() < 2 || !state.config?.model_ready;
  $('cancel-button').disabled = !state.jobId || state.cancelPending || state.requesting;
  document.querySelectorAll('#rule-list input, #candidates input').forEach((input) => { input.disabled = locked; });
  $('toggle-rules').textContent = state.config?.rules?.length && state.selectedRules.size === state.config.rules.length ? '取消全选' : '全选';
  $('selection-summary').textContent = state.candidates.length ? `已选 ${selectedSiteCount()} 个网站 · 至少选择两个` : '每个网站选择一个条目';
}

function clearAnalysis() {
  $('results-body').replaceChildren();
  $('results-table-wrap').hidden = true;
  $('results-empty').hidden = false;
  $('results-empty').querySelector('h3').textContent = '等待第一次检测';
  $('results-empty').querySelector('p').textContent = '选择来源后开始检测，在这里查看线路、画面规格与实际评分。';
  $('analysis-errors').replaceChildren();
  $('analysis-errors').hidden = true;
  $('results-summary').textContent = '只有匹配到足够共同画面的来源才参与排名。数值越高，模型估计的画质越好。';
}

function clearSearch() {
  state.searchJobId = null;
  state.candidates = [];
  state.selected.clear();
  $('candidates').replaceChildren();
  $('candidate-count').textContent = '0';
  $('candidate-empty').hidden = false;
  $('candidate-empty').querySelector('h3').textContent = '正在搜索来源';
  $('candidate-empty').querySelector('p').textContent = '各网站的搜索结果会显示在这里。';
  $('search-errors').replaceChildren();
  $('search-errors').hidden = true;
  clearAnalysis();
}

function renderRules(config, preserveSelection) {
  const oldSelection = new Set(state.selectedRules);
  state.selectedRules.clear();
  const fragment = document.createDocumentFragment();
  for (const rule of config.rules || []) {
    const id = String(rule.id);
    const label = element('label', 'rule-item');
    const input = element('input');
    input.type = 'checkbox'; input.value = id;
    input.checked = preserveSelection ? oldSelection.has(id) : true;
    if (input.checked) state.selectedRules.add(id);
    input.addEventListener('change', () => { input.checked ? state.selectedRules.add(id) : state.selectedRules.delete(id); updateControls(); });
    label.append(input, element('span', '', rule.name || '未命名规则'));
    fragment.append(label);
  }
  if (!(config.rules || []).length) fragment.append(element('p', 'helper-text', '尚未加载规则，请填写规则文件路径。'));
  $('rule-list').replaceChildren(fragment);
  $('rule-count').textContent = String((config.rules || []).length);
}

async function loadConfig(preserveSelection = false, preserveInput = false) {
  const config = await request('/api/config');
  const hadConfig = Boolean(state.config);
  state.config = config;
  state.token = config.token || '';
  if (!preserveInput) $('rules-path').value = config.rules_path || '';
  const options = [new Option('选择文件路径', '')];
  for (const path of config.discovered_paths || []) options.push(new Option(path, path));
  $('discovered-paths').replaceChildren(...options);
  $('discovered-wrap').hidden = !(config.discovered_paths || []).length;
  renderRules(config, preserveSelection && hadConfig);
  $('model-badge').textContent = config.model_ready ? '已就绪' : '未就绪';
  $('model-badge').className = `badge ${config.model_ready ? 'ready' : 'warning'}`;
  $('model-message').textContent = config.model_message || (config.model_ready ? '本地模型已就绪，可以开始画质检测。' : '本地模型尚未就绪。请检查启动窗口中的安装或下载提示。');
  if (!state.jobId) {
    state.busy = Boolean(config.busy);
    if (state.busy) {
      $('job-panel').hidden = false;
      $('job-title').textContent = '本地服务正在处理任务';
      $('job-message').textContent = '请等待当前任务结束，页面将自动恢复操作。';
      $('job-percent').textContent = '';
      $('job-progress').removeAttribute('value');
      $('job-spinner').hidden = false;
      $('cancel-button').hidden = true;
      clearTimeout(state.externalTimer);
      state.externalTimer = setTimeout(pollExternalConfig, 3000);
    } else if ($('cancel-button').hidden) $('job-panel').hidden = true;
  }
  updateControls();
}

async function pollExternalConfig() {
  try { await loadConfig(true, true); }
  catch (error) {
    notify(error.message);
    if (state.busy && !state.jobId) state.externalTimer = setTimeout(pollExternalConfig, 3000);
  }
}

function renderErrors(id, errors) {
  const target = $(id);
  const fragment = document.createDocumentFragment();
  for (const error of errors || []) fragment.append(element('p', '', typeof error === 'string' ? error : `${error.site || '处理提示'}：${error.message || '未能完成处理'}`));
  target.replaceChildren(fragment);
  target.hidden = !(errors || []).length;
}

function renderCandidates(job, initial = false) {
  state.candidates = Array.isArray(job.results) ? job.results : [];
  if (initial) state.selected.clear();
  const groups = new Map();
  for (const candidate of state.candidates) {
    const key = String(candidate.rule_id ?? candidate.site);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(candidate);
  }
  const fragment = document.createDocumentFragment();
  let groupIndex = 0;
  for (const candidates of groups.values()) {
    const group = element('section', 'source-group');
    const header = element('div', 'source-header');
    header.append(element('h3', '', candidates[0].site || '未命名网站'), element('span', 'helper-text', `${candidates.length} 个条目`));
    group.append(header);
    const name = `source-${groupIndex++}`;
    const preselected = candidates.filter((candidate) => candidate.selected);
    if (initial && preselected.length === 1) state.selected.add(String(preselected[0].id));
    const choices = [...candidates, null];
    for (const candidate of choices) {
      const label = element('label', `candidate-option${candidate ? '' : ' skip'}`);
      const input = element('input');
      input.type = 'radio'; input.name = name;
      input.checked = candidate ? state.selected.has(String(candidate.id)) : !candidates.some((item) => state.selected.has(String(item.id)));
      input.addEventListener('change', () => {
        candidates.forEach((item) => state.selected.delete(String(item.id)));
        if (candidate) state.selected.add(String(candidate.id));
        clearAnalysis();
        updateControls();
      });
      label.append(input);
      if (candidate) {
        const text = element('span', 'candidate-text', candidate.title || '未命名条目');
        const href = safeUrl(candidate.url);
        if (href) text.append(element('small', '', href));
        label.append(text, sourceLink(candidate.url, '查看 ↗'));
      } else label.append(element('span', '', '不选择此网站'));
      group.append(label);
    }
    fragment.append(group);
  }
  $('candidates').replaceChildren(fragment);
  $('candidate-count').textContent = String(state.candidates.length);
  $('candidate-empty').hidden = state.candidates.length > 0;
  if (!state.candidates.length) {
    $('candidate-empty').querySelector('h3').textContent = job.status === 'running' ? '正在搜索来源' : job.status === 'cancelled' ? '搜索已取消' : '没有找到可选条目';
    $('candidate-empty').querySelector('p').textContent = job.status === 'running' ? '等待网站返回搜索结果。' : '可以调整番剧名称、选择其他网站后重新搜索。';
  }
  renderErrors('search-errors', job.errors);
  updateControls();
}

async function copyUrl(url, button) {
  const href = safeUrl(url);
  if (!href) return;
  try {
    await navigator.clipboard.writeText(href);
    button.textContent = '已复制';
    setTimeout(() => { if (button.isConnected) button.textContent = '复制'; }, 1800);
  } catch { notify('无法自动复制链接，请右键“打开”并选择复制链接地址。'); }
}

function renderAnalysis(job) {
  const results = Array.isArray(job.results) ? [...job.results] : [];
  const rankValue = (result) => typeof result.rank === 'number' && Number.isFinite(result.rank) && result.rank > 0 ? result.rank : Infinity;
  results.sort((left, right) => rankValue(left) - rankValue(right));
  const fragment = document.createDocumentFragment();
  for (const result of results) {
    const row = element('tr');
    const rankCell = element('td');
    const ranked = typeof result.rank === 'number' && Number.isFinite(result.rank) && result.rank > 0;
    rankCell.append(element('span', `rank${result.rank === 1 ? ' first' : ''}`, ranked ? result.rank : '—'));
    const sourceCell = element('td');
    sourceCell.append(element('div', 'result-site', result.site || '未知网站'), element('div', 'cell-detail', result.title || ''), element('div', 'cell-detail', `${result.road || '默认线路'}${result.episode != null ? ` · 第 ${result.episode} 集` : ''}`));
    const formatCell = element('td');
    formatCell.append(element('div', 'mono', result.resolution || '未取得'), element('div', 'cell-detail', result.codec || '编码未知'));
    const scoreCell = element('td');
    const scored = typeof result.score === 'number' && Number.isFinite(result.score);
    scoreCell.append(element('div', scored ? 'score' : 'score missing', scored ? result.score.toFixed(2) : '未评分'));
    if (!ranked) scoreCell.append(element('div', 'cell-detail', '无排名'));
    const processCell = element('td');
    const status = typeof result.status === 'string' ? result.status : '';
    const statusClass = ['failed', 'error', '未参与排名', '检测失败'].includes(status) ? 'failed' : ['completed', 'success', 'ok', '已完成', '已检测'].includes(status) ? 'completed' : '';
    processCell.append(element('div', `status-label ${statusClass}`, statusNames[status] || (/[\u3400-\u9fff]/.test(status) ? status : '处理中')));
    if (result.samples != null) processCell.append(element('div', 'cell-detail', `有效样本：${Array.isArray(result.samples) ? result.samples.length : result.samples}`));
    if (result.message) processCell.append(element('div', 'cell-detail', result.message));
    const actionCell = element('td');
    const actions = element('div', 'result-actions');
    actions.append(sourceLink(result.page_url, '打开 ↗'));
    if (safeUrl(result.page_url)) {
      const copy = element('button', 'text-button', '复制'); copy.type = 'button';
      copy.addEventListener('click', () => copyUrl(result.page_url, copy));
      actions.append(copy);
    }
    actionCell.append(actions);
    row.append(rankCell, sourceCell, formatCell, scoreCell, processCell, actionCell);
    fragment.append(row);
  }
  $('results-body').replaceChildren(fragment);
  $('results-table-wrap').hidden = !results.length;
  $('results-empty').hidden = Boolean(results.length);
  if (!results.length) {
    $('results-empty').querySelector('h3').textContent = job.status === 'running' ? '正在解析与采样' : job.status === 'cancelled' ? '检测已取消' : '暂无可展示的检测结果';
    $('results-empty').querySelector('p').textContent = job.status === 'running' ? '处理过程和检测结果会随任务进度更新，请稍候。' : '请查看任务提示，调整来源后重新检测。';
  }
  const rankedCount = results.filter((result) => typeof result.rank === 'number' && result.rank > 0).length;
  const episode = job.episode != null ? `第 ${job.episode} 集 · ` : '';
  $('results-summary').textContent = `${episode}${results.length} 条线路${rankedCount ? `，${rankedCount} 条参与排名` : '，尚无可比较排名'}。画质估计不是百分制；未评分不代表画质为零。`;
  renderErrors('analysis-errors', job.errors);
}

function renderProgress(job) {
  const running = job.status === 'running';
  const isSearch = state.jobKind === 'search';
  $('job-panel').hidden = false;
  $('job-title').textContent = running ? (isSearch ? '正在搜索来源' : '正在检测画质') : job.status === 'completed' ? (isSearch ? '搜索完成，请确认来源' : '画质检测完成') : job.status === 'cancelled' ? '任务已取消' : '任务未完成';
  $('job-message').textContent = job.message || (running ? '正在处理，请稍候…' : '请查看下方结果。');
  const percent = typeof job.progress === 'number' ? Math.max(0, Math.min(100, job.progress)) : 0;
  $('job-percent').textContent = `${Math.round(percent)}%`;
  $('job-progress').value = percent;
  $('job-spinner').hidden = !running;
  $('cancel-button').hidden = !running;
  $('cancel-button').textContent = state.cancelPending ? '正在取消…' : '取消任务';
}

async function pollJob(id, version) {
  try {
    const job = await request(`/api/jobs/${encodeURIComponent(id)}`);
    if (version !== state.pollVersion || state.jobId !== id) return;
    renderProgress(job);
    if (state.jobKind === 'search') renderCandidates(job, true); else renderAnalysis(job);
    if (job.status === 'running') { setTimeout(() => pollJob(id, version), 1200); return; }
    if (state.jobKind === 'search') state.searchJobId = job.status === 'completed' ? id : null;
    state.jobId = null; state.busy = false; state.cancelPending = false;
    if (job.status === 'failed') notify(job.message || '任务未完成，请查看错误信息后重试。');
    updateControls();
    try { await loadConfig(true, true); } catch (error) { notify(error.message); }
  } catch (error) {
    if (version !== state.pollVersion) return;
    if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
      // 任务过期等永久错误不能继续轮询，否则界面会一直处于忙碌状态。
      state.jobId = null; state.searchJobId = null; state.busy = false; state.cancelPending = false;
      ++state.pollVersion;
      const message = error.status === 404 ? '任务已过期或本地服务已重启，请重新搜索番剧。' : error.message;
      renderProgress({ status: 'failed', message, progress: 0 });
      notify(message);
      updateControls();
      try { await loadConfig(true, true); } catch (refreshError) { notify(`${message} ${refreshError.message}`); }
      return;
    }
    notify(`${error.message} 正在尝试恢复任务进度。`);
    // 暂时断线时保留任务上下文，避免再次提交造成任务冲突。
    setTimeout(() => pollJob(id, version), 3000);
  }
}

function startJob(id, kind) {
  if (!id) throw new Error('本地服务没有返回任务编号，请检查启动窗口。');
  clearTimeout(state.externalTimer);
  state.jobId = String(id); state.jobKind = kind; state.busy = true; state.cancelPending = false;
  const version = ++state.pollVersion;
  renderProgress({ status: 'running', progress: 0, message: '任务已提交，正在准备…' });
  updateControls();
  pollJob(state.jobId, version);
}

$('discovered-paths').addEventListener('change', () => { if ($('discovered-paths').value) $('rules-path').value = $('discovered-paths').value; });
$('toggle-rules').addEventListener('click', () => {
  const selectAll = state.selectedRules.size !== (state.config?.rules || []).length;
  document.querySelectorAll('#rule-list input').forEach((input) => { input.checked = selectAll; selectAll ? state.selectedRules.add(input.value) : state.selectedRules.delete(input.value); });
  updateControls();
});
$('config-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.busy || state.requesting) return;
  const path = $('rules-path').value.trim();
  if (!path) { notify('请填写规则 JSON 文件的完整路径。'); return; }
  state.requesting = true; updateControls(); notify('');
  try {
    await request('/api/config', { rules_path: path });
    clearSearch(); $('candidate-empty').querySelector('h3').textContent = '规则已更新，可以开始搜索';
    $('candidate-empty').querySelector('p').textContent = '输入番剧名称，搜索新规则中的网站。';
    $('job-panel').hidden = true;
    await loadConfig(false); notify('规则路径已保存并读取。', true);
  } catch (error) { notify(error.message); }
  finally { state.requesting = false; updateControls(); }
});
$('refresh-config').addEventListener('click', async () => {
  if (state.busy || state.requesting) return;
  state.requesting = true; updateControls(); notify('');
  try { await loadConfig(true, true); } catch (error) { notify(error.message); }
  finally { state.requesting = false; updateControls(); }
});
$('search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.busy || state.requesting) return;
  const keyword = $('keyword').value.trim();
  if (!keyword) { notify('请输入番剧名称。'); return; }
  if (!state.selectedRules.size) { notify('请在左侧至少选择一个网站。'); return; }
  state.requesting = true; updateControls(); notify('');
  try {
    const data = await request('/api/search', { keyword, rule_ids: selectedRuleIds() });
    clearSearch(); startJob(data.job_id, 'search');
  } catch (error) { notify(error.message); }
  finally { state.requesting = false; updateControls(); }
});
$('analyze-button').addEventListener('click', async () => {
  if (state.busy || state.requesting || !state.searchJobId) return;
  if (selectedSiteCount() < 2) { notify('请至少选择两个网站中同一部番剧的来源。'); return; }
  const rawEpisode = $('episode').value.trim();
  const episode = rawEpisode === '' ? undefined : Number(rawEpisode);
  if (episode !== undefined && (!Number.isFinite(episode) || episode < 0 || episode > 100000)) { notify('集数应为 0 至 100000 的数字，可填写小数，也可以留空自动选择。'); $('episode').focus(); return; }
  state.requesting = true; updateControls(); notify('');
  try {
    const data = await request('/api/analyze', { search_job_id: state.searchJobId, candidate_ids: [...state.selected], ...(episode !== undefined ? { episode } : {}) });
    clearAnalysis(); startJob(data.job_id, 'analyze');
  } catch (error) { notify(error.message); }
  finally { state.requesting = false; updateControls(); }
});
$('cancel-button').addEventListener('click', async () => {
  if (!state.jobId || state.cancelPending) return;
  state.cancelPending = true; updateControls(); $('cancel-button').textContent = '正在取消…';
  try { await request(`/api/jobs/${encodeURIComponent(state.jobId)}/cancel`, {}); }
  catch (error) { state.cancelPending = false; notify(error.message); updateControls(); $('cancel-button').textContent = '取消任务'; }
});

state.requesting = true;
updateControls();
loadConfig().catch((error) => {
  notify(error.message);
  $('rule-list').replaceChildren(element('p', 'helper-text', '规则未读取，请检查本地服务后重新检查环境。'));
  $('model-badge').textContent = '连接失败';
  $('model-message').textContent = '尚未连接到本地服务。';
}).finally(() => { state.requesting = false; updateControls(); });
