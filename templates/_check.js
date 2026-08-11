// Browser API stubs
var document = { getElementById: function() { return { value: '', textContent: '', innerHTML: '', style: {}, classList: { add: function(){}, remove: function(){}, toggle: function(){} }, addEventListener: function(){}, getAttribute: function(){ return null; }, onclick: null }; }, querySelectorAll: function() { return []; }, querySelector: function() { return null; }, addEventListener: function(){} };
var fetch = function() { return Promise.resolve({ json: function() { return Promise.resolve({}); } }); };
var setInterval = function() {};
var setTimeout = function(f, t) { if (typeof f === 'function') f(); };
var console = { error: function(){}, log: function(){} };
var navigator = { clipboard: null };
var confirm = function() { return false; };
var encodeURIComponent = function(s) { return s; };
var JSON = { stringify: function(o) { return JSON.stringify(o); }, parse: function(s) { return JSON.parse(s); } };
var Promise = global.Promise;
var parseInt = global.parseInt;
var Math = global.Math;
var String = global.String;
var Date = global.Date;

// Actual JS code:

  var currentProject = '';
  var allData = { production: [], group_all: [] };
  var collapsedDepts = {};
  var sortMode = 'name';
  var filterDelivery = '';
  var filterGroup = '';

  // 排序权重：交付状态排序时的优先级
  var deliveryOrder = { 'delivered': 0, 'partial': 1, 'delivering': 2, 'error': 3, 'pending': 4 };

  function showToast(msg) {
    var el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    setTimeout(function() { el.classList.remove('show'); }, 2500);
  }

  function api(method, url, body) {
    var opts = { method: method };
    if (body) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function(r) { return r.json(); });
  }

  function htm(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  // ========== Build card HTML ==========

  function buildCard(p) {
    var syncTag = '<span class="tag tag-pending">待同步</span>';
    if (p.sync_status === 'syncing') syncTag = '<span class="tag tag-syncing">同步中</span>';
    else if (p.sync_status === 'synced') syncTag = '<span class="tag tag-synced">已同步</span>';

    var deliveryTag = '<span class="tag tag-pending">待交付</span>';
    if (p.delivery_status === 'delivered') deliveryTag = '<span class="tag tag-delivered">已交付</span>';
    else if (p.delivery_status === 'delivering') deliveryTag = '<span class="tag tag-syncing">回传中</span>';
    else if (p.delivery_status === 'partial') deliveryTag = '<span class="tag tag-partial">部分交付</span>';
    else if (p.delivery_status === 'error') deliveryTag = '<span class="tag tag-error">回传失败</span>';

    var special = p.is_special ? ' <span class="tag tag-special">特殊</span>' : '';
    var progress = p.sync_progress
      ? '<span class="progress-text">' + htm(p.sync_progress) + '</span>' : '';

    var groupBadge = '';
    if (p.project_type === 'group') {
      groupBadge = p.has_production_match
        ? ' <span class="tag tag-group">已在制作部</span>'
        : ' <span class="tag tag-group">仅组内</span>';
    } else if (p.on_group) {
      groupBadge = ' <span class="tag tag-group">已在组盘</span>';
    } else {
      groupBadge = ' <span class="tag tag-nogroup">未拷贝</span>';
    }

    var actionsHtml = '';
    if (p.project_type === 'production') {
      actionsHtml =
        '<button class="btn btn-primary btn-sm" data-action="sync" data-project="' + htm(p.name) + '">同步素材</button> ' +
        '<button class="btn btn-secondary btn-sm" data-action="files" data-project="' + htm(p.name) + '">查看成片</button>';
    } else {
      // group projects on O-drive — only show output files
      actionsHtml =
        '<button class="btn btn-secondary btn-sm" data-action="files" data-project="' + htm(p.name) + '">查看成片</button>';
    }

    return '<div class="project-card">'
      + '<div class="card-top">'
      + '<div class="name">' + htm(p.name) + special + groupBadge + '</div>'
      + '</div>'
      + '<div class="status-row">'
      + '<div class="status-item"><div class="status-label">素材同步</div><div class="status-value">' + syncTag + progress + '</div></div>'
      + '<div class="status-item"><div class="status-label">成片交付</div><div class="status-value">' + deliveryTag + '</div></div>'
      + '</div>'
      + '<div class="time-row">同步: ' + htm(p.last_synced_at || '从未') + '<br>交付: ' + htm(p.last_delivered_at || '从未') + '</div>'
      + '<div class="card-actions">' + actionsHtml + '</div>'
      + '</div>';
  }

  // ========== Render all ==========

  function naturalCompare(a, b) {
    var re = /(\d+)/;
    var ax = [], bx = [];
    a = String(a); b = String(b);
    while (a.length && b.length) {
      var am = a.match(re), bm = b.match(re);
      if (!am || !bm) { ax.push(a); bx.push(b); break; }
      var ai = am.index, bi = bm.index;
      if (ai !== 0 || bi !== 0) {
        var min = Math.min(ai, bi);
        ax.push(a.substring(0, min)); bx.push(b.substring(0, min));
        a = a.substring(min); b = b.substring(min);
      } else {
        ax.push(parseInt(am[0])); bx.push(parseInt(bm[0]));
        a = a.substring(am[0].length); b = b.substring(bm[0].length);
      }
    }
    while (ax.length && bx.length) {
      var an = ax.shift(), bn = bx.shift();
      if (typeof an === 'number' && typeof bn === 'number') {
        if (an !== bn) return an - bn;
      } else {
        an = String(an).toLowerCase(); bn = String(bn).toLowerCase();
        if (an < bn) return -1;
        if (an > bn) return 1;
      }
    }
    return ax.length - bx.length;
  }

  function sortProjects(list) {
    if (sortMode === 'name') {
      list.sort(function(a, b) { return naturalCompare(a.name, b.name); });
    } else if (sortMode === 'delivery') {
      list.sort(function(a, b) {
        var oa = deliveryOrder[a.delivery_status] || 99;
        var ob = deliveryOrder[b.delivery_status] || 99;
        if (oa !== ob) return oa - ob;
        return naturalCompare(a.name, b.name);
      });
    } else if (sortMode === 'delivered_time') {
      list.sort(function(a, b) {
        var da = a.last_delivered_at || '';
        var db = b.last_delivered_at || '';
        if (da === db) return naturalCompare(a.name, b.name);
        if (!da) return 1;
        if (!db) return -1;
        return da < db ? 1 : -1; // 最近的排前面
      });
    }
  }

  function matchFilter(p) {
    if (filterDelivery && p.delivery_status !== filterDelivery) return false;
    if (filterGroup === 'on_group' && !p.on_group) return false;
    if (filterGroup === 'not_group' && p.on_group) return false;
    return true;
  }

  function renderAll() {
    var keyword = (document.getElementById('search').value || '').trim().toLowerCase();

    // Filter + search
    var filteredProduction = [];
    var filteredGroupOnly = [];

    for (var i = 0; i < allData.production.length; i++) {
      var p = allData.production[i];
      if ((!keyword || p.name.toLowerCase().indexOf(keyword) !== -1) && matchFilter(p)) {
        filteredProduction.push(p);
      }
    }
    for (var i = 0; i < allData.group_all.length; i++) {
      var p = allData.group_all[i];
      if ((!keyword || p.name.toLowerCase().indexOf(keyword) !== -1) && matchFilter(p)) {
        filteredGroupOnly.push(p);
      }
    }

    // Stats
    var total = allData.production.length + allData.group_all.length;
    var shown = filteredProduction.length + filteredGroupOnly.length;
    document.getElementById('search-stats').textContent =
      (keyword || filterDelivery || filterGroup ? '匹配 ' + shown + ' / ' : '共 ') + total + ' 个项目' +
      '（制作部 ' + allData.production.length + ' + 组内NAS ' + allData.group_all.length + '）';

    // Sort
    sortProjects(filteredProduction);
    sortProjects(filteredGroupOnly);

    // Group production by department
    var deptGroups = {};
    for (var i = 0; i < filteredProduction.length; i++) {
      var d = filteredProduction[i].department || '其他';
      if (!deptGroups[d]) deptGroups[d] = [];
      deptGroups[d].push(filteredProduction[i]);
    }

    var html = '';

    // -- Group-only section FIRST (at top) --
    if (filteredGroupOnly.length > 0 || !keyword) {
      var gCollapsed = '__group__' in collapsedDepts ? collapsedDepts['__group__'] : false;
      html += '<div class="dept-section">'
        + '<div class="dept-header" data-dept="__group__">'
        + '<span class="dept-arrow' + (gCollapsed ? ' collapsed' : '') + '">&#9660;</span>'
        + '<span class="dept-name">组内NAS</span>'
        + '<span class="dept-badge dept-badge-group">' + filteredGroupOnly.length + ' 个项目</span>'
        + '</div>'
        + '<div class="dept-body' + (gCollapsed ? ' hidden' : '') + '" data-dept-body="__group__">';
      if (filteredGroupOnly.length === 0 && (keyword || filterDelivery || filterGroup)) {
        html += '<div class="empty" style="grid-column:1/-1"><div style="font-size:13px">没有匹配的项目</div></div>';
      } else {
        for (var k = 0; k < filteredGroupOnly.length; k++) {
          html += buildCard(filteredGroupOnly[k]);
        }
      }
      html += '</div></div>';
    }

    // -- Production sections by department --
    var deptKeys = Object.keys(deptGroups).sort();
    for (var di = 0; di < deptKeys.length; di++) {
      var dept = deptKeys[di];
      var items = deptGroups[dept];
      var collapsed = dept in collapsedDepts ? collapsedDepts[dept] : true;
      html += '<div class="dept-section">'
        + '<div class="dept-header" data-dept="' + htm(dept) + '">'
        + '<span class="dept-arrow' + (collapsed ? ' collapsed' : '') + '">&#9660;</span>'
        + '<span class="dept-name">' + htm(dept) + '</span>'
        + '<span class="dept-badge">' + items.length + ' 个项目</span>'
        + '</div>'
        + '<div class="dept-body' + (collapsed ? ' hidden' : '') + '" data-dept-body="' + htm(dept) + '">';
      for (var j = 0; j < items.length; j++) {
        html += buildCard(items[j]);
      }
      html += '</div></div>';
    }

    if (deptKeys.length === 0 && !keyword && !filterDelivery && !filterGroup) {
      html += '<div class="empty"><div class="empty-icon">&#128193;</div><div>暂无制作部项目，点击右上角"刷新项目"扫描 NAS</div></div>';
    }

    if (deptKeys.length === 0 && filteredGroupOnly.length === 0 && (keyword || filterDelivery || filterGroup)) {
      html += '<div class="empty"><div class="empty-icon">&#128269;</div><div>没有找到匹配的项目</div></div>';
    }

    document.getElementById('project-sections').innerHTML = html;
  }

  // ========== Load ==========

  function loadProjects() {
    api('GET', '/api/projects').then(function(data) {
      allData = data;
      renderAll();
    }).catch(function(e) { console.error(e); });
  }

  // ========== Scan ==========

  function scanProjects() {
    showToast('正在扫描所有 NAS 源...');
    api('POST', '/api/scan').then(function(data) {
      if (data.ok) {
        showToast('扫描完成：制作部 ' + data.count + ' 个项目');
        loadProjects();
      } else {
        showToast('扫描失败: ' + (data.message || '未知错误'));
      }
    }).catch(function() { showToast('扫描失败，请检查 NAS 连接'); });
  }

  // ========== Sync ==========

  function syncProject(name) {
    if (!confirm('确认要将"' + name + '"完整同步到组内 NAS 吗？\n\n这会拷贝整个项目目录，耗时取决于项目大小。')) return;
    showToast('同步已启动...');
    api('POST', '/api/sync/' + encodeURIComponent(name)).then(function() {
      showToast('素材同步已启动，请稍后查看状态');
      setTimeout(loadProjects, 3000);
    }).catch(function() { showToast('操作失败'); });
  }

  // ========== Files modal ==========

  var batchTimer = null;
  var batchProject = '';

  function showFiles(name) {
    currentProject = name;
    document.getElementById('modal-title').textContent = name + ' - 成片文件';
    document.getElementById('modal').classList.add('active');
    document.getElementById('file-list').innerHTML = '<li class="loading">加载中...</li>';
    document.getElementById('modal-toolbar').style.display = 'none';
    document.getElementById('batch-progress').style.display = 'none';
    document.getElementById('batch-result-actions').style.display = 'none';
    document.getElementById('progress-fill').style.background = '#0071e3';
    document.getElementById('select-all').checked = false;
    stopBatchPolling();

    api('GET', '/api/output_files/' + encodeURIComponent(name)).then(function(files) {
      var list = document.getElementById('file-list');
      var toolbar = document.getElementById('modal-toolbar');
      document.getElementById('file-count').textContent = '共 ' + files.length + ' 个文件';

      if (!files.length) {
        list.innerHTML = '<li style="padding:24px;text-align:center;color:#86868b;font-size:13px">暂无成片文件</li>';
        return;
      }

      toolbar.style.display = 'flex';
      var html = '';
      for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var isVideo = ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v'].indexOf(f.ext.toLowerCase()) !== -1;
        var nameHtml = isVideo
          ? '<div class="file-name clickable" data-preview="' + htm(f.name) + '">' + htm(f.name) + '</div>'
          : '<div class="file-name">' + htm(f.name) + '</div>';
        html += '<li class="file-item">'
          + '<input type="checkbox" class="file-checkbox" data-fname="' + htm(f.name) + '" onchange="updateBatchButton()">'
          + '<div class="file-info">'
          + nameHtml
          + '<div class="file-meta">' + f.size_mb + ' MB | ' + htm(f.ext) + ' | ' + htm(f.mtime) + '</div>'
          + '</div>'
          + (isVideo ? '<button class="btn btn-secondary btn-sm" data-action="preview" data-project="' + htm(f.name) + '">预览</button> ' : '')
          + '<button class="btn btn-primary btn-sm" data-action="deliver" data-project="' + htm(f.name) + '">回传</button>'
          + '</li>';
      }
      list.innerHTML = html;
      updateBatchButton();
    }).catch(function() {
      document.getElementById('file-list').innerHTML = '<li class="loading">加载失败</li>';
    });
  }

  function toggleSelectAll() {
    var checked = document.getElementById('select-all').checked;
    var boxes = document.querySelectorAll('.file-checkbox');
    for (var i = 0; i < boxes.length; i++) { boxes[i].checked = checked; }
    updateBatchButton();
  }

  function updateBatchButton() {
    var checked = document.querySelectorAll('.file-checkbox:checked').length;
    var btn = document.getElementById('btn-batch-deliver');
    btn.textContent = checked ? '批量回传 (' + checked + ')' : '批量回传';
    btn.disabled = !checked;
  }

  function batchDeliver() {
    var boxes = document.querySelectorAll('.file-checkbox:checked');
    if (!boxes.length) return;
    var names = [];
    for (var i = 0; i < boxes.length; i++) { names.push(boxes[i].getAttribute('data-fname')); }

    if (!confirm('确认批量回传 ' + names.length + ' 个文件到制作部 NAS ？')) return;

    batchProject = currentProject;
    document.getElementById('batch-progress').style.display = 'block';
    document.getElementById('progress-fill').style.width = '0%';
    document.getElementById('progress-fill').style.background = '#0071e3';
    document.getElementById('progress-text').textContent = '准备中...';
    document.getElementById('batch-result-actions').style.display = 'none';
    document.getElementById('btn-batch-deliver').disabled = true;

    api('POST', '/api/deliver_batch/' + encodeURIComponent(batchProject), { file_names: names }).then(function(data) {
      if (!data.ok) { showToast('批量回传失败: ' + data.message); return; }
      showToast('批量回传已启动 (' + data.total + ' 个文件)');
      startBatchPolling();
    }).catch(function() { showToast('操作失败'); });
  }

  function startBatchPolling() {
    stopBatchPolling();
    batchTimer = setInterval(function() {
      api('GET', '/api/project/' + encodeURIComponent(batchProject) + '/progress').then(function(data) {
        if (!data.ok) return;
        var pct = 0;
        var m = (data.sync_progress || '').match(/回传\s+(\d+)\/(\d+)/);
        if (m) {
          pct = Math.round(parseInt(m[1]) / parseInt(m[2]) * 100);
          document.getElementById('progress-fill').style.width = pct + '%';
          document.getElementById('progress-text').textContent = '回传中 ' + m[1] + ' / ' + m[2] + ' (' + pct + '%)';
        }
        // 完成：sync_progress 清空 + delivery_status 为 delivered
        if (!data.sync_progress && data.delivery_status === 'delivered') {
          document.getElementById('progress-fill').style.width = '100%';
          document.getElementById('progress-text').textContent = '✅ 全部回传完成';
          stopBatchPolling();
          showToast('批量回传完成');
          showDeliveryResultActions();
          loadProjects();
        }
        // 失败：全部回传失败
        if (data.delivery_status === 'error') {
          document.getElementById('progress-fill').style.width = '100%';
          document.getElementById('progress-fill').style.background = '#ff3b30';
          document.getElementById('progress-text').textContent = '❌ ' + (data.sync_progress || '全部回传失败');
          stopBatchPolling();
          showToast('批量回传失败');
          updateBatchButton();
          loadProjects();
        }
      });
    }, 1500);
  }

  function showDeliveryResultActions() {
    // 获取目标路径和源路径，显示按钮
    var actionsDiv = document.getElementById('batch-result-actions');
    var btnOpenDest = document.getElementById('btn-open-dest');
    var btnCopyDest = document.getElementById('btn-copy-dest');
    var btnOpenSource = document.getElementById('btn-open-source');
    var btnCopySource = document.getElementById('btn-copy-source');

    // 获取目标路径
    api('GET', '/api/project/' + encodeURIComponent(batchProject) + '/dest_dir').then(function(data) {
      if (data.ok) {
        var destDir = data.dest_dir;
        btnOpenDest.style.display = '';
        btnCopyDest.style.display = '';
        btnOpenDest.onclick = function() {
          api('POST', '/api/project/' + encodeURIComponent(batchProject) + '/open_folder', { which: 'dest' }).then(function(r) {
            if (!r.ok) showToast('打开失败: ' + r.message);
          });
        };
        btnCopyDest.onclick = function() {
          copyToClipboard(destDir);
        };
      } else {
        btnOpenDest.style.display = 'none';
        btnCopyDest.style.display = 'none';
      }
    });

    // 获取源路径
    api('GET', '/api/project/' + encodeURIComponent(batchProject) + '/source_dir').then(function(data) {
      if (data.ok) {
        var sourceDir = data.source_dir;
        btnOpenSource.style.display = '';
        btnCopySource.style.display = '';
        btnOpenSource.onclick = function() {
          api('POST', '/api/project/' + encodeURIComponent(batchProject) + '/open_folder', { which: 'source' }).then(function(r) {
            if (!r.ok) showToast('打开失败: ' + r.message);
          });
        };
        btnCopySource.onclick = function() {
          copyToClipboard(sourceDir);
        };
      } else {
        btnOpenSource.style.display = 'none';
        btnCopySource.style.display = 'none';
      }
    });

    actionsDiv.style.display = 'flex';
    updateBatchButton();
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function() {
        showToast('已复制: ' + text);
      }).catch(function() {
        fallbackCopy(text);
      });
    } else {
      fallbackCopy(text);
    }
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      showToast('已复制: ' + text);
    } catch (e) {
      showToast('复制失败，请手动复制: ' + text);
    }
    document.body.removeChild(ta);
  }

  function stopBatchPolling() {
    if (batchTimer) { clearInterval(batchTimer); batchTimer = null; }
  }

  function deliverFile(fileName) {
    if (!confirm('确认将 "' + fileName + '" 回传到制作部 NAS ？')) return;
    showToast('正在回传...');
    api('POST', '/api/deliver/' + encodeURIComponent(currentProject), { file_path: fileName }).then(function(data) {
      if (data.ok) {
        // 显示成功状态和路径按钮
        document.getElementById('batch-progress').style.display = 'block';
        document.getElementById('progress-fill').style.width = '100%';
        document.getElementById('progress-fill').style.background = '#0071e3';
        document.getElementById('progress-text').textContent = '✅ 回传成功: ' + fileName;
        batchProject = currentProject;
        showDeliveryResultActions();
      } else {
        showToast('回传失败: ' + data.message);
      }
      loadProjects();
    }).catch(function() { showToast('操作失败'); });
  }

  function closeModal() {
    document.getElementById('modal').classList.remove('active');
    stopBatchPolling();
  }

  // ========== Video preview ==========

  function previewFile(fileName) {
    var overlay = document.getElementById('preview-overlay');
    var video = document.getElementById('preview-video');
    var title = document.getElementById('preview-title');
    title.textContent = fileName;
    video.src = '/api/preview/' + encodeURIComponent(currentProject) + '/' + encodeURIComponent(fileName);
    overlay.classList.add('active');
    video.play().catch(function() {});
  }

  function closePreview() {
    var overlay = document.getElementById('preview-overlay');
    var video = document.getElementById('preview-video');
    video.pause();
    video.src = '';
    overlay.classList.remove('active');
  }

  // ========== Department collapse ==========

  function toggleDept(dept) {
    collapsedDepts[dept] = !collapsedDepts[dept];
    var body = document.querySelector('[data-dept-body="' + dept.replace(/"/g, '\\"') + '"]');
    var arrow = document.querySelector('[data-dept="' + dept.replace(/"/g, '\\"') + '"] .dept-arrow');
    if (body) body.classList.toggle('hidden');
    if (arrow) arrow.classList.toggle('collapsed');
  }

  // ========== Event delegation ==========

  document.addEventListener('click', function(e) {
    // Department header click
    var hdr = e.target.closest('.dept-header');
    if (hdr) {
      toggleDept(hdr.getAttribute('data-dept'));
      return;
    }

    // Preview clickable file name
    var previewName = e.target.getAttribute('data-preview');
    if (previewName) {
      previewFile(previewName);
      return;
    }

    var btn = e.target.closest('button');
    if (!btn) return;
    var action = btn.getAttribute('data-action');
    var project = btn.getAttribute('data-project');
    if (action === 'sync' && project) syncProject(project);
    else if (action === 'files' && project) showFiles(project);
    else if (action === 'deliver' && project) deliverFile(project);
    else if (action === 'preview' && project) previewFile(project);
  });

  document.getElementById('modal').addEventListener('click', function(e) {
    if (e.target === this || e.target.id === 'modal-close') closeModal();
  });

  document.getElementById('preview-overlay').addEventListener('click', function(e) {
    if (e.target === this || e.target.id === 'preview-close') closePreview();
  });

  document.getElementById('btn-scan').addEventListener('click', scanProjects);
  document.getElementById('btn-batch-deliver').addEventListener('click', batchDeliver);

  // Sort & filter
  document.getElementById('sort-select').addEventListener('change', function() {
    sortMode = this.value;
    renderAll();
  });
  document.getElementById('filter-delivery').addEventListener('change', function() {
    filterDelivery = this.value;
    renderAll();
  });
  document.getElementById('filter-group').addEventListener('change', function() {
    filterGroup = this.value;
    renderAll();
  });

  // Search
  document.getElementById('search').addEventListener('input', function() {
    renderAll();
  });

  // ========== Logs & status ==========

  function loadLogs() {
    api('GET', '/api/logs?limit=50').then(function(logs) {
      var container = document.getElementById('logs');
      if (!logs.length) {
        container.innerHTML = '<div class="loading">暂无日志</div>';
        return;
      }
      var html = '';
      for (var i = 0; i < logs.length; i++) {
        var l = logs[i];
        var typeText = l.type === 'sync' ? '同步' : '交付';
        html += '<div class="log-item">'
          + '<span class="log-time">' + htm(l.created_at) + '</span>'
          + '<span class="log-type log-type-' + l.type + '">' + typeText + '</span>'
          + '<span class="log-project" title="' + htm(l.project_name) + '">' + htm(l.project_name) + '</span>'
          + '<span class="log-message ' + htm(l.status) + '">' + htm(l.title) + ' - ' + htm(l.message) + '</span>'
          + '</div>';
      }
      container.innerHTML = html;
    }).catch(function() {});
  }

  function loadStatus() {
    api('GET', '/api/status').then(function(status) {
      var dot = document.getElementById('watcher-dot');
      var text = document.getElementById('watcher-status');
      if (status.watcher_enabled) {
        text.textContent = '监听中 (' + status.watched_dirs + '个目录)';
        dot.classList.remove('offline');
      } else {
        text.textContent = '监听已禁用';
        dot.classList.add('offline');
      }
    }).catch(function() {});
  }

  loadProjects();
  loadLogs();
  loadStatus();
  setInterval(function() { loadProjects(); loadLogs(); loadStatus(); }, 8000);
