export class ApiError extends Error {
  constructor(message, status, details) { super(message); this.name = 'ApiError'; this.status = status; this.details = details; }
}

export async function request(path, options = {}) {
  const { signal, ...rest } = options;
  let response;
  try { response = await fetch(path, { ...rest, signal, headers: { 'Content-Type': 'application/json', ...(rest.headers || {}) } }); }
  catch (error) { if (error.name === 'AbortError') throw error; throw new ApiError('无法连接本地服务。请确认网页服务正在运行，再刷新页面。', 0); }
  if (!response.ok) {
    let value; try { value = await response.json(); } catch { value = null; }
    let message = value?.detail || value?.error || value?.message || `请求失败（HTTP ${response.status}）`;
    if (Array.isArray(message)) message = message.map(item => `${(item.loc || []).slice(1).join('.')}：${item.msg}`).join('；');
    if (typeof message === 'object') message = message.message || JSON.stringify(message);
    throw new ApiError(String(message), response.status, value);
  }
  if (response.status === 204) return null;
  return response.json();
}

const projectPath = id => `/api/projects/${encodeURIComponent(id)}`;
export const api = {
  bootstrap: () => request('/api/bootstrap'),
  settings: () => request('/api/settings'),
  reloadSettings: () => request('/api/settings/reload', { method: 'POST', body: '{}' }),
  saveSettings: body => request('/api/settings', { method: 'PUT', body: JSON.stringify(body) }),
  settingsOperation: body => request('/api/settings/operations', { method: 'POST', body: JSON.stringify(body) }),
  knowledge: id => request(`${projectPath(id)}/knowledge`),
  importKnowledge: (id, body) => request(`${projectPath(id)}/knowledge`, { method: 'POST', body: JSON.stringify(body) }),
  clearKnowledge: id => request(`${projectPath(id)}/knowledge/clear`, { method: 'POST', body: JSON.stringify({ confirm: true }) }),
  createProject: body => request('/api/projects', { method: 'POST', body: JSON.stringify(body) }),
  importProject: body => request('/api/projects/import', { method: 'POST', body: JSON.stringify(body) }),
  updateProject: (id, body) => request(projectPath(id), { method: 'PATCH', body: JSON.stringify(body) }),
  document: (id, kind, chapter) => request(`${projectPath(id)}/documents/${encodeURIComponent(kind)}${chapter ? `?chapter=${chapter}` : ''}`),
  saveDocument: (id, kind, body, chapter) => request(`${projectPath(id)}/documents/${encodeURIComponent(kind)}${chapter ? `?chapter=${chapter}` : ''}`, { method: 'PUT', body: JSON.stringify(body) }),
  chapters: id => request(`${projectPath(id)}/chapters`),
  startJob: (id, body) => request(`${projectPath(id)}/jobs`, { method: 'POST', body: JSON.stringify(body) }),
  jobs: id => request(`/api/jobs?project_id=${encodeURIComponent(id)}`),
  job: id => request(`/api/jobs/${encodeURIComponent(id)}`),
  cancelJob: id => request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' }),
  roles: id => request(`${projectPath(id)}/roles`),
  history: (id, kind, chapter) => request(`${projectPath(id)}/documents/${encodeURIComponent(kind)}/history${chapter ? `?chapter=${chapter}` : ''}`),
  acceptJob: id => request(`/api/jobs/${encodeURIComponent(id)}/apply`, { method: 'POST', body: '{}' }),
};
