const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  page.on('console', msg => {
    console.log('[' + msg.type() + ']', msg.text().substring(0, 400));
  });

  await page.goto('http://127.0.0.1:8089/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  // Inject console.trace on fetchEpisodeStatus
  await page.evaluate(() => {
    const origFetch = window.fetchEpisodeStatus;
    if (origFetch) {
      window.fetchEpisodeStatus = function(name) {
        console.log('[TRACE] fetchEpisodeStatus called for:', name?.substring(0,50));
        return origFetch.call(this, name);
      };
      console.log('[TRACE] wrapped fetchEpisodeStatus');
    } else {
      console.log('[TRACE] fetchEpisodeStatus NOT FOUND on window');
    }
    // Check allData
    console.log('[TRACE] typeof allData:', typeof window.allData);
    // Try to find the closure variable
    console.log('[TRACE] window keys:', Object.keys(window).filter(k => k.includes('allData') || k.includes('project')).slice(0,20));
  });

  // Now click ep-detail to see if the page works at all
  const firstDetail = page.locator('[data-action="ep-detail"]').first();
  console.log('\n[TRACE] ep-detail buttons found:', await firstDetail.count());
  if (await firstDetail.count() > 0) {
    const name = await firstDetail.getAttribute('data-project');
    console.log('[TRACE] first ep-detail project:', name?.substring(0,60));
    // Click it
    await firstDetail.click();
    await page.waitForTimeout(3000);

    // Then check cache after modal opened
    await page.evaluate(() => {
      console.log('[TRACE] cache keys after detail click:', Object.keys(window._episodeStatusCache || {}).length);
    });
  }

  // Also manually trigger loadAllEpisodeSummary
  await page.evaluate(() => {
    // Call the function by finding it from somewhere
    if (window.loadAllEpisodeSummary) {
      console.log('[TRACE] calling loadAllEpisodeSummary manually');
      window.loadAllEpisodeSummary();
    } else {
      // Try using the already-defined function name from inline script scope
      // It won't be on window, so we need another way
      console.log('[TRACE] loadAllEpisodeSummary not on window');
    }
  });

  await page.waitForTimeout(3000);

  // Check summaries
  const texts = await page.locator('.ep-missing-summary').allTextContents();
  console.log('\n[TRACE] summaries after wait:');
  texts.forEach((t,i) => console.log(`  [${i}] "${t?.substring(0,100)}"`));

  await browser.close();
})().catch(e => console.error('FATAL:', e));
