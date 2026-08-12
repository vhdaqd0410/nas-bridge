const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  page.on('console', async msg => {
    const text = msg.text();
    if (text.includes('ep-') || text.includes('summary') || text.includes('episode')) {
      console.log('[' + msg.type() + ']', text.substring(0, 300));
    }
  });

  await page.goto('http://127.0.0.1:8089/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  // Call /episodes_status API from page to see what it returns
  const apiResult = await page.evaluate(async () => {
    // Find first 剪辑中 project name from DOM
    const firstEdit = document.querySelector('[data-custom-status="剪辑中"]');
    if (!firstEdit) return { error: 'no edit project found' };
    const projectName = firstEdit.dataset.project || firstEdit.querySelector?.('.name')?.textContent?.trim() || '';
    // Try from a card
    const epProj = document.querySelector('[data-ep-project]');
    const name = epProj ? epProj.dataset.epProject : '';
    if (!name) return { error: 'no project name' };
    const r = await fetch('/api/project/' + encodeURIComponent(name) + '/episodes_status');
    const j = await r.json();
    return { project: name, ok: j.ok, total: j.total, missing_len: j.missing?.length, current_count: j.current_count, has_editor_plan: !!j.editor_plan };
  });
  console.log('\n=== /episodes_status API result ===');
  console.log(JSON.stringify(apiResult, null, 2));

  // Check if there's any project_type filter issue in loadAllEpisodeSummary
  const hasGroupProjects = await page.evaluate(() => {
    return window.allData?.projects?.filter(p => p.custom_status === '剪辑中').map(p => ({ name: p.name.substring(0,30), type: p.project_type }));
  });
  console.log('\n=== 剪辑中 projects in allData ===');
  console.log(JSON.stringify(hasGroupProjects, null, 2));

  // After wait, check what _episodeStatusCache contains
  await page.waitForTimeout(2000);
  const cacheState = await page.evaluate(() => {
    const out = {};
    for (const k of Object.keys(window._episodeStatusCache || {})) {
      const v = window._episodeStatusCache[k];
      out[k.substring(0,30)] = { ok: v.ok, total: v.total, missing_len: v.missing?.length };
    }
    return out;
  });
  console.log('\n=== _episodeStatusCache after 2s ===');
  console.log(JSON.stringify(cacheState, null, 2));

  // Check if there are missing .ep-person-nums spans (old class name!)
  const oldStyleExists = await page.locator('.ep-missing-nums').count();
  console.log('\n=== Old style elements left ===');
  console.log('.ep-missing-nums:', oldStyleExists);

  await browser.close();
})().catch(e => console.error('FATAL:', e));
