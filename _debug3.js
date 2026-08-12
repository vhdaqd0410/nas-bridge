const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  await page.goto('http://127.0.0.1:8089/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  const debug = await page.evaluate(() => {
    if (!window.allData) return { error: 'no allData' };
    const projs = window.allData.projects || [];
    const editProjs = projs.filter(p => (p.custom_status || '') === '剪辑中');
    return {
      total: projs.length,
      editCount: editProjs.length,
      editProjs: editProjs.map(p => ({
        name: p.name.substring(0, 40),
        custom_status: p.custom_status,
        project_type: p.project_type,
        total_episodes: p.total_episodes,
        current_episodes: p.current_episodes
      }))
    };
  });
  console.log('=== allData 剪辑中项目 ===');
  console.log(JSON.stringify(debug, null, 2));

  // Manually call loadAllEpisodeSummary by calling fetchEpisodeStatus for each
  await page.evaluate(async () => {
    if (!window.allData) return;
    const projs = window.allData.projects || [];
    const editProjs = projs.filter(p => (p.custom_status || '') === '剪辑中');
    console.log('\nManually fetching ' + editProjs.length + ' projects...');
    for (const p of editProjs) {
      // Use module-level function by finding it... we can't access it directly
      // Instead, call via API
      const r = await fetch('/api/project/' + encodeURIComponent(p.name) + '/episodes_status');
      const j = await r.json();
      console.log('  ' + p.name.substring(0, 35) + ' -> ok=' + j.ok + ' total=' + j.total + ' missing=' + j.missing?.length);
    }
  });

  await page.waitForTimeout(1000);

  const texts = await page.locator('.ep-missing-summary').allTextContents();
  console.log('\n=== summaries after manual fetch ===');
  texts.forEach((t,i) => console.log(`  [${i}] "${t?.substring(0,120)}"`));

  const cacheKeys = await page.evaluate(() => Object.keys(window._episodeStatusCache || {}).length);
  console.log('\n=== _episodeStatusCache keys ===', cacheKeys);

  await browser.close();
})().catch(e => console.error('FATAL:', e));
