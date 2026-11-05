import { api, ApiError } from './api.js';
import { store, persist, flush, editorState, setContent, markSaved, hasDirty } from './store.js';
import { createSettingsController, ensureSettingsSaved } from './settings.js';
import { resultSummary } from './task-results.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const count = text => [...String(text || '').replace(/\s/g, '')].length;
const state = { projects: [], config: {}, project: null, chapters: [], warnings: [], roles: [], docs: {}, task: null, polling: null, loading: false, renderVersion: 0, pendingActions: new Set(), lastCompleted: '', dialogClose: null, fontSize: 14 };
const views = { architecture: { title: '生成大纲', english: 'STORY ARCHITECTURE', description: '从一个灵感出发，构筑人物、世界与故事主线。', model: 'architecture_llm' }, directory: { title: '生成目录', english: 'CHAPTER BLUEPRINT', description: '把故事拆成有起伏、有推进的章节，让每一章都有方向。', model: 'chapter_outline_llm' }, draft: { title: '生成草稿', english: 'CHAPTER DRAFT', description: '准备上下文，确认提示词，让故事在笔下逐章展开。', model: 'prompt_draft_llm' }, finalize: { title: '定稿章节', english: 'POLISH & FINALIZE', description: '审校与扩写正文，确认定稿，更新后续创作需要的记忆。', model: 'final_chapter_llm' } };
const kindNames = { architecture: '小说大纲', directory: '章节目录', chapter: '章节正文', prompt: '生成提示词', summary: '全局摘要', characters: '人物状态', plot_arcs: '剧情要点', review: '审校报告' };
const taskNames = { architecture: '生成大纲', directory: '生成目录', prompt: '准备提示词', draft: '生成草稿', review: '一致性审校', enrich: '扩写章节', finalize: '定稿章节', batch: '批量创作', reindex: '补建检索记忆', knowledge_import: '导入知识库', knowledge_clear: '清理向量索引' };
const statusNames = { queued: '排队中', running: '生成中', cancelling: '正在停止', cancelled: '已停止', succeeded: '已完成', failed: '任务失败', interrupted: '任务中断' };
const activeStatuses = ['queued', 'running', 'cancelling'];
const chapterKinds = ['chapter', 'prompt', 'review'];
const settings = createSettingsController({
  isVisible: () => store.view === 'settings',
  getProject: () => state.project,
  toast,
  confirm: confirmDialog,
  prompt: settingsPrompt,
  openDirectory: path => showImportProject(path),
  onSaved: data => {
    state.config = { ...state.config, models: data.models || state.config.models || [], tasks: data.tasks || data.config?.choose_configs || {}, other_params: data.config?.other_params || {}, language: data.config?.language || 'zh' };
  },
});

function toast(message, type = 'success') {
  const node = document.createElement('div'); node.className = `toast ${type}`; node.textContent = message; $('#toast-container').append(node);
  setTimeout(() => node.remove(), type === 'error' ? 9000 : 4500);
}

async function guard(key, action) {
  if (state.pendingActions.has(key)) return;
  state.pendingActions.add(key); syncBusy();
  try { return await action(); }
  catch (error) { console.error(error); toast(error.message || '操作失败，请查看任务日志后重试。', 'error'); }
  finally { state.pendingActions.delete(key); syncBusy(); }
}

function isRunning() { return activeStatuses.includes(state.task?.status); }
function currentForm() {
  const key = store.projectId;
  if (!store.forms[key]?.scopes) {
    const legacy = store.forms[key];
    store.forms[key] = { scopes: legacy ? { architecture: { ...legacy } } : {} };
  }
  const scope = ['draft', 'finalize'].includes(store.view) ? `${store.view}:${store.chapter}` : store.view;
  if (!store.forms[key].scopes[scope]) store.forms[key].scopes[scope] = { ...(state.project?.params || {}), chapter: store.chapter, role_ids: [] };
  return store.forms[key].scopes[scope];
}
function getDoc(kind, chapter = chapterKinds.includes(kind) ? store.chapter : undefined) { chapter = chapterKinds.includes(kind) ? chapter : undefined; return editorState(store.projectId, kind, chapter, state.docs[docKey(kind, chapter)] || {}); }
function docKey(kind, chapter) { return `${kind}${chapterKinds.includes(kind) && chapter ? `:${chapter}` : ''}`; }
function currentTab() { return store.selectedTab[store.view] || (store.view === 'draft' ? 'prompt' : 'chapter'); }
function currentKind() { return ['architecture', 'directory'].includes(store.view) ? store.view : currentTab(); }
function currentChapter(kind = currentKind()) { return chapterKinds.includes(kind) ? Number(store.chapter) : undefined; }
function currentEditor() { return getDoc(currentKind(), currentChapter()); }
function modelName(view = store.view) {
  const name = state.config.tasks?.[views[view]?.model];
  if (!name) return '未选择模型预设';
  const model = state.config.models?.find(item => item.name === name)?.model_name;
  return model ? `${name} · ${model}` : name;
}
function selectedChapter() { return state.chapters.find(chapter => chapter.chapter_number === Number(store.chapter)); }
function chapterLabel(chapter) { return `第 ${chapter.chapter_number} 章${chapter.chapter_title ? ` · ${chapter.chapter_title}` : ''}`; }
function formatTime(date) { if (!date) return ''; const value = new Date(date); return Number.isNaN(value.getTime()) ? String(date) : value.toLocaleString('zh-CN', { hour12: false }); }
function chapterStatus(chapter) { return ({ finalized: '已定稿', index_pending: '已定稿 · 检索待补建', memory_stale: '创作记忆待重建', modified: '定稿后已修改', draft: '已有草稿', empty: '待创作' })[chapter?.status] || (chapter?.word_count ? '已有正文' : '待创作'); }
function statusClass(chapter) { return chapter?.status === 'finalized' ? 'success' : ['index_pending', 'memory_stale', 'modified'].includes(chapter?.status) ? 'warning' : ''; }

function syncBusy() {
  const running = isRunning();
  $$('[data-generation]').forEach(button => { button.disabled = running || state.pendingActions.has('job') || !state.project; });
  $$('[data-save]').forEach(button => { button.disabled = running || state.pendingActions.has('save') || !state.project || !currentEditor().modified; });
  $('#export-project').disabled = !state.project || state.pendingActions.has('export');
  $('#job-history').disabled = !state.project;
  $('#task-cancel').disabled = !running || state.task?.status === 'cancelling' || state.pendingActions.has('cancel');
  $('#task-cancel').textContent = state.task?.status === 'cancelling' ? '等待当前步骤结束…' : '停止任务';
}

function showDialog(title, body, actions = '', options = {}) {
  const dialog = $('#app-dialog'); if (dialog.open) dialog.close();
  dialog.className = options.wide ? 'dialog-wide' : '';
  $('#dialog-content').innerHTML = `<div class="dialog-header"><h2>${escape(title)}</h2><button type="button" class="icon-button" data-close-dialog aria-label="关闭对话框">✕</button></div><div class="dialog-body">${body}</div><div class="dialog-actions">${actions || '<button type="button" class="button button-outline" data-close-dialog>关闭</button>'}</div>`;
  $$('[data-close-dialog]', dialog).forEach(button => button.addEventListener('click', () => dialog.close()));
  state.dialogClose = options.onClose || null;
  dialog.showModal(); return dialog;
}

function confirmDialog(title, message, actionText = '继续', danger = false) {
  return new Promise(resolve => {
    let settled = false;
    const complete = value => { if (settled) return; settled = true; resolve(value); };
    const dialog = showDialog(title, `<p>${escape(message)}</p>`, `<button type="button" class="button button-outline" data-close-dialog>取消</button><button type="button" class="button ${danger ? 'button-danger' : 'button-primary'}" id="dialog-confirm">${escape(actionText)}</button>`, { onClose: () => complete(false) });
    $('#dialog-confirm', dialog).addEventListener('click', () => { complete(true); dialog.close(); });
  });
}

function settingsPrompt(title, label, value = '') {
  return new Promise(resolve => {
    let settled = false;
    const complete = result => { if (!settled) { settled = true; resolve(result); } };
    const dialog = showDialog(title, `<form id="settings-name-form"><div class="field"><label for="settings-preset-name">${escape(label)}</label><input id="settings-preset-name" maxlength="150" value="${escape(value)}" required autocomplete="off"></div></form>`, '<button type="button" class="button button-outline" data-close-dialog>取消</button><button type="submit" form="settings-name-form" class="button button-primary">确定</button>', { onClose: () => complete(null) });
    $('#settings-name-form', dialog).addEventListener('submit', event => { event.preventDefault(); complete($('#settings-preset-name', dialog).value); dialog.close(); });
    $('#settings-preset-name', dialog).focus(); $('#settings-preset-name', dialog).select();
  });
}

async function bootstrap() {
  try {
    const data = await api.bootstrap(); state.projects = data.projects || []; state.config = data.config || {};
    $('#connection-dot').className = 'status-dot online'; $('#connection-text').textContent = '本地服务已连接';
    const desired = state.projects.some(project => project.id === store.projectId) ? store.projectId : data.active_project_id || state.projects[0]?.id;
    renderProjectSelector();
    if (desired) await openProject(desired); else { state.project = null; render(); }
  } catch (error) {
    $('#connection-dot').className = 'status-dot error'; $('#connection-text').textContent = '本地服务未连接';
    $('#project-select').innerHTML = '<option>无法连接</option>';
    $('#main-content').innerHTML = `<div class="empty-workspace"><div class="empty-mark">叙</div><h1>工作台暂未连接</h1><p>${escape(error.message)}</p><button class="button button-primary" type="button" id="retry-connection">重新连接</button></div>`;
    $('#retry-connection').addEventListener('click', bootstrap);
  }
}

function renderProjectSelector() {
  $('#project-select').innerHTML = state.projects.length ? state.projects.map(project => `<option value="${escape(project.id)}">${escape(project.name || project.path)}</option>`).join('') : '<option value="">尚未打开小说</option>';
  $('#project-select').value = store.projectId;
}

async function openProject(id) {
  const project = state.projects.find(item => item.id === id); if (!project) return;
  clearTimeout(state.polling); state.task = null; $('#task-panel').hidden = true;
  state.loading = true; store.projectId = id; state.project = project; state.docs = {}; state.chapters = []; state.roles = []; state.renderVersion += 1;
  const version = state.renderVersion;
  $('#project-select').value = id; $('#project-path').textContent = project.path; $('#project-path').title = project.path;
  $('#footer-project').textContent = project.path || '本地创作 · 文件保存在你的电脑';
  persist(); render();
  const results = await Promise.allSettled([api.chapters(id), api.roles(id), ...['architecture', 'directory', 'summary', 'characters', 'plot_arcs'].map(kind => api.document(id, kind)), api.jobs(id)]);
  if (version !== state.renderVersion) return;
  if (results[0].status === 'fulfilled') { state.chapters = results[0].value.chapters || []; state.warnings = results[0].value.warnings || []; }
  else toast(results[0].reason.message, 'error');
  if (results[1].status === 'fulfilled') state.roles = results[1].value.roles || [];
  ['architecture', 'directory', 'summary', 'characters', 'plot_arcs'].forEach((kind, index) => { const result = results[index + 2]; if (result.status === 'fulfilled') { state.docs[kind] = result.value; getDoc(kind); } });
  const jobsResult = results[7];
  if (jobsResult.status === 'fulfilled') {
    const jobs = jobsResult.value.jobs || [];
    const task = jobs.find(job => activeStatuses.includes(job.status)) || jobs.find(job => job.id === store.taskId) || jobs[0];
    if (task) { state.task = task; store.taskId = task.id; renderTask(); beginPolling(); }
    else { state.task = null; $('#task-panel').hidden = true; }
  }
  state.loading = false; await loadChapter(); render(); syncBusy();
}

async function loadChapter() {
  if (!state.project) return;
  const projectId = store.projectId, chapter = Number(store.chapter), version = state.renderVersion;
  const results = await Promise.allSettled(chapterKinds.map(kind => api.document(projectId, kind, chapter)));
  if (version !== state.renderVersion || chapter !== Number(store.chapter)) return;
  results.forEach((result, index) => { if (result.status === 'fulfilled') { const kind = chapterKinds[index]; state.docs[docKey(kind, chapter)] = result.value; getDoc(kind, chapter); } });
}

async function refreshDocuments(documents = [], force = false) {
  for (const remote of documents) {
    if (!remote.kind) continue;
    const chapter = chapterKinds.includes(remote.kind) ? remote.chapter : undefined;
    const local = getDoc(remote.kind, chapter), shouldKeep = local.modified && !force && local.content !== remote.content;
    state.docs[docKey(remote.kind, chapter)] = remote;
    if (!shouldKeep) { local.content = remote.content || ''; local.modified = false; }
    local.savedContent = remote.content || ''; local.revision = remote.revision || local.revision || ''; persist();
  }
  const result = await api.chapters(store.projectId); state.chapters = result.chapters || []; state.warnings = result.warnings || [];
}

async function refreshLiveDocuments() {
  const requests = ['architecture', 'directory', 'summary', 'characters', 'plot_arcs'].map(kind => api.document(store.projectId, kind));
  requests.push(...chapterKinds.map(kind => api.document(store.projectId, kind, Number(store.chapter))));
  const results = await Promise.allSettled(requests);
  await refreshDocuments(results.filter(result => result.status === 'fulfilled').map(result => result.value));
}

function field(name, label, type = 'text', options = {}) {
  const value = currentForm()[name] ?? options.default ?? '';
  let input;
  if (type === 'textarea') input = `<textarea id="field-${name}" name="${name}" data-param="${name}" rows="${options.rows || 3}" placeholder="${escape(options.placeholder || '')}">${escape(value)}</textarea>`;
  else input = `<input id="field-${name}" name="${name}" data-param="${name}" type="${type}" value="${escape(value)}" ${type === 'number' ? `min="${options.min || (name === 'word_number' ? 100 : 1)}" max="${options.max || 1000000}"` : ''} placeholder="${escape(options.placeholder || '')}">`;
  return `<div class="field ${options.full ? 'full-width' : ''}"><label for="field-${name}">${escape(label)}${options.optional ? '<span class="optional">选填</span>' : ''}</label>${input}${options.help ? `<p class="field-help">${escape(options.help)}</p>` : ''}</div>`;
}

function render() {
  const view = views[store.view];
  $$('[data-view]').forEach(button => { button.classList.toggle('active', button.dataset.view === store.view); button.setAttribute('aria-current', button.dataset.view === store.view ? 'page' : 'false'); });
  if (store.view === 'settings') { renderSettings(); return; }
  if (!state.project) { $('#main-content').innerHTML = '<div class="empty-workspace"><div class="empty-mark">叙</div><h1>写下你的下一个故事</h1><p>新建一部小说，或打开已有的小说目录。<br>从大纲、目录到章节定稿，按自己的节奏完成创作。</p><div class="button-row"><button type="button" class="button button-primary" data-action="new">＋ 新建小说</button><button type="button" class="button button-outline" data-action="import">打开已有小说</button></div></div>'; bindActions(); syncBusy(); return; }
  const docsDirty = Object.entries(store.documents).filter(([key, value]) => key.startsWith(`${store.projectId}:`) && value.modified).length;
  $('#main-content').innerHTML = `<div class="page-header"><div><div class="page-kicker">${view.english}</div><h1>${view.title}</h1><p>${view.description}</p></div><div class="header-meta">模型预设 · ${escape(modelName())}</div></div><div class="step-strip">${Object.entries(views).map(([key, item], index) => `<span class="${key === store.view ? 'current' : index < Object.keys(views).indexOf(store.view) ? 'done' : ''}">${String(index + 1).padStart(2, '0')}　${item.title}</span>`).join('')}</div>${state.loading ? '<div class="notice"><span class="spinner"></span>正在读取小说文件…</div>' : ''}${prerequisiteNotice()}${docsDirty ? '<div class="notice"><span aria-hidden="true">✎</span><p>工作台保留了未保存的编辑。切换模块不会丢失，保存后才会写入小说文件。</p></div>' : ''}<div class="work-grid"><aside class="parameter-column">${renderParameters()}${renderSideInfo()}</aside><div class="content-stack">${renderEditor()}${store.view === 'directory' ? renderDirectoryPreview() : ''}${renderNextStep()}</div></div>`;
  bindActions(); bindParameters(); bindEditor(); syncBusy();
}

function prerequisiteNotice() {
  if (store.view === 'directory' && !getDoc('architecture').content.trim()) return '<div class="notice warning"><span>↗</span><p>请先生成并采用大纲，或在大纲页面粘贴并保存已有大纲。</p><button class="button-link" data-view="architecture">前往生成大纲</button></div>';
  if (['draft', 'finalize'].includes(store.view) && !getDoc('directory').content.trim()) return '<div class="notice warning"><span>↗</span><p>当前还没有章节目录。先生成目录，可自动带入章节标题和梗概。</p><button class="button-link" data-view="directory">前往生成目录</button></div>';
  if (state.warnings.length && ['draft', 'directory', 'finalize'].includes(store.view)) return `<div class="notice warning"><span>!</span><p>${state.warnings.map(escape).join('；')}</p></div>`;
  return '';
}

function roleField() {
  if (!state.roles.length) return '';
  const selected = currentForm().role_ids || [];
  return `<div class="field full-width"><label for="role-select">角色库参考<span class="optional">可多选</span></label><select id="role-select" multiple size="${Math.min(state.roles.length, 4)}">${state.roles.map(role => `<option value="${escape(role.id)}" ${selected.includes(role.id) ? 'selected' : ''}>${escape(role.name)}${role.category ? ` · ${escape(role.category)}` : ''}</option>`).join('')}</select><p class="field-help">按住 Ctrl / ⌘ 选择多个角色。</p></div>`;
}

function renderParameters() {
  const view = store.view, form = currentForm(); let body = '', actions = '';
  if (view === 'architecture') {
    body = field('topic', '故事主题', 'textarea', { placeholder: '你想讲述一个怎样的故事？', rows: 3, full: true }) + field('genre', '小说类型', 'text', { placeholder: '如：玄幻、都市、悬疑' }) + `<div class="field-grid">${field('num_chapters', '总章节数', 'number', { default: 50, max: 10000 })}${field('word_number', '单章字数', 'number', { default: 3000, max: 100000 })}</div>` + field('user_guidance', '创作要求', 'textarea', { optional: true, placeholder: '文风、视角、人物设定、避免的情节…', full: true }) + (state.roles.length ? `<details class="context-details" data-form-details="rolesExpanded" ${form.rolesExpanded ? 'open' : ''}><summary>角色库参考 <span>可选</span></summary><div class="context-fields">${roleField()}</div></details>` : '');
    actions = '<button type="button" class="button button-primary button-block" data-generation data-action="generate-architecture">✦ 生成大纲</button><button type="button" class="button button-outline button-block" data-generation data-action="resume-architecture">继续上次生成</button><p class="parameter-note">重新生成会保留为候选版本。采用结果后，才会替换当前大纲。</p>';
  } else if (view === 'directory') {
    body = field('num_chapters', '全书章节数', 'number', { default: 50, max: 10000 }) + field('word_number', '单章目标字数', 'number', { default: 3000, max: 100000 }) + field('user_guidance', '目录生成要求', 'textarea', { optional: true, placeholder: '章节节奏、重要转折、分卷安排…', rows: 5, full: true });
    actions = '<button type="button" class="button button-primary button-block" data-generation data-action="generate-directory">✦ 生成目录</button><button type="button" class="button button-outline button-block" data-generation data-action="resume-directory">继续生成目录</button><p class="parameter-note">依据已保存的大纲生成章节蓝图。编辑目录后，请保存并重新解析。</p>';
  } else {
    const chapter = selectedChapter();
    const numbers = [...new Set([...state.chapters.map(item => item.chapter_number), Number(store.chapter)])].sort((a, b) => a - b);
    body = `<div class="chapter-controls"><button type="button" class="chapter-arrow" data-action="previous-chapter" aria-label="上一章" ${Number(store.chapter) <= 1 ? 'disabled' : ''}>‹</button><select id="chapter-select" aria-label="选择章节">${numbers.map(number => { const item = state.chapters.find(chapter => chapter.chapter_number === number); return `<option value="${number}" ${number === Number(store.chapter) ? 'selected' : ''}>${escape(item ? chapterLabel(item) : `第 ${number} 章`)}</option>`; }).join('')}</select><button type="button" class="chapter-arrow" data-action="next-chapter" aria-label="下一章">›</button></div><div class="status-pills"><span class="pill ${statusClass(chapter)}">${escape(chapterStatus(chapter))}</span><span class="pill">${chapter?.word_count || 0} 字已保存</span></div>${chapter?.chapter_summary ? `<div class="chapter-context"><strong>${escape(chapter.chapter_title || '章节梗概')}</strong>${escape(chapter.chapter_summary)}</div>` : ''}` + field('word_number', '本章目标字数', 'number', { default: 3000, max: 100000 });
    if (view === 'draft') {
      body += field('user_guidance', '本章创作要求', 'textarea', { optional: true, placeholder: '本章重点、叙述风格、衔接要求…', full: true }) + `<details class="context-details" data-form-details="contextExpanded" ${form.contextExpanded ? 'open' : ''}><summary>上下文补充 <span>人物、道具与场景 · 可选</span></summary><div class="context-fields">` + field('characters_involved', '出场人物', 'text', { optional: true, placeholder: '如：林舟、顾晚' }) + field('key_items', '关键物品', 'text', { optional: true, placeholder: '本章的重要道具' }) + field('scene_location', '场景地点', 'text', { optional: true, placeholder: '故事发生在哪里' }) + field('time_constraint', '时间限制', 'text', { optional: true, placeholder: '如：同一天傍晚' }) + roleField() + '</div></details>';
      actions = '<button type="button" class="button button-primary button-block" data-generation data-action="prepare-prompt">① 准备提示词</button><button type="button" class="button button-outline button-block" data-generation data-action="generate-draft">② 生成草稿</button><button type="button" class="button button-quiet button-block" data-generation data-action="batch">批量顺序创作 →</button><p class="parameter-note">先准备并采用提示词，可以编辑确认，再生成本章正文。</p>';
    } else {
      body += field('user_guidance', '审校 / 扩写要求', 'textarea', { optional: true, placeholder: '关注的人物逻辑、节奏，或希望补足的细节…', rows: 4, full: true });
      actions = '<button type="button" class="button button-primary button-block" data-generation data-action="finalize">✓ 定稿本章</button><div class="button-row" style="margin-top:8px"><button type="button" class="button button-outline" style="flex:1" data-generation data-action="review">一致性审校</button><button type="button" class="button button-outline" style="flex:1" data-generation data-action="enrich">扩写正文</button></div><button type="button" class="button button-quiet button-block" data-generation data-action="reindex">补建检索记忆</button><p class="parameter-note">定稿会更新全局摘要、人物状态和检索记忆。审校与扩写结果需确认采用。</p>';
    }
  }
  return `<section class="card parameter-card"><div class="card-header"><h2>${['draft', 'finalize'].includes(view) ? '章节与参数' : '创作参数'}</h2><span class="micro">${['draft', 'finalize'].includes(view) ? `CH. ${String(store.chapter).padStart(2, '0')}` : 'PARAMETERS'}</span></div><div class="parameter-body">${body}</div><div class="parameter-actions">${actions}</div></section>`;
}

function renderSideInfo() {
  const messages = { architecture: ['大纲是一切的起点', '主题与要求越具体，故事的方向就越清楚。生成后可以直接编辑，形成你自己的创作蓝图。'], directory: ['每一章，都有它的位置', '完整目录是草稿的导航。你可以随时调整文本，保存后章节列表会同步更新。'], draft: ['让灵感保持连贯', '生成草稿会结合大纲、目录、前文摘要与人物状态。采用候选结果后，再保存你的修改。'], finalize: ['为下一章留下线索', '定稿负责更新创作记忆；扩写是独立操作，可先预览。修改早期章节后，关注后续记忆是否需要重建。'] };
  const [title, text] = messages[store.view]; return `<div class="card side-info"><strong>✧ ${title}</strong><p>${text}</p></div>`;
}

function renderEditor() {
  const kind = currentKind(), doc = currentEditor(), file = state.docs[docKey(kind, currentChapter())]?.filename;
  const tabs = store.view === 'draft' ? [['prompt', '生成提示词'], ['chapter', '章节正文']] : store.view === 'finalize' ? [['chapter', '章节正文'], ['review', '审校报告'], ['summary', '全局摘要'], ['characters', '人物状态'], ['plot_arcs', '剧情要点']] : [];
  const placeholders = { architecture: '在这里构筑你的故事。\n\n填写左侧创作参数，点击「生成大纲」。\n也可以直接粘贴已有大纲，编辑并保存。', directory: '故事的章节，从这里展开。\n\n点击「生成目录」，将已保存的大纲转化为章节蓝图。\n你也可以在此编辑并保存已有的目录。', prompt: '先看清故事将如何发生。\n\n点击「准备提示词」，后台将整理本章需要的上下文。\n完成后采用结果，可在这里编辑提示词，再点击「生成草稿」。', chapter: '这一章，等你落笔。\n\n生成并采用草稿后，正文会显示在这里。\n你也可以直接输入或粘贴已有正文。', review: '审校报告会显示在这里。\n\n点击「一致性审校」，检查当前正文的人物、剧情与设定是否连贯。', summary: '定稿章节后，这里会记录全书已经发生的情节。', characters: '定稿章节后，这里会记录人物状态与变化。', plot_arcs: '在这里记录主线推进、伏笔与待回收线索。' };
  return `<section class="card editor-card">${tabs.length ? `<div class="editor-tabs" role="tablist" aria-label="内容视图">${tabs.map(([key, name]) => `<button type="button" role="tab" aria-selected="${kind === key}" data-tab="${key}" class="${kind === key ? 'active' : ''}">${name}${getDoc(key).modified ? ' ·' : ''}</button>`).join('')}</div>` : ''}<div class="editor-toolbar"><div class="editor-heading">${escape(kindNames[kind])}<span class="file-label">${escape(file ? file.split(/[\\/]/).pop() : chapterKinds.includes(kind) ? `CHAPTER ${String(store.chapter).padStart(2, '0')}` : 'DOCUMENT')}</span></div><div class="editor-actions"><button type="button" class="icon-button" data-action="font-smaller" aria-label="缩小字号" title="缩小字号">A−</button><button type="button" class="icon-button" data-action="font-larger" aria-label="放大字号" title="放大字号">A＋</button><button type="button" class="icon-button" data-action="zen" aria-label="切换专注模式" title="专注模式">⛶</button><button type="button" class="button button-quiet button-small" data-action="history">历史</button><button type="button" class="button button-primary button-small" data-save data-action="save">保存修改</button></div></div><label class="visually-hidden" for="content-editor">${escape(kindNames[kind])}</label><textarea id="content-editor" class="editor" style="font-size:${state.fontSize}px" spellcheck="false" placeholder="${escape(placeholders[kind] || '')}">${escape(doc.content)}</textarea><div class="editor-footer"><span id="editor-save-state" class="save-state ${doc.modified ? 'dirty' : ''}">${doc.modified ? '有未保存的修改 · 浏览器已保留' : doc.revision ? '已保存到小说文件' : '尚未保存内容'}</span><span><span id="word-count">${count(doc.content).toLocaleString('zh-CN')}</span> 字 <span aria-hidden="true">·</span> <button type="button" class="button-link" style="font-size:10px;padding:0" data-action="export-current">导出当前内容</button></span></div></section>`;
}

function renderDirectoryPreview() {
  return `<section class="card directory-preview-card"><div class="card-header"><h2>章节预览 <span class="muted">· ${state.chapters.length} 章</span></h2><button type="button" class="button-link" data-action="parse-directory">保存并重新解析 ↻</button></div><div class="directory-preview">${state.chapters.length ? state.chapters.map(chapter => `<button type="button" class="directory-item" data-open-chapter="${chapter.chapter_number}"><span class="directory-num">${String(chapter.chapter_number).padStart(2, '0')}</span><span><strong>${escape(chapter.chapter_title || `第 ${chapter.chapter_number} 章`)}</strong><p>${escape(chapter.chapter_summary || '未填写本章简述')}</p></span><span class="pill ${statusClass(chapter)}" style="margin-left:auto">${escape(chapterStatus(chapter))}</span></button>`).join('') : '<div class="preview-empty">保存章节目录后，这里将显示解析的章节列表。</div>'}</div></section>`;
}

function renderNextStep() {
  const target = { architecture: 'directory', directory: 'draft', draft: 'finalize', finalize: 'draft' }[store.view];
  const texts = { architecture: ['大纲准备好了？', '为故事规划每一章。'], directory: ['章节蓝图已就绪？', '开始写下第一段正文。'], draft: ['这一章写得怎么样？', '审校、扩写，确认后更新创作记忆。'], finalize: ['故事继续向前。', '定稿后切换到下一章，继续创作。'] };
  const [title, description] = texts[store.view];
  return `<div class="next-step"><span><strong>${title}</strong> ${description}</span><button type="button" class="button button-outline button-small" data-action="${store.view === 'finalize' ? 'write-next' : 'next-step'}" data-target="${target}">${store.view === 'finalize' ? '创作下一章' : `前往${views[target].title}`} →</button></div>`;
}

function renderSettings() {
  settings.mount($('#main-content')); syncBusy();
}

function bindParameters() {
  const form = currentForm();
  $$('[data-param]').forEach(input => input.addEventListener('input', () => { form[input.dataset.param] = input.type === 'number' ? Number(input.value) : input.value; persist(); }));
  $('#role-select')?.addEventListener('change', event => { form.role_ids = [...event.target.selectedOptions].map(option => option.value); persist(); });
  $$('[data-form-details]').forEach(details => details.addEventListener('toggle', () => { form[details.dataset.formDetails] = details.open; persist(); }));
  $('#chapter-select')?.addEventListener('change', event => switchChapter(Number(event.target.value)));
}

function bindEditor() {
  const editor = $('#content-editor'); if (!editor) return;
  editor.addEventListener('input', () => {
    const doc = setContent(store.projectId, currentKind(), currentChapter(), editor.value);
    $('#word-count').textContent = count(editor.value).toLocaleString('zh-CN');
    $('#editor-save-state').className = `save-state ${doc.modified ? 'dirty' : ''}`;
    $('#editor-save-state').textContent = doc.modified ? '有未保存的修改 · 浏览器已保留' : '已保存到小说文件'; syncBusy();
  });
  editor.addEventListener('keydown', event => {
    if (event.key === 'Tab') { event.preventDefault(); const start = editor.selectionStart, end = editor.selectionEnd; editor.setRangeText('　　', start, end, 'end'); editor.dispatchEvent(new Event('input')); }
  });
}

function bindActions() {
  $$('[data-view]', $('#main-content')).forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  $$('[data-tab]').forEach(button => button.addEventListener('click', () => { store.selectedTab[store.view] = button.dataset.tab; persist(); render(); }));
  $$('[data-open-chapter]').forEach(button => button.addEventListener('click', async () => { await switchChapter(Number(button.dataset.openChapter)); switchView('draft'); }));
  $$('[data-action]').forEach(button => button.addEventListener('click', () => handleAction(button.dataset.action, button)));
}

async function handleAction(action, button) {
  const actions = {
    new: showNewProject, import: showImportProject, save: () => guard('save', saveCurrent), history: () => guard('history', showHistory),
    'generate-architecture': () => startGeneration('architecture'), 'resume-architecture': () => startGeneration('architecture', 'resume'),
    'generate-directory': () => startGeneration('directory'), 'resume-directory': () => startGeneration('directory', 'resume'),
    'prepare-prompt': () => startGeneration('prompt'), 'generate-draft': () => startGeneration('draft'),
    review: () => startGeneration('review'), enrich: () => startGeneration('enrich'), finalize: () => startGeneration('finalize'), reindex: () => startGeneration('reindex'),
    'previous-chapter': () => switchChapter(Math.max(1, Number(store.chapter) - 1)), 'next-chapter': () => switchChapter(Number(store.chapter) + 1),
    'next-step': () => switchView(button.dataset.target), 'write-next': async () => { await switchChapter(Number(store.chapter) + 1); switchView('draft'); },
    'parse-directory': () => guard('save', async () => { await saveCurrent(); render(); }),
    'font-smaller': () => { state.fontSize = Math.max(11, state.fontSize - 1); $('#content-editor').style.fontSize = `${state.fontSize}px`; },
    'font-larger': () => { state.fontSize = Math.min(24, state.fontSize + 1); $('#content-editor').style.fontSize = `${state.fontSize}px`; },
    zen: () => document.body.classList.toggle('zen-mode'), 'export-current': exportCurrent, batch: showBatch,
  };
  if (actions[action]) await actions[action]();
}

async function switchView(view) {
  if (!views[view] && view !== 'settings') return;
  store.view = view; persist(); render();
  history.replaceState(null, '', `#${view}`);
}

async function switchChapter(chapter) {
  if (!Number.isInteger(chapter) || chapter < 1 || chapter > 10000) return toast('章节编号应在 1 到 10000 之间。', 'warning');
  store.chapter = chapter; currentForm().chapter = chapter; persist(); await loadChapter(); render();
}

async function saveCurrent(options = {}) {
  if (!state.project) return;
  if (isRunning()) throw new Error('这部小说的任务正在运行，编辑已在浏览器中保留，任务结束后即可保存。');
  const kind = options.kind || currentKind(), chapter = options.chapter || currentChapter(kind), doc = getDoc(kind, chapter);
  try {
    const saved = await api.saveDocument(store.projectId, kind, { content: doc.content, revision: doc.revision }, chapter);
    state.docs[docKey(kind, chapter)] = saved; markSaved(doc, saved);
    if (kind === 'directory' || kind === 'chapter') { const data = await api.chapters(store.projectId); state.chapters = data.chapters || []; state.warnings = data.warnings || []; }
    if (!options.quiet) toast(`${kindNames[kind]}已保存。`); render(); return saved;
  } catch (error) {
    if (error.status !== 409 || error.message.includes('任务')) throw error;
    const latest = await api.document(store.projectId, kind, chapter);
    const replace = await confirmDialog('文件已有更新', '小说文件已被其他操作更新。你的编辑仍然保留。是否用当前编辑覆盖最新版本？覆盖前的文件会进入历史记录。', '保存我的编辑');
    if (!replace) return null;
    const saved = await api.saveDocument(store.projectId, kind, { content: doc.content, revision: latest.revision }, chapter); state.docs[docKey(kind, chapter)] = saved; markSaved(doc, saved); toast('已保存你的编辑，旧版本可在历史中查看。'); render(); return saved;
  }
}

function validateParams(type) {
  const form = currentForm();
  if (type === 'architecture' && !String(form.topic || '').trim()) throw new Error('请先填写故事主题。');
  if (['architecture', 'directory'].includes(type) && (!Number.isInteger(Number(form.num_chapters)) || Number(form.num_chapters) < 1 || Number(form.num_chapters) > 10000)) throw new Error('总章节数应为 1 到 10000 的整数。');
  if (form.word_number && (!Number.isInteger(Number(form.word_number)) || Number(form.word_number) < 100 || Number(form.word_number) > 100000)) throw new Error('单章目标字数应为 100 到 100000 的整数。');
  if (type === 'directory' && !getDoc('architecture').savedContent.trim()) throw new Error('请先生成并采用大纲，或在大纲页面保存已有内容。');
  if (['prompt', 'draft', 'batch'].includes(type) && !getDoc('directory').savedContent.trim()) throw new Error('请先生成并采用目录，或在目录页面保存已有内容。');
  if (type === 'draft' && !getDoc('prompt', store.chapter).content.trim()) throw new Error('请先准备并采用本章提示词，再生成草稿。');
  if (['review', 'enrich', 'finalize'].includes(type) && !getDoc('chapter', store.chapter).content.trim()) throw new Error('本章正文为空。请先生成草稿，或输入已有正文。');
}

async function persistParams() {
  const form = currentForm();
  const fields = store.view === 'architecture' ? ['topic', 'genre', 'num_chapters', 'word_number', 'user_guidance'] : store.view === 'directory' ? ['num_chapters', 'word_number'] : [];
  if (!fields.length) return;
  const params = Object.fromEntries(fields.filter(key => form[key] !== undefined).map(key => [key, form[key]]));
  const result = await api.updateProject(store.projectId, { params });
  state.project.params = { ...state.project.params, ...params }; if (result?.project) state.project = result.project;
}

async function startGeneration(type, mode = 'new', extra = {}) {
  return guard('job', async () => {
    if (isRunning()) return toast('当前有任务正在运行，请完成或停止后再开始。', 'warning');
    validateParams(type);
    const pendingSettings = settings.isDirty();
    if (!await ensureSettingsSaved(settings, () => confirmDialog('先保存模型设置',
      '模型或连接设置有未保存的修改。生成任务读取已保存配置，是否先保存全部设置，避免继续使用旧模型？', '保存后生成'))) return;
    if (pendingSettings) render();
    if (['directory', 'prompt', 'draft'].includes(type)) {
      const prerequisites = type === 'directory' ? ['architecture'] : ['architecture', 'directory'];
      const dirty = prerequisites.filter(kind => getDoc(kind).modified);
      if (dirty.length) {
        const save = await confirmDialog('先保存创作上下文', `${dirty.map(kind => kindNames[kind]).join('、')}有未保存的修改。本次生成需要读取已保存文件，是否先保存这些修改？`, '保存后生成');
        if (!save) return;
        for (const kind of dirty) if (!await saveCurrent({ kind, quiet: true })) return;
      }
    }
    if (['architecture', 'directory'].includes(type) && mode === 'new' && getDoc(type).content.trim()) {
      const confirmed = await confirmDialog('重新生成候选版本', `将生成新的${kindNames[type]}候选，当前内容与历史版本会保留。完成后可以预览并选择采用。`, '开始生成'); if (!confirmed) return;
    }
    if (type === 'finalize') {
      const modifiedMemory = ['summary', 'characters', 'plot_arcs'].filter(kind => getDoc(kind).modified);
      if (modifiedMemory.length) throw new Error(`请先保存${modifiedMemory.map(kind => kindNames[kind]).join('、')}的修改，再定稿章节。`);
      const confirmed = await confirmDialog('定稿本章', `将以当前编辑器中的第 ${store.chapter} 章正文定稿，更新全局摘要、人物状态和检索记忆。正文的修改也将保存。`, '确认定稿'); if (!confirmed) return;
    }
    await persistParams();
    const form = currentForm();
    const payload = { type, mode, chapter: Number(store.chapter), word_number: Number(form.word_number || state.project.params?.word_number || 3000), user_guidance: form.user_guidance || '', characters_involved: form.characters_involved || '', key_items: form.key_items || '', scene_location: form.scene_location || '', time_constraint: form.time_constraint || '', role_ids: form.role_ids || [], ...extra };
    if (type === 'draft') payload.custom_prompt_text = getDoc('prompt', store.chapter).content;
    if (['review', 'enrich', 'finalize'].includes(type)) payload.content = getDoc('chapter', store.chapter).content;
    if (type === 'draft' && getDoc('chapter', store.chapter).modified) {
      if (!await confirmDialog('正文有未保存的编辑', '生成草稿会得到新的候选结果。当前正文编辑会继续保留，采用候选时需要确认替换。', '开始生成')) return;
    }
    const task = await api.startJob(store.projectId, payload); state.task = task; store.taskId = task.id; persist(); renderTask(); expandTask(true); beginPolling(); toast(`${taskNames[type]}已开始，可在底部查看进度。`);
  });
}

function expandTask(expanded) { $('#task-details').hidden = !expanded; $('#task-toggle').setAttribute('aria-expanded', String(expanded)); }
function renderTask() {
  const task = state.task; if (!task) { $('#task-panel').hidden = true; return; }
  $('#task-panel').hidden = false; $('#task-title').textContent = taskNames[task.type] || task.type || '创作任务';
  $('#task-stage').textContent = ({ architecture: '构建故事架构', directory: '生成章节蓝图', prompt: '整理上下文与场景计划', draft: '生成本章正文', enrich: '扩写本章正文', review: '核对人物与剧情', index: '更新检索记忆', index_failed: '检索记忆待补建', summary: '更新全局摘要', characters: '更新人物状态', memory_committed: '创作记忆已保存', memory_rebuild: '重建创作记忆', batch_skip: '跳过已定稿章节', batch_stopped: '批量任务已停止', completed: '本步骤已完成' })[task.stage] || task.stage || ''; $('#task-state').textContent = statusNames[task.status] || task.status;
  $('#task-state').className = `pill ${task.status === 'succeeded' ? 'success' : ['failed', 'interrupted'].includes(task.status) ? 'error' : ''}`;
  $('#task-icon').textContent = isRunning() ? '◌' : task.status === 'succeeded' ? '✓' : task.status === 'failed' ? '!' : '·'; $('#task-icon').className = `task-status-icon ${isRunning() ? 'running' : ''}`;
  const logs = $('#task-logs'), nearBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 50;
  logs.textContent = (task.events || []).map(event => `${event.time ? `[${formatTime(event.time)}] ` : ''}${event.message || ''}`).join('\n') + (task.error ? `\n错误：${task.error}` : '');
  if (!logs.textContent.trim()) logs.textContent = '任务已提交，等待执行…'; if (nearBottom) logs.scrollTop = logs.scrollHeight;
  const hasResults = !!task.result?.documents?.length;
  const directResult = ['finalize', 'batch', 'reindex', 'knowledge_import', 'knowledge_clear'].includes(task.type);
  $('#task-result').hidden = isRunning() || (!hasResults && !directResult);
  $('#task-result').textContent = task.applied ? '查看已采用结果' : directResult ? '查看任务结果' : '预览并采用';
  $('#task-hint').textContent = task.status === 'cancelling' ? '停止请求已发送，将在当前模型调用结束后停止后续步骤。' : isRunning() ? '任务在后台执行，切换页签不会中断。' : task.applied ? '结果已保存到小说文件。' : task.status === 'succeeded' && hasResults ? '生成结果已就绪，预览并采用后才会写入小说文件。' : task.status === 'failed' ? '任务未完成，查看日志了解原因后可重试。' : '任务状态与日志会保留，刷新页面仍可查看。';
  syncBusy();
}

function beginPolling() {
  clearTimeout(state.polling);
  if (!state.task?.id) return;
  if (isRunning()) state.polling = setTimeout(pollTask, 1800);
}

async function pollTask() {
  if (!state.task?.id) return;
  const id = state.task.id;
  try {
    const previous = state.task.status; const result = await api.job(id); if (state.task?.id !== id) return;
    state.task = result; renderTask();
    if (activeStatuses.includes(previous) && !activeStatuses.includes(result.status) && state.lastCompleted !== id) {
      state.lastCompleted = id;
      if (result.status === 'succeeded') {
        if (['finalize', 'batch', 'reindex', 'knowledge_import', 'knowledge_clear'].includes(result.type)) {
          // These operations commit confirmed content; use their returned documents as the authoritative saved version.
          await refreshDocuments(result.result?.saved_documents || []); await refreshLiveDocuments(); render();
          if (result.result?.details?.index === 'failed') toast('正文与创作记忆已保存，检索索引未完成，可点击「补建检索记忆」重试。', 'warning');
          else if (result.result?.stopped) toast(`批量任务已停止：${result.result.reason || '请查看任务结果'}`, 'warning');
          else toast(`${taskNames[result.type]}已完成。`);
          if (!$('#app-dialog').open) await guard('candidate', showCandidate);
        } else { toast(`${taskNames[result.type]}已完成，请预览并采用结果。`); if (!$('#app-dialog').open) await guard('candidate', showCandidate); }
      } else if (result.status === 'failed') toast(result.error || '任务执行失败，请查看底部日志。', 'error');
      else if (result.status === 'cancelled') { await refreshLiveDocuments(); render(); toast('任务已停止，已提交的章节和创作记忆会保留。', 'warning'); }
    }
    beginPolling();
  } catch (error) {
    if (state.task?.id !== id) return;
    $('#task-hint').textContent = '连接中断，正在重连。任务可能仍在后台执行。'; state.polling = setTimeout(pollTask, 3500);
  }
}

async function showCandidate() {
  const task = state.task; if (!task) return;
  const candidates = task.result?.documents || [], documents = candidates.length ? candidates : task.result?.saved_documents || [], direct = !candidates.length && ['finalize', 'batch', 'reindex', 'knowledge_import', 'knowledge_clear'].includes(task.type);
  if (!documents.length && !direct) return toast('此任务暂时没有可预览的结果。', 'warning');
  let selected = 0;
  const renderPreview = () => {
    const doc = documents[selected]; $('#candidate-preview', dialog).textContent = doc?.content || resultSummary(task);
    const counter = $('#candidate-count', dialog);
    if (counter) counter.textContent = doc ? `${count(doc.content).toLocaleString('zh-CN')} 字` : '';
  };
  const dialog = showDialog(`${taskNames[task.type] || '任务'} · ${task.applied || direct ? '已保存结果' : '候选结果'}`, `<div class="candidate-note">${task.applied || direct ? '本次任务已提交确认的内容。历史记录中可查看旧版本。' : '此处为只读预览。采用后将保存关联内容，再到主编辑器修改并保存；旧版本进入历史。'}${task.result?.warnings?.length ? `<br>${task.result.warnings.map(escape).join('<br>')}` : ''}</div>${task.result?.details || task.result?.stopped || task.result?.chapters ? `<p class="dialog-description" style="margin-top:12px;white-space:pre-wrap">${escape(resultSummary(task))}</p>` : ''}${documents.length ? `<div class="candidate-toolbar" style="margin-top:16px"><select class="candidate-selector" id="candidate-select" aria-label="选择候选文档">${documents.map((doc, index) => `<option value="${index}">${escape(kindNames[doc.kind] || doc.kind)}${chapterKinds.includes(doc.kind) && doc.chapter ? ` · 第 ${doc.chapter} 章` : ''}</option>`).join('')}</select><span class="muted" id="candidate-count"></span></div>` : ''}<pre id="candidate-preview"></pre>`, `<button type="button" class="button button-outline" data-close-dialog>${task.applied || direct ? '关闭' : '稍后再看'}</button>${!task.applied && !direct && documents.length ? '<button type="button" class="button button-primary" id="apply-candidate">✓ 采用本次结果</button>' : ''}`, { wide: true });
  renderPreview(); $('#candidate-select')?.addEventListener('change', event => { selected = Number(event.target.value); renderPreview(); });
  $('#apply-candidate')?.addEventListener('click', () => guard('apply', async () => {
    $('#apply-candidate').disabled = true;
    try {
      const dirty = documents.filter(doc => getDoc(doc.kind, doc.chapter || undefined).modified);
      if (dirty.length) {
        const ok = window.confirm(`以下内容有未保存修改：${dirty.map(doc => kindNames[doc.kind] || doc.kind).join('、')}。采用候选会替换这些编辑。是否继续？`);
        if (!ok) return;
      }
      const result = await api.acceptJob(task.id); await refreshDocuments(result.documents || documents, true);
      if (result.project) { state.project = result.project; const index = state.projects.findIndex(project => project.id === result.project.id); if (index >= 0) state.projects[index] = result.project; }
      state.task.applied = true; if (task.type === 'prompt') store.selectedTab.draft = 'prompt'; if (['draft', 'enrich'].includes(task.type)) { store.selectedTab.draft = 'chapter'; store.selectedTab.finalize = 'chapter'; } if (task.type === 'review') store.selectedTab.finalize = 'review';
      const chapterResult = documents.find(doc => chapterKinds.includes(doc.kind)); if (chapterResult?.chapter) { store.chapter = chapterResult.chapter; await loadChapter(); }
      persist(); dialog.close(); renderTask(); render(); toast('已采用并保存生成结果。');
    } finally { if ($('#apply-candidate')) $('#apply-candidate').disabled = false; }
  }));
}

async function showHistory() {
  const kind = currentKind(), chapter = currentChapter(), result = await api.history(store.projectId, kind, chapter), versions = result.versions || [];
  let selected = null;
  const dialog = showDialog(`${kindNames[kind]} · 历史版本`, `<p class="dialog-description">恢复历史会先载入编辑器，确认后点击保存。当前文件不会立即改变。</p><div class="history-list">${versions.length ? versions.map((version, index) => `<button type="button" class="history-item" data-version="${index}"><span>${escape(formatTime(version.created_at) || version.id)}<br><small>${escape(version.source || '文件保存')} · ${count(version.content)} 字</small></span><span>预览 →</span></button>`).join('') : '<div class="preview-empty">还没有历史记录。每次覆盖保存时，旧版本会自动保留。</div>'}</div><pre id="history-preview" hidden></pre>`, '<button type="button" class="button button-outline" data-close-dialog>关闭</button><button type="button" class="button button-primary" id="restore-history" disabled>载入此版本</button>', { wide: true });
  $$('[data-version]', dialog).forEach(button => button.addEventListener('click', () => {
    selected = versions[Number(button.dataset.version)]; $$('[data-version]', dialog).forEach(item => item.classList.toggle('selected', item === button)); $('#history-preview').hidden = false; $('#history-preview').textContent = selected.content || ''; $('#restore-history').disabled = false;
  }));
  $('#restore-history').addEventListener('click', () => { if (!selected) return; if (getDoc(kind, chapter).modified && !window.confirm('载入历史会替换编辑器中未保存的修改，是否继续？')) return; setContent(store.projectId, kind, chapter, selected.content || ''); dialog.close(); render(); toast('历史版本已载入编辑器，请确认后保存。'); });
}

async function showJobHistory() {
  const result = await api.jobs(store.projectId), jobs = result.jobs || [];
  const running = jobs.find(job => activeStatuses.includes(job.status));
  const dialog = showDialog('当前小说 · 任务记录', `<p class="dialog-description">生成的候选会保留在任务中。打开记录后，可在底部查看日志或再次预览、采用。</p><div class="history-list">${jobs.length ? jobs.map((job, index) => `<button type="button" class="history-item" data-job-index="${index}"><span>${escape(taskNames[job.type] || job.type)}${job.chapter && ['draft', 'prompt', 'review', 'enrich', 'finalize', 'reindex'].includes(job.type) ? ` · 第 ${job.chapter} 章` : ''}<br><small>${escape(formatTime(job.created_at))}${job.applied ? ' · 已采用' : ''}</small></span><span class="pill ${job.status === 'succeeded' ? 'success' : ['failed', 'interrupted'].includes(job.status) ? 'error' : ''}">${escape(statusNames[job.status] || job.status)} →</span></button>`).join('') : '<div class="preview-empty">还没有生成任务。</div>'}</div>`, '', { wide: true });
  $$('[data-job-index]', dialog).forEach(button => button.addEventListener('click', () => {
    const job = jobs[Number(button.dataset.jobIndex)];
    if (running && running.id !== job.id) {
      const texts = (job.result?.documents || job.result?.saved_documents || []).map(doc => `${kindNames[doc.kind] || doc.kind}${chapterKinds.includes(doc.kind) ? ` · 第 ${doc.chapter} 章` : ''}\n\n${doc.content || ''}`).join('\n\n────────\n\n');
      showDialog(`${taskNames[job.type] || '任务'} · 历史记录`, `<p class="dialog-description">当前有任务正在运行。此处可只读查看历史，当前任务结束后可从任务记录中采用候选。</p><pre>${escape(texts || (job.events || []).map(item => item.message).join('\n') || job.error || '此任务没有文本结果。')}</pre>`, '', { wide: true });
      return;
    }
    state.task = job; store.taskId = job.id; persist(); dialog.close(); renderTask(); expandTask(true); beginPolling();
    if (!isRunning() && (job.result?.documents?.length || ['finalize', 'batch', 'reindex', 'knowledge_import', 'knowledge_clear'].includes(job.type))) guard('candidate', showCandidate);
  }));
}

function showNewProject() {
  const defaults = state.config.other_params || {}, defaultChapters = Math.min(10000, Math.max(1, Number(defaults.num_chapters) || 50)), defaultWords = Math.min(100000, Math.max(100, Number(defaults.word_number) || 3000));
  const dialog = showDialog('开始一部新小说', `<p class="dialog-description">小说的大纲、目录、章节和创作记忆将分别保存在独立目录。参数默认读取“设置 → 创作默认值”。</p><form id="new-project-form"><div class="field"><label for="new-name">小说名称</label><input id="new-name" required maxlength="100" placeholder="给你的故事取个名字" autocomplete="off"></div><div class="field"><label for="new-path">保存目录<span class="optional">选填</span></label><input id="new-path" value="${escape(defaults.filepath || '')}" placeholder="留空则在默认小说目录下创建" autocomplete="off"><p class="field-help">填写电脑上的目录路径，目录将由本地服务创建。</p></div><div class="field-grid"><div class="field"><label for="new-chapters">计划章节数</label><input id="new-chapters" type="number" min="1" max="10000" value="${defaultChapters}" required></div><div class="field"><label for="new-words">单章目标字数</label><input id="new-words" type="number" min="100" max="100000" value="${defaultWords}" required></div></div></form>`, '<button type="button" class="button button-outline" data-close-dialog>取消</button><button type="submit" form="new-project-form" class="button button-primary" id="create-project-submit">创建小说</button>');
  $('#new-project-form').addEventListener('submit', event => { event.preventDefault(); guard('create', async () => {
    const name = $('#new-name').value.trim(), path = $('#new-path').value.trim(); if (!name) return;
    $('#create-project-submit').disabled = true;
    try { const numChapters = Number($('#new-chapters').value); const data = await api.createProject({ name, ...(path ? { path } : {}), params: { ...defaults, num_chapters: numChapters, word_number: Number($('#new-words').value) } }); const project = data.project || data; const registered = state.projects.some(item => item.id === project.id); if (!registered) state.projects.push(project); store.chapter = Math.min(numChapters, Math.max(1, Number(defaults.chapter_num) || 1)); dialog.close(); renderProjectSelector(); store.view = 'architecture'; await openProject(project.id); toast(registered ? '此目录已登记，已打开现有小说。' : '新小说已创建，开始构思大纲吧。'); }
    finally { if ($('#create-project-submit')) $('#create-project-submit').disabled = false; }
  }); });
}

function showImportProject(initialPath = '') {
  if (typeof initialPath !== 'string') initialPath = '';
  const dialog = showDialog('打开已有小说目录', `<p class="dialog-description">输入小说所在的本机目录，可直接读取已有大纲、目录与章节文件。</p><form id="import-project-form"><div class="field"><label for="import-path">小说目录</label><input id="import-path" required value="${escape(initialPath)}" placeholder="例如 D:\\我的小说\\长夜行" autocomplete="off"></div><div class="field"><label for="import-name">显示名称<span class="optional">选填</span></label><input id="import-name" placeholder="留空使用目录名称" autocomplete="off"></div></form>`, '<button type="button" class="button button-outline" data-close-dialog>取消</button><button type="submit" form="import-project-form" class="button button-primary" id="import-project-submit">打开小说</button>');
  $('#import-project-form').addEventListener('submit', event => { event.preventDefault(); guard('import', async () => {
    $('#import-project-submit').disabled = true;
    try { const data = await api.importProject({ path: $('#import-path').value.trim(), name: $('#import-name').value.trim() || undefined }); const project = data.project || data; if (!state.projects.some(item => item.id === project.id)) state.projects.push(project); dialog.close(); renderProjectSelector(); await openProject(project.id); toast('小说目录已打开。'); }
    finally { if ($('#import-project-submit')) $('#import-project-submit').disabled = false; }
  }); });
}

function showBatch() {
  const total = Number(currentForm().num_chapters || state.project.params?.num_chapters || state.chapters.length || 50);
  const dialog = showDialog('批量顺序创作', `<p class="dialog-description">按章节顺序准备提示词、生成正文并定稿。每章更新摘要与人物状态后，再继续下一章。</p><form id="batch-form"><div class="field-grid"><div class="field"><label for="batch-start">起始章节</label><input id="batch-start" type="number" min="1" max="10000" value="${store.chapter}" required></div><div class="field"><label for="batch-end">结束章节</label><input id="batch-end" type="number" min="1" max="10000" value="${Math.max(Number(store.chapter), total)}" required></div></div><div class="field"><label for="batch-min-words">低于多少字时扩写</label><input id="batch-min-words" type="number" min="1" max="100000" value="${Math.round(Number(currentForm().word_number || 3000) * .7)}"></div><label class="checkbox-field"><input type="checkbox" id="batch-enrich" checked> 正文字数不足时自动扩写</label><div class="batch-summary">批量任务会直接保存和定稿生成的章节，逐章提交结果。已有章节可能被覆盖，旧内容会保留在历史中。停止任务会在当前模型调用结束后生效。</div><label class="checkbox-field"><input type="checkbox" id="batch-confirm" required> 我已确认章节范围，允许批量生成并保存</label></form>`, '<button type="button" class="button button-outline" data-close-dialog>取消</button><button type="submit" form="batch-form" class="button button-primary">开始批量创作</button>');
  $('#batch-form').addEventListener('submit', event => {
    event.preventDefault(); const start = Number($('#batch-start').value), end = Number($('#batch-end').value), min_words = Number($('#batch-min-words').value), auto_enrich = $('#batch-enrich').checked;
    if (end < start) return toast('结束章节不能小于起始章节。', 'warning');
    const dirtyChapters = Object.entries(store.documents).filter(([key, doc]) => key.startsWith(`${store.projectId}:chapter:`) && doc.modified).map(([key]) => Number(key.split(':').pop())).filter(chapter => chapter >= start && chapter <= end);
    if (dirtyChapters.length) return toast(`第 ${dirtyChapters.join('、')} 章有未保存编辑，请先保存后再批量创作。`, 'warning');
    dialog.close(); startGeneration('batch', 'new', { start, end, min_words, auto_enrich });
  });
}

function download(filename, content, mime = 'text/plain;charset=utf-8') {
  const blob = new Blob(['\ufeff', content], { type: mime }), url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = filename.replace(/[<>:"/\\|?*]/g, '_'); anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function exportCurrent() { if (!state.project) return; const kind = currentKind(); download(`${state.project.name}_${kindNames[kind]}${currentChapter() ? `_第${store.chapter}章` : ''}.txt`, currentEditor().content); }
function showExport() {
  const dialog = showDialog('导出小说', `<p>选择导出内容。全书正文按章号合并，使用已保存到文件的内容；当前内容包含编辑器中尚未保存的修改。</p><div class="button-row" style="flex-direction:column;align-items:stretch"><button type="button" class="button button-outline" id="export-book">导出全书正文 · TXT</button><button type="button" class="button button-outline" id="export-current-dialog">导出当前编辑内容 · TXT</button><button type="button" class="button button-outline" id="export-backup">导出文本备份 · JSON</button></div><p class="field-help">文本备份包含大纲、目录、章节、摘要、人物状态与剧情要点，不包含 API 密钥或向量索引。</p>`);
  $('#export-current-dialog').addEventListener('click', () => { exportCurrent(); dialog.close(); });
  $('#export-book').addEventListener('click', () => exportAll(false)); $('#export-backup').addEventListener('click', () => exportAll(true));
}

async function exportAll(backup) {
  return guard('export', async () => {
    const id = store.projectId, data = await api.chapters(id), chapters = data.chapters || [];
    const documents = [];
    // Read sequentially to keep very large novels from flooding the local server.
    for (const chapter of [...chapters].sort((a, b) => a.chapter_number - b.chapter_number)) {
      const document = await api.document(id, 'chapter', chapter.chapter_number); if (document.content?.trim()) documents.push({ ...document, title: chapter.chapter_title || '' });
    }
    if (!backup && !documents.length) throw new Error('还没有已保存的章节正文可以导出。');
    if (backup) {
      const extras = await Promise.all(['architecture', 'directory', 'summary', 'characters', 'plot_arcs'].map(kind => api.document(id, kind)));
      download(`${state.project.name}_文本备份.json`, JSON.stringify({ format: 'AI_NovelGenerator.text-backup.v1', exported_at: new Date().toISOString(), project: { name: state.project.name, params: state.project.params }, documents: [...extras, ...documents] }, null, 2), 'application/json;charset=utf-8');
    } else download(`${state.project.name}_全书正文.txt`, `${state.project.name}\n\n${documents.map(doc => `第 ${doc.chapter} 章${doc.title ? ` ${doc.title}` : ''}\n\n${doc.content.trim()}`).join('\n\n\n')}`);
    $('#app-dialog').close(); toast('导出文件已生成。');
  });
}

$('#app-dialog').addEventListener('close', () => { const callback = state.dialogClose; state.dialogClose = null; callback?.(); });
$('#new-project').addEventListener('click', showNewProject); $('#import-project').addEventListener('click', showImportProject); $('#export-project').addEventListener('click', showExport);
$('#job-history').addEventListener('click', () => guard('jobs', showJobHistory));
$('#project-select').addEventListener('change', event => guard('project', () => openProject(event.target.value)));
$$('[data-view]', $('.sidebar')).forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
$('.brand').addEventListener('click', event => { event.preventDefault(); switchView('architecture'); });
$('#task-toggle').addEventListener('click', () => expandTask($('#task-details').hidden));
$('#task-result').addEventListener('click', () => guard('candidate', showCandidate));
$('#task-cancel').addEventListener('click', () => guard('cancel', async () => { if (!isRunning()) return; const task = await api.cancelJob(state.task.id); state.task = task.job || task; renderTask(); beginPolling(); toast('已请求停止，等待当前步骤结束。', 'warning'); }));
window.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (!$('#app-dialog').open) { if (store.view === 'settings') settings.save(); else if (state.project && views[store.view]) guard('save', saveCurrent); } }
  if (event.key === 'Escape') document.body.classList.remove('zen-mode');
});
window.addEventListener('beforeunload', event => { flush(); if (settings.isDirty() || (state.project && hasDirty(store.projectId))) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('hashchange', () => { const view = location.hash.slice(1); if (views[view] || view === 'settings') switchView(view); });
const initialView = location.hash.slice(1); if (views[initialView] || initialView === 'settings') store.view = initialView;
bootstrap();
