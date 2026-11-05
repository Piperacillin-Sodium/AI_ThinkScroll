import assert from 'node:assert/strict';
import test from 'node:test';
import { resultSummary } from '../frontend/js/task-results.js';

test('directory candidate summaries accept the numeric chapter count from persisted jobs', () => {
  const task = { type: 'directory', status: 'succeeded', result: { chapters: 10,
    documents: [{ kind: 'directory', content: '第1章 - 灯塔来信', base_revision: 'old' }] } };
  assert.equal(resultSummary(task), '已生成 10 章目录。');
  assert.equal(task.result.documents[0].content, '第1章 - 灯塔来信');
});

test('batch summaries preserve per-chapter results and stopping reasons', () => {
  const summary = resultSummary({ status: 'succeeded', result: { stopped: true, reason: '正文需人工确认',
    chapters: [{ chapter: 1, status: 'completed' }, { chapter: 2, status: 'skipped', reason: '已经定稿' }] } });
  assert.match(summary, /第 1 章：已生成并定稿/);
  assert.match(summary, /第 2 章：已定稿，跳过（已经定稿）/);
  assert.match(summary, /任务停止：正文需人工确认/);
});

test('direct results without candidate documents still describe committed memory and indexing', () => {
  assert.match(resultSummary({ status: 'succeeded', result: { already_finalized: true,
    details: { memory: 'completed', index: 'failed', index_details: { error: '请补建' } } } }), /索引信息：请补建/);
  assert.equal(resultSummary({ status: 'succeeded', result: { message: '已清理索引。' } }), '已清理索引。');
});

test('missing or unexpected chapter summaries do not break other task previews', () => {
  for (const chapters of [undefined, null, {}, '10', []]) {
    assert.equal(resultSummary({ status: 'succeeded', result: { chapters } }), '任务已完成。');
  }
});
