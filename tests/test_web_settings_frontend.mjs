import assert from 'node:assert/strict';
import test from 'node:test';
import { decodeKnowledgeText, readModelForm, ensureSettingsSaved } from '../frontend/js/settings.js';

test('generation cannot silently use saved settings after editing a new model', async () => {
  let dirty = true, saves = 0;
  const controller = { isDirty: () => dirty, save: async () => { saves++; dirty = false; } };
  assert.equal(await ensureSettingsSaved(controller, async () => false), false);
  assert.equal(saves, 0);
  assert.equal(await ensureSettingsSaved(controller, async () => true), true);
  assert.equal(saves, 1);
  assert.equal(dirty, false);
});

test('generation stops if settings validation or a revision conflict leaves edits unsaved', async () => {
  const controller = { isDirty: () => true, save: async () => undefined };
  await assert.rejects(() => ensureSettingsSaved(controller, async () => true), /尚未保存成功/);
});

test('already saved settings need no extra confirmation', async () => {
  let called = false;
  assert.equal(await ensureSettingsSaved({ isDirty: () => false }, async () => { called = true; }), true);
  assert.equal(called, false);
});

function modelForm(values) {
  return { querySelector: selector => {
    if (selector === '[data-clear-secret="api_key"]') return { checked: values.clear_api_key || false };
    const key = selector.replace('#setting-', '');
    if (!(key in values)) return null;
    return { value: String(values[key]), type: ['temperature', 'max_tokens', 'timeout', 'retrieval_k'].includes(key) ? 'number' : 'text' };
  } };
}

test('model tests read the visible model ID even if autofill did not emit input', () => {
  const saved = { model_name: 'old-model', interface_format: 'OpenAI', base_url: 'https://old.invalid/v1',
    temperature: 0.7, max_tokens: 8192, timeout: 600, original_name: '预设名称', has_api_key: true };
  const current = readModelForm(modelForm({ model_name: ' new-model ', base_url: 'https://new.invalid/v1',
    api_key: '', temperature: '0.3', max_tokens: '4096', timeout: '30' }), saved);
  assert.equal(current.model_name, 'new-model');
  assert.equal(current.base_url, 'https://new.invalid/v1');
  assert.equal(current.temperature, 0.3);
  assert.equal(current.original_name, '预设名称');
  assert.equal(saved.model_name, 'old-model');
  assert.equal(Object.hasOwn(current, 'api_key'), false);
});

test('reading an unchanged form preserves the saved-key marker without making a new edit', () => {
  const saved = { model_name: 'test-model', has_api_key: true };
  assert.deepEqual(readModelForm(modelForm({ model_name: 'test-model', api_key: '' }), saved), saved);
});

test('embedding tests read the visible model and retain explicit credential clearing', () => {
  const saved = { model_name: 'old-embedding', original_name: '旧预设', api_key: 'draft-key' };
  const current = readModelForm(modelForm({ model_name: 'new-embedding', api_key: '', retrieval_k: 8, clear_api_key: true }), saved, 'embedding');
  assert.equal(current.model_name, 'new-embedding');
  assert.equal(current.retrieval_k, 8);
  assert.equal(current.clear_api_key, true);
  assert.equal(current.api_key, '');
});

test('knowledge files preserve UTF-8 Chinese and discard a UTF-8 BOM', () => {
  assert.equal(decodeKnowledgeText(new TextEncoder().encode('中文资料\n第二行')), '中文资料\n第二行');
  assert.equal(decodeKnowledgeText(new Uint8Array([0xef, 0xbb, 0xbf, 0x61])), 'a');
});

test('legacy GBK and GB2312 Chinese files import without replacement characters', () => {
  assert.equal(decodeKnowledgeText(new Uint8Array([0xd6, 0xd0, 0xce, 0xc4])), '中文');
});

test('malformed files report an encoding error rather than silently damaging content', () => {
  assert.throws(() => decodeKnowledgeText(new Uint8Array([0xff, 0xff])), /编码/);
});
