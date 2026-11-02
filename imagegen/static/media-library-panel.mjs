// Media library & smart insert proposal UI panel for FrameForge Montage

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function button(text, onClick, className = 'montage-btn') {
  const b = node('button', text, className);
  b.type = 'button';
  if (onClick) b.onclick = onClick;
  return b;
}

export function mountMediaLibraryButton(toolbar, { projectId, api: appApi, notice, beforeOpen }) {
  const api = (path, options) => {
    const route = path.replace(/^\/api/, '');
    if (options?.method === 'DELETE') return appApi(route + '/delete', {});
    return appApi(route, options?.method === 'POST' ? (options.body || {}) : undefined);
  };
  const open = async fn => { try { await beforeOpen?.(); await fn(); } catch(e) { notice(e.message, true); } };
  const btn = button('📁 Материалы для вставок', () => open(() => openMediaLibraryModal({ projectId, api, notice })), 'montage-btn montage-media-lib-btn');
  const matchBtn = button('✨ Подобрать вставки', () => open(() => openMediaMatchModal({ projectId, api, notice })), 'montage-btn montage-btn-accent');
  const stockBtn = button('🔍 Pixabay', () => open(() => openStockSearchModal({ projectId, api, notice })), 'montage-btn');
  toolbar.append(btn, stockBtn, matchBtn);
  return { libBtn: btn, stockBtn, matchBtn };
}

export async function openMediaLibraryModal({ projectId, api, notice }) {
  const existing = document.querySelector('.media-library-overlay');
  if (existing) existing.remove();

  const overlay = node('div', null, 'media-library-overlay');
  const modal = node('div', null, 'media-library-modal');
  overlay.append(modal);

  // Header
  const header = node('div', null, 'media-library-header');
  const title = node('h3', 'Материалы для вставок (фото и видео)', 'media-library-title');
  const closeBtn = button('✕', () => overlay.remove(), 'media-library-close-btn');
  header.append(title, closeBtn);

  // Toolbar
  const tools = node('div', null, 'media-library-toolbar');
  const fileInput = node('input');
  fileInput.type = 'file';
  fileInput.multiple = true;
  fileInput.accept = '.png,.jpg,.jpeg,.webp,.bmp,.mp4,.mov,.m4v,.mkv,.webm';
  fileInput.style.display = 'none';

  const uploadBtn = button('+ Загрузить файлы', () => fileInput.click(), 'montage-btn montage-btn-primary');
  const analyzeBtn = button('⚡ Анализировать материалы (Qwen3-VL)', () => runAnalysis(), 'montage-btn montage-btn-accent');
  const openMatchBtn = button('✨ Перейти к подбору вставок', () => {
    overlay.remove();
    openMediaMatchModal({ projectId, api, notice });
  }, 'montage-btn');

  const statusSpan = node('span', '', 'media-library-status');
  const stockBtn = button('🔍 Найти на Pixabay', () => openStockSearchModal({ projectId, api, notice, onItemImported: () => loadItems() }), 'montage-btn montage-btn-accent');
  tools.append(uploadBtn, fileInput, stockBtn, analyzeBtn, openMatchBtn, statusSpan);

  // The server reads local files itself; the browser only sends the folder path.
  const folderBox = node('section', null, 'media-folder-box');
  const folderLabel = node('label', 'Папка с футажами на этом компьютере');
  const folderInput = node('input');folderInput.type='text';folderInput.placeholder='C:\\Путь\\к\\футажам';
  folderInput.setAttribute('aria-label','Путь к папке с футажами');
  const pathKey='frameforge-media-folder:'+projectId;
  try { folderInput.value=localStorage.getItem(pathKey)||''; } catch {}
  const folderStart=button('▶ Подобрать из папки',startFolder,'montage-btn montage-btn-accent');
  const row=node('div',null,'media-folder-row');row.append(folderInput,folderStart);
  const autoLabel=node('label'),autoApply=node('input');autoApply.type='checkbox';autoApply.checked=true;
  autoLabel.append(autoApply,document.createTextNode(' Сразу вставить подходящие в монтаж (можно откатить)'));
  const folderStatus=node('div','','media-folder-status');folderStatus.setAttribute('role','status');
  const folderErrors=node('div','','media-folder-errors');
  folderLabel.append(row);folderBox.append(folderLabel,autoLabel,folderStatus,folderErrors);
  let folderActive=false,folderWasActive=false;
  function folderBusy(value) {
    folderActive=value;folderStart.disabled=value;folderInput.disabled=value;autoApply.disabled=value;
    uploadBtn.disabled=value;analyzeBtn.disabled=value;
  }
  async function startFolder() {
    if (!folderInput.value.trim()) {folderInput.focus();notice('Укажите путь к папке.',true);return;}
    folderBusy(true);
    try {
      try {localStorage.setItem(pathKey,folderInput.value.trim());}catch{}
      await api(`/api/projects/${projectId}/edit/media-folder`,{method:'POST',body:{folder_path:folderInput.value.trim(),recursive:true,auto_apply:autoApply.checked}});
      folderWasActive=true;pollFolder();
    }catch(e){folderBusy(false);folderStatus.textContent=e.message;notice(e.message,true);}
  }
  async function pollFolder() {
    if(!overlay.isConnected)return;
    try {
      const job=await api(`/api/projects/${projectId}/edit/media-folder`);
      if(job.state==='idle')return;
      if(!folderInput.value)folderInput.value=job.folder_path||'';
      const active=['queued','running'].includes(job.state);folderBusy(active);
      folderStatus.textContent=`${job.message||'Подготовка…'}${active?' · '+Math.round((job.progress||0)*100)+'%':''}`;
      folderErrors.textContent=(job.errors||[]).map(e=>`${e.file}: ${e.error}`).join('\n');
      if(active){folderWasActive=true;setTimeout(pollFolder,1500);}
      else {
        await loadItems();
        if(folderWasActive && job.applied_count){window.location.reload();return;}
        folderWasActive=false;
      }
    }catch(e){folderStatus.textContent='Не удалось получить состояние: '+e.message;if(folderActive)setTimeout(pollFolder,3000);}
  }

  // Progress box
  const progressBox = node('div', null, 'media-library-progress-box');
  progressBox.style.display = 'none';
  const progressBar = node('div', null, 'media-library-progress-bar');
  const progressText = node('span', '0%', 'media-library-progress-text');
  progressBox.append(progressBar, progressText);

  // Content grid
  const grid = node('div', null, 'media-library-grid');

  modal.append(header, folderBox, tools, progressBox, grid);
  document.body.append(overlay);

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
  const escHandler = (e) => {
    if (e.key === 'Escape') {
      overlay.remove();
      window.removeEventListener('keydown', escHandler);
    }
  };
  window.addEventListener('keydown', escHandler);

  let pollingTimer = null;

  async function loadItems() {
    grid.innerHTML = '';
    try {
      const items = await api(`/api/projects/${projectId}/edit/media-library`);
      if (!items || !items.length) {
        grid.innerHTML = '<div class="media-library-empty">Материалы пока не загружены. Добавьте фото или видеоролики для автоматических вставок в монтаж.</div>';
        return;
      }

      for (const item of items) {
        const card = node('div', null, 'media-library-card');
        
        // Thumbnail
        const thumbBox = node('div', null, 'media-card-thumb-box');
        const img = node('img');
        img.className = 'media-card-thumb';
        img.src = item.thumbnail_path ? `/api/media-file/${item.thumbnail_path}` : '';
        img.alt = item.original_name;
        
        const typeBadge = node('span', item.media_type === 'image' ? 'ФОТО' : `ВИДЕО · ${(item.duration_ms / 1000).toFixed(1)}с`, 'media-card-badge');
        thumbBox.append(img, typeBadge);

        // Info
        const info = node('div', null, 'media-card-info');
        const nameEl = node('div', item.original_name, 'media-card-name');
        nameEl.title = item.original_name;

        const segments = item.segments || [];
        const completedCount = segments.filter(s => s.analysis_status === 'completed').length;
        const statusClass = completedCount === segments.length ? 'status-ok' : (segments.some(s => s.analysis_status === 'failed') ? 'status-err' : 'status-pending');
        const statusText = completedCount === segments.length ? `✓ Анализ готов (${segments.length} пл.)` : `⏳ Ожидает анализ (${completedCount}/${segments.length})`;
        
        const statusEl = node('div', statusText, `media-card-status ${statusClass}`);

        const firstAnalyzed = segments.find(s => s.analysis_summary);
        const descEl = node('div', firstAnalyzed ? firstAnalyzed.analysis_summary : 'Анализ ещё не запущен.', 'media-card-desc');
        descEl.title = descEl.textContent;

        const delBtn = button('🗑', async (e) => {
          e.stopPropagation();
          if (confirm(`Удалить ${item.original_name}?`)) {
            await api(`/api/projects/${projectId}/edit/media-library/${item.id}`, { method: 'DELETE' });
            notice('Файл удалён из библиотеки.');
            loadItems();
          }
        }, 'media-card-del-btn');

        info.append(nameEl, statusEl, descEl);
        card.append(thumbBox, info, delBtn);
        grid.append(card);
      }
    } catch (e) {
      notice(e.message, true);
    }
  }

  // File upload
  fileInput.onchange = async () => {
    const files = [...fileInput.files];
    if (!files.length) return;
    uploadBtn.disabled = true;analyzeBtn.disabled=true;folderStart.disabled=true;
    statusSpan.textContent = `Загрузка 0/${files.length}...`;

    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        statusSpan.textContent = `Загрузка ${i + 1}/${files.length}: ${file.name}`;
        
        if (file.size > 512000000) throw Error(`${file.name}: файл должен быть не больше 512 МБ.`);
        await api(`/api/projects/${projectId}/edit/media-library/upload`, {method:'POST', body:file});
      }
      notice(`Загружено файлов: ${files.length}`);
      await loadItems();
    } catch (e) {
      notice(e.message, true);
    } finally {
      uploadBtn.disabled = folderActive;analyzeBtn.disabled=folderActive;folderStart.disabled=folderActive;
      fileInput.value = '';
      statusSpan.textContent = '';
    }
  };

  async function runAnalysis() {
    analyzeBtn.disabled = true;
    progressBox.style.display = 'flex';
    progressBar.style.width = '0%';
    progressText.textContent = 'Запуск Qwen3-VL...';

    try {
      await api(`/api/projects/${projectId}/edit/media-analysis`, { method: 'POST' });
      notice('Запущен визуальный анализ медиатеки.');
      pollAnalysis();
    } catch (e) {
      notice(e.message, true);
      analyzeBtn.disabled = false;
      progressBox.style.display = 'none';
    }
  }

  async function pollAnalysis() {
    if (!overlay.isConnected) return;
    try {
      const job = await api(`/api/projects/${projectId}/edit/media-analysis`);
      if (!job || job.state === 'idle') {
        progressBox.style.display = 'none';
        analyzeBtn.disabled = false;
        return;
      }

      if (job.state === 'running' || job.state === 'queued') {
        progressBox.style.display = 'flex';
        const pct = Math.round((job.progress || 0) * 100);
        progressBar.style.width = `${pct}%`;
        progressText.textContent = `${job.current_step || 'Обработка...'} (${pct}%)`;
        pollingTimer = setTimeout(pollAnalysis, 1500);
      } else {
        progressBox.style.display = 'none';
        analyzeBtn.disabled = false;
        if (job.state === 'completed' && !job.error) {
          notice('Анализ медиатеки успешно завершён!');
        } else if (job.state === 'failed' || job.error) {
          notice(`Ошибка анализа: ${job.error || 'неизвестная ошибка'}`, true);
        }
        await loadItems();
      }
    } catch (e) {
      pollingTimer = setTimeout(pollAnalysis, 3000);
    }
  }

  await loadItems();
  await pollFolder();
  const currentJob = await api(`/api/projects/${projectId}/edit/media-analysis`).catch(() => null);
  if (!folderActive && currentJob && (currentJob.state === 'running' || currentJob.state === 'queued')) {
    pollAnalysis();
  }
}

export async function openMediaMatchModal({ projectId, api, notice }) {
  const existing = document.querySelector('.media-match-overlay');
  if (existing) existing.remove();

  const overlay = node('div', null, 'media-library-overlay media-match-overlay');
  const modal = node('div', null, 'media-library-modal');
  overlay.append(modal);

  const header = node('div', null, 'media-library-header');
  const title = node('h3', 'Подбор футажей на целые сегменты озвучки', 'media-library-title');
  const closeBtn = button('✕', () => overlay.remove(), 'media-library-close-btn');
  header.append(title, closeBtn);

  const tools = node('div', null, 'media-library-toolbar');
  const generateBtn = button('🔄 Рассчитать предложения вставок', () => generateProposals(), 'montage-btn montage-btn-accent');
  const applyBtn = button('✓ Применить выбранные вставки', () => applySelected(), 'montage-btn montage-btn-primary');
  const rollbackBtn = button('↶ Откатить применённый монтаж', () => rollback(), 'montage-btn');
  applyBtn.style.display = 'none';

  tools.append(generateBtn, applyBtn, rollbackBtn);

  const list = node('div', null, 'media-match-list');
  list.style.flex = '1';
  list.style.overflowY = 'auto';
  list.style.padding = '20px';

  modal.append(header, tools, list);
  document.body.append(overlay);

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
  const escHandler = (e) => {
    if (e.key === 'Escape') {
      overlay.remove();
      window.removeEventListener('keydown', escHandler);
    }
  };
  window.addEventListener('keydown', escHandler);

  let currentProposalId = null;
  const selectedProposalIds = new Set();

  function renderProposals(proposals) {
    list.innerHTML = '';
    selectedProposalIds.clear();

    if (!proposals || !proposals.length) {
      list.innerHTML = '<div class="media-library-empty">Нет предложений вставок. Убедитесь, что материалы загружены и проанализированы через Qwen3-VL.</div>';
      applyBtn.style.display = 'none';
      return;
    }

    applyBtn.style.display = 'inline-block';

    for (const p of proposals) {
      if (p.match_status === 'matched') selectedProposalIds.add(p.proposal_id);

      const item = node('div', null, 'media-match-item');
      item.style.display = 'flex';
      item.style.gap = '16px';
      item.style.padding = '14px';
      item.style.background = '#1a1e2b';
      item.style.border = '1px solid #272f44';
      item.style.borderRadius = '8px';
      item.style.marginBottom = '12px';

      // Checkbox
      const chk = node('input');
      chk.type = 'checkbox';
      chk.checked = p.match_status === 'matched';
      chk.onchange = () => {
        if (chk.checked) selectedProposalIds.add(p.proposal_id);
        else selectedProposalIds.delete(p.proposal_id);
      };

      // Thumbnail
      const isVideo = p.candidate_segment?.media_type === 'video';
      const thumb = node(isVideo ? 'video' : 'img');
      if (isVideo) { thumb.controls=true; thumb.muted=true; thumb.preload='metadata'; }
      thumb.className = 'media-card-thumb';
      thumb.style.width = '100px';
      thumb.style.height = '64px';
      thumb.style.objectFit = 'cover';
      thumb.style.borderRadius = '4px';
      thumb.src = isVideo ? `/api/media-file/${p.candidate_segment.file_path}#t=${(p.candidate_segment.seg_start_ms||0)/1000}` : (p.candidate_segment?.thumbnail_path ? `/api/media-file/${p.candidate_segment.thumbnail_path}` : '');

      // Info
      const body = node('div');
      body.style.flex = '1';

      const statusBadge = node('span', p.status_label || (p.match_status === 'matched' ? 'подходит' : 'нужна проверка'), 'media-match-badge');
      statusBadge.style.display = 'inline-block';
      statusBadge.style.padding = '2px 8px';
      statusBadge.style.borderRadius = '4px';
      statusBadge.style.fontSize = '0.75rem';
      statusBadge.style.fontWeight = '600';
      statusBadge.style.marginRight = '8px';
      if (p.match_status === 'matched') {
        statusBadge.style.background = 'rgba(16, 185, 129, 0.2)';
        statusBadge.style.color = '#34d399';
        statusBadge.style.border = '1px solid rgba(16, 185, 129, 0.4)';
      } else {
        statusBadge.style.background = 'rgba(245, 158, 11, 0.2)';
        statusBadge.style.color = '#fbbf24';
        statusBadge.style.border = '1px solid rgba(245, 158, 11, 0.4)';
      }

      const phrase = node('span', `«${p.phrase_quote}»`, 'media-match-phrase');
      phrase.style.fontWeight = '600';
      phrase.style.color = '#e2e8f0';

      const phraseLine = node('div');
      phraseLine.style.display = 'flex';
      phraseLine.style.alignItems = 'center';
      phraseLine.style.marginBottom = '6px';
      phraseLine.append(statusBadge, phrase);

      const reason = node('div', p.reason, 'media-match-reason');
      reason.style.fontSize = '0.85rem';
      reason.style.color = '#a5b4fc';
      reason.style.marginBottom = '4px';

      const timing = node('div', `Целый сегмент: ${(p.insert_start_ms / 1000).toFixed(2)}с – ${(p.insert_end_ms / 1000).toFixed(2)}с (${(p.insert_duration_ms / 1000).toFixed(1)}с)`, 'media-match-timing');
      timing.style.fontSize = '0.78rem';
      timing.style.color = '#94a3b8';

      body.append(phraseLine, reason, timing);
      item.append(chk, thumb, body);
      list.append(item);
    }
  }

  async function generateProposals() {
    generateBtn.disabled = true;
    list.innerHTML = '<div class="media-library-empty">Сопоставление фраз озвучки с визуальными образами...</div>';
    try {
      const res = await api(`/api/projects/${projectId}/edit/media-match`, { method: 'POST', body: { policy: 'balanced' } });
      currentProposalId = res.proposal_id;
      renderProposals(res.proposals);
      notice(`Найдено предложений вставок: ${res.total_proposals}`);
    } catch (e) {
      notice(e.message, true);
    } finally {
      generateBtn.disabled = false;
    }
  }

  async function applySelected() {
    if (!currentProposalId || !selectedProposalIds.size) {
      notice('Выберите хотя бы одну вставку для применения.', true);
      return;
    }
    applyBtn.disabled = true;
    try {
      const res = await api(`/api/projects/${projectId}/edit/media-match/${currentProposalId}/apply`, {
        method: 'POST',
        body: { accepted_proposal_ids: [...selectedProposalIds] }
      });
      notice(`Успешно применены вставки: ${res.applied_count}. Создана ревизия монтажа ${res.new_revision}.`);
      overlay.remove();
      window.location.reload();
    } catch (e) {
      notice(e.message, true);
    } finally {
      applyBtn.disabled = false;
    }
  }

  async function rollback() {
    if (!currentProposalId) {
      const latest = await api(`/api/projects/${projectId}/edit/media-match`).catch(() => null);
      if (latest && latest.id) currentProposalId = latest.id;
    }
    if (!currentProposalId) {
      notice('Нет применённых предложений для отката.', true);
      return;
    }
    try {
      const res = await api(`/api/projects/${projectId}/edit/media-match/${currentProposalId}/rollback`, { method: 'POST' });
      notice(`Монтаж успешно откатан к исходной версии. Текущая ревизия: ${res.new_revision}.`);
      overlay.remove();
      window.location.reload();
    } catch (e) {
      notice(e.message, true);
    }
  }

  try {
    const existing = await api(`/api/projects/${projectId}/edit/media-match`);
    if (existing && existing.proposals && existing.proposals.length) {
      currentProposalId = existing.id;
      renderProposals(existing.proposals);
    }
  } catch (e) {}
}


export async function openStockSearchModal({ projectId, api, notice, onItemImported }) {
  const existing = document.querySelector('.stock-search-overlay');
  if (existing) existing.remove();

  const overlay = node('div', null, 'media-library-overlay stock-search-overlay');
  const modal = node('div', null, 'media-library-modal stock-search-modal');
  overlay.append(modal);

  // Header
  const header = node('div', null, 'media-library-header');
  const title = node('h3', 'Поиск материалов на Pixabay (Фото и Видео)', 'media-library-title');
  const closeBtn = button('✕', () => overlay.remove(), 'media-library-close-btn');
  header.append(title, closeBtn);

  // Search Controls
  const searchBox = node('div', null, 'stock-search-controls');
  searchBox.style.cssText = 'display:flex;gap:10px;padding:16px 20px;border-bottom:1px solid #2a3147;background:#151825;flex-wrap:wrap;align-items:center;';

  const queryInput = node('input');
  queryInput.type = 'text';
  queryInput.placeholder = 'Поиск на русском или английском (напр., водопад, горы, forest river)...';
  queryInput.style.cssText = 'flex:1;min-width:240px;padding:9px 12px;background:#0e111a;border:1px solid #3b4566;border-radius:8px;color:#f1f5f9;font:inherit;';

  const typeSelect = node('select');
  typeSelect.style.cssText = 'padding:9px 12px;background:#0e111a;border:1px solid #3b4566;border-radius:8px;color:#f1f5f9;font:inherit;';
  for (const [val, label] of [['photo', 'Фото'], ['video', 'Видео']]) {
    const opt = node('option', label);
    opt.value = val;
    typeSelect.append(opt);
  }

  const orientSelect = node('select');
  orientSelect.style.cssText = 'padding:9px 12px;background:#0e111a;border:1px solid #3b4566;border-radius:8px;color:#f1f5f9;font:inherit;';
  for (const [val, label] of [['horizontal', '16:9 Горизонтально'], ['all', 'Любая ориентация']]) {
    const opt = node('option', label);
    opt.value = val;
    orientSelect.append(opt);
  }

  const searchBtn = button('Найти', () => doSearch(1), 'montage-btn montage-btn-accent');
  searchBtn.style.padding = '9px 20px';

  searchBox.append(queryInput, typeSelect, orientSelect, searchBtn);

  // Status & Info bar
  const statusBar = node('div', null, 'stock-status-bar');
  statusBar.style.cssText = 'display:flex;justify-content:space-between;padding:10px 20px;font-size:12px;color:#94a3b8;border-bottom:1px solid #1f2538;align-items:center;';
  const statusMsg = node('span', 'Введите запрос для поиска стоковых материалов Pixabay');
  const rateLimitBadge = node('span', '', 'stock-rate-badge');
  rateLimitBadge.style.cssText = 'background:#1a2133;padding:3px 8px;border-radius:6px;border:1px solid #2d3854;';
  statusBar.append(statusMsg, rateLimitBadge);

  // Results grid
  const grid = node('div', null, 'stock-results-grid');
  grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill, minmax(260px, 1fr));gap:14px;padding:20px;overflow-y:auto;flex:1;max-height:60vh;';

  // Pagination bar
  const pagination = node('div', null, 'stock-pagination-bar');
  pagination.style.cssText = 'display:none;justify-content:center;gap:12px;padding:12px;border-top:1px solid #1f2538;align-items:center;';
  const prevBtn = button('« Предыдущая', () => doSearch(currentPage - 1), 'montage-btn');
  const pageLabel = node('span', 'Страница 1');
  const nextBtn = button('Следующая »', () => doSearch(currentPage + 1), 'montage-btn');
  pagination.append(prevBtn, pageLabel, nextBtn);

  modal.append(header, searchBox, statusBar, grid, pagination);
  document.body.append(overlay);

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
  const escHandler = (e) => {
    if (e.key === 'Escape') {
      overlay.remove();
      window.removeEventListener('keydown', escHandler);
    }
  };
  window.addEventListener('keydown', escHandler);
  queryInput.focus();
  queryInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doSearch(1);
  });

  let currentPage = 1;
  let currentHits = 0;
  const importedExtIds = new Set();

  async function doSearch(page = 1) {
    const q = queryInput.value.trim();
    if (!q) {
      queryInput.focus();
      return;
    }
    currentPage = Math.max(1, page);
    searchBtn.disabled = true;
    statusMsg.textContent = `Поиск "${q}" на Pixabay...`;
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:#727e9e;">Загрузка результатов...</div>';

    try {
      const res = await api(`/api/projects/${projectId}/stock/search`, {
        method: 'POST',
        body: {
          q,
          media_type: typeSelect.value,
          orientation: orientSelect.value,
          page: currentPage,
          per_page: 24
        }
      });

      if (!res.ok) throw new Error(res.error || 'Ошибка поиска');

      currentHits = res.total_hits || 0;
      const rate = res.rate_limit || {};
      if (rate.remaining !== undefined) {
        rateLimitBadge.textContent = `Лимит: ${rate.remaining} / ${rate.limit} запр./мин`;
      }

      statusMsg.textContent = `Найдено: ${currentHits} ${typeSelect.value === 'video' ? 'видео' : 'фото'}${res.cached ? ' (из кэша 24ч)' : ''}`;

      renderCards(res.items || []);

      if (currentHits > 24) {
        pagination.style.display = 'flex';
        pageLabel.textContent = `Страница ${currentPage} из ${Math.ceil(currentHits / 24)}`;
        prevBtn.disabled = currentPage <= 1;
        nextBtn.disabled = currentPage * 24 >= currentHits;
      } else {
        pagination.style.display = 'none';
      }
    } catch (e) {
      statusMsg.textContent = `Ошибка: ${e.message}`;
      grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;padding:40px;color:#f87171;">${e.message}</div>`;
      notice(e.message, true);
    } finally {
      searchBtn.disabled = false;
    }
  }

  function renderCards(items) {
    grid.innerHTML = '';
    if (!items.length) {
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:#727e9e;">Ничего не найдено по этому запросу. Попробуйте изменить ключевые слова.</div>';
      return;
    }

    for (const item of items) {
      const card = node('div', null, 'stock-card');
      card.style.cssText = 'background:#131724;border:1px solid #232c42;border-radius:10px;overflow:hidden;display:flex;flex-direction:column;gap:8px;padding:10px;transition:border-color 0.15s;';

      const thumbBox = node('div', null, 'stock-thumb-box');
      thumbBox.style.cssText = 'position:relative;width:100%;height:140px;background:#0a0d16;border-radius:6px;overflow:hidden;display:flex;align-items:center;justify-content:center;';

      if (item.media_type === 'video' && item.preview_url) {
        const vid = node('video');
        vid.src = item.preview_url;
        vid.preload = 'none';
        vid.muted = true;
        vid.loop = true;
        vid.style.cssText = 'width:100%;height:140px;object-fit:cover;';
        if (item.thumbnail_url) vid.poster = item.thumbnail_url;
        thumbBox.onmouseenter = () => vid.play().catch(() => {});
        thumbBox.onmouseleave = () => { vid.pause(); vid.currentTime = 0; };
        thumbBox.append(vid);
      } else {
        const img = node('img');
        img.src = item.preview_url || item.thumbnail_url;
        img.loading = 'lazy';
        img.style.cssText = 'width:100%;height:140px;object-fit:cover;';
        thumbBox.append(img);
      }

      const badgeBox = node('div');
      badgeBox.style.cssText = 'position:absolute;bottom:6px;left:6px;display:flex;gap:4px;';
      const typeBadge = node('span', item.media_type === 'video' ? `Видео ${item.duration_sec ? item.duration_sec + 'с' : ''}` : 'Фото', 'stock-badge');
      typeBadge.style.cssText = 'background:rgba(0,0,0,0.75);color:#cbd5e1;font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px;';
      badgeBox.append(typeBadge);
      thumbBox.append(badgeBox);

      const infoBox = node('div');
      infoBox.style.cssText = 'display:flex;flex-direction:column;gap:4px;flex:1;';

      const titleEl = node('div', item.title || 'Материал Pixabay', 'stock-title');
      titleEl.style.cssText = 'font-size:13px;font-weight:600;color:#f1f5f9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';

      const metaRow = node('div');
      metaRow.style.cssText = 'font-size:11px;color:#8a99b5;display:flex;justify-content:space-between;align-items:center;';
      metaRow.innerHTML = `<span>${item.width}×${item.height}</span> <a href="${item.page_url}" target="_blank" rel="noopener" style="color:#a78bfa;text-decoration:none;">Автор: ${item.author || 'Pixabay'} ↗</a>`;

      infoBox.append(titleEl, metaRow);

      const actionRow = node('div');
      actionRow.style.cssText = 'margin-top:4px;';

      const isAlreadyImported = importedExtIds.has(item.external_id);
      const importBtn = button(isAlreadyImported ? '✓ В медиатеке' : '+ В медиатеку', async () => {
        importBtn.disabled = true;
        importBtn.textContent = 'Импорт...';
        try {
          const res = await api(`/api/projects/${projectId}/stock/import`, {
            method: 'POST',
            body: {
              external_id: item.external_id,
              media_type: item.media_type
            }
          });
          if (res.ok) {
            importedExtIds.add(item.external_id);
            importBtn.textContent = '✓ В медиатеке';
            importBtn.className = 'montage-btn montage-btn-ok';
            notice(`Материал "${item.title}" добавлен в медиатеку.`);
            onItemImported?.();
          } else {
            throw new Error(res.error || 'Ошибка импорта');
          }
        } catch (e) {
          importBtn.disabled = false;
          importBtn.textContent = '+ В медиатеку';
          notice(`Ошибка импорта: ${e.message}`, true);
        }
      }, isAlreadyImported ? 'montage-btn montage-btn-ok' : 'montage-btn montage-btn-primary');
      importBtn.disabled = isAlreadyImported;
      importBtn.style.width = '100%';

      actionRow.append(importBtn);
      card.append(thumbBox, infoBox, actionRow);
      grid.append(card);
    }
  }
}
