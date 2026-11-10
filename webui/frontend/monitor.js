// ── Monitoring page (Checkmk, set up and kept current by helix-monitor) ─────────────────────────
const mon = {timer: null, st: null};
const monApi = (p, o) => fetchJSON('/api/checkmk' + p, o);
const monUrl = (st) => `${location.protocol}//${location.hostname}:${(st && st.port) || 5050}/cmk/`;

async function loadMonitoring() {
  clearInterval(mon.timer);
  await monRefresh();
  mon.timer = setInterval(() => { if (location.hash === '#monitoring' && !document.hidden) monRefresh(); }, 4000);
}

async function monRefresh() {
  const root = document.getElementById('mon-body'); if (!root) return;
  let st;
  try { st = await monApi('/setup'); } catch (e) { root.innerHTML = `<div class="vm-note err">${_esc(errText(e))}</div>`; return; }
  mon.st = st;
  const job = st.job || {}, running = job.state === 'running';
  if (!st.installed) { root.innerHTML = '<div class="vm-note warn">Monitoring is not part of this installation. Update Helix OS to a version that includes it.</div>'; return; }
  const dot = (ok, warn) => `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${ok ? 'var(--ok)' : warn ? 'var(--warn)' : 'var(--err)'};margin-right:6px"></span>`;
  let html = '';
  if (!st.enabled || running || job.state === 'failed') {
    html += `<div class="vf-card"><div class="vm-h">Turn on monitoring</div>
      <p style="font-size:12.5px;margin:0 0 10px">Helix will download and start <b>Checkmk</b> (a full monitoring system, in a container), install its agent on this machine and register it. After that nothing needs configuring:
      <b>new containers, VMs and disks are added to monitoring by themselves</b>, and removed ones are dropped. It watches the array, disk health (SMART), containers, VMs, SMB/NFS services, CPU, memory, filesystems and network.</p>
      <ul style="font-size:12px;color:var(--fg-muted);margin:0 0 10px 18px"><li>Needs the array started (Checkmk lives in Docker on your array), about 1 GB of disk and roughly 1 GB of memory.</li><li>The first start downloads the Checkmk image and takes a few minutes.</li></ul>
      ${running ? '' : '<button class="settings-btn primary" onclick="monEnable()">Enable monitoring</button>'}
      ${job.log && job.log.length ? `<pre class="vm-pre" style="margin-top:10px">${_esc(job.log.join('\n'))}${running ? '\n…' : ''}</pre>` : ''}
      ${job.state === 'failed' ? '<div class="fm-err">Setup did not finish. Fix the problem named above and try again: it picks up where it left off.</div>' : ''}</div>`;
  }
  if (st.enabled && !running) {
    html += `<div class="vf-card"><div class="vm-h">Status</div>
      <div style="font-size:12.5px;line-height:1.9">${dot(st.ready)}Checkmk: ${st.ready ? 'running' : (st.container === 'running' ? 'starting up…' : 'not running (' + _esc(st.container) + '). It starts with the array.')}<br>
      ${dot(st.agent_socket)}Agent on this machine: ${st.agent_socket ? 'answering' : 'not running'}<br>
      ${dot(st.sync)}Automatic updates: ${st.sync ? 'watching for new or removed containers, VMs and disks' : 'not running'}
      ${st.last_sync && st.last_sync.at ? `<span class="vm-dim"> · last change ${new Date(st.last_sync.at * 1000).toLocaleTimeString()} (+${st.last_sync.added.length} / −${st.last_sync.removed.length})</span>` : ''}</div>
      <div class="vf-foot"><a class="settings-btn primary" style="text-decoration:none" href="${_esc(monUrl(st))}" target="_blank" rel="noopener">Open Checkmk ↗</a>
        <button class="ctr-btn" onclick="monShowLogin()">Show login</button><span style="flex:1"></span><button class="ctr-btn" onclick="monDisable()">Turn off</button></div></div>
      <div class="vf-card"><div class="vm-h">What is being monitored</div><div id="mon-svcs" class="vm-dim">Loading…</div></div>`;
  }
  root.innerHTML = html;
  if (st.enabled && !running && st.ready) monServices();
}

async function monServices() {
  const el = document.getElementById('mon-svcs'); if (!el) return;
  try {
    const s = await monApi('/services');
    if (!s.length) { el.textContent = 'Nothing discovered yet; this fills in within a minute.'; return; }
    const lab = {0: ['OK', 'var(--ok)'], 1: ['WARN', 'var(--warn)'], 2: ['CRIT', 'var(--err)'], 3: ['UNKNOWN', 'var(--fg-dim)']};
    const groups = {Containers: [], VMs: [], Disks: [], Array: [], System: []};
    s.forEach(x => { const d = x.description || ''; (/^Container /.test(d) ? groups.Containers : /^VM /.test(d) ? groups.VMs : /^Disk .* SMART/.test(d) ? groups.Disks : /^(Array|Share service)/.test(d) ? groups.Array : groups.System).push(x); });
    const bad = s.filter(x => +x.state > 0).length;
    el.innerHTML = `<div style="font-size:12.5px;margin-bottom:8px">${s.length} services · ${bad ? `<b style="color:var(--warn)">${bad} need attention</b>` : '<b style="color:var(--ok)">all OK</b>'}</div>` +
      Object.entries(groups).filter(([, v]) => v.length).map(([g, v]) => `<div class="vm-h">${g} (${v.length})</div><table class="vm-mini"><tbody>${v.sort((a, b) => b.state - a.state || a.description.localeCompare(b.description)).map(x => {
        const l = lab[+x.state] || lab[3]; return `<tr><td style="width:60px;color:${l[1]};font-weight:600">${l[0]}</td><td>${_esc(x.description)}</td><td class="vm-dim">${_esc((x.plugin_output || '').slice(0, 90))}</td></tr>`; }).join('')}</tbody></table>`).join('');
  } catch (e) { el.textContent = errText(e); }
}

async function monEnable() {
  try { await monApi('/enable', {method: 'POST'}); } catch (e) { alert(errText(e)); return; }
  monRefresh();
}
async function monDisable() {
  if (!confirm('Turn monitoring off? Checkmk stops. Its history is kept and comes back if you enable it again.')) return;
  try { await monApi('/disable', {method: 'POST'}); } catch (e) { alert(errText(e)); }
  monRefresh();
}
async function monShowLogin() {
  try {
    const c = await monApi('/login');
    showModal('Checkmk login', `<p style="font-size:12.5px">Use these to sign in to Checkmk's web page. Helix generated the password; it is stored in <span class="mono">/boot/config/checkmk/</span>.</p>
      <div class="form-label">User</div><div class="mono">${_esc(c.user)}</div><div class="form-label" style="margin-top:8px">Password</div><div class="mono" style="user-select:all">${_esc(c.password)}</div>`,
      '<button class="settings-btn primary" onclick="closeModal()">Close</button>');
  } catch (e) { alert(errText(e)); }
}
