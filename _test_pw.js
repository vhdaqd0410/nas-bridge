const { chromium } = require('playwright');

const BATCH_TEXT = `任显翔：1-4
陈陆杰：5-9
陈春阳：10-15
程梦：16-21
张靖杰：22-27
金文龙：28-33
刘梦真：34-39
张淯升：40-45
杨倩：46-51
袁绍杰：52-57
陈浩博：58-63
王田田：64-69
王傲雪：70-75
李钊琦：76-81`;

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  page.on('console', msg => {
    if (msg.type() === 'error') console.log('[browser-error]', msg.text());
  });

  await page.goto('http://127.0.0.1:8089/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Open detail modal
  const detailBtn = page.locator('[data-action="ep-detail"]').first();
  await detailBtn.click();
  await page.waitForTimeout(600);

  // Check batch toggle button exists
  const toggleBtn = page.locator('#ep-batch-toggle');
  console.log('Batch toggle exists:', await toggleBtn.count());

  // Click toggle to show textarea
  await toggleBtn.click();
  await page.waitForTimeout(100);

  const textarea = page.locator('#ep-batch-input');
  console.log('Textarea visible:', await textarea.isVisible());

  // Paste
  await textarea.fill(BATCH_TEXT);
  console.log('Textarea content length:', (await textarea.inputValue()).length);

  // Click apply
  await page.locator('#ep-batch-apply').click();
  await page.waitForTimeout(200);

  // Check result label
  const result = await page.locator('#ep-batch-apply-result').textContent();
  console.log('Apply result:', result);

  // Verify some inputs got filled
  const checkCases = [
    { ep: 1,  expect: '任显翔' },
    { ep: 4,  expect: '任显翔' },
    { ep: 5,  expect: '陈陆杰' },
    { ep: 9,  expect: '陈陆杰' },
    { ep: 40, expect: '张淯升' },
    { ep: 81, expect: '李钊琦' },
  ];

  for (const c of checkCases) {
    const input = page.locator(`.ep-creator-input[data-ep="${c.ep}"]`);
    const val = await input.inputValue();
    console.log(`  ep ${c.ep}: "${val}" (expect "${c.expect}") ${val === c.expect ? '✅' : '❌'}`);
  }

  // Click save
  await page.locator('#ep-detail-save').click();
  await page.waitForTimeout(800);

  // Verify via API
  const apiResp = await page.evaluate(async () => {
    const r = await fetch('/api/project/' + encodeURIComponent('1D-2_10624_《萌宝练气三万层，下山被宠上天》（The Five-Year-Old Archmage Who Saved a Fallen House)') + '/episodes_status');
    return r.json();
  });
  console.log('API editor_plan[1]:', apiResp.editor_plan['1']);
  console.log('API editor_plan[81]:', apiResp.editor_plan['81']);
  console.log('editor_plan total keys:', Object.keys(apiResp.editor_plan).length);

  await browser.close();
})().catch(e => console.error('FATAL:', e));
