// ===== DOM Elements =====
const $ = s => document.querySelector(s);
const uploadArea = $('#uploadArea'), dataFile = $('#dataFile'), filterFile = $('#filterFile');
const btnAnalyze = $('#btnAnalyze'), btnFilter = $('#btnFilter');
const loading = $('#loading'), chartsSection = $('#chartsSection'), summaryPanel = $('#summaryPanel');
const filterLabel = $('#filterLabel');

// ===== File Upload =====
uploadArea.addEventListener('click', () => dataFile.click());
uploadArea.addEventListener('dragover', e => { e.preventDefault(); uploadArea.classList.add('dragover'); });
uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
uploadArea.addEventListener('drop', e => {
  e.preventDefault(); uploadArea.classList.remove('dragover');
  if (e.dataTransfer.files.length) { dataFile.files = e.dataTransfer.files; onDataFile(); }
});
dataFile.addEventListener('change', onDataFile);
function onDataFile() {
  if (dataFile.files.length) {
    uploadArea.classList.add('has-file');
    uploadArea.querySelector('p').textContent = '✅ ' + dataFile.files[0].name;
    btnAnalyze.disabled = false;
  }
}
filterFile.addEventListener('change', () => {
  if (filterFile.files.length) {
    filterLabel.classList.add('has-file');
    filterLabel.textContent = '✅ ' + filterFile.files[0].name;
  }
});

// ===== Actions =====
btnAnalyze.addEventListener('click', async () => {
  if (!dataFile.files.length) return;
  showLoading();
  const fd = new FormData();
  fd.append('datafile', dataFile.files[0]);
  if (filterFile.files.length) fd.append('filterfile', filterFile.files[0]);
  fd.append('top_n', $('#topN').value);
  try {
    const res = await fetch('/api/analyze', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { alert(data.error); hideLoading(); return; }
    renderAll(data);
  } catch (e) { alert('分析失败: ' + e.message); }
  hideLoading();
});

btnFilter.addEventListener('click', async () => {
  showLoading();
  try {
    const res = await fetch('/api/filter_by_date', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start: $('#dateStart').value, end: $('#dateEnd').value,
        top_n: parseInt($('#topN').value)
      })
    });
    const data = await res.json();
    if (data.error) { alert(data.error); hideLoading(); return; }
    renderAll(data);
  } catch (e) { alert('筛选失败: ' + e.message); }
  hideLoading();
});

function showLoading() { loading.style.display = 'block'; chartsSection.style.display = 'none'; }
function hideLoading() { loading.style.display = 'none'; }

// ===== Color Palette =====
const TEAL = '#009688', ORANGE = '#e65100', BLUE = '#1565c0', RED = '#c62828', PURPLE = '#6a1b9a';
const TEAL2 = 'rgba(0,150,136,0.15)', ORANGE2 = 'rgba(230,81,0,0.15)';
const GRAY = '#666';

// ===== Render All =====
function renderAll(data) {
  // Summary
  summaryPanel.style.display = '';
  $('#statTotal').textContent = data.summary.total.toLocaleString();
  $('#statPass').textContent = data.summary.pass.toLocaleString();
  $('#statFail').textContent = data.summary.fail.toLocaleString();
  $('#statRate').textContent = data.summary.fail_rate + '%';

  // Date range
  if (data.date_range && data.date_range.min) {
    $('#dateStart').value = data.date_range.min;
    $('#dateEnd').value = data.date_range.max;
    $('#dateHint').textContent = `数据范围: ${data.date_range.min} ~ ${data.date_range.max}`;
    btnFilter.disabled = false;
  }

  chartsSection.style.display = '';
  renderCorrelation(data.correlation_top);
  renderPassNG(data.pass_ng_comparison);
  renderHardware(data.hardware_attribution);
  renderBoxplot(data.boxplot_data);
  renderTrend(data.ng_trend);
  renderStation(data.station_dist, data.mode_dist);
  renderTable(data.all_scores || data.correlation_top);
  
  // Call LLM Summary
  fetchLLMSummary(data);
}

// ===== LLM Summary Fetching =====
async function fetchLLMSummary(analysisData) {
  const section = $('#llmSection');
  const status = $('#llmStatus');
  const output = $('#llmOutput');
  
  section.style.display = 'block';
  status.style.display = 'flex';
  output.innerHTML = '';
  
  try {
    const res = await fetch('/api/get_llm_summary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(analysisData)
    });
    const data = await res.json();
    status.style.display = 'none';
    
    if (data.error) {
      output.innerHTML = `<p style="color:var(--red)">${data.error}</p>`;
    } else {
      // Simple Markdown parser for specific structure
      output.innerHTML = parseMarkdown(data.summary);
    }
  } catch (e) {
    status.style.display = 'none';
    output.innerHTML = `<p style="color:var(--red)">总结生成请求失败: ${e.message}</p>`;
  }
}

function parseMarkdown(md) {
  return md
    .replace(/### (.*)/g, '<h3>$1</h3>')
    .replace(/\*\*(.*?)\*\*/g, '<b>$1</b>')
    .replace(/- (.*)/g, '<li>$1</li>')
    .replace(/\n\n/g, '<br/>')
    .replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>')
    .replace(/<\/ul><ul>/g, '');
}

// ===== Chart: Correlation Top N =====
function renderCorrelation(items) {
  const ch = echarts.init($('#chartCorrelation'));
  const names = items.map(i => shortName(i.feature)).reverse();
  const vals = items.map(i => i.score).reverse();
  ch.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: p => `${items[items.length-1-p[0].dataIndex].feature}<br/>Score: <b>${p[0].value}</b>` },
    grid: { left: 160, right: 60, top: 20, bottom: 30 },
    xAxis: { type: 'value', max: 1, axisLabel: { color: GRAY } },
    yAxis: { type: 'category', data: names, axisLabel: { color: '#333', fontSize: 11 } },
    series: [{
      type: 'bar', data: vals, barWidth: 22,
      itemStyle: { borderRadius: [0, 4, 4, 0], color: ORANGE },
      label: { show: true, position: 'right', formatter: '{c}', color: ORANGE, fontSize: 12, fontWeight: 600 }
    }]
  });
  window.addEventListener('resize', () => ch.resize());
}

// ===== Chart: Pass/NG Comparison =====
function renderPassNG(items) {
  const ch = echarts.init($('#chartPassNG'));
  if (!items.length) { ch.setOption({ title:{text:'无分类数据',left:'center',top:'center',textStyle:{color:GRAY}}}); return; }
  const item = items[0];
  ch.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['Pass (合格)', 'Fail (不良)'], top: 0 },
    grid: { left: 50, right: 30, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: item.categories, axisLabel: { color: GRAY, rotate: 20, fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { color: GRAY } },
    series: [
      { name: 'Pass (合格)', type: 'bar', data: item.pass_values, barGap: '10%',
        itemStyle: { color: TEAL, borderRadius: [3,3,0,0] } },
      { name: 'Fail (不良)', type: 'bar', data: item.fail_values,
        itemStyle: { color: ORANGE, borderRadius: [3,3,0,0] } }
    ]
  });
  window.addEventListener('resize', () => ch.resize());
}

// ===== Chart: Hardware Attribution =====
function renderHardware(items) {
  const ch = echarts.init($('#chartHardware'));
  if (!items.length) { ch.setOption({title:{text:'无数据',left:'center',top:'center',textStyle:{color:GRAY}}}); return; }
  const top = items.slice(0, 8);
  const names = top.map(i => i.value + '\n(' + shortName(i.column) + ')');
  ch.setOption({
    tooltip: { trigger: 'axis', formatter: p => {
      const d = top[p[0].dataIndex];
      return `${d.column}<br/>${d.value}<br/>NG: <b style="color:${ORANGE}">${d.ng_count}</b> / ${d.total}<br/>NG率: <b style="color:${RED}">${d.ng_rate}%</b>`;
    }},
    grid: { left: 50, right: 30, top: 30, bottom: 80 },
    xAxis: { type: 'category', data: names, axisLabel: { color: GRAY, fontSize: 9, interval: 0, rotate: 25 } },
    yAxis: { type: 'value', name: 'NG Count', nameTextStyle: { color: GRAY },
      axisLabel: { color: GRAY } },
    series: [{
      type: 'bar', data: top.map(i => i.ng_count), barWidth: 30,
      itemStyle: { borderRadius: [4,4,0,0], color: ORANGE },
      label: { show: true, position: 'top', formatter: p => top[p.dataIndex].ng_rate+'%',
        color: RED, fontSize: 11, fontWeight: 600 }
    }]
  });
  window.addEventListener('resize', () => ch.resize());
}

// ===== Chart: Boxplot =====
function renderBoxplot(items) {
  const ch = echarts.init($('#chartBoxplot'));
  if (!items.length) {
    ch.setOption({title:{text:'无连续变量数据',left:'center',top:'center',textStyle:{color:GRAY}}});
    return;
  }
  const names = items.map(i => shortName(i.feature));
  const passData = items.map(i => i.pass);
  const failData = items.map(i => i.fail);
  ch.setOption({
    tooltip: { trigger: 'item',
      formatter: p => `${p.seriesName}<br/>Max: ${p.value[5]}<br/>Q3: ${p.value[4]}<br/>Median: ${p.value[3]}<br/>Q1: ${p.value[2]}<br/>Min: ${p.value[1]}` },
    legend: { data: ['Pass (合格)', 'Fail (不良)'], top: 0 },
    grid: { left: 55, right: 30, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: names, axisLabel: { color: GRAY, fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { color: GRAY } },
    series: [
      { name: 'Pass (合格)', type: 'boxplot', data: passData,
        itemStyle: { color: TEAL2, borderColor: TEAL } },
      { name: 'Fail (不良)', type: 'boxplot', data: failData,
        itemStyle: { color: ORANGE2, borderColor: ORANGE } }
    ]
  });
  window.addEventListener('resize', () => ch.resize());
}

// ===== Chart: NG Trend =====
function renderTrend(trend) {
  const ch = echarts.init($('#chartTrend'));
  if (!trend.dates.length) { ch.setOption({title:{text:'无时间数据',left:'center',top:'center',textStyle:{color:GRAY}}}); return; }
  ch.setOption({
    tooltip: { trigger: 'axis', formatter: p => {
      const i = p[0].dataIndex;
      return `${trend.dates[i]}<br/>总数: ${trend.totals[i]}<br/>Fail: <b style="color:${ORANGE}">${trend.fails[i]}</b><br/>不良率: <b style="color:${RED}">${trend.rates[i]}%</b>`;
    }},
    legend: { data: ['Fail数量', '不良率(%)'], top: 0 },
    grid: { left: 50, right: 50, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: trend.dates, axisLabel: { color: GRAY, fontSize: 10, rotate: 30 } },
    yAxis: [
      { type: 'value', name: 'Fail数', nameTextStyle: { color: GRAY }, axisLabel: { color: GRAY } },
      { type: 'value', name: '不良率(%)', nameTextStyle: { color: GRAY }, axisLabel: { color: GRAY }, splitLine: { show: false } }
    ],
    series: [
      { name: 'Fail数量', type: 'bar', data: trend.fails,
        itemStyle: { color: ORANGE, borderRadius: [3,3,0,0] } },
      { name: '不良率(%)', type: 'line', yAxisIndex: 1, data: trend.rates, smooth: true,
        lineStyle: { color: RED, width: 2 }, itemStyle: { color: RED },
        areaStyle: { color: 'rgba(198,40,40,0.08)' } }
    ]
  });
  window.addEventListener('resize', () => ch.resize());
}

// ===== Chart: Station Distribution =====
function renderStation(stationDist, modeDist) {
  const ch = echarts.init($('#chartStation'));
  const colors = [TEAL, ORANGE, BLUE, PURPLE, RED, '#f9a825', '#2e7d32', '#d81b60'];
  const sData = Object.entries(stationDist).map(([k,v], i) => ({ name: k, value: v, itemStyle: { color: colors[i%colors.length] } }));
  const mData = Object.entries(modeDist).map(([k,v], i) => ({ name: k, value: v, itemStyle: { color: colors[(i+3)%colors.length] } }));
  ch.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { orient: 'vertical', right: 10, top: 'center', textStyle: { color: GRAY, fontSize: 10 } },
    series: [
      { name: 'Failed Station', type: 'pie', radius: ['25%','45%'], center: ['35%','45%'],
        data: sData, label: { color: '#333', fontSize: 10 },
        emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } } },
      { name: 'Failure Mode', type: 'pie', radius: ['55%','75%'], center: ['35%','45%'],
        data: mData, label: { color: '#333', fontSize: 10 },
        emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } } }
    ]
  });
  window.addEventListener('resize', () => ch.resize());
}

// ===== Table =====
function renderTable(scores) {
  const tbody = $('#corrTable tbody');
  tbody.innerHTML = scores.map((s, i) =>
    `<tr><td>${i+1}</td><td title="${s.feature}">${s.feature}</td><td>${s.score}</td><td>${s.type==='categorical'?'分类':'连续'}</td><td>${s.method}</td></tr>`
  ).join('');
}

// ===== Utility =====
function shortName(name) {
  return name.replace(/^VCM_/, '').replace(/_/g, ' ').replace(/Staging time$/, 'ST');
}
