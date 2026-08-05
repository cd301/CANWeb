/* ── CANWeb Frontend ──────────────────────────────────────────────────────── */
'use strict';

// ── Socket.IO ──────────────────────────────────────────────────────────────
const socket = io();

// ── State ──────────────────────────────────────────────────────────────────
let rawMode = 'scroll';           // 'scroll' | 'refresh'
let rawPaused = false;
let rawFilter = '';
let rawRows = [];                 // For refresh mode: {id -> tr}
const MAX_SCROLL_ROWS = 500;

let signalLatest = {};            // {signalKey: {value, unit, ts}}
let signalRows = {};              // {signalKey: tr}

// Plots state
let plotTabs = [];                // [{id, title, plots: [{id, title, type, signal}]}]
let activePlotTabId = null;
let plotInstances = {};           // {plotId: {layout, traces}}
let plotUpdateTimers = {};        // {plotId: intervalId}
const PLOT_REFRESH_MS = 500;

let periodicTxTimer = null;

// Config
let appConfig = { plots: [], interface: 'sim' };

// ── Helpers ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
function formatHex(hex) {
  return hex.match(/.{1,2}/g)?.join(' ') ?? hex;
}
function nowStr() {
  return new Date().toISOString().slice(11, 23);
}

// ── Tab Navigation ─────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    $('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'plots') refreshAllPlots();
  });
});

// ── Stats Updates ──────────────────────────────────────────────────────────
socket.on('stats', stats => {
  $('stat-rx').textContent = 'RX: ' + stats.rx.toLocaleString();
  $('stat-errors').textContent = 'ERR: ' + stats.errors;
  $('stat-load').textContent = 'Load: ' + stats.bus_load + '%';
  $('stat-baud').textContent = (stats.bitrate / 1000) + ' kbps';
});

// ── Raw Traffic ────────────────────────────────────────────────────────────
socket.on('can_frame', frame => {
  if (rawPaused) return;

  if (rawFilter && frame.id.toLowerCase() !== rawFilter.toLowerCase()) return;

  if (rawMode === 'scroll') {
    appendScrollRow(frame);
  } else {
    updateRefreshRow(frame);
  }

  // Update decoded signals table
  if (frame.decoded) {
    const msg = frame.decoded.name;
    Object.entries(frame.decoded.signals).forEach(([sig, val]) => {
      const key = msg + '.' + sig;
      signalLatest[key] = { value: val, ts: frame.ts };
      updateSignalRow(key, val, frame.ts);
    });
  }
});

function appendScrollRow(frame) {
  const tbody = $('raw-body');
  const tr = buildRawRow(frame);
  tbody.appendChild(tr);
  // Prune
  while (tbody.rows.length > MAX_SCROLL_ROWS) {
    tbody.deleteRow(0);
  }
  // Auto-scroll
  const container = $('raw-container');
  container.scrollTop = container.scrollHeight;
}

function updateRefreshRow(frame) {
  const tbody = $('raw-body');
  if (rawRows[frame.id]) {
    const tr = rawRows[frame.id];
    fillRawRow(tr, frame);
  } else {
    const tr = buildRawRow(frame);
    tbody.appendChild(tr);
    rawRows[frame.id] = tr;
  }
}

function buildRawRow(frame) {
  const tr = document.createElement('tr');
  fillRawRow(tr, frame);
  return tr;
}

function fillRawRow(tr, frame) {
  const dec = frame.decoded;
  const decStr = dec
    ? Object.entries(dec.signals).map(([k, v]) => `${k}=${v}`).join('  ')
    : '';
  tr.innerHTML = `
    <td>${frame.ts.toFixed(3)}</td>
    <td class="td-id">${frame.id}</td>
    <td>${frame.dlc}</td>
    <td class="td-data${frame.error ? ' td-error' : ''}">${formatHex(frame.data)}</td>
    <td>${frame.ext ? 'EXT' : 'STD'}</td>
    <td style="color:var(--warn)">${dec ? '[' + dec.name + '] ' + decStr : ''}</td>
  `;
  if (frame.error) tr.style.background = 'rgba(248,81,73,0.08)';
}

// ── Raw Controls ───────────────────────────────────────────────────────────
document.querySelectorAll('input[name="raw-mode"]').forEach(r => {
  r.addEventListener('change', e => {
    rawMode = e.target.value;
    rawRows = {};
    $('raw-body').innerHTML = '';
  });
});

$('btn-clear-raw').addEventListener('click', () => {
  $('raw-body').innerHTML = '';
  rawRows = {};
  fetch('/api/buffer/clear', { method: 'POST' });
});

$('btn-pause-raw').addEventListener('click', function() {
  rawPaused = !rawPaused;
  this.textContent = rawPaused ? 'Resume' : 'Pause';
  this.style.color = rawPaused ? 'var(--warn)' : '';
});

$('btn-export-csv').addEventListener('click', () => {
  window.location = '/api/export/csv';
});

$('raw-filter').addEventListener('input', e => {
  rawFilter = e.target.value.trim();
  rawRows = {};
  $('raw-body').innerHTML = '';
});

// ── Decoded Signals ────────────────────────────────────────────────────────
function updateSignalRow(key, value, ts) {
  const tbody = $('signal-body');
  if (signalRows[key]) {
    const tds = signalRows[key].querySelectorAll('td');
    tds[1].textContent = typeof value === 'number' ? value.toFixed(4) : value;
    tds[3].textContent = ts.toFixed(3);
  } else {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${key}</td>
      <td>${typeof value === 'number' ? value.toFixed(4) : value}</td>
      <td>-</td>
      <td>${ts.toFixed(3)}</td>
    `;
    tbody.appendChild(tr);
    signalRows[key] = tr;
  }
}

// DBC Upload
$('dbc-upload-form').addEventListener('submit', async e => {
  e.preventDefault();
  const file = $('dbc-file').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch('/api/dbc/upload', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.ok) {
    const msgs = data.messages.map(m => `${m.id} ${m.name} (${m.signals.join(', ')})`).join('\n');
    $('dbc-info').textContent = `Loaded: ${file.name}  •  ${data.messages.length} messages`;
    // Populate plot signal selector
    refreshSignalSelector();
  } else {
    $('dbc-info').textContent = 'Error: ' + data.error;
  }
});

$('btn-clear-dbc').addEventListener('click', async () => {
  await fetch('/api/dbc/clear', { method: 'POST' });
  $('dbc-info').textContent = 'DBC cleared.';
  $('signal-body').innerHTML = '';
  signalRows = {};
  signalLatest = {};
  refreshSignalSelector();
});

async function refreshSignalSelector() {
  const res = await fetch('/api/signals');
  const signals = await res.json();
  const sel = $('plot-signal');
  sel.innerHTML = '<option value="">-- select --</option>';
  signals.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s;
    sel.appendChild(opt);
  });
}

// ── Plots ──────────────────────────────────────────────────────────────────
function renderPlotTabs() {
  const nav = $('plot-tabs-nav');
  const content = $('plot-tabs-content');
  nav.innerHTML = '';
  content.innerHTML = '';

  plotTabs.forEach(tab => {
    // Nav button
    const btn = document.createElement('button');
    btn.className = 'plot-tab-btn' + (tab.id === activePlotTabId ? ' active' : '');
    btn.textContent = tab.title;
    btn.addEventListener('click', () => {
      activePlotTabId = tab.id;
      renderPlotTabs();
      refreshAllPlots();
    });
    nav.appendChild(btn);

    // Add close button per tab
    const closeBtn = document.createElement('button');
    closeBtn.textContent = '×';
    closeBtn.style.cssText = 'font-size:11px;padding:2px 6px;margin-left:-4px;';
    closeBtn.addEventListener('click', e => {
      e.stopPropagation();
      closePlotTab(tab.id);
    });
    nav.appendChild(closeBtn);

    // Pane
    const pane = document.createElement('div');
    pane.className = 'plot-tab-pane' + (tab.id === activePlotTabId ? ' active' : '');
    pane.id = 'pane-' + tab.id;

    tab.plots.forEach(plot => {
      const card = document.createElement('div');
      card.className = 'plot-card';
      card.innerHTML = `
        <div class="plot-card-header">
          <span class="plot-card-title">${plot.title} <small style="color:var(--text-dim)">[${plot.type}] ${plot.signal}</small></span>
          <button class="btn-remove-plot" data-plot-id="${plot.id}">Remove</button>
        </div>
        <div class="plot-container" id="plotdiv-${plot.id}"></div>
      `;
      card.querySelector('.btn-remove-plot').addEventListener('click', () => {
        removePlot(tab.id, plot.id);
      });
      pane.appendChild(card);
    });

    content.appendChild(pane);
  });

  // Initialise / refresh plots for active tab
  if (activePlotTabId) {
    const tab = plotTabs.find(t => t.id === activePlotTabId);
    if (tab) tab.plots.forEach(p => initPlot(p));
  }
}

function initPlot(plot) {
  const div = document.getElementById('plotdiv-' + plot.id);
  if (!div) return;
  Plotly.purge(div);
  const layout = {
    paper_bgcolor: '#161b22',
    plot_bgcolor: '#0d1117',
    font: { color: '#c9d1d9', size: 11 },
    margin: { l: 50, r: 20, t: 20, b: 40 },
    xaxis: { gridcolor: '#30363d', zerolinecolor: '#30363d' },
    yaxis: { gridcolor: '#30363d', zerolinecolor: '#30363d' },
    showlegend: false,
  };
  Plotly.newPlot(div, [{ x: [], y: [], type: plot.type === 'histogram' ? 'histogram' : 'scatter',
    mode: plot.type === 'line' ? 'lines' : 'markers',
    marker: { color: '#58a6ff' },
    line: { color: '#58a6ff' },
  }], layout, { responsive: true, displayModeBar: false });

  // Start live update
  if (plotUpdateTimers[plot.id]) clearInterval(plotUpdateTimers[plot.id]);
  plotUpdateTimers[plot.id] = setInterval(() => updatePlot(plot), PLOT_REFRESH_MS);
}

async function updatePlot(plot) {
  if (!plot.signal) return;
  try {
    const res = await fetch('/api/signals/' + encodeURIComponent(plot.signal));
    const data = await res.json();
    const pts = data.points;
    if (!pts || pts.length === 0) return;

    const div = document.getElementById('plotdiv-' + plot.id);
    if (!div || !div._fullLayout) return;

    if (plot.type === 'histogram') {
      Plotly.restyle(div, { x: [pts.map(p => p[1])] }, 0);
    } else {
      Plotly.restyle(div, {
        x: [pts.map(p => new Date(p[0] * 1000).toISOString().slice(11, 23))],
        y: [pts.map(p => p[1])],
      }, 0);
    }
  } catch (e) {
    // ignore network errors
  }
}

function refreshAllPlots() {
  const tab = plotTabs.find(t => t.id === activePlotTabId);
  if (!tab) return;
  tab.plots.forEach(p => {
    const div = document.getElementById('plotdiv-' + p.id);
    if (div && !div._fullLayout) initPlot(p);
    else updatePlot(p);
  });
}

function removePlot(tabId, plotId) {
  if (plotUpdateTimers[plotId]) {
    clearInterval(plotUpdateTimers[plotId]);
    delete plotUpdateTimers[plotId];
  }
  const tab = plotTabs.find(t => t.id === tabId);
  if (tab) tab.plots = tab.plots.filter(p => p.id !== plotId);
  renderPlotTabs();
}

function closePlotTab(tabId) {
  const tab = plotTabs.find(t => t.id === tabId);
  if (tab) tab.plots.forEach(p => {
    if (plotUpdateTimers[p.id]) clearInterval(plotUpdateTimers[p.id]);
  });
  plotTabs = plotTabs.filter(t => t.id !== tabId);
  if (activePlotTabId === tabId) activePlotTabId = plotTabs[0]?.id ?? null;
  renderPlotTabs();
}

// Add plot tab button
$('btn-add-tab').addEventListener('click', () => {
  const id = 'tab_' + Date.now();
  const title = 'Tab ' + (plotTabs.length + 1);
  plotTabs.push({ id, title, plots: [] });
  activePlotTabId = id;
  renderPlotTabs();
  // Open add-plot modal
  openAddPlotModal();
});

// Modal
function openAddPlotModal() {
  refreshSignalSelector();
  $('modal-add-plot').classList.remove('hidden');
  $('modal-backdrop').classList.remove('hidden');
}
function closeModal() {
  $('modal-add-plot').classList.add('hidden');
  $('modal-backdrop').classList.add('hidden');
}

$('btn-modal-cancel').addEventListener('click', closeModal);
$('modal-backdrop').addEventListener('click', closeModal);

$('btn-modal-ok').addEventListener('click', () => {
  const title = $('plot-title').value || 'Plot';
  const type = $('plot-type').value;
  const signal = $('plot-signal').value;
  const tab = plotTabs.find(t => t.id === activePlotTabId);
  if (!tab) { closeModal(); return; }
  const plotId = 'plot_' + Date.now();
  tab.plots.push({ id: plotId, title, type, signal });
  renderPlotTabs();
  closeModal();
  setTimeout(() => initPlot({ id: plotId, title, type, signal }), 100);
});

// Export HTML
$('btn-export-html').addEventListener('click', async () => {
  const tab = plotTabs.find(t => t.id === activePlotTabId);
  if (!tab || tab.plots.length === 0) { alert('No plots to export.'); return; }
  let html = `<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"><\/script>
<style>body{background:#0d1117;color:#c9d1d9;font-family:sans-serif;}</style>
</head><body><h2>${tab.title}</h2>`;
  for (const plot of tab.plots) {
    const div = document.getElementById('plotdiv-' + plot.id);
    if (div && div._fullLayout) {
      const graphJson = Plotly.toImage ? '' : '';
      const svgStr = await Plotly.toImage(div, { format: 'svg', width: 900, height: 400 });
      html += `<h3>${plot.title}</h3><img src="${svgStr}" style="width:100%"/>`;
    }
  }
  html += '</body></html>';
  const blob = new Blob([html], { type: 'text/html' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'canweb_plots.html';
  a.click();
});

// ── Save / Load Config ─────────────────────────────────────────────────────
$('btn-save-config').addEventListener('click', async () => {
  const config = {
    interface: $('sel-interface').value,
    channel: $('inp-channel').value,
    bitrate: $('sel-bitrate').value,
    plotTabs: plotTabs.map(t => ({
      id: t.id,
      title: t.title,
      plots: t.plots.map(p => ({ id: p.id, title: p.title, type: p.type, signal: p.signal })),
    })),
  };
  const fmt = prompt('Save format: json or yaml?', 'json') || 'json';
  const res = await fetch('/api/config/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format: fmt, config }),
  });
  if (res.ok) {
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fmt === 'yaml' ? 'canweb_config.yaml' : 'canweb_config.json';
    a.click();
  }
});

$('config-file').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch('/api/config/load', { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) { alert('Config load error: ' + data.error); return; }
  const cfg = data.config;
  if (cfg.interface) $('sel-interface').value = cfg.interface;
  if (cfg.channel) $('inp-channel').value = cfg.channel;
  if (cfg.bitrate) $('sel-bitrate').value = cfg.bitrate;
  if (cfg.plotTabs) {
    // Clear old timers
    Object.values(plotUpdateTimers).forEach(t => clearInterval(t));
    plotUpdateTimers = {};
    plotTabs = cfg.plotTabs;
    activePlotTabId = plotTabs[0]?.id ?? null;
    renderPlotTabs();
  }
  alert('Config loaded.');
});

// ── Transmit ───────────────────────────────────────────────────────────────
$('btn-transmit').addEventListener('click', async () => {
  const id = $('tx-id').value.trim();
  const data = $('tx-data').value.trim().replace(/\s+/g, '');
  const ext = $('tx-ext').checked;
  const res = await fetch('/api/transmit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, data, extended: ext }),
  });
  const result = await res.json();
  appendTxLog(id, data, result.ok ? 'OK' : ('ERR: ' + result.error));
});

function esc(str) {
  const d = document.createElement('span');
  d.textContent = String(str);
  return d.textContent;
}

function appendTxLog(id, data, status) {
  const tbody = $('tx-body');
  const tr = document.createElement('tr');
  const cells = [nowStr(), id, formatHex(data), status];
  cells.forEach((text, i) => {
    const td = document.createElement('td');
    td.textContent = text;
    if (i === 1) td.className = 'td-id';
    if (i === 2) td.className = 'td-data';
    if (i === 3) td.style.color = status === 'OK' ? 'var(--accent2)' : 'var(--danger)';
    tr.appendChild(td);
  });
  tbody.insertBefore(tr, tbody.firstChild);
  while (tbody.rows.length > 50) tbody.deleteRow(tbody.rows.length - 1);
}

// Periodic transmit
$('btn-ptx-start').addEventListener('click', () => {
  if (periodicTxTimer) return;
  const id = $('ptx-id').value.trim();
  const data = $('ptx-data').value.trim().replace(/\s+/g, '');
  const ms = parseInt($('ptx-interval').value) || 100;
  $('btn-ptx-start').disabled = true;
  $('btn-ptx-stop').disabled = false;
  periodicTxTimer = setInterval(async () => {
    await fetch('/api/transmit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, data }),
    });
  }, ms);
});

$('btn-ptx-stop').addEventListener('click', () => {
  if (periodicTxTimer) { clearInterval(periodicTxTimer); periodicTxTimer = null; }
  $('btn-ptx-start').disabled = false;
  $('btn-ptx-stop').disabled = true;
});

// ── Settings / Connect ─────────────────────────────────────────────────────
$('btn-connect').addEventListener('click', async () => {
  const iface = $('sel-interface').value;
  const channel = $('inp-channel').value;
  const bitrate = $('sel-bitrate').value;
  const res = await fetch('/api/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ interface: iface, channel, bitrate: parseInt(bitrate) }),
  });
  const data = await res.json();
  const msg = $('connect-msg');
  if (data.ok) {
    msg.style.color = 'var(--accent2)';
    msg.textContent = `Connected: ${data.interface} / ${data.channel} @ ${data.bitrate / 1000} kbps`;
    $('stat-conn').textContent = '● Connected';
    $('stat-conn').classList.add('connected');
  } else {
    msg.style.color = 'var(--danger)';
    msg.textContent = 'Error: ' + data.error;
  }
});

$('btn-disconnect').addEventListener('click', async () => {
  await fetch('/api/disconnect', { method: 'POST' });
  $('connect-msg').textContent = 'Disconnected.';
  $('stat-conn').textContent = '○ Disconnected';
  $('stat-conn').classList.remove('connected');
});

$('btn-diagnose').addEventListener('click', async () => {
  const out = $('diag-output');
  out.textContent = 'Running diagnostics…';
  out.classList.remove('hidden');
  const data = await fetch('/api/diagnose').then(r => r.json());
  let html = `<strong>Python:</strong> ${data.python}<br><strong>Platform:</strong> ${data.platform}<br><br>`;
  html += '<table class="diag-table"><thead><tr><th>Component</th><th>Status</th><th>Detail</th></tr></thead><tbody>';
  for (const c of data.checks) {
    const icon = c.ok ? '✔' : '✘';
    const cls  = c.ok ? 'diag-ok' : 'diag-fail';
    html += `<tr><td>${c.name}</td><td class="${cls}">${icon}</td><td>${c.detail}</td></tr>`;
  }
  html += '</tbody></table>';
  out.innerHTML = html;
});

// ── Init ───────────────────────────────────────────────────────────────────
(async () => {
  // Load DBC info if any was previously loaded
  const info = await fetch('/api/dbc/info').then(r => r.json());
  if (info.loaded) {
    $('dbc-info').textContent = `Loaded: ${info.filename}  •  ${info.messages.length} messages`;
    await refreshSignalSelector();
  }
})();
