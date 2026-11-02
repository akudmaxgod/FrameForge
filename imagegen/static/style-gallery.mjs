/**
 * FrameForge Style Gallery Modal & Compact Selector
 * Implements Insaga-style compact card and rich gallery dialog.
 */

/**
 * Creates the compact style selector button for the sidebar controls.
 * @param {Object} options
 * @param {Object} options.activePreset - Active style preset object {id, name, subtitle, image, ...}
 * @param {boolean} options.locked - Whether selection is locked (e.g. existing project opened)
 * @param {Function} options.onOpen - Callback when the card is clicked to open the gallery modal
 * @returns {HTMLButtonElement}
 */
export function createStyleSelectorCard({ activePreset, locked = false, onOpen }) {
  const card = document.createElement('button');
  card.type = 'button';
  card.className = 'style-selector-card' + (locked ? ' locked' : '');
  card.dataset.testid = 'style-selector-card';
  card.setAttribute('aria-haspopup', 'dialog');
  card.setAttribute('aria-expanded', 'false');

  if (locked) {
    card.disabled = true;
    card.setAttribute('aria-disabled', 'true');
    card.title = 'Стиль закреплён в рамках текущего проекта';
  } else {
    card.title = 'Нажмите, чтобы выбрать визуальный стиль';
    card.addEventListener('click', (e) => {
      e.preventDefault();
      onOpen?.(card);
    });
  }

  // Thumbnail
  const thumbWrap = document.createElement('div');
  thumbWrap.className = 'style-selector-thumb-wrap';
  const img = document.createElement('img');
  img.className = 'style-selector-thumb';
  img.src = activePreset.image || '/presets/realism.png';
  img.alt = activePreset.name || 'Стиль';
  img.loading = 'lazy';
  thumbWrap.appendChild(img);

  // Info: Subtitle & Title
  const info = document.createElement('div');
  info.className = 'style-selector-info';
  const subtitle = document.createElement('span');
  subtitle.className = 'style-selector-subtitle';
  subtitle.textContent = 'Визуальный стиль';
  const title = document.createElement('span');
  title.className = 'style-selector-title';
  title.textContent = activePreset.name || 'Реализм';
  info.append(subtitle, title);

  // Right chevron
  const chevron = document.createElement('span');
  chevron.className = 'style-selector-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>`;

  card.append(thumbWrap, info, chevron);
  return card;
}

/**
 * Opens the Style Gallery modal dialog.
 * @param {Object} options
 * @param {string} options.activePresetId - Currently selected preset id
 * @param {Array} options.presets - List of style presets
 * @param {Function} options.onSelect - Callback with selected preset id: (id) => void
 * @param {HTMLElement} [options.triggerElement] - The element that triggered the dialog, to return focus to
 */
export function openStyleGallery({ activePresetId, presets, onSelect, triggerElement }) {
  let dialog = document.getElementById('style-gallery-modal');
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.id = 'style-gallery-modal';
    dialog.className = 'style-gallery-dialog';
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'style-gallery-title');
    document.body.appendChild(dialog);
  }

  dialog.innerHTML = `
    <div class="style-gallery-header">
      <h2 id="style-gallery-title" class="style-gallery-title">Выберите стиль для вашей истории</h2>
      <button type="button" class="style-gallery-close" aria-label="Закрыть" data-testid="style-gallery-close">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>
    <div class="style-gallery-search-wrap">
      <div class="style-gallery-search-inner">
        <span class="style-search-icon" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="8"></circle>
            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
          </svg>
        </span>
        <input type="search" class="style-gallery-search" placeholder="Поиск стиля..." aria-label="Поиск стиля" data-testid="style-gallery-search" autocomplete="off" />
      </div>
    </div>
    <div class="style-gallery-content">
      <div class="style-gallery-grid" role="listbox" aria-label="Доступные стили" data-testid="style-gallery-grid"></div>
      <div class="style-gallery-empty" style="display:none;" data-testid="style-gallery-empty">
        <div class="style-gallery-empty-icon" aria-hidden="true">🔍</div>
        <strong>Стиль не найден</strong>
        <span>Попробуйте изменить запрос или очистить строку поиска</span>
      </div>
    </div>
  `;

  const closeBtn = dialog.querySelector('.style-gallery-close');
  const searchInput = dialog.querySelector('.style-gallery-search');
  const grid = dialog.querySelector('.style-gallery-grid');
  const emptyEl = dialog.querySelector('.style-gallery-empty');

  const closeDialog = () => {
    if (dialog.open) {
      dialog.close();
    }
    if (triggerElement) {
      triggerElement.setAttribute('aria-expanded', 'false');
      try {
        triggerElement.focus();
      } catch (e) {}
    }
  };

  closeBtn.addEventListener('click', (e) => {
    e.preventDefault();
    closeDialog();
  });

  // Close on backdrop click
  dialog.addEventListener('click', (e) => {
    const rect = dialog.getBoundingClientRect();
    const isInDialog = (
      rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
      rect.left <= e.clientX && e.clientX <= rect.left + rect.width
    );
    if (!isInDialog) {
      closeDialog();
    }
  });

  // Handle native cancel (Escape key)
  dialog.addEventListener('cancel', () => {
    if (triggerElement) {
      triggerElement.setAttribute('aria-expanded', 'false');
      setTimeout(() => {
        try {
          triggerElement.focus();
        } catch (e) {}
      }, 0);
    }
  });

  const renderGrid = (query = '') => {
    const q = query.trim().toLowerCase();
    grid.innerHTML = '';

    const matches = presets.filter((p) => {
      if (!q) return true;
      const nameMatch = (p.name || '').toLowerCase().includes(q);
      const subMatch = (p.subtitle || '').toLowerCase().includes(q);
      const idMatch = (p.id || '').toLowerCase().includes(q);
      const descMatch = (p.description || '').toLowerCase().includes(q);
      return nameMatch || subMatch || idMatch || descMatch;
    });

    if (matches.length === 0) {
      grid.style.display = 'none';
      emptyEl.style.display = 'flex';
      return;
    }

    grid.style.display = 'grid';
    emptyEl.style.display = 'none';

    for (const preset of matches) {
      const isSelected = preset.id === activePresetId;
      const cardItem = document.createElement('div');
      cardItem.className = 'style-card-item' + (isSelected ? ' selected' : '');
      cardItem.tabIndex = 0;
      cardItem.setAttribute('role', 'option');
      cardItem.setAttribute('aria-selected', isSelected ? 'true' : 'false');
      cardItem.dataset.styleId = preset.id;
      cardItem.dataset.testid = `style-card-${preset.id}`;
      cardItem.title = `${preset.name} (${preset.subtitle || ''})`;

      // Preview Wrap
      const previewWrap = document.createElement('div');
      previewWrap.className = 'style-card-preview-wrap';
      const img = document.createElement('img');
      img.className = 'style-card-image';
      img.src = preset.image || '/presets/realism.png';
      img.alt = preset.name;
      img.loading = 'lazy';
      previewWrap.appendChild(img);

      if (isSelected) {
        const badge = document.createElement('div');
        badge.className = 'style-card-badge';
        badge.setAttribute('aria-label', 'Выбран');
        badge.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
        previewWrap.appendChild(badge);
      }
      cardItem.appendChild(previewWrap);

      // Details
      const details = document.createElement('div');
      details.className = 'style-card-details';
      const nameEl = document.createElement('div');
      nameEl.className = 'style-card-name';
      nameEl.textContent = preset.name;
      const descEl = document.createElement('p');
      descEl.className = 'style-card-desc';
      descEl.textContent = preset.description || preset.subtitle || '';
      details.append(nameEl, descEl);
      cardItem.appendChild(details);

      // Selection actions
      const selectAction = () => {
        closeDialog();
        onSelect?.(preset.id);
      };

      cardItem.addEventListener('click', (e) => {
        e.preventDefault();
        selectAction();
      });

      cardItem.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          selectAction();
        }
      });

      grid.appendChild(cardItem);
    }
  };

  searchInput.addEventListener('input', () => {
    renderGrid(searchInput.value);
  });

  // Render initial list
  renderGrid('');

  if (triggerElement) {
    triggerElement.setAttribute('aria-expanded', 'true');
  }

  // Show modal dialog
  if (typeof dialog.showModal === 'function') {
    dialog.showModal();
  } else {
    dialog.setAttribute('open', '');
  }

  // Focus search input
  setTimeout(() => {
    try {
      searchInput.focus();
    } catch (e) {}
  }, 50);
}
