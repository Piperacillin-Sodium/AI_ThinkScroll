import { api } from './api.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const clone = value => JSON.parse(JSON.stringify(value));
const groups = { llm: ['模型预设', 'API、模型与生成参数'], tasks: ['任务模型', '五个创作任务分别指定'], embedding: ['Embedding', '向量模型与检索范围'], defaults: ['创作默认值', '小说参数与创作语言'], proxy: ['代理设置', '网络地址与端口'], webdav: ['WebDAV 备份', '连接、备份与恢复配置'], knowledge: ['知识库管理', '导入资料与清理索引'] };
const tasks = { architecture_llm: '生成大纲', chapter_outline_llm: '生成目录', prompt_draft_llm: '提示词与草稿', final_chapter_llm: '定稿章节', consistency_review_llm: '一致性审校' };
const llmInterfaces = ['OpenAI', 'Azure OpenAI', 'Ollama', 'DeepSeek', 'Gemini', 'ML Studio'];
const embeddingInterfaces = ['OpenAI', 'Azure OpenAI', 'Gemini', 'Ollama', 'ML Studio', 'SiliconFlow'];

// Read the visible values at submission time as well as listening for edits.
// Browser autofill and some input methods may only emit change on blur.
export function readModelForm(root, preset, kind = 'llm') {
  const result = clone(preset);
  const fields = kind === 'llm'
    ? ['interface_format', 'model_name', 'base_url', 'api_key', 'temperature', 'max_tokens', 'timeout']
    : ['interface_format', 'model_name', 'base_url', 'api_key', 'retrieval_k'];
  for (const key of fields) {
    const input = root.querySelector(`#setting-${key}`);
    if (!input) continue;
    if (key === 'api_key' && input.value === '' && !Object.hasOwn(result, key)) continue;
    result[key] = input.type === 'number' && input.value !== '' ? Number(input.value) : input.value;
    if (['interface_format', 'model_name', 'base_url'].includes(key)) result[key] = result[key].trim();
  }
  const clear = root.querySelector('[data-clear-secret="api_key"]');
  if (clear && (clear.checked || Object.hasOwn(result, 'clear_api_key'))) result.clear_api_key = clear.checked;
  return result;
}

export async function ensureSettingsSaved(controller, confirm) {
  if (!controller.isDirty()) return true;
  if (!await confirm()) return false;
  await controller.save();
  if (controller.isDirty()) throw new Error('模型或连接设置尚未保存成功，请到设置页修正后再开始生成。');
  return true;
}

export function decodeKnowledgeText(bytes) {
  for (const encoding of ['utf-8', 'gb18030']) {
    try { return new TextDecoder(encoding, { fatal: true }).decode(bytes); }
    catch { /* Try the original desktop app's common Chinese encodings next. */ }
  }
  throw new Error('无法识别知识文件编码，请另存为 UTF-8 或 GBK 后重试。');
}

// Settings drafts intentionally stay in memory. Keys/passwords are never persisted in browser storage.
export function createSettingsController(options) {
  let root, config = null, revision = '', dirty = false, section = 'llm', busy = false, loading = null, error = '', conflict = false, result = null, metadata = {};
  let selectedLLM = '', selectedEmbedding = '', operationJob = null, knowledge = null;
  const visible = () => root && options.isVisible();
  const notify = (message, type) => options.toast(message, type);
  const getPreset = (kind = section) => config?.[kind === 'llm' ? 'llm_configs' : 'embedding_configs']?.[kind === 'llm' ? selectedLLM : selectedEmbedding];
  const markDirty = () => { dirty = true; conflict = false; updateStatus(); };
  const button = (action, text, style = 'outline', disabled = false) => `<button type="button" class="button button-${style}" data-setting-action="${action}" ${disabled || (busy && action !== 'cancel-operation') ? 'disabled' : ''}>${text}</button>`;
  const field = (key, label, value, { type = 'text', help = '', full = false, min, max, step, choices, placeholder = '', rows = 3, autocomplete = 'off' } = {}) => {
    const id = `setting-${key}`;
    const attr = `id="${esc(id)}" data-setting-field="${esc(key)}" ${busy ? 'disabled' : ''}`;
    let input;
    if (choices) input = `<select ${attr}>${choices.map(item => { const [name, text] = Array.isArray(item) ? item : [item, item]; return `<option value="${esc(name)}" ${String(name) === String(value) ? 'selected' : ''}>${esc(text)}</option>`; }).join('')}</select>`;
    else if (type === 'textarea') input = `<textarea ${attr} rows="${rows}" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`;
    else if (type === 'checkbox') input = `<label class="settings-toggle"><input ${attr} type="checkbox" ${value ? 'checked' : ''}><span>${esc(placeholder || '启用')}</span></label>`;
    else input = `<input ${attr} type="${type}" value="${esc(value)}" autocomplete="${autocomplete}" ${min !== undefined ? `min="${min}"` : ''} ${max !== undefined ? `max="${max}"` : ''} ${step !== undefined ? `step="${step}"` : ''} placeholder="${esc(placeholder)}" spellcheck="false">`;
    return `<div class="field ${full ? 'full-width' : ''}"><label for="${esc(id)}">${esc(label)}</label>${input}${help ? `<p class="field-help">${esc(help)}</p>` : ''}</div>`;
  };
  const secretField = (key, label, preset, marker = 'has_api_key') => `${field(key, label, preset[key] || '', { type: 'password', full: true, autocomplete: 'new-password', placeholder: preset[marker] ? '已保存 · 留空保留原值，输入新值可替换' : '输入密钥 / 密码', help: preset[`clear_${key}`] ? '保存后将清除已保存的凭据。' : preset[key] ? '已输入新凭据，保存后生效。' : preset[marker] ? '服务端已保存，浏览器不读取原值。' : '尚未配置。' })}<label class="checkbox-field secret-clear"><input type="checkbox" data-clear-secret="${esc(key)}" ${preset[`clear_${key}`] ? 'checked' : ''} ${busy ? 'disabled' : ''}>保存时清除${esc(label)}</label>`;

  function updateStatus() {
    if (!visible()) return;
    const status = root.querySelector('#settings-save-state');
    if (status) { status.className = `save-state ${dirty ? 'dirty' : ''}`; status.textContent = busy ? '正在处理…' : dirty ? '有未保存的设置 · 切换模块会保留' : '配置已保存到本机'; }
    root.querySelectorAll('[data-setting-action="save"]').forEach(node => { node.disabled = busy || !config || !dirty; });
  }

  function receive(data) {
    config = clone(data.config || data.settings || data);
    metadata = data;
    revision = data.revision || '';
    config.llm_configs ||= {}; config.embedding_configs ||= {}; config.choose_configs ||= {}; config.other_params ||= {}; config.proxy_setting ||= {}; config.webdav_config ||= {};
    selectedLLM = config.last_llm_config_name in config.llm_configs ? config.last_llm_config_name : Object.keys(config.llm_configs)[0] || '';
    selectedEmbedding = config.last_embedding_interface_format in config.embedding_configs ? config.last_embedding_interface_format : Object.keys(config.embedding_configs)[0] || '';
    // Server responses contain credential-presence flags only. Never accept returned secret values.
    for (const collection of [config.llm_configs, config.embedding_configs]) for (const [name, preset] of Object.entries(collection)) { delete preset.api_key; preset.original_name = name; }
    delete config.webdav_config.webdav_password;
    dirty = false; error = ''; conflict = false;
    options.onSaved(data);
  }

  async function load(force = false) {
    if (loading) return loading;
    if (config && !force) return;
    loading = (async () => {
      try { const data = await (force ? api.reloadSettings() : api.settings()); receive(data); }
      catch (exc) { error = exc.message; }
      finally { loading = null; if (visible()) render(); }
    })();
    return loading;
  }

  function presetPanel(kind) {
    const isLLM = kind === 'llm', collection = config[isLLM ? 'llm_configs' : 'embedding_configs'];
    const selected = isLLM ? selectedLLM : selectedEmbedding, preset = collection[selected], interfaces = (isLLM ? metadata.interfaces?.llm : metadata.interfaces?.embedding) || (isLLM ? llmInterfaces : embeddingInterfaces);
    const toolbar = `<div class="preset-picker">${field('preset_selection', isLLM ? '当前模型预设' : '当前 Embedding 预设', selected, { choices: Object.keys(collection), full: true })}<div class="button-row preset-actions">${button('add', '＋ 新增')}${button('rename', '重命名', 'outline', !preset)}${button('copy', '复制', 'outline', !preset)}${button('delete', '删除', 'danger', Object.keys(collection).length <= 1)}</div></div>`;
    if (!preset) return `${toolbar}<div class="preview-empty">先新增一个预设，再填写连接信息。</div>`;
    const optionsList = [...new Set([...interfaces, preset.interface_format].filter(Boolean))];
    const body = field('interface_format', '接口格式', preset.interface_format, { choices: optionsList }) + field('model_name', '模型名称', preset.model_name, { placeholder: isLLM ? '服务商提供的模型 ID' : 'Embedding 模型 ID' }) + field('base_url', 'Base URL', preset.base_url, { full: true, placeholder: 'https://…', help: '按服务商要求填写接口地址；保留原软件支持的各类接口。' }) + secretField('api_key', 'API Key', preset);
    const parameters = isLLM ? field('temperature', 'Temperature', preset.temperature ?? 0.7, { type: 'number', min: 0, max: 2, step: 0.01, help: '数值越高，输出越多样。' }) + field('max_tokens', 'Max Tokens', preset.max_tokens ?? 8192, { type: 'number', min: 0, max: 2000000, step: 1, help: '单次模型响应的最大 Token 数，保留桌面版的 0 值。' }) + field('timeout', 'Timeout（秒）', preset.timeout ?? 600, { type: 'number', min: 0, max: 86400, step: 1 }) : field('retrieval_k', 'Retrieval Top-K', preset.retrieval_k ?? 4, { type: 'number', min: 1, max: 10000, step: 1, help: '检索与当前章节最相关的资料条数。' });
    return `${toolbar}<div class="settings-field-grid">${body}${parameters}</div><div class="settings-panel-actions">${button(isLLM ? 'test-llm' : 'test-embedding', '测试当前配置')}${button('save', '保存全部设置', 'primary', !dirty)}<span class="settings-inline-note">测试可使用当前填写的参数。</span></div>${!isLLM ? '<p class="settings-footnote">切换 Embedding 模型后，已有向量索引可能需要清理并重新建立。</p>' : ''}`;
  }

  function panelBody() {
    if (section === 'llm' || section === 'embedding') return presetPanel(section);
    if (section === 'tasks') return `<p class="settings-description">为每个步骤选择已创建的模型预设。修改预设名称或删除预设时，任务引用会同步更新。</p><div class="settings-field-grid">${Object.entries(tasks).map(([key, label]) => field(key, label, config.choose_configs[key], { choices: Object.keys(config.llm_configs), full: true })).join('')}</div><div class="settings-panel-actions">${button('save', '保存任务模型', 'primary', !dirty)}${button('refresh-presets', '刷新预设列表')}</div>`;
    if (section === 'defaults') {
      const p = config.other_params;
      return `<p class="settings-description">完整保留桌面版的小说参数。默认值用于新建小说和新的创作表单；已有项目内容可在创作页继续独立编辑。</p><div class="settings-field-grid">${field('topic', '故事主题', p.topic, { type: 'textarea', full: true })}${field('genre', '小说类型', p.genre)}${field('num_chapters', '总章节数', p.num_chapters ?? 10, { type: 'number', min: 0, max: 10000 })}${field('word_number', '单章目标字数', p.word_number ?? 3000, { type: 'number', min: 0, max: 100000 })}${field('chapter_num', '当前章节号', p.chapter_num ?? '1', { type: 'number', min: 1, max: 10000 })}${field('filepath', '小说保存路径', p.filepath, { full: true, placeholder: '本机文件夹的完整路径' })}${field('user_guidance', '创作指导', p.user_guidance, { type: 'textarea', full: true, rows: 4 })}${field('characters_involved', '出场人物', p.characters_involved)}${field('key_items', '关键道具', p.key_items)}${field('scene_location', '场景地点', p.scene_location)}${field('time_constraint', '时间约束', p.time_constraint)}${field('language', '创作语言模式', config.language || (config.is_english ? 'en' : 'zh'), { choices: [['zh', '中文模式'], ['en', 'English mode']], full: true, help: '使用相应语言的生成提示词；界面仍保持中文。' })}</div>${p.filepath ? `<div class="settings-panel-actions">${button('open-default-directory', '打开此小说目录')}</div>` : ''}`;
    }
    if (section === 'proxy') {
      const p = config.proxy_setting;
      return `<div class="settings-field-grid">${field('enabled', '启用代理', p.enabled, { type: 'checkbox', full: true, placeholder: '通过代理连接模型服务' })}${field('proxy_url', '代理地址', p.proxy_url, { full: true, placeholder: '127.0.0.1 或 http://127.0.0.1' })}${field('proxy_port', '代理端口', p.proxy_port, { type: 'number', min: 1, max: 65535, placeholder: '例如 7890' })}</div><p class="settings-footnote">保存后用于后续模型请求；当前正在执行的任务继续使用启动时的配置。</p>`;
    }
    if (section === 'webdav') {
      const p = config.webdav_config;
      return `<p class="settings-description">将配置保存到你指定的 WebDAV 服务，远程路径沿用 AI_Novel_Generator/config.json。备份包含模型密钥，请使用你信任的服务。</p><div class="settings-field-grid">${field('webdav_url', 'WebDAV URL', p.webdav_url, { full: true, placeholder: 'https://…/dav/' })}${field('webdav_username', 'WebDAV 用户名', p.webdav_username, { full: true })}${secretField('webdav_password', 'WebDAV 密码', p, 'has_webdav_password')}</div><div class="settings-panel-actions">${button('test-webdav', '测试连接')}${button('backup-webdav', '备份配置')}${button('restore-webdav', '恢复配置', 'danger')}</div><p class="settings-footnote">恢复成功后会重新加载设置；覆盖本地配置前会建立本地备份。</p>`;
    }
    const project = options.getProject();
    return `<p class="settings-description">为当前小说导入参考资料，生成草稿时可按相关性检索。清理索引后，可在定稿页补建章节检索记忆。</p><div class="knowledge-project"><span class="muted">当前小说</span><strong>${esc(project?.name || '请先打开或新建小说')}</strong><span>${esc(project?.path || '')}</span></div><div class="field"><label for="setting-knowledge-file">导入知识文件</label><input id="setting-knowledge-file" type="file" accept=".txt,.md" ${busy || !project ? 'disabled' : ''}><p class="field-help">支持 UTF-8、GBK / GB2312 的 TXT、Markdown 文件，每份最多 500 万字符。</p></div><div class="settings-panel-actions">${button('import-knowledge', '导入知识库', 'primary', !project)}${button('clear-index', '清理向量索引', 'danger', !project)}${button('refresh-knowledge', '刷新资料列表', 'quiet', !project)}</div>${knowledge?.projectId === project?.id ? `<div class="knowledge-list"><strong>已导入的知识资料 <span class="pill ${knowledge.has_vectorstore ? 'success' : 'warning'}">${knowledge.has_vectorstore ? '索引已建立' : '暂无检索索引'}</span></strong>${knowledge.files?.length ? knowledge.files.map(file => `<div><span>${esc(file.name)}</span><small>${Number(file.characters || 0).toLocaleString('zh-CN')} 字${file.indexed ? '' : ' · 待入库'}</small>${button('reimport-knowledge:' + encodeURIComponent(file.name), '重新入库', 'outline', !project)}</div>`).join('') : '<p class="muted">尚无已导入的资料。</p>'}</div>` : ''}`;
  }

  function render() {
    if (!visible()) return;
    root.innerHTML = `<div class="page-header"><div><div class="page-kicker">WORKSPACE SETTINGS</div><h1>设置</h1><p>管理模型、创作参数与连接，完整保留原软件的设置能力。</p></div><div class="header-meta">本机配置 · config.json</div></div><div class="settings-savebar"><span id="settings-save-state" class="save-state"></span><div class="button-row">${button('reload', '加载配置')}${button('save', '保存全部设置', 'primary', !dirty)}</div></div>${error ? `<div class="notice ${conflict ? 'warning' : 'error'}"><p>${esc(error)}${conflict ? ' 当前编辑仍已保留，请加载最新配置后重新修改。' : ''}</p>${button('reload', conflict ? '加载最新配置' : '重试加载')}</div>` : ''}${config ? `<div class="settings-layout"><div class="settings-menu" role="tablist" aria-label="设置分组">${Object.entries(groups).map(([key, labels], index) => `<button type="button" role="tab" aria-selected="${section === key}" class="${section === key ? 'active' : ''}" data-setting-section="${key}"><span class="settings-menu-number">${String(index + 1).padStart(2, '0')}</span><span><strong>${labels[0]}</strong><small>${labels[1]}</small></span></button>`).join('')}</div><section class="card settings-panel"><div class="card-header"><h2>${groups[section][0]}</h2><span class="micro">${groups[section][1]}</span></div><div class="settings-panel-body"><fieldset class="settings-fieldset" ${busy ? 'disabled' : ''}>${panelBody()}</fieldset>${result ? `<div class="settings-result ${result.error ? 'error' : ''}" role="status"><strong>${esc(result.title)}</strong><p>${esc(result.message)}</p></div>` : ''}${operationJob ? `<div class="settings-job"><div><strong>${esc(operationJob.stage || '设置任务')}</strong><span class="pill">${esc(({ queued: '排队中', running: '执行中', cancelling: '正在停止', succeeded: '已完成', failed: '失败', cancelled: '已停止', interrupted: '已中断' })[operationJob.status] || operationJob.status)}</span>${busy ? button('cancel-operation', '停止任务', 'outline', operationJob.status === 'cancelling') : ''}</div><pre aria-live="polite">${esc((operationJob.events || []).map(event => event.message).join('\n'))}</pre></div>` : ''}</div></section></div>` : `<div class="card loading-state">${loading ? '<span class="spinner"></span>正在读取完整配置…' : '点击“加载配置”重试。'}</div>`}`;
    root.querySelectorAll('[data-setting-section]').forEach(node => node.addEventListener('click', () => { section = node.dataset.settingSection; result = null; render(); if (section === 'knowledge') refreshKnowledge(); }));
    root.querySelectorAll('[data-setting-field]').forEach(node => {
      node.addEventListener('change', () => fieldChanged(node));
      if (node.tagName !== 'SELECT' && node.type !== 'checkbox') node.addEventListener('input', () => fieldChanged(node));
    });
    root.querySelectorAll('[data-clear-secret]').forEach(node => node.addEventListener('change', () => { const target = section === 'webdav' ? config.webdav_config : getPreset(); target[`clear_${node.dataset.clearSecret}`] = node.checked; if (node.checked) target[node.dataset.clearSecret] = ''; markDirty(); render(); }));
    root.querySelectorAll('[data-setting-action]').forEach(node => node.addEventListener('click', () => action(node.dataset.settingAction)));
    updateStatus();
  }

  function fieldChanged(node) {
    // A previous result describes the previous inputs, never the current edits.
    result = null;
    if (!busy) operationJob = null;
    root.querySelector('.settings-result')?.remove();
    if (!busy) root.querySelector('.settings-job')?.remove();
    const key = node.dataset.settingField;
    if (key === 'preset_selection') {
      if (section === 'llm') { selectedLLM = node.value; config.last_llm_config_name = node.value; config.last_interface_format = getPreset().interface_format; }
      else { selectedEmbedding = node.value; config.last_embedding_interface_format = node.value; }
      markDirty(); result = null; render(); return;
    }
    let target = section === 'tasks' ? config.choose_configs : section === 'defaults' ? config.other_params : section === 'proxy' ? config.proxy_setting : section === 'webdav' ? config.webdav_config : getPreset();
    if (key === 'language') target = config;
    target[key] = node.type === 'checkbox' ? node.checked : node.type === 'number' && node.value !== '' ? Number(node.value) : node.value;
    if (['api_key', 'webdav_password'].includes(key) && node.value) { target[`clear_${key}`] = false; root.querySelector(`[data-clear-secret="${key}"]`).checked = false; }
    if (section === 'llm' && key === 'interface_format') config.last_interface_format = node.value;
    markDirty();
  }

  async function editPreset(actionName) {
    const isLLM = section === 'llm', map = config[isLLM ? 'llm_configs' : 'embedding_configs'], selected = isLLM ? selectedLLM : selectedEmbedding;
    if (actionName === 'delete') {
      if (Object.keys(map).length <= 1) throw new Error('至少保留一个预设。');
      const references = isLLM ? Object.values(config.choose_configs).filter(name => name === selected).length : 0;
      const replacement = Object.keys(map).find(name => name !== selected);
      if (!await options.confirm('删除模型预设', `删除“${selected}”？${references ? ` ${references} 个任务将自动改用“${replacement}”。` : ''} 保存设置后生效。`, '删除预设', true)) return;
      delete map[selected];
      if (isLLM) { selectedLLM = replacement; config.last_llm_config_name = replacement; for (const key of Object.keys(config.choose_configs)) if (config.choose_configs[key] === selected) config.choose_configs[key] = replacement; }
      else { selectedEmbedding = replacement; config.last_embedding_interface_format = replacement; }
    } else {
      const name = await options.prompt(actionName === 'add' ? '新增预设' : actionName === 'rename' ? '重命名预设' : '复制预设', '预设名称', actionName === 'add' ? '' : actionName === 'copy' ? `${selected} 副本` : selected);
      if (name === null) return;
      const trimmed = name.trim();
      if (!trimmed) throw new Error('预设名称不能为空。');
      if (['__proto__', 'constructor', 'prototype'].includes(trimmed)) throw new Error('此名称为保留名称，请使用其他名称。');
      if (trimmed === selected && actionName === 'rename') return;
      if (Object.hasOwn(map, trimmed)) throw new Error('此名称已经存在，请使用其他名称。');
      if (actionName === 'add') map[trimmed] = isLLM ? { interface_format: 'OpenAI', base_url: 'https://api.openai.com/v1', model_name: '', temperature: 0.7, max_tokens: 8192, timeout: 600, has_api_key: false } : { interface_format: 'OpenAI', base_url: 'https://api.openai.com/v1', model_name: '', retrieval_k: 4, has_api_key: false };
      else {
        map[trimmed] = clone(map[selected]);
        if (actionName === 'copy') { const source = map[selected].original_name || map[selected].copy_api_key_from; if (source) map[trimmed].copy_api_key_from = source; delete map[trimmed].original_name; }
        else { delete map[selected]; if (isLLM) for (const key of Object.keys(config.choose_configs)) if (config.choose_configs[key] === selected) config.choose_configs[key] = trimmed; }
      }
      if (isLLM) { selectedLLM = trimmed; config.last_llm_config_name = trimmed; config.last_interface_format = map[trimmed].interface_format; }
      else { selectedEmbedding = trimmed; config.last_embedding_interface_format = trimmed; }
    }
    result = null; markDirty(); render();
  }

  function validate() {
    for (const [name, p] of Object.entries(config.llm_configs)) {
      if (p.temperature === '' || !Number.isFinite(Number(p.temperature)) || Number(p.temperature) < 0 || Number(p.temperature) > 2) throw new Error(`“${name}”的 Temperature 应为 0 到 2。`);
      for (const key of ['max_tokens', 'timeout']) if (!Number.isInteger(Number(p[key])) || Number(p[key]) < 0) throw new Error(`“${name}”的 ${key} 应为非负整数。`);
    }
    for (const [name, p] of Object.entries(config.embedding_configs)) if (!Number.isInteger(Number(p.retrieval_k)) || Number(p.retrieval_k) < 1) throw new Error(`“${name}”的 Retrieval Top-K 应为正整数。`);
    for (const [key, label] of Object.entries(tasks)) if (!config.llm_configs[config.choose_configs[key]]) throw new Error(`请为“${label}”选择有效的预设。`);
  }

  async function save() {
    if (busy || !config) return null;
    if ((section === 'llm' || section === 'embedding') && getPreset()) {
      const current = readModelForm(root, getPreset(), section);
      if (JSON.stringify(current) !== JSON.stringify(getPreset())) { Object.assign(getPreset(), current); markDirty(); }
    }
    if (!dirty) return null;
    validate();
    busy = true; result = null; render();
    try { const data = await api.saveSettings({ config: clone(config), revision }); receive(data); notify('全部设置已保存，后续任务将使用新配置。'); return data; }
    catch (exc) { if (exc.status === 409) { conflict = true; error = exc.message; } throw exc; }
    finally { busy = false; render(); }
  }

  async function refreshKnowledge() {
    const project = options.getProject(); if (!project) return;
    try { const value = await api.knowledge(project.id); knowledge = { ...value, projectId: project.id }; if (visible() && section === 'knowledge') render(); }
    catch (exc) { notify(exc.message, 'error'); }
  }

  async function operation(title, work) {
    busy = true; result = null; render();
    try {
      let data = await work();
      if (data.job) data = data.job;
      if (data.id && data.status) {
        operationJob = data; render();
        let retries = 0;
        while (['queued', 'running', 'cancelling'].includes(operationJob.status)) {
          await new Promise(resolve => setTimeout(resolve, 1200));
          try { operationJob = await api.job(operationJob.id); retries = 0; }
          catch (exc) { if (++retries > 5) throw exc; }
          render();
        }
        if (operationJob.status !== 'succeeded') throw new Error(operationJob.error || (operationJob.status === 'cancelled' ? '任务已停止。' : '任务未完成，请重试。'));
        data = operationJob.result || {};
      }
      result = { title, message: (data.message || data.detail || '操作已完成。') + (data.dimension ? `\n向量维度：${data.dimension}` : '') };
      if (!visible()) notify(`${title}已完成。`);
      return data;
    }
    catch (exc) { result = { title: `${title}未完成`, message: exc.message, error: true }; throw exc; }
    finally { busy = false; render(); }
  }

  async function action(name) {
    if (name === 'cancel-operation' && operationJob) {
      try { operationJob = await api.cancelJob(operationJob.id); render(); }
      catch (exc) { notify(exc.message, 'error'); }
      return;
    }
    if (busy) return;
    try {
      if (name === 'save') return await save();
      if (name === 'reload' || name === 'refresh-presets') {
        if (dirty && !await options.confirm('重新加载配置', '重新加载会丢弃本页未保存的设置。是否加载后端最新配置？', '加载配置')) return;
        result = null; error = ''; await load(true); return;
      }
      if (['add', 'rename', 'copy', 'delete'].includes(name)) return await editPreset(name);
      if (name === 'open-default-directory') return options.openDirectory(config.other_params.filepath || '');
      if (name === 'test-llm' || name === 'test-embedding') {
        const kind = name === 'test-llm' ? 'llm' : 'embedding';
        const current = readModelForm(root, getPreset(kind), kind);
        if (JSON.stringify(current) !== JSON.stringify(getPreset(kind))) { Object.assign(getPreset(kind), current); markDirty(); }
        const payload = { type: kind === 'llm' ? 'test_llm' : 'test_embedding', name: kind === 'llm' ? selectedLLM : selectedEmbedding, config: current, revision, proxy_setting: clone(config.proxy_setting) };
        return await operation(`${kind === 'llm' ? '模型连接测试' : 'Embedding 连接测试'} · ${current.model_name || '未填写模型'}`, () => api.settingsOperation(payload));
      }
      if (name === 'test-webdav') return await operation('WebDAV 连接测试', () => api.settingsOperation({ type: 'test_webdav', webdav_config: clone(config.webdav_config), revision, proxy_setting: clone(config.proxy_setting) }));
      if (name === 'backup-webdav') {
        if (dirty && !await save()) return;
        if (!await options.confirm('备份配置到 WebDAV', '将包含模型密钥的当前配置上传到你设置的 WebDAV 地址。', '备份配置')) return;
        return await operation('WebDAV 配置备份', () => api.settingsOperation({ type: 'webdav_backup', revision }));
      }
      if (name === 'restore-webdav') {
        if (!await options.confirm('从 WebDAV 恢复配置', '远程配置将覆盖本地配置和未保存的设置；恢复前会备份本地文件。是否继续？', '恢复配置', true)) return;
        const data = await operation('WebDAV 配置恢复', () => api.settingsOperation({ type: 'webdav_restore', webdav_config: clone(config.webdav_config), proxy_setting: clone(config.proxy_setting), revision }));
        if (data.settings) receive(data.settings); else if (data.config) receive(data); else await load(true);
        render(); notify('已恢复配置，任务模型和预设已刷新。'); return;
      }
      if (name === 'refresh-knowledge') return await refreshKnowledge();
      const project = options.getProject();
      if (!project) throw new Error('请先打开或新建一部小说。');
      if (name.startsWith('reimport-knowledge:')) {
        const filename = decodeURIComponent(name.slice('reimport-knowledge:'.length));
        const data = await operation('重新导入知识库', () => api.importKnowledge(project.id, { filename }));
        await refreshKnowledge(); return data;
      }
      if (name === 'clear-index') {
        if (!await options.confirm('清理向量索引', `清理“${project.name}”的向量检索索引？章节原文和知识源文件会保留；需要重新导入知识或补建章节检索记忆。`, '清理索引', true)) return;
        const data = await operation('清理向量索引', () => api.clearKnowledge(project.id)); await refreshKnowledge(); return data;
      }
      if (name === 'import-knowledge') {
        const file = root.querySelector('#setting-knowledge-file')?.files?.[0];
        if (!file) throw new Error('请先选择 TXT 或 Markdown 知识文件。');
        if (file.size > 20 * 1024 * 1024) throw new Error('知识文件应小于 20 MB。');
        const text = decodeKnowledgeText(await file.arrayBuffer());
        if (!text.trim()) throw new Error('知识文件内容为空。');
        if (text.length > 5000000) throw new Error('单个知识文件不能超过 500 万字符。');
        const data = await operation('导入知识库', () => api.importKnowledge(project.id, { filename: file.name, content: text }));
        await refreshKnowledge(); options.onKnowledgeJob?.(data); return;
      }
    } catch (exc) { notify(exc.message || '操作失败，请重试。', 'error'); }
  }

  return { mount(container) { root = container; if (!config) load(); render(); if (section === 'knowledge' && knowledge?.projectId !== options.getProject()?.id) refreshKnowledge(); }, isDirty: () => dirty, save: () => action('save'), refresh: () => load(true) };
}
