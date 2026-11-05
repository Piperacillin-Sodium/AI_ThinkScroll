const statusNames = { queued: '排队中', running: '生成中', cancelling: '正在停止', cancelled: '已停止', succeeded: '已完成', failed: '任务失败', interrupted: '任务中断' };

export function resultSummary(task) {
  const result = task.result || {}, details = result.details || {}, lines = [];
  if (result.message) lines.push(result.message);
  if (result.already_finalized) lines.push('此正文版本已经定稿，未重复更新摘要与人物状态。');
  if (details.memory) lines.push(`创作记忆：${({ completed: '已更新摘要与人物状态', stale: '需要重新定稿' })[details.memory] || details.memory}`);
  if (details.index) lines.push(`检索索引：${({ succeeded: '已完成', failed: '未完成，请补建检索记忆', pending: '待更新' })[details.index] || details.index}`);
  if (details.index_details?.error) lines.push(`索引信息：${details.index_details.error}`);
  if (result.stopped) lines.push(`任务停止：${result.reason || '请查看日志'}`);
  // Directory jobs store a count; batch jobs store one record per chapter.
  // Keep both schemas readable, including already persisted directory jobs.
  if (Array.isArray(result.chapters)) {
    lines.push(...result.chapters.map(item => `第 ${item.chapter} 章：${item.status === 'completed' ? '已生成并定稿' : item.status === 'skipped' ? '已定稿，跳过' : item.status}${item.reason ? `（${item.reason}）` : ''}`));
  } else if (Number.isInteger(result.chapters) && result.chapters >= 0) {
    lines.push(`已生成 ${result.chapters} 章目录。`);
  }
  if (!lines.length) lines.push(task.status === 'succeeded' ? '任务已完成。' : statusNames[task.status] || '任务结果');
  return lines.join('\n');
}
