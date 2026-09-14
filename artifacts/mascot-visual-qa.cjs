const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.request.post('http://127.0.0.1:8137/api/settings', {
    data: { values: { show_left_sidebar: true, show_right_panel: true, theme_mode: 'light' } },
  });
  await page.goto('http://127.0.0.1:8137/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#composer-companion .harness-companion');
  await page.evaluate(() => { HarnessCompanion.setState('idle'); HarnessCompanion.setState('working'); });
  await page.waitForTimeout(500);
  const initial = await page.evaluate(() => {
    const rect = selector => {
      const r = document.querySelector(selector).getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
    };
    return {
      hosts: document.querySelectorAll('.harness-companion').length,
      robot: rect('#composer-companion'), send: rect('#send'), composer: rect('#composer'),
      railPosition: getComputedStyle(document.querySelector('.companion-rail')).position,
      mouths: document.querySelectorAll('.harness-companion__mouth').length,
      poseParts: [...document.querySelectorAll('.harness-companion__pose')].map(n => n.childNodes.length),
      bubble: rect('#composer-companion .harness-companion__speech'), text: rect('#composer textarea'),
      speech: [...document.querySelectorAll('.harness-companion__speech')].map(n => n.textContent),
      animated: [...document.querySelectorAll('.harness-companion__pose')].map(n => getComputedStyle(n).animationName),
    };
  });
  assert.equal(initial.hosts, 2);
  assert.ok(Math.abs(initial.robot.x + initial.robot.width / 2 - initial.send.x - initial.send.width / 2) < 1);
  assert.ok(initial.robot.bottom < initial.send.y);
  // La fascia e' fuori dal flusso: il riquadro di scrittura non cresce per
  // farle posto, la mascotte gli poggia sopra.
  assert.equal(initial.railPosition, 'absolute');
  assert.ok(Math.abs(initial.robot.bottom - initial.composer.y) < 6);
  // Fedelta' al simbolo: testa con antenna e occhi, niente bocca.
  assert.equal(initial.mouths, 0);
  assert.deepEqual(initial.poseParts, [2, 2]);
  assert.ok(initial.bubble.bottom < initial.text.y);
  assert.deepEqual(initial.animated, ['companion-working', 'companion-working']);
  assert.deepEqual(initial.speech, ['Ci penso io…', 'Ci penso io…']);
  await page.screenshot({ path: 'artifacts/mascot-light.png' });
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; HarnessCompanion.setState('success'); });
  await page.waitForTimeout(420);
  await page.screenshot({ path: 'artifacts/mascot-dark-success.png' });
  await page.locator('#composer textarea').fill('Un messaggio su più righe\nper verificare che la mascotte\ne la nuvoletta restino sopra al testo.');
  await page.evaluate(() => { HarnessCompanion.setState('idle'); HarnessCompanion.setState('error'); });
  await page.waitForTimeout(450);
  const multiline = await page.evaluate(() => ({
    bubbleBottom: document.querySelector('#composer-companion .harness-companion__speech').getBoundingClientRect().bottom,
    textTop: document.querySelector('#composer textarea').getBoundingClientRect().top,
    gesture: document.querySelector('#composer-companion svg').dataset.gesture,
  }));
  assert.ok(multiline.bubbleBottom < multiline.textTop);
  assert.equal(multiline.gesture, 'shake');
  await page.screenshot({ path: 'artifacts/mascot-multiline.png' });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.evaluate(() => HarnessCompanion.setState('working'));
  const reduced = await page.locator('.harness-companion__pose').evaluateAll(nodes => nodes.map(n => getComputedStyle(n).animationName));
  assert.deepEqual(reduced, ['none', 'none']);
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.setViewportSize({ width: 900, height: 720 });
  await page.locator('#toggle-sidebar').click();
  await page.evaluate(() => { HarnessCompanion.setState('idle'); HarnessCompanion.setState('welcome'); });
  await page.waitForTimeout(500);
  assert.ok(await page.locator('#composer-companion').isVisible());
  await page.screenshot({ path: 'artifacts/mascot-compact.png' });
  await page.waitForTimeout(3600);
  const speechHidden = await page.locator('.harness-companion__speech').evaluateAll(nodes => nodes.every(n => n.hidden));
  assert.ok(speechHidden);
  assert.deepEqual(errors, []);
  await page.locator('#toggle-sidebar').click();
  await page.evaluate(() => HarnessCompanion.setState('error'));
  await page.waitForTimeout(450);
  const narrowLogo = await page.evaluate(() => ({
    bubble: document.querySelector('#sidebar .harness-companion__speech').getBoundingClientRect().right,
    edge: document.querySelector('#sidebar').getBoundingClientRect().right,
    wordmarkOpacity: getComputedStyle(document.querySelector('#sidebar .logo + div')).opacity,
  }));
  assert.ok(narrowLogo.bubble < narrowLogo.edge);
  assert.equal(narrowLogo.wordmarkOpacity, '0');
  await page.screenshot({ path: 'artifacts/mascot-narrow-error.png' });
  await page.goto('http://127.0.0.1:8137/brand/index.html', { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: 'Completato', exact: true }).click();
  await page.waitForTimeout(420);
  await page.locator('.motion').screenshot({ path: 'artifacts/mascot-demo.png' });
  const report = { initial, multiline, reduced, speechHidden, narrowLogo, errors };
  fs.writeFileSync('artifacts/mascot-visual-qa.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
