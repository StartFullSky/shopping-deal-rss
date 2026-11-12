'use strict';

const $ = id => document.getElementById(id);
const state = {config: null, token: '', runs: [], current: null, filter: 'all', page: 1, pageSize: 10, active: null, poll: null, pollBusy: false, dirty: false};
const sourceNames = {'kuan': '酷安', 'weibo-chaohua': '微博超话'};
const reasonNames = {
  source_not_allowed: '来源不在采集范围', stale: '超过消息有效期', include_filter: '未命中包含关键词',
  keyword_excluded: '命中排除关键词', duplicate_message: '本批消息重复', batch_item_limit: '超过本轮处理上限',
  source_identity_missing: '缺少来源标识', already_sent: '相同内容已发送', baseline_suppressed: '首次基线已记录',
  duplicate_content: '相同内容已在其他线报发送', in_progress: '已有任务正在处理', content_in_progress: '相同内容正在处理',
  delivery_unconfirmed: '上次送达状态待确认', retry_exhausted: '已达到重试次数上限', retry_not_due: '尚未到重试时间',
  superseded: '已有更新版本', local_rules_passed: '通过本地规则', ai_disabled_local_rules_passed: 'AI 已关闭，通过本地规则',
  ai_unavailable_local_rules_passed: 'AI 调用异常，按本地规则放行', ai_unknown_local_rules_passed: 'AI 无法判断，按本地规则处理',
  ai_unavailable_deferred: 'AI 调用异常，按配置暂缓处理', ai_unknown_deferred: 'AI 无法判断，按配置暂缓处理',
  not_selected: '本轮未进入审核', previous_run_interrupted: '上轮运行中断，可重新试跑'
};
const deliveryNames = {preview: '预览保留，未发送', queued: '等待程序处理', ai_rejected: 'AI 判为非商品，已拦截',
  review_deferred: '等待后续审核', not_eligible: '未进入发送阶段', sent: '已确认发送成功', failed: '发送失败，等待重试',
  delivery_unknown: '送达状态待确认', configuration_error: '推送配置不可用', rules_changed_or_expired: '规则变化或消息过期', lease_lost: '处理占用已变化'};
const pageCopy = {
  deals: ['线报列表', 'DISCOVER & REVIEW', '每一条线报，都有据可查。', '查看筛选结果，理解每一次保留与排除。'],
  settings: ['规则配置', 'FILTER YOUR WAY', '让规则，贴近你的偏好。', '先试算影响，再让下一轮运行使用新规则。'],
  history: ['试跑记录', 'RUN HISTORY', '回看结果，也回看过程。', '每一批数据都有独立的运行记录和配置版本。']
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function clear(element) { element.replaceChildren(); }
function toast(message, error = false) {
  $('toast').textContent = message;
  $('toast').className = 'toast' + (error ? ' error' : '');
  $('toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $('toast').hidden = true; }, error ? 7000 : 3500);
}
async function api(path, body) {
  const options = {headers: {}, cache: 'no-store'};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-Local-Token': state.token};
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('无法连接本地后台，请确认服务仍在运行。'); }
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '操作失败，请重试。');
  return value;
}
function dateParts(timestamp) {
  if (!timestamp) return ['—', ''];
  const date = new Date(typeof timestamp === 'number' ? timestamp * 1000 : timestamp);
  if (Number.isNaN(date.getTime())) return ['—', ''];
  return [date.toLocaleTimeString('zh-CN', {hour12: false, hour: '2-digit', minute: '2-digit'}),
    date.toLocaleDateString('zh-CN', {month: '2-digit', day: '2-digit'})];
}
function fullDate(timestamp) {
  if (!timestamp) return '—';
  return new Date(typeof timestamp === 'number' ? timestamp * 1000 : timestamp).toLocaleString('zh-CN', {hour12: false});
}
function seconds(value) { return Number(value || 0).toFixed(2); }
function explain(reason) {
  if (reasonNames[reason]) return reasonNames[reason];
  if (/^model_http_\d+$/.test(reason || '')) return `模型请求失败（HTTP ${reason.slice(11)}）`;
  if ((reason || '').startsWith('review_unavailable_')) return 'AI 复核暂不可用';
  return reason || '—';
}
function category(row) {
  if (row.rule_decision === 'filtered') return 'rules';
  if (row.is_product === false) return 'ai';
  if (row.eligible_to_send) return 'kept';
  return 'deferred';
}
function statusTag(row) {
  const kind = category(row);
  const labels = {kept: ['positive', row.review_source === 'disabled' ? '规则保留' : row.is_product === true ? '商品保留' : '降级保留'],
    rules: ['neutral', '程序过滤'], ai: ['negative', '非商品'], deferred: ['warning', '暂缓审核']};
  return node('span', 'tag ' + labels[kind][0], labels[kind][1]);
}
function sourceCell(feed) {
  const element = node('div', 'source-cell');
  const isCool = feed === 'kuan';
  element.append(node('span', 'source-icon ' + (isCool ? 'cool' : 'weibo'), isCool ? '酷' : '微'), node('span', '', sourceNames[feed] || feed || '队列'));
  return element;
}
function reasonSub(row) {
  if (row.rule_decision === 'filtered') return '由程序筛选';
  if (row.review_source === 'cache') return '复用审核缓存';
  if (row.review_source === 'model') return 'AI 复核';
  if (row.review_source === 'fallback') return '模型异常，使用本地规则';
  if (row.review_source === 'disabled') return '本轮未调用 AI';
  return '保留原始审核结论';
}
function showPage(name) {
  if (!pageCopy[name]) name = 'deals';
  document.querySelectorAll('.page').forEach(el => { el.hidden = el.id !== 'page-' + name; });
  document.querySelectorAll('[data-page]').forEach(el => el.classList.toggle('active', el.dataset.page === name));
  const [crumb, eyebrow, title, description] = pageCopy[name];
  $('breadcrumb').textContent = crumb;
  $('pageEyebrow').textContent = eyebrow;
  $('pageTitle').textContent = title;
  $('pageDescription').textContent = description;
  document.title = `${crumb} · 线报工作台`;
}

function renderStats() {
  const run = state.current;
  const report = run?.report || {};
  const summary = run?.summary || {};
  const present = !!run?.finished_at && !!run.report?.mode;
  $('statInput').textContent = present ? report.input_count : '—';
  $('statSelected').textContent = present ? report.selected_count : '—';
  $('statEligible').textContent = present ? report.eligible_count : '—';
  clear($('statElapsed'));
  if (present) $('statElapsed').append(node('span', '', seconds(summary.elapsed_seconds)), node('small', '', '秒'));
  else $('statElapsed').textContent = '—';
  const counts = report.feed_counts || {};
  $('statSources').textContent = present ? `酷安 ${counts.kuan || 0} · 微博超话 ${counts['weibo-chaohua'] || 0}` : '等待首次采集';
  $('statReview').textContent = present ? report.review_enabled ? `AI 请求 ${summary.ai_requests || 0} 次 · 缓存命中 ${summary.cache_hits || 0} 条` : 'AI 已关闭，仅依据本地规则' : '根据当前规则判断';
  $('statWait').textContent = present ? `其中限速等待 ${seconds(summary.rate_wait_seconds)} 秒` : '包含限速等待时间';
  $('batchCaption').textContent = run ? `批次 ${run.id.slice(0, 8)} · 采集于 ${fullDate(run.snapshot?.captured_at || run.started_at)}${run.origin === 'reuse' ? ' · 复用历史数据' : ''}` : '开始一次试跑，查看完整筛选过程。';
  const errors = [...(report.feed_errors || []).map(x => `${sourceNames[x.feed] || x.feed}：${explain(x.error)}`), ...(report.errors || []).map(explain)];
  if (run?.error) errors.push(explain(run.error));
  $('pageAlert').textContent = errors.join('；');
  $('pageAlert').hidden = errors.length === 0;
}

function renderDeals() {
  const all = [...(state.current?.rows || [])].sort((a, b) => (b.published_at || 0) - (a.published_at || 0));
  const counts = {all: all.length, kept: 0, rules: 0, ai: 0, deferred: 0};
  all.forEach(row => counts[category(row)]++);
  for (const [key, id] of Object.entries({all: 'tabAll', kept: 'tabKept', rules: 'tabRules', ai: 'tabAi', deferred: 'tabDeferred'})) $(id).textContent = counts[key];
  $('navCount').textContent = all.length;
  $('resultCount').textContent = `${all.length} 条`;
  const search = $('search').value.trim().toLocaleLowerCase();
  const source = $('sourceFilter').value;
  const rows = all.filter(row => (state.filter === 'all' || category(row) === state.filter)
    && (source === 'all' || row.source_feed === source)
    && (!search || `${row.title} ${row.source_content}`.toLocaleLowerCase().includes(search)));
  const pages = Math.max(1, Math.ceil(rows.length / state.pageSize));
  state.page = Math.min(state.page, pages);
  clear($('dealRows'));
  const from = (state.page - 1) * state.pageSize;
  rows.slice(from, from + state.pageSize).forEach(row => {
    const tr = node('tr');
    const titleCell = node('td');
    const title = node('div', 'deal-title', row.title);
    title.title = row.title;
    titleCell.append(title, node('div', 'deal-excerpt', row.source_content || '原文未提供正文'));
    const sourceTd = node('td'); sourceTd.append(sourceCell(row.source_feed));
    const statusTd = node('td'); statusTd.append(statusTag(row));
    const reasonTd = node('td');
    const reason = node('div', 'reason-text', explain(row.reason)); reason.title = explain(row.reason);
    reasonTd.append(reason, node('div', 'reason-sub', reasonSub(row)));
    const [time, date] = dateParts(row.published_at);
    const timeTd = node('td', 'time-cell', time); timeTd.append(node('small', '', date));
    const actionTd = node('td');
    const detail = node('button', 'detail-button', '↗');
    detail.setAttribute('aria-label', `查看详情：${row.title}`);
    detail.addEventListener('click', () => openDetail(row));
    actionTd.append(detail);
    tr.append(titleCell, sourceTd, statusTd, reasonTd, timeTd, actionTd);
    $('dealRows').append(tr);
  });
  $('emptyDeals').hidden = rows.length !== 0;
  if (!rows.length) {
    $('emptyDeals').querySelector('h3').textContent = all.length ? '没有符合条件的线报' : '还没有试跑数据';
    $('emptyDeals').querySelector('p').textContent = all.length ? '试试其他筛选条件，或清空搜索词。' : '从酷安和微博超话采集一批线报，筛选过程会显示在这里。';
    $('emptyTrial').hidden = all.length > 0;
  }
  $('tableInfo').textContent = rows.length ? `显示 ${from + 1}–${Math.min(from + state.pageSize, rows.length)} 条，共 ${rows.length} 条` : '共 0 条';
  $('pageNumber').textContent = `${state.page} / ${pages}`;
  $('previousPage').disabled = state.page <= 1;
  $('nextPage').disabled = state.page >= pages;
}

function openDetail(row) {
  $('detailTitle').textContent = row.title;
  $('detailBody').textContent = row.source_content || '这条线报没有提供正文。';
  clear($('detailMeta'));
  $('detailMeta').append(sourceCell(row.source_feed), node('span', '', fullDate(row.published_at)), statusTag(row));
  clear($('detailFlow'));
  const words = state.current?.config?.settings?.exclude_keywords || [];
  const matches = words.filter(word => `${row.title} ${row.source_content}`.includes(word));
  const localReason = row.rule_decision === 'filtered' ? explain(row.reason) + (matches.length ? `：${matches.join('、')}` : '') : '来源、时效、关键词与去重检查通过';
  const aiReason = row.rule_decision === 'filtered' ? '程序已过滤，未进入 AI 复核' : row.review_source === 'disabled' ? '本轮 AI 已关闭，直接使用本地规则' : explain(row.reason);
  const deliveryReason = row.delivery_status === 'preview' && state.current?.report?.notification_delivery === 'disabled' ? '本轮仅预览，不写入已发送状态' : deliveryNames[row.delivery_status] || '由程序根据规则处理';
  [['1', '程序筛选', localReason], ['2', 'AI 复核', aiReason], ['3', '发送阶段', deliveryReason]].forEach(([number, title, text]) => {
    const item = node('div', 'detail-step');
    const body = node('div'); body.append(node('strong', '', title), node('p', '', text));
    item.append(node('span', '', number), body); $('detailFlow').append(item);
  });
  $('detailLink').hidden = true;
  try {
    const url = new URL(row.source_url);
    if (url.hostname === 'new.xianbao.fun' && ['https:', 'http:'].includes(url.protocol)) {
      $('detailLink').href = url.href; $('detailLink').hidden = false;
    }
  } catch {}
  $('detailDialog').showModal();
}

function renderHistory() {
  clear($('historyRows'));
  $('emptyHistory').hidden = state.runs.length > 0;
  state.runs.forEach(run => {
    const s = run.summary;
    const tr = node('tr');
    const dateTd = node('td', 'time-cell', fullDate(run.started_at));
    dateTd.append(node('div', 'history-id', run.id.slice(0, 12)));
    const origin = {dashboard: '最新采集', reuse: '历史重跑', cli: '程序运行'}[run.origin] || run.origin;
    const countTd = node('td', '', run.finished_at ? `${s.input_count || 0} / ${s.eligible_count || 0}` : '—');
    const aiTd = node('td', '', s.review_enabled ? `${s.ai_requests || 0} 次` : run.finished_at ? '未启用' : '—');
    if (s.total_tokens) aiTd.append(node('div', 'history-id', `${s.total_tokens.toLocaleString()} tokens`));
    const statusTd = node('td');
    const phases = {collecting: ['blue', '采集中'], reviewing: ['blue', '筛选 / 审核中'], completed: [s.errors?.length || s.feed_errors?.length ? 'warning' : 'positive', s.errors?.length || s.feed_errors?.length ? '完成 · 有异常' : '已完成'], failed: ['negative', '运行失败'], interrupted: ['warning', '已中断']};
    const [tone, title] = phases[run.phase] || ['neutral', run.phase];
    statusTd.append(node('span', 'tag ' + tone, title));
    const actionTd = node('td');
    const button = node('button', 'button subtle small', '查看 →');
    button.addEventListener('click', async () => {
      try { await selectRun(run.id); location.hash = 'deals'; showPage('deals'); }
      catch (error) { toast(error.message, true); }
    });
    actionTd.append(button);
    tr.append(dateTd, node('td', '', origin), countTd, aiTd, node('td', '', run.finished_at ? `${seconds(s.elapsed_seconds)} s` : '—'), statusTd, actionTd);
    $('historyRows').append(tr);
  });
}
async function selectRun(ident) {
  state.current = await api('/api/runs/' + ident);
  state.page = 1;
  renderStats(); renderDeals();
}
async function refreshRuns(selectLatest = false) {
  state.runs = (await api('/api/runs')).runs;
  renderHistory();
  if (selectLatest || !state.current) {
    const run = state.runs.find(r => r.phase === 'completed') || state.runs[0];
    if (run) await selectRun(run.id);
  }
  updateActive();
}
function updateActive() {
  state.active = state.runs.find(run => ['collecting', 'reviewing'].includes(run.phase)) || null;
  $('activeBanner').hidden = !state.active;
  $('openTrial').disabled = !!state.active;
  $('emptyTrial').disabled = !!state.active;
  if (state.active) {
    $('activeTitle').textContent = state.active.phase === 'collecting' ? '正在采集线报' : '正在执行规则与可选 AI 审核';
    $('activeDescription').textContent = state.active.phase === 'collecting' ? '两个来源串行请求，等待时间包含全局限速。' : '优先读取审核缓存，结果完成后会保存在本机。';
    $('activeElapsed').textContent = `已运行 ${Math.max(0, Math.floor(Date.now() / 1000 - state.active.started_at))} 秒`;
    if (!state.poll) state.poll = setInterval(pollRun, 1200);
  } else if (state.poll) {
    clearInterval(state.poll); state.poll = null;
  }
}
async function pollRun() {
  if (state.pollBusy) return;
  state.pollBusy = true;
  const previous = state.active?.id;
  try {
    await refreshRuns(false);
    if (previous && !state.active) {
      await selectRun(previous);
      const success = state.current.phase === 'completed';
      toast(success ? '试跑完成，结果已保存。' : '本轮未完成，请查看运行记录。', !success);
    }
  } catch (error) {
    clearInterval(state.poll); state.poll = null;
    toast(error.message, true);
  } finally { state.pollBusy = false; }
}

function words(value) { return [...new Set(value.split(/\r?\n/).map(x => x.trim()).filter(Boolean))]; }
function draft() {
  return {include_keywords: words($('includeKeywords').value), exclude_keywords: words($('excludeKeywords').value),
    max_age_minutes: Number($('maxAge').value), max_items: Number($('maxItems').value),
    feed_limit: Number($('feedLimit').value), search_interval_seconds: Number($('requestGap').value),
    ai: {enabled: $('aiEnabled').checked, base_url: $('modelBaseUrl').value.trim(), model: $('modelName').value.trim(),
      on_error: $('onError').value, on_unknown: $('onUnknown').value}};
}
function fillConfig() {
  const c = state.config.settings;
  $('includeKeywords').value = c.include_keywords.join('\n');
  $('excludeKeywords').value = c.exclude_keywords.join('\n');
  $('maxAge').value = c.max_age_minutes; $('maxItems').value = c.max_items;
  $('feedLimit').value = c.feed_limit; $('requestGap').value = c.search_interval_seconds;
  $('aiEnabled').checked = !!c.ai.enabled; $('modelBaseUrl').value = c.ai.base_url || '';
  $('modelName').value = c.ai.model || ''; $('onError').value = c.ai.on_error; $('onUnknown').value = c.ai.on_unknown;
  $('credentialStatus').textContent = state.config.credential.configured ? `已配置密钥 · ${state.config.credential.source === 'environment' ? '环境变量' : '本机加密文件'}` : '尚未配置模型密钥';
  $('credentialDot').className = state.config.credential.configured ? 'small-dot' : 'small-dot neutral-dot';
  $('modelOverride').textContent = state.config.environment_overrides.model ? `环境变量优先，实际使用：${state.config.effective_model}` : '';
  $('baseUrlOverride').textContent = state.config.environment_overrides.base_url ? `环境变量优先，实际使用：${state.config.effective_base_url}` : '';
  $('configVersion').textContent = `版本 ${state.config.revision.slice(0, 8)}`;
  state.dirty = false; updateDirty();
}
function updateDirty() {
  $('dirtyStatus').textContent = state.dirty ? '有未保存的更改 · 保存后下一轮生效' : '所有更改已保存';
  $('saveConfig').disabled = !state.dirty;
}
async function previewRules() {
  if (!state.current?.snapshot?.items?.length) return toast('请先采集一批线报，再试算关键词规则。', true);
  if (!$('settingsForm').reportValidity()) return;
  $('previewRules').disabled = true;
  try {
    const result = await api('/api/rules/preview', {run_id: state.current.id, settings: draft()});
    clear($('ruleStats')); clear($('ruleRows'));
    [['当前规则保留', result.before_kept], ['草稿规则保留', result.after_kept], ['新增排除', result.newly_removed], ['新增保留', result.newly_kept]].forEach(([label, count]) => {
      const card = node('div', 'preview-stat', label); card.append(node('strong', '', count)); $('ruleStats').append(card);
    });
    const rows = [...result.rows].sort((a, b) => Number(b.before_kept !== b.after_kept) - Number(a.before_kept !== a.after_kept));
    rows.forEach(row => {
      const entry = node('div', 'rule-row'); const text = node('div');
      text.append(node('div', 'deal-title', row.title), node('p', '', explain(row.reason) + (row.matched_keywords.length ? `：${row.matched_keywords.join('、')}` : '')));
      const changed = row.before_kept !== row.after_kept;
      entry.append(text, node('span', 'tag ' + (row.after_kept ? 'positive' : changed ? 'warning' : 'neutral'), changed ? row.after_kept ? '新增保留' : '新增排除' : row.after_kept ? '仍保留' : '仍排除'));
      $('ruleRows').append(entry);
    });
    $('ruleDialog').showModal();
  } catch (error) { toast(error.message, true); }
  finally { $('previewRules').disabled = false; }
}

function openTrial() {
  if (!state.config) return;
  $('trialAi').checked = !!state.config.settings.ai.enabled;
  $('reuseOption').disabled = !state.current?.snapshot?.items?.length;
  document.querySelector('input[name="trialSource"][value="latest"]').checked = true;
  $('trialLimits').textContent = `每个来源最多 ${state.config.settings.feed_limit} 条，串行请求间隔至少 ${state.config.settings.search_interval_seconds} 秒。`;
  $('trialDialog').showModal();
}
async function startTrial(event) {
  event.preventDefault();
  $('startTrial').disabled = true;
  try {
    const result = await api('/api/runs', {source: document.querySelector('input[name="trialSource"]:checked').value,
      use_ai: $('trialAi').checked, run_id: state.current?.id, revision: state.config.revision});
    $('trialDialog').close();
    await refreshRuns(false);
    const immediate = state.runs.find(run => run.id === result.run_id);
    if (immediate && !['collecting', 'reviewing'].includes(immediate.phase)) {
      await selectRun(result.run_id); toast('试跑已完成，结果已保存。');
    } else toast('试跑已开始，可以继续查看已有结果。');
    location.hash = 'deals'; showPage('deals');
  } catch (error) { toast(error.message, true); }
  finally { $('startTrial').disabled = false; }
}

function bindEvents() {
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => { location.hash = button.dataset.page; showPage(button.dataset.page); }));
  window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  }));
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    state.filter = button.dataset.filter; state.page = 1;
    document.querySelectorAll('[data-filter]').forEach(item => item.classList.toggle('selected', item === button)); renderDeals();
  }));
  $('search').addEventListener('input', () => { state.page = 1; renderDeals(); });
  $('sourceFilter').addEventListener('change', () => { state.page = 1; renderDeals(); });
  $('previousPage').addEventListener('click', () => { state.page--; renderDeals(); });
  $('nextPage').addEventListener('click', () => { state.page++; renderDeals(); });
  $('openTrial').addEventListener('click', openTrial); $('emptyTrial').addEventListener('click', openTrial);
  $('trialForm').addEventListener('submit', startTrial);
  ['refreshRuns', 'refreshHistory'].forEach(id => $(id).addEventListener('click', async () => {
    try { await refreshRuns(true); toast('运行记录已刷新。'); }
    catch (error) { toast(error.message, true); }
  }));
  $('settingsForm').addEventListener('input', () => { state.dirty = true; updateDirty(); });
  $('settingsForm').addEventListener('change', () => { state.dirty = true; updateDirty(); });
  $('settingsForm').addEventListener('submit', async event => {
    event.preventDefault(); $('saveConfig').disabled = true;
    try {
      state.config = await api('/api/config', {revision: state.config.revision, settings: draft()});
      fillConfig(); toast('配置已保存，下一轮运行开始生效。');
    } catch (error) { toast(error.message, true); updateDirty(); }
  });
  $('reloadConfig').addEventListener('click', async () => {
    try { state.config = await api('/api/config'); fillConfig(); toast('已重新加载当前配置。'); }
    catch (error) { toast(error.message, true); }
  });
  $('previewRules').addEventListener('click', previewRules);
  window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
}

async function init() {
  bindEvents(); showPage(location.hash.slice(1));
  try {
    const initial = await api('/api/bootstrap');
    state.config = initial.config; state.token = initial.csrf_token; state.runs = initial.runs;
    fillConfig(); renderHistory();
    const latest = state.runs.find(run => run.phase === 'completed') || state.runs[0];
    if (latest) await selectRun(latest.id);
    else { renderStats(); renderDeals(); }
    updateActive();
  } catch (error) { toast(error.message, true); }
}
init();
