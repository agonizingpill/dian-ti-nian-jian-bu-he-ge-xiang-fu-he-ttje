/* 双通道分流复核台：自包含模块，由 app.js 的 renderPage('review') 调用 */
(function () {
  const state = { timer: null, expanded: new Set(), lifts: [] };
  const token = () => localStorage.getItem('lift_token') || '';
  const me = () => JSON.parse(localStorage.getItem('lift_user') || 'null') || {};
  const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m]));

  const STATUS = {
    awaiting_channels: ['等待双通道结果', 'yellow'],
    auto_concluded: ['已自动结论', 'green'],
    manual_pending: ['待人工接手', 'red'],
    manual_claimed: ['人工复核中', 'blue'],
    manual_closed: ['已人工结论', 'green'],
  };
  const REASON = {
    diverged: '两边分歧',
    intake_timeout: '接单处超时未算完',
    backend_timeout: '后台超时未算完',
    both_timeout: '双通道都未算完',
  };
  const SUGGESTION = { ready: 'ready · 就绪', review: 'review · 复核', hold: 'hold · 暂缓' };
  const CHANNEL_NAME = { intake: '接单处', backend: '后台' };

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token()}`, ...(options.headers || {}) },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
    return data;
  }
  function toast(msg, danger = false) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.style.background = danger ? '#a83832' : '#0b1b2b';
    el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 3200);
  }
  const badge = status => {
    const [label, color] = STATUS[status] || [status, 'gray'];
    return `<span class="badge ${color}">${label}</span>`;
  };
  const chanCell = (session, channel) => session.channels[channel] === 'done'
    ? '<span class="chan done">● 已出结果</span>'
    : '<span class="chan pending">… 计算中</span>';

  function reasonTag(s) {
    if (s.escalate_reason && REASON[s.escalate_reason]) {
      return `<span class="reason-tag">${REASON[s.escalate_reason]}</span>`;
    }
    if (s.status === 'awaiting_channels' && s.timed_out) return '<span class="reason-tag">已超时·结算中</span>';
    return '';
  }
  function statusHint(s) {
    if (s.status === 'manual_pending') return '<small class="hint">补算对上可自动收回</small>';
    if (s.status === 'manual_claimed') return `<small class="hint">${esc(s.claimed_by || '')} 复核中</small>`;
    return '';
  }
  function conclusionCell(s) {
    if (!s.conclusion) return '—';
    const source = s.conclusion_source === 'auto' ? '自动' : '人工';
    return `<strong>${esc(SUGGESTION[s.conclusion] || s.conclusion)}</strong><small class="hint">${source}结论${s.decided_at ? ' · ' + esc(s.decided_at) : ''}</small>`;
  }
  function actionCell(s) {
    const buttons = [`<button class="small-btn" data-review-detail="${s.id}">${state.expanded.has(s.id) ? '收起' : '详情'}</button>`];
    if (me().role === 'supervisor' && s.status === 'manual_pending') {
      buttons.push(`<button class="small-btn primary" data-review-claim="${s.id}">接手</button>`);
    }
    if (me().role === 'supervisor' && s.status === 'manual_claimed' && s.claimed_by === me().email) {
      buttons.push(`<button class="small-btn primary" data-review-conclude="${s.id}">填写结论</button>`);
    }
    return buttons.join(' ');
  }

  function rowHtml(s) {
    const diverged = s.diverged ? ' row-diverged' : '';
    let html = `<tr class="review-row${diverged}" data-row="${s.id}">
      <td><strong>${esc(s.asset_code || '#' + s.lift_id)}</strong><span class="subtext">${esc(s.building || '')}</span></td>
      <td><span class="subtext">${esc(s.created_at)}</span><small class="hint">${esc(s.created_by)}</small></td>
      <td>${chanCell(s, 'intake')}</td>
      <td>${chanCell(s, 'backend')}</td>
      <td>${badge(s.status)}${reasonTag(s)}${statusHint(s)}</td>
      <td>${conclusionCell(s)}</td>
      <td>${actionCell(s)}</td>
    </tr>`;
    if (state.expanded.has(s.id)) html += `<tr class="detail-row" data-detail-row="${s.id}"><td colspan="7"><div class="detail-slot" data-detail-slot="${s.id}"><p class="muted">正在读取通道明细…</p></div></td></tr>`;
    return html;
  }

  function detailHtml(d) {
    const cards = ['intake', 'backend'].map(channel => {
      const r = (d.channel_results || []).find(x => x.channel === channel);
      if (!r) return `<div class="chan-card missing"><h4>${CHANNEL_NAME[channel]}</h4><p class="pending-note">… 尚未算出结果</p></div>`;
      return `<div class="chan-card${r.reference_only ? ' reference' : ''}">
        <h4>${CHANNEL_NAME[channel]}${r.reference_only ? '<span class="ref-tag">迟来建议 · 仅作参考</span>' : ''}</h4>
        <p>建议：<strong>${esc(SUGGESTION[r.suggestion] || r.suggestion)}</strong></p>
        <p class="subtext">命中分支</p><pre class="branch-code">${esc(r.branch_signature)}</pre>
        <p class="subtext">${esc(r.model_version)} · ${esc(r.computed_at)}</p>
      </div>`;
    }).join('');
    const verdict = d.diverged
      ? '<p class="verdict diverged">两边建议或命中分支对不上，已转人工。</p>'
      : (d.conclusion ? `<p class="verdict">两边结果一致，${d.conclusion_source === 'auto' ? '已自动出结论' : '人工已结论'}。</p>` : '<p class="verdict">等待两边结果到齐后自动核对。</p>');
    const features = d.features ? `<p class="subtext">送审数据：关键不合格 ${d.features.critical_fail_count} · 平均分 ${d.features.average_score} · 不合格项 ${d.features.failed_item_count} · 资料缺口 ${d.features.documentation_gap}</p>` : '';
    const note = d.conclusion_note ? `<p class="subtext">人工复核依据：${esc(d.conclusion_note)}</p>` : '';
    return `<div class="chan-grid">${cards}</div>${verdict}${features}${note}`;
  }

  async function refresh(container) {
    try {
      const data = await api('/api/review-sessions');
      const body = container.querySelector('[data-board-body]');
      if (!body) return;
      body.innerHTML = data.sessions.length
        ? `<table class="data-table review-table"><thead><tr><th>设备</th><th>送审</th><th>接单处</th><th>后台</th><th>状态</th><th>结论</th><th>操作</th></tr></thead>
           <tbody>${data.sessions.map(rowHtml).join('')}</tbody></table>`
        : '<div class="empty">暂无送审单，点击右上角「发起送审」开始</div>';
      container.querySelector('[data-board-meta]') && (container.querySelector('[data-board-meta]').textContent =
        `双通道核对时限 ${data.timeout_seconds} 秒 · ${data.sessions.length} 条送审单 · 每 4 秒自动刷新`);
      for (const id of state.expanded) loadDetail(id);
    } catch (e) {
      if (!String(e.message).includes('登录')) toast(e.message, true);
    }
  }

  async function loadDetail(id) {
    const slot = document.querySelector(`[data-detail-slot="${id}"]`);
    if (!slot) return;
    try {
      slot.innerHTML = detailHtml(await api(`/api/review-sessions/${id}`));
    } catch (e) {
      slot.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    }
  }

  function modal(title, bodyHtml) {
    const wrap = document.createElement('div');
    wrap.className = 'modal-backdrop';
    wrap.innerHTML = `<div class="modal"><div class="modal-head"><div><h2>${esc(title)}</h2><p class="muted">提交后会写入审计轨迹</p></div><button class="close">×</button></div>${bodyHtml}</div>`;
    document.body.appendChild(wrap);
    wrap.querySelector('.close').onclick = () => wrap.remove();
    return wrap;
  }

  async function openSubmitModal() {
    if (!state.lifts.length) state.lifts = await api('/api/lifts');
    const idemKey = (crypto.randomUUID ? crypto.randomUUID() : `key-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    const wrap = modal('发起送审', `<form><p class="muted">接单处收下送审数据后立即计算本处通道，后台通道由 worker 异步计算；两边对上才自动出结论。</p>
      <label>送审设备<select name="lift_id">${state.lifts.map(l => `<option value="${l.id}">${esc(l.asset_code)} · ${esc(l.building)}</option>`).join('')}</select></label>
      <p class="muted">幂等键：<code>${esc(idemKey)}</code>（提交中断后可安全重试，不会重复建单）</p>
      <p class="feedback muted" data-feedback></p>
      <div class="modal-actions"><button type="button" class="secondary" data-cancel>取消</button><button class="primary" type="submit">提交送审</button></div></form>`);
    wrap.querySelector('[data-cancel]').onclick = () => wrap.remove();
    wrap.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const feedback = wrap.querySelector('[data-feedback]');
      feedback.textContent = '正在提交…';
      try {
        const view = await api('/api/review-sessions', { method: 'POST', body: JSON.stringify({ lift_id: Number(e.target.lift_id.value), idempotency_key: idemKey }) });
        wrap.remove();
        toast(view.replayed ? '该送审单已存在（幂等重放）' : '送审单已创建，等待双通道结果');
        refresh(document.getElementById('page-content'));
      } catch (err) {
        feedback.textContent = `${err.message}（可直接重试，幂等键不变）`;
      }
    };
  }

  function openConcludeModal(id) {
    const wrap = modal('填写人工结论', `<form><label>复核结论<select name="conclusion">
        <option value="ready">ready · 就绪</option><option value="review">review · 复核</option><option value="hold">hold · 暂缓</option></select></label>
      <label>复核依据<textarea name="note" required minlength="4" maxlength="500">双通道结果已核对，结合现场整改资料人工判定。</textarea></label>
      <p class="feedback muted" data-feedback></p>
      <div class="modal-actions"><button type="button" class="secondary" data-cancel>取消</button><button class="primary" type="submit">提交结论</button></div></form>`);
    wrap.querySelector('[data-cancel]').onclick = () => wrap.remove();
    wrap.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const feedback = wrap.querySelector('[data-feedback]');
      try {
        await api(`/api/review-sessions/${id}/conclude`, { method: 'POST', body: JSON.stringify({ conclusion: e.target.conclusion.value, note: e.target.note.value }) });
        wrap.remove();
        toast('人工结论已落定');
        refresh(document.getElementById('page-content'));
      } catch (err) {
        feedback.textContent = err.message;
      }
    };
  }

  window.stopReviewBoard = () => {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  };

  window.renderReviewBoard = async function (container) {
    window.stopReviewBoard();
    const canSubmit = ['operator', 'supervisor'].includes(me().role);
    container.innerHTML = `<div data-board-root><div class="section-head"><div><h2>双通道分流复核台</h2>
        <small data-board-meta>接单处与后台各算一遍，对上才自动出结论</small></div>
      <div class="toolbar">${canSubmit ? '<button class="primary" data-review-submit>+ 发起送审</button>' : ''}
        <button class="small-btn" data-review-refresh>刷新</button></div></div>
      <div class="panel"><div data-board-body><div class="empty">正在加载…</div></div></div></div>`;
    const root = container.querySelector('[data-board-root]');
    root.onclick = async e => {
      const detailBtn = e.target.closest('[data-review-detail]');
      const claimBtn = e.target.closest('[data-review-claim]');
      const concludeBtn = e.target.closest('[data-review-conclude]');
      if (e.target.closest('[data-review-submit]')) { openSubmitModal().catch(err => toast(err.message, true)); return; }
      if (e.target.closest('[data-review-refresh]')) { refresh(container); return; }
      if (detailBtn) {
        const id = Number(detailBtn.dataset.reviewDetail);
        state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
        refresh(container);
        return;
      }
      if (claimBtn) {
        try {
          await api(`/api/review-sessions/${claimBtn.dataset.reviewClaim}/claim`, { method: 'POST' });
          toast('已接手该复核单');
        } catch (err) { toast(err.message, true); }
        refresh(container);
        return;
      }
      if (concludeBtn) openConcludeModal(Number(concludeBtn.dataset.reviewConclude));
    };
    await refresh(container);
    state.timer = setInterval(() => {
      if (!document.contains(container) || !container.querySelector('[data-board-root]')) { window.stopReviewBoard(); return; }
      if (document.querySelector('.modal-backdrop')) return; // 弹窗输入时暂停自动刷新
      refresh(container);
    }, 4000);
  };
})();
