/**
 * 会话批量删除注入脚本 v3
 *
 * 修复：
 * 1. 按钮放到「新建」左边（insertBefore）
 * 2. checkbox 用独立 overlay 层覆盖在按钮上方，不被 Vue 重渲染覆盖
 * 3. 间隔选择只弹「选择到此」，点其他继续选则默认选中
 */
;(function () {
  'use strict';

  let selectMode = false;
  const selected = new Set();
  let sessionMap = [];        // [{sid, title, btn, rect}] 匹配结果
  let rangePopup = null;
  let toolbar = null;
  let batchBtn = null;
  let overlayContainer = null;
  let injected = false;

  // ---- 样式 ----
  const style = document.createElement('style');
  style.textContent = `
    #bd-overlay {
      position: fixed; top: 0; left: 0; width: 100%; height: 100%;
      pointer-events: none; z-index: 9990;
    }
    #bd-overlay .bd-cb {
      position: absolute; pointer-events: auto;
      width: 18px; height: 18px; cursor: pointer;
      accent-color: #5b8def;
    }
    .bd-toolbar {
      display: flex; align-items: center; gap: 8px;
      padding: 6px 10px; border-bottom: 1px solid #333;
      background: #1a1a1a;
    }
    .bd-toolbar .bd-count { font-size: 11px; color: #999; flex: 1; }
    .bd-btn {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 4px 10px; border-radius: 6px;
      font-size: 11px; font-weight: 500; cursor: pointer;
      border: 1px solid transparent; transition: all .15s;
    }
    .bd-btn-ghost { background: transparent; color: #999; border-color: #444; }
    .bd-btn-ghost:hover { background: #2a2a2a; color: #ccc; }
    .bd-btn-danger { background: #ef444420; color: #ef4444; border-color: #ef444440; }
    .bd-btn-danger:hover { background: #ef444435; }
    .bd-btn-danger:disabled { opacity: .4; cursor: not-allowed; }
    .bd-popup {
      position: fixed; z-index: 99999;
      background: #1e1e1e; border: 1px solid #444;
      border-radius: 8px; padding: 4px;
      box-shadow: 0 8px 24px rgba(0,0,0,.5); min-width: 180px;
    }
    .bd-popup .bd-popup-item {
      display: flex; align-items: center; gap: 8px;
      padding: 7px 12px; border-radius: 6px;
      font-size: 12px; color: #eee; cursor: pointer; transition: background .1s;
    }
    .bd-popup .bd-popup-item:hover { background: #2a2a2a; }
    .bd-popup .bd-popup-hint {
      padding: 4px 12px 6px; font-size: 10px; color: #666;
      border-bottom: 1px solid #333; margin-bottom: 2px;
    }
  `;
  document.head.appendChild(style);

  // ---- 定位 sidebar ----
  function findSidebar() {
    for (const el of document.querySelectorAll('header, [class*="border-b"]')) {
      if (el.textContent.includes('会话') && el.querySelector('button')) {
        return el.parentElement;
      }
    }
    return document.querySelector('aside') || null;
  }

  function findHeader() {
    const sidebar = findSidebar();
    if (!sidebar) return null;
    for (const h of sidebar.querySelectorAll('header, [class*="border-b"]')) {
      if (h.textContent.includes('会话')) return h;
    }
    return null;
  }

  function findSessionList() {
    const sidebar = findSidebar();
    if (!sidebar) return null;
    for (const c of sidebar.querySelectorAll('[class*="overflow"]')) {
      if (c.children.length >= 1) return c;
    }
    for (const d of sidebar.querySelectorAll(':scope > div')) {
      if (d.children.length >= 2) return d;
    }
    return null;
  }

  // ---- 获取 session 列表 ----
  async function fetchSessions() {
    try {
      const r = await fetch('/api/sessions');
      const d = await r.json();
      return (d.sessions || []).sort(
        (a, b) => (b.updated_at || '').localeCompare(a.updated_at || '')
      );
    } catch (e) { return []; }
  }

  // ---- 匹配 DOM ----
  function matchToDOM(apiSessions) {
    sessionMap = [];
    const list = findSessionList();
    if (!list) return;
    const btns = Array.from(list.querySelectorAll(':scope > button, :scope > * > button'));
    // 过滤出有时间戳的 session 按钮
    const sessionBtns = btns.filter(b =>
      b.querySelector('[class*="text-fg-faint"]') || b.querySelector('[class*="10px"]')
    );
    const n = Math.min(apiSessions.length, sessionBtns.length);
    for (let i = 0; i < n; i++) {
      const btn = sessionBtns[i];
      const rect = btn.getBoundingClientRect();
      sessionMap.push({
        sid: apiSessions[i].session_id,
        title: apiSessions[i].title || apiSessions[i].session_id.slice(0, 8),
        btn,
        rect,
      });
    }
  }

  // ---- overlay checkbox 层 ----
  function ensureOverlay() {
    if (overlayContainer && document.contains(overlayContainer)) return;
    overlayContainer = document.createElement('div');
    overlayContainer.id = 'bd-overlay';
    document.body.appendChild(overlayContainer);
  }

  function renderOverlayCheckboxes() {
    if (!overlayContainer) return;
    overlayContainer.innerHTML = '';
    for (const item of sessionMap) {
      const rect = item.btn.getBoundingClientRect();
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'bd-cb';
      cb.checked = selected.has(item.sid);

      // 找到会话标题元素，将勾选框放在标题前面
      const titleEl = item.btn.querySelector('[class*="text-[12px]"], [class*="truncate"]');
      if (titleEl) {
        const titleRect = titleEl.getBoundingClientRect();
        // 勾选框放在标题左侧，留出8px间距
        cb.style.left = (titleRect.left - 26) + 'px';
        cb.style.top = (rect.top + (rect.height - 18) / 2) + 'px';
      } else {
        // 兜底：放在按钮左侧外面
        cb.style.left = (rect.left - 22) + 'px';
        cb.style.top = (rect.top + (rect.height - 18) / 2) + 'px';
      }

      cb.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        handleClick(item, e);
      });
      overlayContainer.appendChild(cb);
    }
  }

  function refreshOverlay() {
    if (!selectMode) return;
    // 更新位置（Vue 可能重排了）
    for (let i = 0; i < sessionMap.length; i++) {
      const item = sessionMap[i];
      const rect = item.btn.getBoundingClientRect();
      item.rect = rect;
      const cb = overlayContainer.children[i];
      if (cb) {
        cb.checked = selected.has(item.sid);
        cb.style.left = (rect.left - 22) + 'px';
        cb.style.top = (rect.top + (rect.height - 18) / 2) + 'px';
      }
    }
  }

  function hideOverlay() {
    if (overlayContainer) overlayContainer.innerHTML = '';
  }

  // ---- 点击逻辑 ----
  function handleClick(item, e) {
    const idx = sessionMap.indexOf(item);

    // 已选中 → 取消
    if (selected.has(item.sid)) {
      selected.delete(item.sid);
      renderOverlayCheckboxes();
      updateToolbar();
      hidePopup();
      return;
    }

    // 第一个选中
    if (selected.size === 0) {
      selected.add(item.sid);
      renderOverlayCheckboxes();
      updateToolbar();
      return;
    }

    // 已有选中，检查是否连续
    const indices = [...selected].map(sid => sessionMap.findIndex(m => m.sid === sid)).filter(i => i >= 0).sort((a, b) => a - b);
    const firstIdx = indices[0];
    const lastIdx = indices[indices.length - 1];

    // 连续 → 直接选
    if (idx === firstIdx - 1 || idx === lastIdx + 1) {
      selected.add(item.sid);
      renderOverlayCheckboxes();
      updateToolbar();
      return;
    }

    // 不连续 → 弹出浮层（只弹「选择到此」+ 提示）
    showPopup(item, idx, firstIdx, e);
  }

  // ---- 浮层 ----
  function showPopup(item, idx, firstIdx, e) {
    hidePopup();
    rangePopup = document.createElement('div');
    rangePopup.className = 'bd-popup';

    const skipped = Math.abs(idx - firstIdx) - 1;
    const hint = document.createElement('div');
    hint.className = 'bd-popup-hint';
    const fromTitle = sessionMap[firstIdx]?.title?.slice(0, 12) || '?';
    hint.textContent = `已选「${fromTitle}」，间隔 ${skipped} 个会话`;
    rangePopup.appendChild(hint);

    // 选择到此对话
    const rangeCount = Math.abs(idx - firstIdx) + 1;
    const itemRange = document.createElement('div');
    itemRange.className = 'bd-popup-item';
    itemRange.textContent = `选择到此对话（共 ${rangeCount} 个）`;
    itemRange.addEventListener('click', () => {
      const from = Math.min(firstIdx, idx);
      const to = Math.max(firstIdx, idx);
      for (let i = from; i <= to; i++) {
        selected.add(sessionMap[i].sid);
      }
      renderOverlayCheckboxes();
      updateToolbar();
      hidePopup();
    });
    rangePopup.appendChild(itemRange);

    document.body.appendChild(rangePopup);

    // 定位
    const rect = item.btn.getBoundingClientRect();
    rangePopup.style.top = rect.top + 'px';
    rangePopup.style.left = (rect.right + 8) + 'px';
    requestAnimationFrame(() => {
      const pr = rangePopup.getBoundingClientRect();
      if (pr.right > window.innerWidth - 8) {
        rangePopup.style.left = (rect.left - pr.width - 8) + 'px';
      }
      if (pr.bottom > window.innerHeight - 8) {
        rangePopup.style.top = (window.innerHeight - pr.height - 8) + 'px';
      }
    });

    // 点击外部 → 关闭浮层，但默认选中当前项
    setTimeout(() => {
      document.addEventListener('click', function onClose(ev) {
        if (rangePopup && !rangePopup.contains(ev.target)) {
          hidePopup();
          // 默认选中当前点击的这个
          selected.add(item.sid);
          renderOverlayCheckboxes();
          updateToolbar();
        }
        document.removeEventListener('click', onClose);
      }, { once: true });
    }, 50);
  }

  function hidePopup() {
    if (rangePopup) { rangePopup.remove(); rangePopup = null; }
  }

  // ---- 工具栏 ----
  function ensureToolbar() {
    if (toolbar && document.contains(toolbar)) return;
    toolbar = document.createElement('div');
    toolbar.className = 'bd-toolbar';
    toolbar.style.display = 'none';
    toolbar.innerHTML = `
      <span class="bd-count">已选 <strong class="bd-num">0</strong> 个会话</span>
      <button class="bd-btn bd-btn-ghost bd-cancel">取消</button>
      <button class="bd-btn bd-btn-danger bd-del" disabled>删除选中</button>
    `;
    toolbar.querySelector('.bd-cancel').addEventListener('click', exitSelectMode);
    toolbar.querySelector('.bd-del').addEventListener('click', doBatchDelete);

    const sidebar = findSidebar();
    if (sidebar) {
      const header = findHeader();
      if (header) {
        header.parentNode.insertBefore(toolbar, header.nextSibling);
      } else {
        sidebar.prepend(toolbar);
      }
    }
  }

  function updateToolbar() {
    if (!toolbar) return;
    toolbar.querySelector('.bd-num').textContent = selected.size;
    toolbar.querySelector('.bd-del').disabled = selected.size === 0;
  }

  // ---- 批量删除 ----
  async function doBatchDelete() {
    if (selected.size === 0) return;
    const ids = [...selected];
    if (!window.confirm(ids.length === 1 ? '确认删除 1 个会话？' : `确认删除 ${ids.length} 个会话？\n该操作不可恢复。`)) return;

    try {
      const r = await fetch('/api/sessions/batch-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: ids }),
      });
      const d = await r.json();
      if (d.ok) {
        showToast(`已删除 ${d.deleted} 个会话`, 'success');
        // 直接从 DOM 中移除已删除的 session 按钮（立即生效）
        const deletedSet = new Set(ids);
        for (const item of sessionMap) {
          if (deletedSet.has(item.sid) && item.btn && item.btn.parentNode) {
            item.btn.remove();
          }
        }
        // 同步 Pinia store（如果能访问到）
        try {
          const pinia = window.__pinia;
          if (pinia) {
            const store = pinia._s.get('session');
            if (store?.list) {
              store.list = store.list.filter(s => !deletedSet.has(s.session_id));
              if (store.total !== undefined) store.total = store.list.length;
            }
          }
        } catch (_) {}
      } else {
        showToast('删除失败: ' + (d.error || '未知错误'), 'danger');
      }
    } catch (err) {
      showToast('请求失败: ' + err.message, 'danger');
    }
    selected.clear();
    exitSelectMode();
  }

  // ---- Toast ----
  function showToast(msg, tone) {
    const t = document.createElement('div');
    t.textContent = msg;
    Object.assign(t.style, {
      position: 'fixed', bottom: '20px', left: '50%', transform: 'translateX(-50%)',
      padding: '8px 20px', borderRadius: '8px', fontSize: '13px', zIndex: '99999',
      background: tone === 'success' ? '#22c55e20' : '#ef444420',
      color: tone === 'success' ? '#22c55e' : '#ef4444',
      border: '1px solid ' + (tone === 'success' ? '#22c55e40' : '#ef444440'),
      transition: 'opacity .3s', opacity: '1',
    });
    document.body.appendChild(t);
    setTimeout(() => t.style.opacity = '0', 2500);
    setTimeout(() => t.remove(), 3000);
  }

  // ---- 模式切换 ----
  let _selectModeStyle = null;

  function _injectSelectModeCSS() {
    if (_selectModeStyle) return;
    _selectModeStyle = document.createElement('style');
    _selectModeStyle.id = 'bd-select-mode-css';
    _selectModeStyle.textContent = `
      /* 选择模式：session 按钮整体右移，为左侧 checkbox 腾出空间 */
      .bd-selecting-session button {
        margin-left: 26px !important;
        transition: margin-left 0.15s ease;
      }
    `;
    document.head.appendChild(_selectModeStyle);
  }

  function _removeSelectModeCSS() {
    if (_selectModeStyle) {
      _selectModeStyle.remove();
      _selectModeStyle = null;
    }
  }

  async function enterSelectMode() {
    selectMode = true;
    selected.clear();
    const sessions = await fetchSessions();
    matchToDOM(sessions);

    // 给列表容器加 class，触发全局 CSS 把按钮右移
    const list = findSessionList();
    if (list) list.classList.add('bd-selecting-session');

    _injectSelectModeCSS();
    ensureOverlay();
    renderOverlayCheckboxes();
    ensureToolbar();
    toolbar.style.display = 'flex';
    updateToolbar();

    // 隐藏原有删除按钮
    for (const item of sessionMap) {
      const trash = item.btn.querySelector('[title*="删除"]');
      if (trash) trash.style.display = 'none';
    }
    if (batchBtn) batchBtn.textContent = '退出选择';

    // 定时刷新 overlay 位置（Vue 重渲染会移动按钮位置）
    if (!window._bdRefreshTimer) {
      window._bdRefreshTimer = setInterval(() => {
        if (selectMode) refreshOverlay();
        else { clearInterval(window._bdRefreshTimer); window._bdRefreshTimer = null; }
      }, 300);
    }
  }

  function exitSelectMode() {
    selectMode = false;
    selected.clear();
    hidePopup();
    hideOverlay();
    if (toolbar) toolbar.style.display = 'none';

    // 移除 class + CSS
    const list = findSessionList();
    if (list) list.classList.remove('bd-selecting-session');
    _removeSelectModeCSS();

    for (const item of sessionMap) {
      const trash = item.btn.querySelector('[title*="删除"]');
      if (trash) trash.style.display = '';
    }
    sessionMap = [];
    if (batchBtn) batchBtn.textContent = '批量管理';
  }

  // ---- 注入按钮（放在「新建」左边） ----
  function injectBatchBtn() {
    if (injected && document.contains(batchBtn)) return;
    const header = findHeader();
    if (!header) return;
    if (header.querySelector('.bd-batch-toggle')) { injected = true; return; }

    batchBtn = document.createElement('button');
    batchBtn.className = 'bd-btn bd-btn-ghost bd-batch-toggle';
    batchBtn.textContent = '批量管理';
    batchBtn.style.cssText = 'font-size:11px;';
    batchBtn.addEventListener('click', () => {
      if (selectMode) exitSelectMode();
      else enterSelectMode();
    });

    // 找到「新建」按钮，把「批量管理」插到它左边
    const headerBtns = header.querySelectorAll('button');
    const newBtn = headerBtns[headerBtns.length - 1]; // 最后一个按钮通常是「新建」
    if (newBtn) {
      newBtn.parentNode.insertBefore(batchBtn, newBtn);
    } else {
      header.appendChild(batchBtn);
    }
    injected = true;
  }

  // ---- 轮询检测 ----
  setInterval(() => {
    if (!injected || !document.contains(batchBtn)) {
      injected = false;
      injectBatchBtn();
    }
  }, 1000);

  // ---- 首次启动 ----
  function init() {
    const check = () => {
      if (findSidebar()) injectBatchBtn();
      else setTimeout(check, 500);
    };
    check();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
