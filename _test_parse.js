const text = `任显翔：1-4
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

function parseBatchEditorPlan(text) {
  var plan = {};
  var lines = (text || '').split(/\r?\n/);
  var lineRe = /^(.+?)\s*[:：]\s*(.+?)\s*$/;
  lines.forEach(function(raw) {
    var line = raw.trim();
    if (!line) return;
    var m = line.match(lineRe);
    var name, rangeStr;
    if (m) {
      name = m[1].trim();
      rangeStr = m[2].trim();
    } else {
      var idx = line.search(/\d/);
      if (idx <= 0) return;
      name = line.substring(0, idx).trim().replace(/[:：]$/, '').trim();
      rangeStr = line.substring(idx).trim();
    }
    if (!name || !rangeStr) return;
    var tokens = rangeStr.split(/[，,、\s]+/).filter(Boolean);
    tokens.forEach(function(tok) {
      var rng = tok.match(/^(\d{1,4})\s*[-~到至]\s*(\d{1,4})$/);
      if (rng) {
        var a = parseInt(rng[1], 10), b = parseInt(rng[2], 10);
        if (a > b) { var tmp = a; a = b; b = tmp; }
        for (var n = a; n <= b; n++) plan[String(n)] = name;
      } else {
        var single = tok.match(/^(\d{1,4})$/);
        if (single) plan[String(single[1])] = name;
      }
    });
  });
  return plan;
}

var plan = parseBatchEditorPlan(text);
console.log('Total episodes parsed:', Object.keys(plan).length);
['1','4','5','9','10','15','22','27','40','45','76','81'].forEach(ep => {
  console.log('  ep ' + ep + ' -> ' + plan[ep]);
});
var gaps = [];
for (var i = 1; i <= 81; i++) {
  if (plan[String(i)] === undefined) gaps.push(i);
}
console.log('Gaps in 1..81:', gaps);

// Also test fallback: no colon form
var text2 = `张三 1-3, 7
李四 5 9 11
王五：13~15`;
var p2 = parseBatchEditorPlan(text2);
console.log('\nFallback test:');
console.log('  1->', p2['1'], ' 3->', p2['3'], ' 7->', p2['7']);
console.log('  5->', p2['5'], ' 9->', p2['9'], ' 11->', p2['11']);
console.log('  13->', p2['13'], ' 15->', p2['15']);
