const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.on('console', msg => {
    if (msg.type() === 'error' || msg.type() === 'warning') console.log('[' + msg.type() + ']', msg.text().substring(0, 200));
  });

  await page.goto('http://127.0.0.1:8089/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  const allSummaries = page.locator('.ep-missing-summary');
  const totalCards = await page.locator('.episode-row').count();
  console.log('=== 初始状态 ===');
  console.log('剪辑中卡片数:', totalCards);
  console.log('ep-summary 元素数:', await allSummaries.count());

  // 1. 初次渲染后，所有 summary 都不应是 "加载中..." 占位
  const initialTexts = await allSummaries.allTextContents();
  let badInitial = initialTexts.filter(t => t.includes('加载中'));
  console.log('初始带"加载中"的:', badInitial.length, badInitial.map(s => s.substring(0, 50)));

  // 等 summaries 被异步数据填好
  await page.waitForTimeout(3000);

  // 2. 检查新排版结构: .ep-person-grid 和 .ep-person-row 存在
  const hasGrid = await page.locator('.ep-person-grid').count();
  const hasRow = await page.locator('.ep-person-row').count();
  const hasNameTag = await page.locator('.ep-person-name').count();
  const hasRange = await page.locator('.ep-person-range').count();
  console.log('\n=== 排版结构 ===');
  console.log('  .ep-person-grid:', hasGrid);
  console.log('  .ep-person-row:', hasRow);
  console.log('  .ep-person-name:', hasNameTag);
  console.log('  .ep-person-range:', hasRange);

  // 3. 关键验证：模拟用户等两个轮询周期（约 25 秒），每 8 秒检查一次
  console.log('\n=== 常驻验证（跨轮询周期） ===');
  for (let round = 0; round < 3; round++) {
    await page.waitForTimeout(9000);  // 8 秒轮询 + 1 秒缓冲
    const texts = await allSummaries.allTextContents();
    const flashy = texts.filter(t => t.includes('加载中') || t.includes('请先设置'));
    const withContent = texts.filter(t => t.trim().length > 0).length;
    console.log(`  Round ${round+1}: 有内容的 summary=${withContent}/${texts.length}, 带占位=${flashy.length}`);
    if (flashy.length > 0) {
      console.log('    ❌ 这些 summary 闪回了占位:', flashy.map(s => s.substring(0, 60)));
    }
  }

  // 4. 验证 person-row 内容：应该能看到中文人名 + 范围
  const firstPersonRow = await page.locator('.ep-person-row').first();
  if (await firstPersonRow.count() > 0) {
    const rowText = await firstPersonRow.textContent();
    console.log('\n=== 首个 person-row 内容 ===');
    console.log('  文本:', rowText?.substring(0, 80));
    const nameTag = await firstPersonRow.locator('.ep-person-name').textContent();
    const rangeTag = await firstPersonRow.locator('.ep-person-range').textContent();
    console.log('  人名:', nameTag, '范围:', rangeTag);
    const nameCorrect = /[\u4e00-\u9fa5]{2,}/.test(nameTag || '');
    const rangeCorrect = /\d/.test(rangeTag || '');
    console.log('  人名正确:', nameCorrect, '范围正确:', rangeCorrect);
  }

  await browser.close();
  console.log('\n✅ 全部验证完成');
})().catch(e => console.error('FATAL:', e));
