import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {chromium} from 'playwright-core';
import {MAX_SCENES, SECONDS_PER_FRAME, recommendedSceneCount, storyboardTiming, readingDurationSeconds} from '../imagegen/static/ui-config.mjs';

const root = new URL('../', import.meta.url).pathname.replace(/^\/(.:)/, '$1');
const python = 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe';
const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';

function startFixture() {
  return new Promise((resolve, reject) => {
    const process = spawn(python, ['tests/ui_fixture_server.py'], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
    let output = '';
    const timer = setTimeout(() => reject(new Error('UI fixture did not start: ' + output)), 15000);
    process.stdout.on('data', data => {
      output += data.toString();
      const match = output.match(/READY (http:\/\/\S+) (\S+)/);
      if (match) { clearTimeout(timer); resolve({process, url: match[1], projectId: match[2]}); }
    });
    process.stderr.on('data', data => { output += data.toString(); });
    process.on('exit', code => { if (code && !output.includes('READY')) reject(new Error(output)); });
  });
}

test('расчёт картинок независим от шестисекундной анимации', () => {
  const story = Array.from({length: 155}, (_, index) => `Предложение ${index + 1}.`).join(' ');
  const recommended = recommendedSceneCount(story);
  assert.equal(SECONDS_PER_FRAME, 4.5);
  assert.equal(recommended, Math.ceil(readingDurationSeconds(story) / SECONDS_PER_FRAME));
  assert.notEqual(recommended, 24);
  assert.equal(MAX_SCENES, 2000);
  const timing = storyboardTiming(story, recommended);
  assert.equal(timing.sentences, 155);
  assert.equal(timing.seconds, recommended * 4.5);
  assert.equal(timing.fixedTiming, true);
});

test('основные кнопки и центральная сетка работают в настоящем браузере', {timeout: 60000}, async t => {
  const fixture = await startFixture();
  t.after(() => fixture.process.kill());
  const browser = await chromium.launch({headless: true, executablePath: edge});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  await page.goto(fixture.url);
  await page.getByTestId('style-select').waitFor();

  assert.equal(await page.locator('#style-select option').count(), 6);
  assert.equal(await page.getByTestId('style-medieval').textContent(), 'Средневековая киноживопись (Medieval Epic)');
  assert.equal(await page.locator('[data-testid^="mode-"]').count(), 4);
  await page.locator('#style-select').selectOption('noir');
  await page.getByTestId('mode-illustration').click();
  await page.getByTestId('story-input').fill('Короткая история для проверки интерфейса.');
  await page.getByTestId('auto-scene-count').click();
  assert.equal(await page.getByTestId('scene-count').getAttribute('max'), '2000');
  await page.getByTestId('scene-count').evaluate(node => { node.value = '6'; node.dispatchEvent(new Event('input', {bubbles:true})); });
  await page.getByTestId('generate-project').click();
  await page.getByTestId('operation-progress').waitFor();
  assert.ok(await page.locator('[data-testid="operation-progress"] .progress-track').count() >= 1);

  await page.locator('#project-select').selectOption(fixture.projectId);
  await page.getByRole('heading', {name:'Готовый тестовый проект'}).waitFor();
  // Fixture contains two character candidates, two location candidates and one prop.
  assert.equal(await page.locator('.candidate-grid .candidate').count(), 5);
  assert.equal(await page.locator('.workspace-title').filter({hasText:'Локации'}).count(), 1);
  assert.equal(await page.locator('.workspace-title').filter({hasText:'Реквизит и классификация'}).count(), 1);
  assert.equal(await page.locator('.scene-card').count(), 4);
  assert.ok(await page.locator('.scene-visual img').count() >= 4);
  await page.screenshot({path: root + 'tests/ui-preview.png', fullPage: true});
  await page.locator('.candidate-grid .candidate').first().click();
  // Selecting one reference does not clear the other reference group's selection.
  assert.equal(await page.locator('.candidate-grid .candidate.selected').count(), 2);
  const subjectTestId=await page.locator('.subject-pill').first().getAttribute('data-testid');
  await page.getByTestId('regenerate-' + subjectTestId.replace('subject-','')).click();
  await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Два варианта поставлены в очередь'));
  assert.ok(await page.locator('.scene-card').count() >= 4);

  assert.equal(await page.locator('.scene-card .scene-actions').count(), 0);
  assert.equal(await page.locator('.scene-card .scene-props-tags').count(), 0);
  assert.equal(await page.locator('[data-testid^="regenerate-scene-"]').count(), 4);
  assert.equal(await page.locator('[data-testid^="animate-scene-"]').count(), 4);
  // The per-card control is compact until its native disclosure is opened.
  const choice = page.getByTestId('omni-select-1');
  assert.equal(await choice.count(), 1);
  assert.equal(await choice.isChecked(), false);
  assert.equal(await page.getByTestId('ken-burns-motion-1').count(), 1);
  assert.equal(await page.getByTestId('veo-motion-1').count(), 0);
  assert.match(await page.getByTestId('scene-anim-badge-1').textContent(), /2.5D/);
  await choice.check();
  assert.match(await page.getByTestId('scene-anim-badge-1').textContent(), /Omni/);
  const disclosure = page.getByTestId('scene-animation-disclosure-1');
  await disclosure.evaluate(node => { node.open = true; });
  await page.getByTestId('omni-resolution-1').waitFor({state:'visible'});
  assert.equal(await page.getByTestId('omni-resolution-1').inputValue(), '720p');
  assert.equal(await page.getByTestId('omni-duration-1').inputValue(), '6');
  assert.equal(await page.getByTestId('btn-animate-omni-1').isDisabled(), false);
  assert.equal(await page.getByTestId('veo-motion-1').count(), 0);
});

test('варианты кадра и полноэкранный просмотр работают корректно', {timeout: 60000}, async t => {
  const fixture = await startFixture();
  t.after(() => fixture.process.kill());
  const browser = await chromium.launch({headless: true, executablePath: edge});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  await page.goto(fixture.url);

  await page.locator('#project-select').selectOption(fixture.projectId);
  await page.getByRole('heading', {name:'Готовый тестовый проект'}).waitFor();

  // 1. Scene 1 has 2 variants rendered
  const variantsStrip = page.getByTestId('scene-variants-1');
  await variantsStrip.waitFor();
  assert.equal(await variantsStrip.locator('.scene-variant-item').count(), 2);

  // Variant 1 is active, Variant 2 is candidate
  const var1 = page.getByTestId('variant-item-1-1');
  const var2 = page.getByTestId('variant-item-1-2');
  assert.match(await var1.getAttribute('class'), /is-active/);
  assert.doesNotMatch(await var2.getAttribute('class'), /is-active/);
  assert.equal(await var1.locator('.scene-variant-badge').innerText(), '✓ Выбран');
  assert.equal(await var2.locator('.scene-variant-select-btn').innerText(), 'Выбрать');

  // 2. Select Variant 2
  await var2.locator('.scene-variant-select-btn').click();
  await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('выбран вариант №2'));

  // Variant 2 is now active, persisted after refresh
  await page.reload();
  await page.locator('#project-select').selectOption(fixture.projectId);
  await page.getByRole('heading', {name:'Готовый тестовый проект'}).waitFor();
  const reloadedVar1 = page.getByTestId('variant-item-1-1');
  const reloadedVar2 = page.getByTestId('variant-item-1-2');
  assert.doesNotMatch(await reloadedVar1.getAttribute('class'), /is-active/);
  assert.match(await reloadedVar2.getAttribute('class'), /is-active/);

  // 3. Fullscreen Lightbox Preview on main image click
  const scene1VisualImg = page.locator('.scene-card[data-scene-id]').first().locator('.scene-visual > img');
  await scene1VisualImg.click();
  const viewer = page.locator('#fullscreen-viewer');
  await viewer.waitFor();
  assert.match(await viewer.getAttribute('class'), /open/);
  assert.ok(await viewer.locator('.fullscreen-img').count() >= 1);

  // Close via Escape
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('#fullscreen-viewer')?.classList.contains('open'));

  // Open via variant thumbnail click
  await reloadedVar1.locator('.scene-variant-thumb').click();
  await viewer.waitFor();
  assert.match(await viewer.getAttribute('class'), /open/);

  // Close via Close Button
  await viewer.locator('.fullscreen-close').click();
  await page.waitForFunction(() => !document.querySelector('#fullscreen-viewer')?.classList.contains('open'));
});
