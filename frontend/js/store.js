const STORAGE_KEY = 'novel-workbench-v1';
let cache;
try { cache = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { cache = {}; }
const session = { projectId: cache.projectId || '', view: cache.view || 'architecture', chapter: cache.chapter || 1, taskId: cache.taskId || '', forms: cache.forms || {}, documents: cache.documents || {}, selectedTab: cache.selectedTab || {} };
let timer;
export const store = session;
export function persist() {
  clearTimeout(timer);
  timer = setTimeout(flush, 200);
}
export function flush() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(session)); }
  catch { /* File saves stay available when browser storage is full or disabled. */ }
}
export function documentKey(projectId, kind, chapter) { return `${projectId}:${kind}${chapter ? `:${chapter}` : ''}`; }
export function editorState(projectId, kind, chapter, remote = {}) {
  const key = documentKey(projectId, kind, chapter);
  if (!session.documents[key]) session.documents[key] = { content: remote.content || '', savedContent: remote.content || '', revision: remote.revision || remote.version || '', modified: false };
  const state = session.documents[key];
  if (!state.modified && remote.content !== undefined) { state.content = remote.content || ''; state.savedContent = state.content; state.revision = remote.revision || remote.version || ''; }
  return state;
}
export function setContent(projectId, kind, chapter, content) {
  const state = editorState(projectId, kind, chapter);
  state.content = content;
  state.modified = state.content !== state.savedContent;
  persist();
  return state;
}
export function markSaved(state, response = {}) { state.savedContent = response.content !== undefined ? response.content : state.content; state.modified = state.content !== state.savedContent; state.revision = response.revision || response.version || state.revision; persist(); }
export function hasDirty(projectId) { return Object.entries(session.documents).some(([key, value]) => key.startsWith(`${projectId}:`) && value.modified); }
