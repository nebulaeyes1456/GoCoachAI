// fetch 封装（窗口4）。
// baseURL：浏览器与桌面一致使用 http://127.0.0.1:8765；
// 若页面本身由后端挂在同源（桌面壳/uvicorn 直开），则用相对路径更稳（兼容 localhost 访问）。
// 数据源开关：Mock（默认）/ 真实，见 initSource/setSource。

import { mockApi, MOCK_SAMPLE_SGF } from './mock.js?v=20260910a';

const ABS_BASE = 'http://127.0.0.1:8765';

export function getApiBase() {
  // http(s) 同源（后端托管或任意静态服务器）时用相对路径；
  // 注：页面脚本为 ESM，Chrome 禁止 file:// 下的模块加载，离线调试请用静态服务器
  // （如 `python -m http.server`），而非双击 html。
  return window.location.protocol === 'file:' ? ABS_BASE : '';
}

const SOURCE_KEY = 'goc_data_source';
let source = localStorage.getItem(SOURCE_KEY) || 'mock';

export function getSource() {
  return source;
}

export function setSource(s) {
  source = s === 'real' ? 'real' : 'mock';
  localStorage.setItem(SOURCE_KEY, source);
  window.dispatchEvent(new CustomEvent('goc:sourcechange', { detail: source }));
}

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function realFetch(method, path, body) {
  let resp;
  try {
    resp = await fetch(getApiBase() + path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(
      '无法连接后端（请确认服务已在 8765 端口启动，或切换为 Mock 数据源）', 0
    );
  }
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    data = null;
  }
  if (!resp.ok) {
    const detail = (data && data.detail) || `请求失败（HTTP ${resp.status}）`;
    throw new ApiError(detail, resp.status);
  }
  return data;
}

const realApi = {
  analyze: (sgfText, profile) =>
    realFetch('POST', '/api/v1/review/analyze', { sgf_text: sgfText, profile }),
  reviewStatus: (id) => realFetch('GET', `/api/v1/review/${id}/status`),
  reviewDetail: (id) => realFetch('GET', `/api/v1/review/${id}`),
  explain: (reviewId, moveNumber) =>
    realFetch('POST', '/api/v1/coach/explain', { review_id: reviewId, move_number: moveNumber }),
  summary: (reviewId) =>
    realFetch('POST', '/api/v1/coach/summary', { review_id: reviewId }),
  deep: (reviewId) =>
    realFetch('POST', '/api/v1/coach/deep', { review_id: reviewId }),
  penalty: (reviewId, moveNumber) =>
    realFetch('POST', '/api/v1/coach/penalty', { review_id: reviewId, move_number: moveNumber }),
  playMove: (size, moves, komi, rank) =>
    realFetch('POST', '/api/v1/play/move', { size, moves, komi, ai_rank: rank || null }),
  playTip: (size, moves, coord, best, delta, level) =>
    realFetch('POST', '/api/v1/play/tip', { size, moves, coord, best, delta, level }),
  ttsBlob: async (text, persona) => {
    let resp;
    try {
      resp = await fetch(getApiBase() + '/api/v1/coach/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, persona: persona || 'gentle' }),
      });
    } catch (e) {
      throw new ApiError('无法连接后端进行语音合成', 0);
    }
    if (!resp.ok) {
      throw new ApiError(`语音合成失败（HTTP ${resp.status}）`, resp.status);
    }
    return resp.blob();
  },
  generateProblems: (reviewId, themes, maxProblems, targetRank) =>
    realFetch('POST', '/api/v1/problems/generate', {
      review_id: reviewId, themes, max_problems: maxProblems, target_rank: targetRank,
    }),
  library: ({ theme, rank, limit, offset, sort } = {}) => {
    const qs = new URLSearchParams();
    if (theme) qs.set('theme', theme);
    if (rank !== null && rank !== undefined && rank !== '') qs.set('rank', rank);
    qs.set('limit', limit || 20);
    if (offset) qs.set('offset', offset);
    if (sort) qs.set('sort', sort);
    return realFetch('GET', `/api/v1/problems/library?${qs.toString()}`);
  },
  problem: (id) => realFetch('GET', `/api/v1/problems/${id}`),
  attempt: (problemId, coord) =>
    realFetch('POST', `/api/v1/problems/${problemId}/attempt`, { coord }),
  problemExplain: (problemId) =>
    realFetch('POST', `/api/v1/problems/${problemId}/explain`, {}),
  extractProblem: (reviewId, moveNumber) =>
    realFetch('POST', '/api/v1/problems/extract_from_review',
      { review_id: reviewId, move_number: moveNumber }),
  ask: (sgfText, question, level) =>
    realFetch('POST', '/api/v1/coach/ask', { sgf_text: sgfText, question, level }),
  systemInfo: () => realFetch('GET', '/api/v1/system/info'),
  updateSettings: (updates) => realFetch('PUT', '/api/v1/system/settings', updates),
  // ---- T0 运维/诊断（设置页用）----
  systemHealth: () => realFetch('GET', '/api/v1/system/health'),
  engineStatus: () => realFetch('GET', '/api/v1/system/engine/status'),
  engineRestart: (backend) =>
    realFetch('POST', '/api/v1/system/engine/restart',
      backend === undefined || backend === null ? {} : { backend }),
  systemLogs: (lines = 200) =>
    realFetch('GET', `/api/v1/system/logs?lines=${lines}`),
  cacheClear: (kind) =>
    realFetch('POST', '/api/v1/system/cache/clear', { kind: kind || 'all' }),
  dbBackup: () => realFetch('POST', '/api/v1/system/db/backup'),
  version: () => realFetch('GET', '/api/v1/system/version'),

  // ---- 任务书统一命名封装（对应 /api/v1/system/*） ----
  getSystemInfo: () => realFetch('GET', '/api/v1/system/info'),
  getHealth: () => realFetch('GET', '/api/v1/system/health'),
  getVersion: () => realFetch('GET', '/api/v1/system/version'),
  restartEngine: (payload) =>
    realFetch('POST', '/api/v1/system/engine/restart', payload || {}),
  clearCache: (payload) =>
    realFetch('POST', '/api/v1/system/cache/clear', payload || {}),
};

function dispatch(method, ...args) {
  const impl = source === 'real' ? realApi : mockApi;
  return impl[method](...args);
}

// 统一出口：页面只调用 api.*
export const api = {
  analyze: (sgfText, profile) => dispatch('analyze', sgfText, profile),
  reviewStatus: (id) => dispatch('reviewStatus', id),
  reviewDetail: (id) => dispatch('reviewDetail', id),
  explain: (reviewId, moveNumber) => dispatch('explain', reviewId, moveNumber),
  summary: (reviewId) => dispatch('summary', reviewId),
  deep: (reviewId) => dispatch('deep', reviewId),
  penalty: (reviewId, moveNumber) => dispatch('penalty', reviewId, moveNumber),
  playMove: (size, moves, komi, rank) => dispatch('playMove', size, moves, komi, rank),
  playTip: (size, moves, coord, best, delta, level) =>
    dispatch('playTip', size, moves, coord, best, delta, level),
  // TTS 始终走后端（mock 模式无后端时由调用方降级提示）
  ttsBlob: (text, persona) => realApi.ttsBlob(text, persona),
  generateProblems: (reviewId, themes, maxProblems, targetRank) =>
    dispatch('generateProblems', reviewId, themes, maxProblems, targetRank),
  library: (opts) => dispatch('library', opts || {}),
  problem: (id) => dispatch('problem', id),
  attempt: (problemId, coord) => dispatch('attempt', problemId, coord),
  problemExplain: (problemId) => dispatch('problemExplain', problemId),
  extractProblem: (reviewId, moveNumber) =>
    dispatch('extractProblem', reviewId, moveNumber),
  ask: (sgfText, question, level) => dispatch('ask', sgfText, question, level),
  systemInfo: () => dispatch('systemInfo'),
  updateSettings: (updates) => dispatch('updateSettings', updates),
  systemHealth: () => dispatch('systemHealth'),
  engineStatus: () => dispatch('engineStatus'),
  engineRestart: (backend) => dispatch('engineRestart', backend),
  systemLogs: (lines) => dispatch('systemLogs', lines),
  cacheClear: (kind) => dispatch('cacheClear', kind),
  dbBackup: () => dispatch('dbBackup'),
  version: () => dispatch('version'),

  // ---- 任务书统一命名封装（页面统一用这些方法） ----
  getSystemInfo: () => dispatch('getSystemInfo'),
  getHealth: () => dispatch('getHealth'),
  getVersion: () => dispatch('getVersion'),
  restartEngine: (payload) => dispatch('restartEngine', payload),
  clearCache: (payload) => dispatch('clearCache', payload),
};

export { MOCK_SAMPLE_SGF };
