// FrameForge Material Search & Non-Destructive Overlay UI Component

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function button(text, onClick, className = 'btn-sm') {
  const b = node('button', text, className);
  b.type = 'button';
  if (onClick) b.onclick = (e) => { e.preventDefault(); onClick(); };
  return b;
}

function openExternal(value) {
  let url;
  try { url = new URL(value); } catch { return; }
  if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) {
    window.open(url.href, '_blank', 'noopener,noreferrer');
  }
}

export function mountMaterialSearchButton(toolbar, { projectId, api: appApi, notice, beforeOpen, onDocumentChanged, getSelectedClip, onManualAdd }) {
  const api = (path, body, method = 'GET') => {
    const route = path.replace(/^\/api/, '');
    if (method === 'POST') return appApi(route, body || {});
    return appApi(route);
  };

  const btn = button('🔍 Найти материалы', async () => {
    try {
      await beforeOpen?.();
      await openMaterialSearchModal({ projectId, api, notice, onDocumentChanged, getSelectedClip, onManualAdd });
    } catch (e) {
      notice(e.message, true);
    }
  }, 'montage-btn montage-btn-accent');

  toolbar.append(btn);
  return btn;
}

export async function openMaterialSearchModal({ projectId, api, notice, onDocumentChanged, getSelectedClip, onManualAdd }) {
  const existing = document.querySelector('.material-search-overlay');
  if (existing) existing.remove();

  // Load CSS if not already present
  if (!document.querySelector('link[href*="material-search.css"]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/static/material-search.css';
    document.head.append(link);
  }

  const overlay = node('div', null, 'material-search-overlay');
  const modal = node('div', null, 'material-search-modal');
  overlay.append(modal);

  // Header
  const header = node('div', null, 'material-search-header');
  const title = node('h3', '🔍 Поиск и подбор материалов для сцен', 'material-search-title');
  const actionsWrap = node('div', null, 'material-search-header-actions');

  const creditsBtn = button('📜 Титры и права', async () => {
    try {
      const res = await api(`/projects/${projectId}/material-search/credits`);
      openCreditsModal(res.credits || 'Нет зарегистрированных источников.');
    } catch (e) {
      notice(e.message, true);
    }
  }, 'btn-sm');

  const closeBtn = node('button', '✕', 'material-search-close');
  closeBtn.onclick = () => overlay.remove();
  actionsWrap.append(creditsBtn, closeBtn);
  header.append(title, actionsWrap);

  // Tabs Navigation
  const tabsBar = node('div', null, 'material-search-tabs');
  const browseTabBtn = node('button', '🌐 Интернет-картинки', 'material-search-tab active');
  const scenarioTabBtn = node('button', '📋 По сценарию', 'material-search-tab');
  tabsBar.append(browseTabBtn, scenarioTabBtn);

  const body = node('div', null, 'material-search-body');
  modal.append(header, tabsBar, body);
  document.body.append(overlay);

  // Close on Escape or click outside
  const escHandler = (e) => {
    if (e.key === 'Escape') {
      overlay.remove();
      document.removeEventListener('keydown', escHandler);
    }
  };
  document.addEventListener('keydown', escHandler);
  overlay.onclick = (e) => {
    if (e.target === overlay) {
      overlay.remove();
      document.removeEventListener('keydown', escHandler);
    }
  };

  // State
  let providersData = [];
  try {
    const pRes = await api('/material-search/providers');
    providersData = pRes.providers || pRes || [];
  } catch {
    providersData = [
      { provider_id: 'commons', name: 'Wikimedia Commons', capabilities: { photo: true, video: true } },
      { provider_id: 'pixabay', name: 'Pixabay', capabilities: { photo: true, video: true } },
      { provider_id: 'local_library', name: 'Медиатека проекта', capabilities: { photo: true, video: true } }
    ];
  }

  let currentPlan = null;
  let activeJobId = null;
  let pollTimer = null;

  // Container panels for tabs
  const browsePanel = node('div', null, 'material-browse-panel');
  const scenarioPanel = node('div', null, 'material-scenario-panel');
  scenarioPanel.hidden = true;
  body.append(browsePanel, scenarioPanel);

  browseTabBtn.onclick = () => {
    browseTabBtn.classList.add('active');
    scenarioTabBtn.classList.remove('active');
    browsePanel.hidden = false;
    scenarioPanel.hidden = true;
  };

  scenarioTabBtn.onclick = () => {
    scenarioTabBtn.classList.add('active');
    browseTabBtn.classList.remove('active');
    browsePanel.hidden = true;
    scenarioPanel.hidden = false;
  };

  // --- TAB 1: MANUAL IMAGE SEARCH AND IMPORT ---
  const searchBar = node('div', null, 'material-browse-search-bar');
  const browseInput = node('input', null, 'material-browse-input');
  browseInput.type = 'text';
  browseInput.maxLength = 400;
  browseInput.setAttribute('aria-label', 'Запрос для поиска картинок');
  browseInput.placeholder = 'Например: Castle Ward Winterfell';
  const sceneQuery = () => String(getSelectedClip?.()?.search_text || getSelectedClip?.()?.quote || '')
    .replace(/\s+/g, ' ').trim().slice(0, 240).trim();
  browseInput.value = sceneQuery();

  const findBtn = button('🔍 Найти', () => doBrowseSearch(), 'btn-sm primary');
  const browserBtn = button('Открыть поиск в браузере', () => {
    const query = browseInput.value.trim() || sceneQuery();
    if (!query) {
      notice('Введите запрос или выберите сцену в монтаже.', true);
      return;
    }
    const url = new URL('https://www.google.com/search');
    url.search = new URLSearchParams({ tbm: 'isch', q: query }).toString();
    openExternal(url.href);
  });
  searchBar.append(browseInput, findBtn, browserBtn);
  browseInput.onkeydown = (e) => { if (e.key === 'Enter') doBrowseSearch(); };

  // Context Filters Row
  const filtersRow = node('div', null, 'material-browse-filters-row');
  const providerSelect = node('select', null, 'material-search-select');
  providerSelect.setAttribute('aria-label', 'Источник картинок');
  providerSelect.append(
    new Option('Wikimedia Commons', 'commons'),
    new Option('Openverse', 'openverse'),
    new Option('SearXNG (локальный сервис)', 'searxng')
  );
  filtersRow.append(node('span', 'Источник:'), providerSelect);

  const contextFilters = node('span', null, 'material-browse-context-filters');

  // Universe
  contextFilters.append(node('span', 'Вселенная:'));
  const bUniverse = node('select', null, 'material-search-select');
  bUniverse.append(
    new Option('Вселенная (любая)', ''),
    new Option('🐉 Игра престолов (Game of Thrones)', 'game_of_thrones'),
    new Option('⚔️ Ведьмак (The Witcher)', 'witcher'),
    new Option('💍 Властелин колец (Lord of the Rings)', 'lord_of_the_rings'),
    new Option('⚡ Гарри Поттер (Harry Potter)', 'harry_potter'),
    new Option('🛡️ Marvel Cinematic Universe', 'mcu'),
    new Option('🌌 Звёздные войны (Star Wars)', 'star_wars'),
    new Option('🌿 Природа и география', 'nature_geography')
  );
  contextFilters.append(bUniverse);

  // Adaptation
  contextFilters.append(node('span', 'Вариант:'));
  const bAdaptation = node('select', null, 'material-search-select');
  bAdaptation.append(
    new Option('Любой', 'any'),
    new Option('Сериал', 'series'),
    new Option('Фильм', 'movie'),
    new Option('Книга', 'book'),
    new Option('Игра', 'game')
  );
  contextFilters.append(bAdaptation);

  // Image Type
  contextFilters.append(node('span', 'Тип:'));
  const bType = node('select', null, 'material-search-select');
  bType.append(
    new Option('Любое', 'any'),
    new Option('Кадры / промо', 'still'),
    new Option('Иллюстрации / арт', 'illustration'),
    new Option('Карты', 'map')
  );
  contextFilters.append(bType);
  filtersRow.append(contextFilters);
  const commercial = node('input');
  commercial.type = 'checkbox';
  commercial.checked = true;
  const licenseFilter = node('label', null, 'material-browse-context-filters');
  licenseFilter.append(commercial, 'Лицензия допускает коммерческое использование и изменения');
  filtersRow.append(licenseFilter);
  function updateProviderFilters() {
    contextFilters.hidden = providerSelect.value !== 'searxng';
    licenseFilter.hidden = providerSelect.value !== 'openverse';
  }
  providerSelect.onchange = updateProviderFilters;
  updateProviderFilters();

  // Orientation
  filtersRow.append(node('span', 'Формат:'));
  const bOrient = node('select', null, 'material-search-select');
  bOrient.append(
    new Option('Любой', ''),
    new Option('Горизонтальные', 'horizontal'),
    new Option('Вертикальные', 'vertical')
  );
  filtersRow.append(bOrient);

  browsePanel.append(node('p','Commons и Openverse ищут в открытых каталогах. Перед использованием проверяйте лицензию на странице источника.','material-search-label'));
  const browseStatus=node('div','Введите запрос и нажмите «Найти».','material-search-label');browseStatus.setAttribute('role','status');browseStatus.style.cssText='padding:12px;white-space:pre-wrap;';
  browsePanel.append(browseStatus);
  const browseGrid = node('div', null, 'material-browse-grid');
  const importRow = node('div', null, 'material-browse-search-bar');
  const linkInput = node('input', null, 'material-browse-input');
  linkInput.type = 'url';
  linkInput.placeholder = 'Прямая HTTPS-ссылка на JPG, PNG или WebP';
  linkInput.setAttribute('aria-label', 'Ссылка на изображение');
  linkInput.maxLength = 8192;
  const fileInput = node('input');
  fileInput.type = 'file';
  fileInput.accept = '.jpg,.jpeg,.png,.webp,.bmp';
  fileInput.hidden = true;
  const importStatus = node('div', null, 'material-search-label');
  importStatus.setAttribute('role', 'status');

  async function importImage(path, payload) {
    importLinkBtn.disabled = importFileBtn.disabled = true;
    importStatus.textContent = 'Импорт изображения…';
    try {
      const result = await api(`/projects/${projectId}/${path}`, payload, 'POST');
      const item = result.item || result;
      importStatus.textContent = 'Изображение добавлено в медиатеку. ';
      if (onManualAdd && getSelectedClip?.()?.clip_id) {
        const applyImported = button('На выбранный кадр', async () => {
          if (!getSelectedClip?.()?.clip_id) {
            notice('Выберите исходную сцену в монтаже.', true);
            return;
          }
          applyImported.disabled = true;
          try {
            await onManualAdd(item);
            overlay.remove();
          } catch (error) {
            notice(error.message, true);
            applyImported.disabled = false;
          }
        });
        importStatus.append(applyImported);
      }
    } catch (error) {
      importStatus.textContent = error.message;
    } finally {
      importLinkBtn.disabled = importFileBtn.disabled = false;
    }
  }

  const importLinkBtn = button('Импорт по ссылке', () => {
    if (!linkInput.value.trim()) {
      importStatus.textContent = 'Вставьте прямую ссылку на файл изображения.';
      return;
    }
    return importImage('material-search/image-link', { url: linkInput.value.trim() });
  });
  const importFileBtn = button('Выбрать файл…', () => fileInput.click());
  fileInput.onchange = async () => {
    const file = fileInput.files?.[0];
    fileInput.value = '';
    if (file) await importImage('edit/media-library/upload', file);
  };
  importRow.append(linkInput, importLinkBtn, importFileBtn, fileInput);
  browsePanel.append(searchBar, filtersRow, importRow, importStatus, browseGrid);

  async function doBrowseSearch() {
    if (findBtn.disabled) return;
    const q = browseInput.value.trim();
    if (!q) {
      notice('Введите поисковый запрос.', true);
      return;
    }
    findBtn.disabled = true;
    browseStatus.textContent='Ищу картинки…';browseStatus.style.color='';
    findBtn.textContent = 'Поиск…';
    browseGrid.innerHTML = '';
    const loadingCard = node('div', 'Выполняется поиск изображений…', 'material-search-loading');
    browseGrid.append(loadingCard);

    try {
      const res = await api(`/projects/${projectId}/material-search/browse`, {
        query: q,
        provider: providerSelect.value,
        universe_id: bUniverse.value || null,
        adaptation: bAdaptation.value,
        image_type: bType.value,
        filters: {
          orientation: bOrient.value || null,
          commercial: commercial.checked
        }
      }, 'POST');

      browseStatus.textContent=`Найдено: ${(res.results||[]).length}. Запрос: ${res.query||q}`;
      browseGrid.innerHTML = '';

      const items = res.results || [];
      if (!items.length) {
        browseGrid.append(node('div', res.engines_unavailable ? 'Поисковые источники SearXNG временно недоступны. Попробуйте позже.' : 'Ничего не найдено по данному запросу.', 'material-search-empty'));
        return;
      }

      for (const item of items) {
        const card = node('div', null, 'material-browse-card');

        // Thumb wrap
        const thumbWrap = node('div', null, 'material-browse-thumb-wrap');
        const img = node('img', null, 'material-browse-thumb loading');
        img.alt = item.title;
        img.loading = 'lazy';
        img.src = `/api/projects/${projectId}/material-search/browse/${res.session_id}/items/${item.id}/preview`;
        img.onload = () => img.classList.remove('loading');
        img.onerror = () => {
          img.classList.remove('loading');
          img.alt = 'Превью недоступно';
        };

        const resPill = node('span', `${item.width || '?'}x${item.height || '?'}`, 'material-browse-res-pill');
        thumbWrap.append(img, resPill);
        card.append(thumbWrap);

        // Meta
        const meta = node('div', null, 'material-browse-meta');
        meta.append(node('div', item.title, 'material-browse-card-title'));
        meta.append(node('div', `🌐 ${item.author || item.source_page_url}`, 'material-browse-card-source'));
        const license = item.rights_status === 'recorded_license' ? item.license_name : 'Права не установлены';
        meta.append(node('div', license, 'material-browse-card-source'));
        card.append(meta);

        // Actions
        const actions = node('div', null, 'material-browse-actions');

        const applyBtn = button('🎬 На выбранный кадр', async () => {
          try {
            applyBtn.disabled = true;
            applyBtn.textContent = 'Применение…';
            const aRes = await api(`/projects/${projectId}/material-search/browse/${res.session_id}/items/${item.id}/apply`, getSelectedClip?.() || {}, 'POST');
            notice('Изображение успешно наложено поверх сцены монтажа!', false);
            await onDocumentChanged?.(aRes.document, aRes.revision, aRes.sources);
            overlay.remove();
          } catch (e) {
            notice(e.message, true);
          } finally {
            applyBtn.disabled = false;
            applyBtn.textContent = '🎬 На выбранный кадр';
          }
        }, 'btn-sm primary');

        const importBtn = button('📥 В медиатеку', async () => {
          try {
            importBtn.disabled = true;
            await api(`/projects/${projectId}/material-search/browse/${res.session_id}/items/${item.id}/import`, {}, 'POST');
            notice('Изображение добавлено в медиатеку проекта.', false);
            importBtn.textContent = '✓ В медиатеке';
          } catch (e) {
            notice(e.message, true);
            importBtn.disabled = false;
          }
        }, 'btn-sm');

        const srcBtn = button('🔗 Источник', () => {
          openExternal(item.source_page_url);
        }, 'btn-sm');
        srcBtn.disabled = !item.source_page_url;

        const verifyBtn=button('Проверить соответствие',async()=>{
          const quote=getSelectedClip?.()?.quote;
          if(!quote){notice('Выберите сцену с цитатой сценария для проверки.',true);return;}
          verifyBtn.disabled=true;
          try{const checked=await api(`/projects/${projectId}/material-search/browse/${res.session_id}/items/${item.id}/verify`,{quote},'POST');const verdict=checked.verification||{};notice(`${verdict.status_label||verdict.semantic_status||'нужна проверка'}: ${verdict.reason||''}. ${verdict.identity_reason||''}`);}
          catch(e){notice(e.message,true);}finally{verifyBtn.disabled=false;}
        },'btn-sm');
        actions.append(applyBtn, importBtn, srcBtn, verifyBtn);
        card.append(actions);
        browseGrid.append(card);
      }
    } catch (e) {
      browseGrid.innerHTML = '';
      browseStatus.textContent='Поиск не выполнен: '+e.message;browseStatus.style.color='#fca5a5';
      notice(e.message, true);
    } finally {
      findBtn.disabled = false;
      findBtn.textContent = '🔍 Найти';
    }
  }

  // --- TAB 2: SCENARIO PANEL ---
  // Render Step 1: Scope & Provider Configuration
  const scopeBox = node('div', null, 'material-search-scope-box');

  const provGroup = node('div', null, 'material-search-controls-group');
  provGroup.append(node('span', 'Источники: ', 'material-search-label'));

  const providerCheckboxes = {};
  for (const p of providersData) {
    if (p.manual_only) continue;
    const lbl = node('label', null, 'material-search-label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = p.provider_id;
    cb.checked = true;
    providerCheckboxes[p.provider_id] = cb;
    const pLabel = p.name || p.display_name || p.provider_id;
    lbl.append(cb, document.createTextNode(' ' + pLabel));
    provGroup.append(lbl);
  }

  const universeGroup = node('div', null, 'material-search-controls-group');
  universeGroup.append(node('span', 'Тематика: ', 'material-search-label'));
  const universeSelect = node('select', null, 'material-search-select');
  const optGeneral = node('option', '🌿 Природа / География / Факты');
  optGeneral.value = 'nature_geography';
  const optGot = node('option', '🐉 Игра престолов (Game of Thrones)');
  optGot.value = 'game_of_thrones';
  const optHp = node('option', '⚡ Гарри Поттер (Harry Potter)');
  optHp.value = 'harry_potter';
  universeSelect.append(optGeneral, optGot, optHp);
  universeGroup.append(universeSelect);

  const planBtn = button('📋 Составить план запросов', async () => {
    try {
      planBtn.disabled = true;
      planBtn.textContent = 'Составление плана…';
      const selectedProviders = Object.keys(providerCheckboxes).filter(k => providerCheckboxes[k].checked);
      if (!selectedProviders.length) {
        throw new Error('Выберите хотя бы один источник.');
      }
      const res = await api(`/projects/${projectId}/material-search/plans`, {
        options: {
          universe_id: universeSelect.value,
          preferred_providers: selectedProviders
        }
      }, 'POST');
      currentPlan = res;
      renderPlanSection();
    } catch (e) {
      notice(e.message, true);
    } finally {
      planBtn.disabled = false;
      planBtn.textContent = '📋 Составить план запросов';
    }
  }, 'btn-sm primary');

  scopeBox.append(provGroup, universeGroup, planBtn);
  scenarioPanel.append(scopeBox);

  // Dynamic containers
  const planSection = node('div', null, 'material-search-plan-section');
  planSection.hidden = true;
  scenarioPanel.append(planSection);

  const progressBox = node('div', null, 'material-search-progress-box');
  progressBox.hidden = true;
  scenarioPanel.append(progressBox);

  const resultsSection = node('div', null, 'material-search-results-section');
  resultsSection.hidden = true;
  scenarioPanel.append(resultsSection);

  // Functions to render sections
  function renderPlanSection() {
    planSection.innerHTML = '';
    planSection.hidden = false;

    const head = node('div', null, 'material-search-plan-head');
    const titleP = node('h4', 'План запросов по смысловым сегментам озвучки', 'material-search-title');
    const statsP = node('span', `Сегментов: ${currentPlan.segments_count || currentPlan.requirements?.length}, Запросов: ${currentPlan.total_queries}`, 'material-search-plan-stats');
    head.append(titleP, statsP);
    planSection.append(head);

    const reqsList = node('div', null, 'material-search-req-list');
    for (const req of (currentPlan.requirements || [])) {
      const card = node('div', null, 'material-search-req-card' + (req.enabled ? '' : ' disabled'));

      const rHead = node('div', null, 'material-search-req-header');
      const timeSpan = node('span', `⏱ ${((req.start_ms || 0) / 1000).toFixed(1)}c - ${((req.end_ms || 0) / 1000).toFixed(1)}c (${((req.duration_ms || 0) / 1000).toFixed(1)}c)`, 'material-search-req-time');
      const quoteSpan = node('span', `«${req.quote}»`, 'material-search-req-quote');

      const toggleCb = document.createElement('input');
      toggleCb.type = 'checkbox';
      toggleCb.checked = req.enabled;
      toggleCb.title = 'Включить поиск для этого сегмента';
      toggleCb.onchange = async () => {
        try {
          const upd = await api(`/projects/${projectId}/material-search/plans/${currentPlan.plan_id}/update`, {
            requirement_id: req.id,
            enabled: toggleCb.checked
          }, 'POST');
          currentPlan = upd;
          renderPlanSection();
        } catch (e) {
          notice(e.message, true);
        }
      };

      rHead.append(toggleCb, timeSpan, quoteSpan);
      card.append(rHead);

      // Tags & Entities
      const tagsWrap = node('div', null, 'material-search-req-tags');
      for (const ent of (req.subject_entities || [])) {
        tagsWrap.append(node('span', `🏷 ${ent}`, 'material-search-tag entity'));
      }
      for (const mand of (req.mandatory_details || [])) {
        tagsWrap.append(node('span', `✓ ${mand}`, 'material-search-tag'));
      }
      for (const forb of (req.forbidden_substitutions || [])) {
        tagsWrap.append(node('span', `✕ не ${forb}`, 'material-search-tag'));
      }
      card.append(tagsWrap);

      // Queries Input Row
      const qRow = node('div', null, 'material-search-query-row');
      const qInput = node('input', null, 'material-search-query-input');
      qInput.type = 'text';
      qInput.value = (req.effective_queries || []).join(', ');
      qInput.placeholder = 'Поисковые фразы через запятую...';
      qInput.onchange = async () => {
        const queries = qInput.value.split(',').map(s => s.trim()).filter(Boolean);
        try {
          const upd = await api(`/projects/${projectId}/material-search/plans/${currentPlan.plan_id}/update`, {
            requirement_id: req.id,
            user_queries: queries
          }, 'POST');
          currentPlan = upd;
          renderPlanSection();
        } catch (e) {
          notice(e.message, true);
        }
      };

      // Role select
      const roleSel = node('select', null, 'material-search-select');
      const rExact = node('option', 'Точный кадр'); rExact.value = 'exact';
      const rTheme = node('option', 'Иллюстрация темы'); rTheme.value = 'theme';
      const rAtmo = node('option', 'Атмосферный фон'); rAtmo.value = 'atmosphere';
      roleSel.append(rExact, rTheme, rAtmo);
      roleSel.value = req.requested_role || 'exact';
      roleSel.onchange = async () => {
        try {
          const upd = await api(`/projects/${projectId}/material-search/plans/${currentPlan.plan_id}/update`, {
            requirement_id: req.id,
            requested_role: roleSel.value
          }, 'POST');
          currentPlan = upd;
          renderPlanSection();
        } catch (e) {
          notice(e.message, true);
        }
      };

      qRow.append(node('span', 'Запросы:', 'material-search-label'), qInput, roleSel);
      card.append(qRow);
      reqsList.append(card);
    }
    planSection.append(reqsList);

    // Launch Search Button
    const launchBtn = button('🚀 Начать поиск и проверку материалов', async () => {
      try {
        launchBtn.disabled = true;
        launchBtn.textContent = 'Запуск задачи…';
        const selectedProviders = Object.keys(providerCheckboxes).filter(k => providerCheckboxes[k].checked);
        const jobRes = await api(`/projects/${projectId}/material-search/jobs`, {
          plan_id: currentPlan.plan_id,
          providers: selectedProviders
        }, 'POST');
        activeJobId = jobRes.job_id;
        startJobPolling(activeJobId);
      } catch (e) {
        notice(e.message, true);
        launchBtn.disabled = false;
        launchBtn.textContent = '🚀 Начать поиск и проверку материалов';
      }
    }, 'btn-sm primary');
    planSection.append(launchBtn);
  }

  function startJobPolling(jobId) {
    if (pollTimer) clearInterval(pollTimer);
    progressBox.hidden = false;
    resultsSection.hidden = true;

    const poll = async () => {
      try {
        const job = await api(`/projects/${projectId}/material-search/jobs/${jobId}`);
        renderProgress(job);
        if (job.state === 'completed') {
          clearInterval(pollTimer);
          const candRes = await api(`/projects/${projectId}/material-search/jobs/${jobId}/candidates`);
          renderResults(candRes.candidates || []);
        } else if (job.state === 'failed' || job.state === 'cancelled') {
          clearInterval(pollTimer);
          notice(`Задача поиска ${job.state === 'cancelled' ? 'отменена' : 'завершена с ошибкой: ' + (job.error || '')}`, true);
        }
      } catch (e) {
        clearInterval(pollTimer);
        notice(e.message, true);
      }
    };

    poll();
    pollTimer = setInterval(poll, 1200);
  }

  function renderProgress(job) {
    progressBox.innerHTML = '';
    const pct = Math.round((job.progress || 0) * 100);

    const barBg = node('div', null, 'material-search-progress-bar-bg');
    const barFill = node('div', null, 'material-search-progress-bar-fill');
    barFill.style.width = `${pct}%`;
    barBg.append(barFill);

    const infoRow = node('div', null, 'material-search-progress-info');
    infoRow.append(node('span', job.current_step || 'Поиск материалов…'));
    infoRow.append(node('span', `${pct}% (${job.processed_queries || 0} / ${job.total_queries || 0} запросов, кандидатов: ${job.total_candidates || 0})`));

    const cancelBtn = button('Остановить', async () => {
      try {
        await api(`/projects/${projectId}/material-search/jobs/${job.id}/cancel`, {}, 'POST');
        notice('Отмена запрошена...');
      } catch (e) {
        notice(e.message, true);
      }
    }, 'btn-sm danger');

    progressBox.append(barBg, infoRow, cancelBtn);
  }

  function renderResults(candidates) {
    resultsSection.innerHTML = '';
    resultsSection.hidden = false;

    if (!candidates.length) {
      resultsSection.append(node('p', 'По заданным критериям кандидатов не найдено. Попробуйте уточнить ключевые слова в плане.'));
      return;
    }

    // Group candidates by requirement_id
    const groups = {};
    for (const c of candidates) {
      if (!groups[c.requirement_id]) groups[c.requirement_id] = [];
      groups[c.requirement_id].push(c);
    }

    for (const req of (currentPlan.requirements || [])) {
      const cands = groups[req.id] || [];
      if (!cands.length) continue;

      const groupWrap = node('div', null, 'material-search-segment-group');
      const gTitle = node('div', null, 'material-search-segment-title');
      gTitle.append(node('span', `🎬 Сегмент: «${req.quote}»`));
      gTitle.append(node('span', `(${cands.length} найдено)`, 'material-search-plan-stats'));
      groupWrap.append(gTitle);

      const grid = node('div', null, 'material-search-grid');
      for (const cand of cands) {
        const item = cand.candidate || {};
        const card = node('div', null, 'material-search-card' + (cand.is_selected ? ' applied' : ''));

        // Preview Wrap
        const prevWrap = node('div', null, 'material-search-preview-wrap');
        const img = node('img', null, 'material-search-preview-img');
        img.src = item.preview_url || item.thumbnail_url || item.download_url || '';
        img.loading = 'lazy';
        prevWrap.append(img);

        // Badges
        const badgesWrap = node('div', null, 'material-search-card-badges');
        const pBadge = node('span', item.provider || cand.provider, `badge-prov ${item.provider || cand.provider}`);
        const sBadge = node('span', cand.semantic_status === 'matched' ? '✓ Совпадает' : cand.semantic_status === 'rejected' ? '✕ Отклонено' : '⚠ Нужна проверка', `badge-status ${cand.semantic_status}`);
        badgesWrap.append(pBadge, sBadge);
        prevWrap.append(badgesWrap);
        card.append(prevWrap);

        // Content
        const content = node('div', null, 'material-search-card-content');
        const cTitle = node('div', item.title || 'Без названия', 'material-search-card-title');
        cTitle.title = item.title || '';
        content.append(cTitle);

        const meta = node('div', null, 'material-search-card-meta');
        const authorLine = node('div', `Автор: ${item.author || 'Не указан'}`);
        const licLine = node('div', `Лицензия: ${item.license_name || 'Не указана'}`);
        const dimLine = node('div', `Размер: ${item.width || 0}x${item.height || 0} (${item.media_type === 'video' ? 'Видео' : 'Фото'})`);
        meta.append(authorLine, licLine, dimLine);
        content.append(meta);

        // Reasons
        const reasonsList = cand.reasons || [];
        if (reasonsList.length) {
          const rBox = node('div', reasonsList.join(' • '), 'material-search-card-reasons');
          content.append(rBox);
        }

        // Actions
        const actions = node('div', null, 'material-search-card-actions');
        if (item.source_page_url) {
          const srcLink = button('Источник ↗', () => {
            window.open(item.source_page_url, '_blank', 'noopener,noreferrer');
          }, 'btn-sm');
          actions.append(srcLink);
        }

        // Apply / Rollback buttons
        if (cand.is_selected) {
          const rollBtn = button('↩ Откатить', async () => {
            try {
              rollBtn.disabled = true;
              rollBtn.textContent = 'Откат…';
              const res = await api(`/projects/${projectId}/material-search/candidates/rollback`, {
                origin_clip_ids: cand.origin_clip_ids || req.origin_clip_ids
              }, 'POST');
              cand.is_selected = 0;
              notice('Материал удалён, оригинальная сцена восстановлена.');
              card.classList.remove('applied');
              onDocumentChanged?.(res.document, res.revision);
            } catch (e) {
              notice(e.message, true);
            } finally {
              rollBtn.disabled = false;
              rollBtn.textContent = '↩ Откатить';
            }
          }, 'btn-sm danger');
          actions.append(rollBtn);
        } else {
          const applyBtn = button('✓ Применить к сцене', async () => {
            try {
              applyBtn.disabled = true;
              applyBtn.textContent = 'Применение…';
              const res = await api(`/projects/${projectId}/material-search/candidates/${cand.id}/apply`, {}, 'POST');
              cand.is_selected = 1;
              notice('Материал успешно наложен на сегмент!');
              card.classList.add('applied');
              onDocumentChanged?.(res.document, res.revision);
            } catch (e) {
              notice(e.message, true);
            } finally {
              applyBtn.disabled = false;
              applyBtn.textContent = '✓ Применить к сцене';
            }
          }, 'btn-sm primary');
          actions.append(applyBtn);
        }

        content.append(actions);
        card.append(content);
        grid.append(card);
      }
      groupWrap.append(grid);
      resultsSection.append(groupWrap);
    }
  }

  function openCreditsModal(creditsText) {
    const credOverlay = node('div', null, 'material-search-overlay');
    const credModal = node('div', null, 'material-search-modal');
    credModal.style.maxWidth = '700px';
    credModal.style.height = 'auto';

    const cHead = node('div', null, 'material-search-header');
    cHead.append(node('h3', '📜 Титры и лицензии использованных материалов', 'material-search-title'));
    const cClose = node('button', '✕', 'material-search-close');
    cClose.onclick = () => credOverlay.remove();
    cHead.append(cClose);

    const cBody = node('div', null, 'material-search-body');
    const pre = node('pre', creditsText, 'material-credits-pre');
    const copyBtn = button('📋 Скопировать текст для описания', () => {
      navigator.clipboard.writeText(creditsText);
      notice('Титры скопированы в буфер обмена!');
    }, 'btn-sm primary');

    cBody.append(pre, copyBtn);
    credModal.append(cHead, cBody);
    credOverlay.append(credModal);
    document.body.append(credOverlay);
  }
}
