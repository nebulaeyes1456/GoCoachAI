// WebSocket 进度监听（窗口4）。
// - 真实模式：连接 /ws/review/{id}，断线自动重连；
//   连接建立后先查一次 status（避免错过已完成任务的 done 消息），
//   并以 GET status 轮询作为兜底。
// - Mock 模式：本地模拟进度推进。
// 用法：watchReview(reviewId, { onProgress, onDone, onFailed }) -> { close() }

import { api, getSource, getApiBase } from './api.js';

export function wsUrlFor(reviewId) {
  const base = getApiBase().replace(/^http/, 'ws');
  return `${base}/ws/review/${reviewId}`;
}

export function watchReview(reviewId, handlers = {}) {
  if (getSource() === 'mock') {
    return watchMock(handlers);
  }
  return watchReal(reviewId, handlers);
}

// ---------------------------------------------------------------------------
// Mock：约 4 秒推进到 100%
// ---------------------------------------------------------------------------

function watchMock({ onProgress, onDone, onFailed }) {
  let closed = false;
  let progress = 0;
  let step = 0;
  const timer = setInterval(() => {
    if (closed) return;
    step += 1;
    progress = Math.min(1, step / 18 + Math.random() * 0.03);
    if (progress >= 1) {
      clearInterval(timer);
      if (onDone) onDone();
      return;
    }
    if (onProgress) onProgress(Math.round(progress * 1000) / 1000);
  }, 200);
  return { close() { closed = true; clearInterval(timer); } };
}

// ---------------------------------------------------------------------------
// 真实：WS + 轮询兜底
// ---------------------------------------------------------------------------

function watchReal(reviewId, { onProgress, onDone, onFailed }) {
  let closed = false;
  let finished = false;
  let ws = null;
  let pollTimer = null;
  let retryDelay = 2000;
  const MAX_RETRY = 30;

  const finish = () => { finished = true; };

  const checkStatus = async () => {
    if (closed || finished) return;
    try {
      const st = await api.reviewStatus(reviewId);
      if (closed) return;
      if (st.status === 'done') { finish(); stopAll(); if (onDone) onDone(); }
      else if (st.status === 'failed') {
        finish(); stopAll();
        if (onFailed) onFailed(st.error || '分析失败');
      } else if (typeof st.progress === 'number') {
        if (onProgress) onProgress(st.progress);
      }
    } catch (e) { /* 轮询失败忽略，等下次 */ }
  };

  const startPolling = () => {
    if (pollTimer) return;
    pollTimer = setInterval(checkStatus, 2500);
  };

  const connect = () => {
    if (closed || finished) return;
    let socket;
    try {
      socket = new WebSocket(wsUrlFor(reviewId));
    } catch (e) {
      scheduleRetry();
      return;
    }
    ws = socket;

    socket.onopen = () => {
      retryDelay = 2000;
      startPolling();
      checkStatus(); // 连接建立后立刻对齐一次状态
    };

    socket.onmessage = (ev) => {
      if (closed || finished) return;
      let msg = null;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || !msg.type) return;
      if (msg.type === 'progress') {
        if (onProgress) onProgress(msg.progress);
      } else if (msg.type === 'done') {
        finish(); stopAll();
        if (onDone) onDone();
      } else if (msg.type === 'failed') {
        finish(); stopAll();
        if (onFailed) onFailed(msg.error || '分析失败');
      }
    };

    socket.onclose = () => {
      ws = null;
      if (!closed && !finished) scheduleRetry();
    };

    socket.onerror = () => { /* onclose 会跟进处理 */ };
  };

  const scheduleRetry = () => {
    if (closed || finished) return;
    setTimeout(() => {
      if (!closed && !finished) connect();
      retryDelay = Math.min(retryDelay * 1.5, 15000);
    }, retryDelay);
  };

  const stopAll = () => {
    if (ws) { try { ws.onclose = null; ws.close(); } catch (e) { /* 忽略 */ } ws = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };

  connect();
  // 兜底轮询：即使 WS 建立失败也能推进
  startPolling();

  return {
    close() {
      closed = true;
      stopAll();
    },
  };
}
