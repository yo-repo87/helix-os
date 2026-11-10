// ── Monitoring tab: Overview · Alerts · Checkmk (embedded control panel) ────────────────────────
// Backend: routers/checkmk.py. Setup/automation: helix-monitor. Alerts -> bell: services/monitor_alerts.py.
const mon = {timer: null, st: null, tab: 'overview', framed: false};
const monApi = (p, o) => fetchJSON('/api/checkmk' + p, o);
const monJson = (b) => ({method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
const monDot = (c) => `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${c};margin-right:6px"></span>`;
const monName = (st) => (st && st.provider === 'netdata') ? 'Netdata' : 'Checkmk';
const MON_LAB = {0: ['OK', 'var(--ok)'], 1: ['WARN', 'var(--warn)'], 2: ['CRIT', 'var(--err)'], 3: ['UNKNOWN', 'var(--fg-dim)']};
const monAge = (ts) => { const s = Math.max(0, Date.now() / 1000 - ts); return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min' : s < 172800 ? Math.round(s / 3600) + ' h' : Math.round(s / 86400) + ' d'; };

function monTabFromHash() { const m = (location.hash || '').match(/^#monitoring\/(\w+)/); return m && ['overview', 'alerts', 'checkmk'].includes(m[1]) ? m[1] : 'overview'; }

async function loadMonitoring() {
  clearInterval(mon.timer);
  mon.tab = monTabFromHash();
  document.querySelectorAll('.mon-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === mon.tab));
  ['overview', 'alerts', 'checkmk'].forEach(t => { const p = document.getElementById('mon-pane-' + t); if (p) p.hidden = t !== mon.tab; });
  await monRefresh(true);
  mon.timer = setInterval(() => { if (location.hash.startsWith('#monitoring') && !document.hidden && mon.tab !== 'checkmk') monRefresh(false); }, 5000);
}
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#monitoring')) loadMonitoring(); });

async function monRefresh(first) {
  let st;
  try { st = await monApi('/setup'); } catch (e) { document.getElementById('mon-pane-overview').innerHTML = `<div class="vm-note err">${_esc(errText(e))}</div>`; return; }
  mon.st = st;
  const embedTab = document.querySelector('.mon-tab[data-tab=checkmk]'); if (embedTab) embedTab.textContent = st.provider === 'netdata' ? 'Dashboard' : 'Checkmk';
  if (mon.tab === 'overview') monOverview(st);
  else if (mon.tab === 'alerts') monAlerts(st);
  else monCheckmk(st, first);
}

function monGate(st) {          // what every tab shows until monitoring is on
  const job = st.job || {}, running = job.state === 'running', nd = st.provider === 'netdata';
  if (!st.installed) return '<div class="vm-note warn">Monitoring is not part of this installation. Update Helix OS to a version that includes it.</div>';
  if (st.enabled && !running) return '';
  return `<div class="vf-card"><div class="vm-h">Turn on monitoring</div>
    <p style="font-size:12.5px;margin:0 0 10px">Helix will download and start <b>${monName(st)}</b> (${nd ? 'a lightweight monitoring system that suits small boards like the Raspberry Pi' : 'a full monitoring system'}, in a container)${nd ? '' : ', install its agent on this machine and register it'}. After that nothing needs configuring:
    <b>new containers, VMs and disks are picked up by themselves</b>, and problems show up on the notification bell. It watches the array, disk health (SMART), containers, VMs, SMB/NFS services, CPU, memory, filesystems and network.</p>
    <ul style="font-size:12px;color:var(--fg-muted);margin:0 0 10px 18px"><li>Needs the array started (${monName(st)} lives in Docker on your array), about ${nd ? '300 MB of disk and 150 MB' : '1 GB of disk and roughly 1 GB'} of memory.</li><li>The first start downloads the ${monName(st)} image and takes a few minutes.</li></ul>
    ${running ? '' : '<button class="settings-btn primary" onclick="monEnable()">Enable monitoring</button>'}
    ${job.log && job.log.length ? `<pre class="vm-pre" style="margin-top:10px">${_esc(job.log.join('\n'))}${running ? '\n…' : ''}</pre>` : ''}
    ${job.state === 'failed' ? '<div class="fm-err">Setup did not finish. Fix the problem named above and try again: it picks up where it left off.</div>' : ''}</div>`;
}

async function monOverview(st) {
  const el = document.getElementById('mon-pane-overview'), gate = monGate(st);
  if (gate) { el.innerHTML = gate; return; }
  const ok = (b) => b ? 'var(--ok)' : 'var(--err)';
  const html = `<div class="vf-card"><div class="vm-h">Status</div>
    <div style="font-size:12.5px;line-height:1.9">${monDot(ok(st.ready))}${monName(st)}: ${st.ready ? 'running' : (st.container === 'running' ? 'starting up…' : 'not running (' + _esc(st.container) + '). It starts with the array.')}<br>
    ${st.provider === 'netdata' ? `${monDot(ok(st.ready))}Containers, VMs and disks: found automatically by Netdata` : `${monDot(ok(st.agent_socket))}Agent on this machine: ${st.agent_socket ? 'answering' : 'not running'}<br>
    ${monDot(ok(st.sync))}Automatic updates: ${st.sync ? 'watching for new or removed containers, VMs and disks' : 'not running'}`}
    ${st.last_sync && st.last_sync.at ? `<span class="vm-dim"> · last change ${new Date(st.last_sync.at * 1000).toLocaleTimeString()} (+${st.last_sync.added.length} / −${st.last_sync.removed.length})</span>` : ''}</div>
    ${st.container === 'absent' ? `<div class="fm-warn" style="margin-top:8px">${monName(st)}'s container is missing. <button class="ctr-btn" onclick="monEnable()">Repair</button> recreates it; your saved Checkmk data is reused when it still exists.</div>` : ''}
    <div class="vf-foot"><a class="settings-btn primary" style="text-decoration:none" href="#monitoring/checkmk">Open ${st.provider === 'netdata' ? 'dashboard' : 'Checkmk'}</a>${st.provider === 'netdata' ? '' : '<button class="ctr-btn" onclick="monShowLogin()">Show login</button>'}<span style="flex:1"></span><button class="ctr-btn" onclick="monDisable()">Turn off</button></div></div>
    <div class="vf-card"><div class="vm-h">What is being monitored</div><div id="mon-svcs" class="vm-dim">Loading…</div></div>`;
  if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; }
  if (st.ready) monServices();
}

async function monServices() {
  const el = document.getElementById('mon-svcs'); if (!el) return;
  try {
    const s = await monApi('/services');
    if (!s.length) { el.textContent = 'Nothing discovered yet; this fills in within a minute.'; return; }
    const groups = {Containers: [], VMs: [], Disks: [], Array: [], System: []};
    s.forEach(x => { const d = x.description || ''; (/^Container /.test(d) ? groups.Containers : /^VM /.test(d) ? groups.VMs : /^Disk .* SMART/.test(d) ? groups.Disks : /^(Array|Share service)/.test(d) ? groups.Array : groups.System).push(x); });
    const bad = s.filter(x => +x.state > 0).length;
    el.innerHTML = `<div style="font-size:12.5px;margin-bottom:8px">${s.length} services · ${bad ? `<b style="color:var(--warn)">${bad} need attention</b> (see Alerts)` : '<b style="color:var(--ok)">all OK</b>'}</div>` +
      Object.entries(groups).filter(([, v]) => v.length).map(([g, v]) => `<div class="vm-h">${g} (${v.length})</div><table class="vm-mini"><tbody>${v.sort((a, b) => b.state - a.state || a.description.localeCompare(b.description)).map(x => {
        const l = MON_LAB[+x.state] || MON_LAB[3]; return `<tr><td style="width:60px;color:${l[1]};font-weight:600">${l[0]}</td><td>${_esc(x.description)}</td><td class="vm-dim">${_esc((x.plugin_output || '').slice(0, 90))}</td></tr>`; }).join('')}</tbody></table>`).join('');
  } catch (e) { el.textContent = errText(e); }
}

// ── Alerts ──────────────────────────────────────────────────────────────────────────────────────
async function monAlerts(st) {
  const el = document.getElementById('mon-pane-alerts'), gate = monGate(st);
  if (gate) { el.innerHTML = gate; return; }
  const [pr, cfg, notes] = await Promise.all([monApi('/problems').catch(() => ({problems: []})), monApi('/alert-settings').catch(() => ({})), fetchJSON('/api/notifications/?limit=200').catch(() => [])]);
  const mine = (Array.isArray(notes) ? notes : []).filter(n => n.source === 'monitoring').slice(0, 40);
  const sw = (k, label, help) => `<label class="vf-check" style="margin:4px 0"><input type="checkbox" data-k="${k}" ${cfg[k] ? 'checked' : ''} onchange="monSetting(this)"> ${label}${help ? ` <span class="vm-dim">· ${help}</span>` : ''}</label>`;
  const html = `<div class="vf-card"><div class="vm-h">Needs attention now</div>${pr.problems.length ? `<table class="vm-mini"><tbody>${pr.problems.map(p => { const l = MON_LAB[p.state] || MON_LAB[3];
      return `<tr><td style="width:64px;color:${l[1]};font-weight:600">${l[0]}</td><td>${_esc(p.name)}</td><td class="vm-dim">${_esc((p.text || '').slice(0, 110))}</td><td class="vm-dim" style="white-space:nowrap">${monAge(p.since)}${p.ack ? ' · acknowledged' : ''}</td></tr>`; }).join('')}</tbody></table>`
      : `<div style="font-size:12.5px;color:var(--ok)">${monDot('var(--ok)')}Nothing needs attention.</div>`}</div>
    <div class="vf-card"><div class="vm-h">What goes to the notification bell</div>
      ${sw('enabled', 'Send monitoring alerts to the bell', 'and to the email/Discord/… channels set up in <a class="panel-action" href="#notif-config">Settings → Notifications</a>')}
      <div style="margin-left:22px">${sw('critical', 'Critical problems')}${sw('warning', 'Warnings')}${sw('recovered', 'Back to normal')}${sw('changes', 'New or removed containers, VMs and disks', 'so you know monitoring noticed')}</div>
      <div class="vf-help" style="margin-top:6px">One message per change, not one per minute. A change must last about 40 seconds before it is announced. Anything in a Checkmk downtime is ignored.</div></div>
    <div class="vf-card"><div class="vm-h">Recent alerts</div>${mine.length ? `<table class="vm-mini"><tbody>${mine.map(n => `<tr><td style="width:70px"><span class="notif-dot ${n.level}" style="display:inline-block"></span> ${_esc(n.level)}</td><td>${_esc(n.title)}</td><td class="vm-dim">${_esc(n.message || '')}</td><td class="vm-dim" style="white-space:nowrap">${fmtAge(n.ts)}</td></tr>`).join('')}</tbody></table>` : '<div class="vm-dim" style="font-size:12.5px">Nothing yet.</div>'}
      <div class="vf-help" style="margin-top:6px">${mon.st && mon.st.provider === 'netdata' ? 'Netdata also raises its own alarms (CPU, memory, disk space, network); they appear above and on the bell.' : 'For rules, schedules and who gets which alert, use Checkmk\'s own notification settings in the Checkmk tab.'}</div></div>`;
  if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; }
}
async function monSetting(cb) {
  try { await monApi('/alert-settings', monJson({[cb.dataset.k]: cb.checked})); showToast('Saved', 'ok'); } catch (e) { cb.checked = !cb.checked; alert(errText(e)); }
}

// ── Embedded Checkmk ────────────────────────────────────────────────────────────────────────────
async function monCheckmk(st, first) {
  const el = document.getElementById('mon-pane-checkmk'), gate = monGate(st);
  if (gate) { el.innerHTML = gate; mon.framed = false; return; }
  if (!st.ready) { el.innerHTML = `<div class="vm-note warn">${monName(st)} is ${st.container === 'running' ? 'still starting up' : 'not running'}. ${st.container === 'running' ? 'This page opens by itself in a moment.' : 'It starts with the array.'}</div>`; mon.framed = false; setTimeout(() => { if (mon.tab === 'checkmk') monRefresh(false); }, 4000); return; }
  if (mon.framed && el.querySelector('iframe')) return;
  if (st.provider === 'netdata') {
    el.innerHTML = `<div class="vf-foot" style="margin:0 0 8px"><span class="vm-dim" style="font-size:12px">Netdata's live dashboard: every chart, per container and per VM.</span><span style="flex:1"></span><a class="ctr-btn" style="text-decoration:none" href="/netdata/" target="_blank" rel="noopener">Open in its own tab ↗</a></div>
      <iframe id="cmkframe" src="/netdata/" style="width:100%;height:calc(100vh - 210px);min-height:520px;border:1px solid var(--border);border-radius:8px;background:#fff"></iframe>`;
    mon.framed = true; return;
  }
  let c; try { c = await monApi('/login'); } catch (e) { el.innerHTML = `<div class="vm-note err">${_esc(errText(e))}</div>`; return; }
  el.innerHTML = `<div class="vf-foot" style="margin:0 0 8px"><span class="vm-dim" style="font-size:12px">Checkmk's own control panel: hosts, rules, notifications, dashboards. You are signed in as <b>${_esc(c.user)}</b>.</span><span style="flex:1"></span><a class="ctr-btn" style="text-decoration:none" href="/cmk/" target="_blank" rel="noopener">Open in its own tab ↗</a></div>
    <iframe name="cmkframe" id="cmkframe" style="width:100%;height:calc(100vh - 210px);min-height:520px;border:1px solid var(--border);border-radius:8px;background:#fff"></iframe>
    <form id="cmklogin" method="POST" action="/cmk/check_mk/login.py" target="cmkframe" hidden><input name="_username" value="${_esc(c.user)}"><input name="_password" value="${_esc(c.password)}"><input name="_login" value="1"><input name="_origtarget" value="index.py"><input name="filled_in" value="login"></form>`;
  document.getElementById('cmklogin').submit();
  mon.framed = true;
}

// ── actions ─────────────────────────────────────────────────────────────────────────────────────
async function monEnable() { try { await monApi('/enable', {method: 'POST'}); } catch (e) { alert(errText(e)); return; } monRefresh(false); }
async function monDisable() {
  if (!confirm('Turn monitoring off? ' + monName(mon.st) + ' stops. Its history is kept and comes back if you enable it again.')) return;
  try { await monApi('/disable', {method: 'POST'}); } catch (e) { alert(errText(e)); }
  mon.framed = false; monRefresh(false);
}
async function monShowLogin() {
  try {
    const c = await monApi('/login');
    showModal('Checkmk login', `<p style="font-size:12.5px">Helix signs you in automatically on the Checkmk tab. If you open Checkmk in its own tab, use these. The password was generated by Helix and is stored in <span class="mono">/boot/config/checkmk/</span>.</p>
      <div class="form-label">User</div><div class="mono">${_esc(c.user)}</div><div class="form-label" style="margin-top:8px">Password</div><div class="mono" style="user-select:all">${_esc(c.password)}</div>`,
      '<button class="settings-btn primary" onclick="closeModal()">Close</button>');
  } catch (e) { alert(errText(e)); }
}

// sidebar badge: how many things need attention
async function monBadge() {
  try {
    const d = await monApi('/problems'), b = document.getElementById('mon-badge'); if (!b) return;
    const n = (d.problems || []).filter(p => !p.ack).length, crit = (d.problems || []).some(p => p.state === 2 && !p.ack);
    b.hidden = !n; b.textContent = n; b.style.background = crit ? 'var(--err)' : 'var(--warn)';
  } catch (_) {}
}
document.addEventListener('DOMContentLoaded', () => { setTimeout(monBadge, 3000); setInterval(() => { if (!document.hidden) monBadge(); }, 30000); });

// ── first-setup invitation: a one-time dialog once the array is started, then a banner on the dashboard ──
async function monPromoCheck() {
  let d; try { d = await monApi('/prompt'); } catch (_) { return; }
  const promo = document.getElementById('mon-promo');
  if (promo) { promo.dataset.on = d.show && d.state === 'later' ? '1' : '0'; promo.hidden = !(promo.dataset.on === '1' && !(location.hash && location.hash !== '#dashboard' && location.hash !== '#')); }
  // Only once per page load, only on the dashboard or Main (never on top of Settings, an update, a form), and never over another dialog.
  const quiet = !location.hash || ['#dashboard', '#main'].includes(location.hash);
  if (d.show && d.state === 'pending' && !mon.promptShown && quiet && !document.getElementById('modal-overlay').style.display.includes('flex')) {
    mon.promptShown = true;
    const nm = d.provider === 'netdata' ? 'Netdata' : 'Checkmk';
    showModal('Turn on monitoring?', `<p style="font-size:13px;margin:0 0 10px">Your array is up. Helix can now watch this server for you with <b>${nm}</b>:</p>
      <ul style="font-size:12.5px;margin:0 0 10px 18px;line-height:1.7"><li>disk health, the array, containers, VMs, shares, CPU and memory</li><li>new containers and VMs are added by themselves</li><li>problems show up on the notification bell (and email/Discord if you set those up)</li></ul>
      <p style="font-size:11.5px;color:var(--fg-muted);margin:0">It runs in a container on your array (${d.provider === 'netdata' ? 'light enough for a Raspberry Pi' : 'about 1 GB of disk and memory'}). The first start downloads it, which takes a few minutes. You can also do this later from the Monitoring tab.</p>`,
      '<button class="ctr-btn" onclick="monPromoLater()">Not now</button><button class="settings-btn primary" onclick="monPromoEnable()">Enable monitoring</button>');
  }
}
async function monPromoLater() { try { await monApi('/prompt/later', {method: 'POST'}); } catch (_) {} closeModal(); monPromoCheck(); }
async function monPromoEnable() { closeModal(); location.hash = '#monitoring'; try { await monApi('/enable', {method: 'POST'}); } catch (e) { alert(errText(e)); } if (location.hash.startsWith('#monitoring')) loadMonitoring(); }
document.addEventListener('DOMContentLoaded', () => { setTimeout(monPromoCheck, 4000); setInterval(() => { if (!document.hidden) monPromoCheck(); }, 60000); });
window.addEventListener('hashchange', () => { const p = document.getElementById('mon-promo'); if (p && p.dataset.on === '1') p.hidden = !(!location.hash || location.hash === '#dashboard' || location.hash === '#'); });
