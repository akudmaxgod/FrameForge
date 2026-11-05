// ==UserScript==
// @name         FrameForge <-> Google Flow Automation Bridge
// @namespace    https://frameforge.app/
// @version      5.2
// @description  Клиентская генерация раскадровки, персонажей и локаций в Google Flow с фиксацией референсов
// @author       FrameForge
// @match        *://flow.google.com/*
// @match        https://flow.google.com/*
// @match        *://*.google.com/*flow*
// @match        https://labs.google/*flow*
// @include      *flow.google.com*
// @include      https://flow.google.com*
// @include      *labs.google*flow*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_setClipboard
// @connect      127.0.0.1
// @connect      localhost
// @connect      *
// @updateURL    http://127.0.0.1:8765/google_flow_autopilot.user.js
// @downloadURL  http://127.0.0.1:8765/google_flow_autopilot.user.js
// @noframes
// ==/UserScript==

(function () {
    'use strict';

    console.log('%c🎬 [FrameForge Autopilot v5.2] Инициализация на Google Flow...', 'color: #8b5cf6; font-weight: bold; font-size: 14px;');

    const FF_HOST = 'http://127.0.0.1:8765';
    let autopilotRunning = false;
    let autopilotAbort = false;

    // --- 1. СТИЛИЗАЦИЯ И СЕТЬ ---
    function css(el, styles) {
        if (!el) return el;
        for (const [prop, value] of Object.entries(styles)) {
            const kebab = prop.replace(/([A-Z])/g, '-$1').toLowerCase();
            try {
                el.style.setProperty(kebab, value, 'important');
            } catch (e) {
                el.style[prop] = value;
            }
        }
        return el;
    }

    function showToast(msg) {
        try {
            const toast = document.createElement('div');
            toast.className = 'ff-toast-msg';
            toast.textContent = msg;
            css(toast, {
                position: 'fixed',
                top: '24px',
                right: '24px',
                'z-index': '2147483647',
                background: '#1e153b',
                border: '2px solid #9333ea',
                color: '#ffffff',
                padding: '12px 20px',
                'border-radius': '12px',
                'font-size': '13px',
                'font-weight': '600',
                'box-shadow': '0 8px 30px rgba(0,0,0,0.8), 0 0 20px rgba(147,51,234,0.4)',
                'font-family': 'system-ui, -apple-system, sans-serif',
                transition: 'all 0.3s ease'
            });
            (document.body || document.documentElement).appendChild(toast);
            setTimeout(() => {
                toast.style.opacity = '0';
                setTimeout(() => toast.remove(), 300);
            }, 3500);
        } catch (e) {}
    }

    async function req(method, url, data) {
        const fullUrl = FF_HOST + url;
        const bodyStr = data ? JSON.stringify(data) : undefined;
        const headers = {
            'Content-Type': 'application/json',
            'X-Client': 'google-flow'
        };

        if (typeof GM_xmlhttpRequest === 'function') {
            return new Promise((resolve, reject) => GM_xmlhttpRequest({
                method, url: fullUrl, data: bodyStr, headers, timeout: 30000,
                onload(res) {
                    try {
                        const result = JSON.parse(res.responseText);
                        if (res.status < 200 || res.status >= 300) throw new Error(result.error || `HTTP ${res.status}`);
                        resolve(result);
                    } catch (error) { reject(error); }
                },
                onerror: () => reject(new Error('FrameForge недоступен')),
                ontimeout: () => reject(new Error('Таймаут FrameForge; проверьте сохранение перед повтором'))
            }));
        }
        const res = await fetch(fullUrl, {method, headers, body: bodyStr, signal: AbortSignal.timeout(30000)});
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || `HTTP ${res.status}`);
        return result;
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    // --- 2. DOM-СЕЛЕКТОРЫ И СТРУКТУРА GOOGLE FLOW ---

    function querySelectorAllDeep(selector, root = document) {
        let results = Array.from(root.querySelectorAll(selector));
        const all = root.querySelectorAll('*');
        for (const el of all) {
            if (el.shadowRoot) {
                results = results.concat(querySelectorAllDeep(selector, el.shadowRoot));
            }
        }
        return results;
    }

    function visible(el) {
        return !el.closest('#ff-bridge-root') && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    }

    // Поиск поля ввода промпта (ProseMirror внутри flow-rich-text-editor)
    function getPromptInput() {
        const direct = document.querySelector('flow-rich-text-editor div[contenteditable="true"], div.ProseMirror[contenteditable="true"], .ProseMirror');
        if (direct && visible(direct)) return direct;

        const editables = querySelectorAllDeep('flow-rich-text-editor [contenteditable="true"], [contenteditable="true"].ProseMirror, [contenteditable="true"], [role="textbox"]');
        for (const el of editables) {
            if (!visible(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.top >= window.innerHeight * 0.4 && r.width > 30) {
                return el;
            }
        }

        const textareas = querySelectorAllDeep('textarea');
        for (const el of textareas) {
            if (!visible(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.top >= window.innerHeight * 0.4 && r.width > 30) {
                return el;
            }
        }

        return null;
    }

    // Вставка текста в ProseMirror и синхронизация с Angular
    function setPromptText(inputEl, text) {
        if (!inputEl) return;
        inputEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        inputEl.focus();

        const isEditable = inputEl.isContentEditable || inputEl.getAttribute('contenteditable') === 'true' || inputEl.getAttribute('role') === 'textbox';

        if (isEditable) {
            try {
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(inputEl);
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('delete', false, null);
            } catch (e) {}

            // Вставка через DataTransfer ClipboardEvent (ProseMirror обрабатывает нативно)
            let inserted = false;
            try {
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const pasteEv = new ClipboardEvent('paste', {
                    clipboardData: dt,
                    bubbles: true,
                    cancelable: true,
                    composed: true
                });
                inserted = !inputEl.dispatchEvent(pasteEv); // true if prevented (handled)
            } catch (e) {}

            if (!inserted && inputEl.textContent !== text) {
                try {
                    const beforeInputEv = new InputEvent('beforeinput', {
                        bubbles: true,
                        cancelable: true,
                        composed: true,
                        inputType: 'insertText',
                        data: text
                    });
                    inputEl.dispatchEvent(beforeInputEv);
                } catch (e) {}
            }

            if (inputEl.textContent !== text) {
                try {
                    document.execCommand('insertText', false, text);
                } catch (e) {}
            }

            if (inputEl.textContent !== text) {
                inputEl.textContent = text;
                inputEl.innerText = text;
            }
        } else {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set ||
                           Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
            if (setter) setter.call(inputEl, text);
            else inputEl.value = text;
        }

        // Диспатчим события для Angular Change Detection
        inputEl.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
        inputEl.dispatchEvent(new Event('change', { bubbles: true, composed: true }));

        const editorParent = inputEl.closest('flow-rich-text-editor');
        if (editorParent) {
            editorParent.dispatchEvent(new CustomEvent('contentChange', { bubbles: true, composed: true, detail: text }));
        }
    }

    // Поиск кнопки отправки (кнопка внутри flow-generate-icon-button или класс generate-icon-button)
    function getSubmitButton(inputEl) {
        const selector = 'flow-generate-icon-button button, button.generate-icon-button, button[aria-label*="генерац"], button[aria-label*="Generate"], button[aria-label*="Send"], button[type="submit"]';
        const scope = inputEl.closest('form') || document;
        const buttons = querySelectorAllDeep(selector, scope).filter(visible);
        if (buttons.length > 1) throw new Error('Найдено несколько кнопок генерации. Оставьте один активный редактор.');
        return buttons[0];
    }

    async function triggerSubmit(inputEl) {
        const deadline = Date.now() + 15000;
        while (Date.now() < deadline) {
            const btn = getSubmitButton(inputEl);
            if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
                btn.click();
                return;
            }
            await sleep(250);
        }
        throw new Error('Кнопка генерации не найдена или остаётся недоступной.');
    }

    // Определение готовых изображений в галерее Flow
    function getGalleryImages() {
        const imgs = querySelectorAllDeep('img');
        return imgs.filter(img => {
            if (img.closest && img.closest('#ff-bridge-root')) return false;
            const src = img.src || img.getAttribute('src') || '';
            const rect = img.getBoundingClientRect();
            const isGoodSize = rect.width > 80 && rect.height > 80;
            const isNotAvatar = !src.includes('avatar') && !src.includes('profile') && !src.includes('google-logo');
            return visible(img) && img.complete && img.naturalWidth >= 256 && img.naturalHeight >= 256 && isGoodSize && isNotAvatar;
        });
    }

    function imgToBase64(imgEl) {
        return new Promise(async (resolve, reject) => {
            try {
                if (imgEl instanceof HTMLCanvasElement) {
                    resolve(imgEl.toDataURL('image/png'));
                    return;
                }
                const src = imgEl.src || imgEl.getAttribute('src');
                if (!src) return resolve(null);
                if (src.startsWith('data:')) return resolve(src);

                try {
                    const c = document.createElement('canvas');
                    c.width = imgEl.naturalWidth || imgEl.width || 512;
                    c.height = imgEl.naturalHeight || imgEl.height || 512;
                    const ctx = c.getContext('2d');
                    ctx.drawImage(imgEl, 0, 0);
                    const dataUrl = c.toDataURL('image/png');
                    if (dataUrl && dataUrl.length > 1000) {
                        resolve(dataUrl);
                        return;
                    }
                } catch (e) {}

                const res = await fetch(src, {signal: AbortSignal.timeout(30000)});
                if (!res.ok) throw new Error(`Изображение: HTTP ${res.status}`);
                const blob = await res.blob();
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            } catch (err) {
                reject(err);
            }
        });
    }

    // --- 3. ВЫПОЛНЕНИЕ ОДНОЙ ГЕНЕРАЦИИ ---

    async function generateItem(promptText) {
        const input = getPromptInput();
        if (!input) throw new Error('Поле ввода Google Flow не найдено!');

        const initialImages = getGalleryImages();
        const initialSrcs = new Set(initialImages.map(img => img.src || img.getAttribute('src')));

        setPromptText(input, promptText);
        await sleep(400);

        await triggerSubmit(input);
        await sleep(2500);

        let elapsed = 0;
        while (elapsed < 120) {
            await sleep(1000);
            elapsed++;

            const currentImages = getGalleryImages();
            const newImg = currentImages.find(img => {
                const s = img.src || img.getAttribute('src');
                return s && !initialSrcs.has(s);
            });

            if (newImg) {
                showToast('✨ Изображение создано! Сохранение...');
                await sleep(2000);
                return await imgToBase64(newImg);
            }
        }
        throw new Error('Таймаут ожидания генерации изображения в Google Flow.');
    }

    // --- 4. ГЛАВНЫЙ ЦИКЛ АВТОПИЛОТА ---

    async function runAutopilot() {
        if (autopilotRunning) return;
        autopilotRunning = true;
        autopilotAbort = false;
        updateUIState();

        try {
            showToast('🚀 Загрузка проекта из FrameForge...');
            const {project_id: activeProjId} = await req('GET', '/api/projects/current');
            if (!activeProjId) throw new Error('Нет активного проекта во FrameForge.');
            const project = await req('GET', `/api/projects/${activeProjId}`);
            setStatus(`Проект: ${project.title || activeProjId}`);
            let completed = 0;
            for (const kind of ['characters', 'locations', 'scenes']) {
                // Recompile after reference uploads so scene prompts are current.
                const data = await req('GET', `/api/projects/${activeProjId}/prompts`);
                const pending = (data[kind] || []).filter(item => kind === 'scenes' ? !item.has_image : !item.locked);
                for (const item of pending) {
                    if (autopilotAbort) break;
                    const current = await req('GET', '/api/projects/current');
                    if (current.project_id !== activeProjId) throw new Error('Активный проект сменился. Очередь остановлена.');
                    const id = kind === 'scenes' ? item.scene_id : item.id;
                    const prompt = item.flow_prompt || item.prompt;
                    if (!id || !prompt) throw new Error('Неполные данные промпта');
                    setStatus(`${project.title || activeProjId}: ${kind} ${item.name || item.position}; сохранено ${completed}`);
                    const b64 = await generateItem(prompt);
                    await req('POST', `/api/${kind}/${encodeURIComponent(id)}/upload`, {image: b64, lock: true});
                    completed++;
                }
                if (autopilotAbort) break;
            }
            setStatus(autopilotAbort ? `Пауза. Сохранено: ${completed}` : `Готово. Сохранено: ${completed}`);
        } catch (err) {
            setStatus(`Ошибка: ${err.message}`);
            console.error('[FrameForge Autopilot Error]', err);
        } finally {
            autopilotRunning = false;
            autopilotAbort = false;
            updateUIState();
        }
    }

    function setStatus(message) {
        const el = document.getElementById('ff-status');
        if (el) el.textContent = message;
    }

    function updateUIState() {
        const btn = document.getElementById('ff-btn-start');
        if (btn) {
            btn.textContent = autopilotRunning ? 'Пауза после текущего кадра' : '🚀 Запустить автопилот';
            btn.style.background = autopilotRunning ? '#dc2626' : 'linear-gradient(135deg, #9333ea, #6366f1)';
        }
    }

    // --- 5. TRUSTED-TYPES БЕЗОПАСНЫЙ UI ---

    function initUI() {
        if (document.getElementById('ff-bridge-root')) return;

        const targetParent = document.body || document.documentElement;
        if (!targetParent) return;

        try {
            const container = document.createElement('div');
            container.id = 'ff-bridge-root';
            css(container, {
                position: 'fixed',
                bottom: '20px',
                right: '20px',
                'z-index': '2147483647',
                width: '320px',
                background: '#0d101a',
                color: '#f1f3f9',
                border: '2px solid #8b5cf6',
                'border-radius': '16px',
                'box-shadow': '0 12px 50px rgba(0,0,0,0.9), 0 0 30px rgba(139,92,246,0.6)',
                'font-family': 'system-ui, -apple-system, sans-serif',
                'font-size': '13px',
                display: 'flex',
                'flex-direction': 'column',
                overflow: 'hidden',
                'box-sizing': 'border-box'
            });

            const header = document.createElement('div');
            header.id = 'ff-header';
            css(header, {
                padding: '12px 16px',
                background: 'linear-gradient(135deg, #2b1854, #14172b)',
                'border-bottom': '1px solid #384266',
                display: 'flex',
                'align-items': 'center',
                'justify-content': 'space-between',
                cursor: 'pointer',
                'user-select': 'none'
            });

            const title = document.createElement('div');
            title.textContent = '🎬 FrameForge Автопилот v5.2';
            css(title, { 'font-weight': 'bold', color: '#c084fc' });

            header.appendChild(title);

            const body = document.createElement('div');
            css(body, { padding: '14px', display: 'flex', 'flex-direction': 'column', gap: '10px' });

            const startBtn = document.createElement('button');
            startBtn.id = 'ff-btn-start';
            startBtn.textContent = '🚀 Запустить автопилот';
            css(startBtn, {
                padding: '10px 14px',
                'border-radius': '8px',
                border: 'none',
                'font-weight': 'bold',
                cursor: 'pointer',
                background: 'linear-gradient(135deg, #9333ea, #6366f1)',
                color: '#ffffff'
            });

            startBtn.addEventListener('click', () => {
                if (autopilotRunning) {
                    autopilotAbort = true;
                    setStatus('Пауза запрошена. Завершаю и сохраняю текущий кадр…');
                    updateUIState();
                } else {
                    runAutopilot();
                }
            });

            const status = document.createElement('div');
            status.id = 'ff-status';
            status.setAttribute('role', 'status');
            status.textContent = 'Готов к запуску';
            body.appendChild(status);
            body.appendChild(startBtn);
            container.appendChild(header);
            container.appendChild(body);
            targetParent.appendChild(container);
        } catch (e) {
            console.error('[FrameForge Error in initUI]:', e);
        }
    }

    setInterval(() => {
        if (!document.getElementById('ff-bridge-root')) {
            initUI();
        }
    }, 1500);

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initUI);
    } else {
        initUI();
    }
})();
