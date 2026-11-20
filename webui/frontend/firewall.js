// ── Firewall page: Overview · Rules · Exposure · Blocked ──────────────────────────────────────────────
// Backend: routers/firewall.py (engine services/firewall.py, root helper network/helix-firewall).
// Modes: off / learn (nothing blocked, would-be blocks are logged) / enforce (default deny inbound).
// Enforcing changes are confirm-or-revert: the server undoes them after `confirm_secs` unless "Keep" is pressed.
const fwS = {tab: 'overview', st: null, timer: null, tick: null, draft: null};
const fwApi = (p, o) => fetchJSON('/api/firewall' + p, o);
const fwPost = (p, body) => fwApi(p, {method: p === '/config' ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify(body || {})});
const FW_WHO = {lan: 'LAN', vpn: 'VPN', lan_vpn: 'LAN and VPN', any: 'Anyone', custom: 'Specific addresses'};
const FW_MODE = {
  off:     ['Off', 'var(--fg-dim)', 'Nothing is filtered. Every service that listens on the network can be reached by anyone who can reach this server.'],
  learn:   ['Learning', 'var(--warn)', 'Nothing is blocked yet. Helix notes every connection it <i>would</i> block, so you can create rules for what you use before switching protection on.'],
  enforce: ['Protecting', 'var(--ok)', 'Incoming connections are blocked unless a rule allows them. SSH and the web UI always stay reachable from your LAN and VPN.'],
};

function fwTabFromHash() { const m = (location.hash || '').match(/^#firewall\/(\w+)/); return m && ['overview', 'rules', 'exposure', 'blocked'].includes(m[1]) ? m[1] : 'overview'; }
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#firewall')) loadFirewall(); });

async function loadFirewall() {
  clearInterval(fwS.timer);
  fwS.tab = fwTabFromHash();
  document.querySelectorAll('.fw-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === fwS.tab));
  ['overview', 'rules', 'exposure', 'blocked'].forEach(t => { const p = document.getElementById('fw-pane-' + t); if (p) p.hidden = t !== fwS.tab; });
  await fwRefresh();
  fwS.timer = setInterval(() => { if (location.hash.startsWith('#firewall') && !document.hidden && fwS.tab !== 'rules') fwRefresh(); }, 10000);
}

async function fwRefresh() {
  try { fwS.st = await fwApi('/status'); } catch (e) { fwPane().innerHTML = `<div class="vm-note err">${_esc(errText(e))}</div>`; return; }
  fwPendingBar();
  if (!fwS.st.available) { fwPane().innerHTML = `<div class="vm-note warn">${_esc(fwS.st.reason || 'The firewall is not available on this system.')}</div>`; return; }
  if (fwS.tab === 'overview') fwOverview();
  else if (fwS.tab === 'rules') fwRules();
  else if (fwS.tab === 'exposure') fwExposure();
  else fwBlocked();
}
const fwPane = () => document.getElementById('fw-pane-' + fwS.tab);
const fwCfg = () => JSON.parse(JSON.stringify(fwS.st.pending || fwS.st.config));

// ── pending change: Keep / Undo with a live countdown ──
function fwPendingBar() {
  const bar = document.getElementById('fw-pending');
  const a = fwS.st && fwS.st.active || {};
  clearInterval(fwS.tick);
  if (!a.pending || !a.deadline) { bar.hidden = true; return; }
  bar.hidden = false;
  const draw = () => {
    const left = Math.max(0, Math.round(a.deadline - Date.now() / 1000));
    bar.innerHTML = `<b>Firewall change applied.</b> If you can still use this page, press Keep. Otherwise it is undone by itself in <b>${left} s</b>.
      <span style="flex:1"></span><button class="settings-btn primary" onclick="fwConfirm()">Keep</button><button class="ctr-btn" onclick="fwRevert()">Undo now</button>`;
    if (left <= 0) { clearInterval(fwS.tick); setTimeout(fwRefresh, 2500); }
  };
  draw(); fwS.tick = setInterval(draw, 1000);
}
async function fwConfirm() { try { await fwPost('/confirm'); showToast('Firewall change kept', 'ok'); } catch (e) { showToast(errText(e), 'warn'); } fwRefresh(); }
async function fwRevert() { try { await fwPost('/revert'); showToast('Firewall change undone', 'ok'); } catch (e) { showToast(errText(e), 'warn'); } fwRefresh(); }

async function fwApply(cfg, what) {
  try {
    const r = await fwPost('/config', {config: cfg});
    showToast(r.needs_confirm ? `${what}: press Keep within ${r.confirm_secs} s` : what, 'ok');
  } catch (e) {
    const msg = errText(e);
    if (/locked out/.test(msg)) { openModal('This would lock you out', `<p style="font-size:13px">${_esc(msg)}</p>`, [{label: 'OK', primary: true, onclick: closeModal}]); }
    else showToast(msg, 'warn');
  }
  await fwRefresh();
}

// ── Overview ──
async function fwOverview() {
  const st = fwS.st, cfg = st.pending || st.config, mode = cfg.mode;
  const [label, colour, text] = FW_MODE[mode] || FW_MODE.off;
  let audit = null, logd = null;
  try { [audit, logd] = await Promise.all([fwApi('/audit'), fwApi('/log?limit=1&hours=24')]); } catch (_) {}
  const warns = audit ? audit.rows.filter(r => r.warn).length : 0;
  const uncovered = audit ? audit.uncovered : 0;
  const since = cfg.learn_since ? new Date(cfg.learn_since * 1000).toLocaleString() : '';
  const btn = (l, fn, primary) => `<button class="${primary ? 'settings-btn primary' : 'ctr-btn'}" onclick="${fn}">${l}</button>`;
  let actions = '';
  if (mode === 'off') actions = btn('Turn on (learn first)', "fwSetMode('learn')", true);
  if (mode === 'learn') actions = btn('Review and start protecting', "location.hash='#firewall/exposure'", true) + btn('Turn off', 'fwOff()');
  if (mode === 'enforce') actions = btn('Back to learn mode', "fwSetMode('learn')") + btn('Turn off', 'fwOff()');
  fwPane().innerHTML = `<div class="vf-card"><div class="vm-h">Status</div>
      <div style="display:flex;align-items:center;gap:10px;margin:4px 0 8px"><span style="width:12px;height:12px;border-radius:50%;background:${colour}"></span>
        <span style="font-size:16px;font-weight:600">${label}</span>${mode === 'learn' && since ? `<span class="vm-dim">since ${_esc(since)}</span>` : ''}</div>
      <p style="font-size:12.5px;margin:0 0 10px">${text}</p>
      <div style="font-size:12.5px;line-height:1.9">
        ${cfg.rules.filter(r => r.enabled).length} rules · SSH and web UI always open from LAN and VPN<br>
        ${audit ? `${audit.rows.filter(r => !r.loopback).length} services listen on the network${uncovered && mode !== 'off' ? `, <b style="color:var(--warn)">${uncovered} without a rule</b>${mode === 'enforce' ? ' (blocked)' : ''}` : ''}<br>` : ''}
        ${warns ? `<span style="color:var(--err)">⚠ ${warns} risky exposure${warns > 1 ? 's' : ''}</span> — <a href="#firewall/exposure">see Exposure</a><br>` : ''}
        ${logd && mode !== 'off' ? `${logd.total} connection${logd.total === 1 ? '' : 's'} ${mode === 'learn' ? 'would have been' : ''} blocked in the last 24 h — <a href="#firewall/blocked">details</a>` : ''}
      </div>
      <div class="vf-foot" style="gap:8px">${actions}</div></div>
    <div class="vf-card"><div class="vm-h">If you ever lock yourself out</div>
      <p style="font-size:12.5px;margin:0">SSH and the web UI cannot be blocked from your LAN or VPN, and every change that switches protection on is undone after ${st.confirm_secs} s unless you press Keep.
      As a last resort, run <code>sudo helix-firewall off</code> on the server's console.</p></div>`;
}
async function fwSetMode(mode) { const c = fwCfg(); c.mode = mode; await fwApply(c, mode === 'learn' ? 'Learn mode on' : 'Firewall on'); }
function fwOff() {
  openModal('Turn the firewall off?', '<p style="font-size:13px">Every service on this server becomes reachable by anyone who can reach it. Your rules are kept.</p>',
    [{label: 'Cancel', onclick: closeModal}, {label: 'Turn off', danger: true, onclick: async () => { closeModal(); try { await fwPost('/off'); } catch (e) { showToast(errText(e), 'warn'); } fwRefresh(); }}]);
}

// ── Rules ──
function fwRules() {
  const cfg = fwS.draft || fwCfg(); fwS.draft = cfg;
  const changed = JSON.stringify(cfg) !== JSON.stringify(fwS.st.pending || fwS.st.config);
  const row = (r, i) => `<tr${r.enabled ? '' : ' style="opacity:.55"'}>
      <td><label class="toggle-switch"><input type="checkbox" ${r.enabled ? 'checked' : ''} onchange="fwS.draft.rules[${i}].enabled=this.checked;fwRules()"><span class="toggle-track"></span></label></td>
      <td>${_esc(r.name)}${r.learned ? ' <span class="vm-dim">(learned)</span>' : ''}</td><td>${_esc(r.ports)}</td><td>${r.proto === 'both' ? 'TCP+UDP' : r.proto.toUpperCase()}</td>
      <td>${_esc(FW_WHO[r.source])}${r.source === 'custom' ? `<div class="vm-dim">${_esc(r.cidrs.join(', '))}</div>` : ''}</td>
      <td style="white-space:nowrap"><button class="ctr-btn" onclick="fwEditRule(${i})">Edit</button><button class="ctr-btn" onclick="fwS.draft.rules.splice(${i},1);fwRules()">Remove</button></td></tr>`;
  fwPane().innerHTML = `<div class="vf-card"><div class="vm-h" style="display:flex;align-items:center">Allow rules<span style="flex:1"></span><button class="settings-btn primary" onclick="fwEditRule(-1)">+ Add rule</button></div>
      <p class="vm-dim" style="margin:0 0 8px">A rule lets a port in. In protecting mode everything else from outside is blocked. Rules cover this server and container ports published by Docker.</p>
      <div style="overflow-x:auto"><table class="vm-table"><thead><tr><th>On</th><th>Name</th><th>Ports</th><th>Protocol</th><th>Who can connect</th><th></th></tr></thead><tbody>
      <tr style="opacity:.8"><td>🔒</td><td>Helix management (SSH, web UI)</td><td>${fwS.st.mgmt_ports.join(', ')}</td><td>TCP</td><td>LAN and VPN</td><td class="vm-dim">always on</td></tr>
      ${cfg.rules.map(row).join('') || '<tr><td colspan="6" class="vm-dim">No rules yet. Learn mode can suggest them from what is listening (Exposure tab).</td></tr>'}
      </tbody></table></div></div>
    <div class="vf-card"><div class="vm-h">Settings</div>
      <div class="settings-row"><label class="settings-label">LAN networks${tipHtml('Addresses that count as "LAN": your own networks. The defaults are all private ranges. One network per line, e.g. 192.168.1.0/24.')}</label>
        <textarea class="settings-input" id="fw-lan" rows="4" style="font-family:monospace" onchange="fwS.draft.lan_networks=this.value.split(/[\\s,]+/).filter(Boolean)">${_esc(cfg.lan_networks.join('\n'))}</textarea></div>
      <div class="settings-toggle-row"><span class="settings-label">Log blocked connections${tipHtml('Shown on the Blocked tab. Limited to 10 per second so a flood cannot fill the log.')}</span>
        <label class="toggle-switch"><input type="checkbox" ${cfg.log_blocked ? 'checked' : ''} onchange="fwS.draft.log_blocked=this.checked"><span class="toggle-track"></span></label></div>
    </div>
    <div class="vf-foot" style="gap:8px"><span class="vm-dim">${changed ? 'Unsaved changes' : ''}</span><span style="flex:1"></span>
      <button class="ctr-btn" ${changed ? '' : 'disabled'} onclick="fwS.draft=null;fwRules()">Reset</button>
      <button class="settings-btn primary" ${changed ? '' : 'disabled'} onclick="fwSaveRules()">Apply</button></div>`;
}
async function fwSaveRules() { const c = fwS.draft; fwS.draft = null; await fwApply(c, 'Rules applied'); }

function fwEditRule(i) {
  const r = i >= 0 ? fwS.draft.rules[i] : {name: '', ports: '', proto: 'tcp', source: 'lan', cidrs: [], enabled: true};
  const opt = (v, cur, l) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`;
  openModal(i >= 0 ? 'Edit rule' : 'Add rule', `
    <div class="settings-row"><label class="settings-label">Name</label><input class="settings-input" id="fwr-name" maxlength="60" value="${_esc(r.name)}" placeholder="e.g. Plex"></div>
    <div class="settings-row"><label class="settings-label">Ports${tipHtml('One or more, comma-separated; ranges like 8000-8010.')}</label><input class="settings-input" id="fwr-ports" value="${_esc(r.ports)}" placeholder="e.g. 32400 or 8000-8010"></div>
    <div class="settings-row"><label class="settings-label">Protocol</label><select class="settings-select" id="fwr-proto">${opt('tcp', r.proto, 'TCP')}${opt('udp', r.proto, 'UDP')}${opt('both', r.proto, 'TCP and UDP')}</select></div>
    <div class="settings-row"><label class="settings-label">Who can connect</label><select class="settings-select" id="fwr-src" onchange="document.getElementById('fwr-cidr-row').hidden=this.value!=='custom'">
      ${Object.entries(FW_WHO).map(([k, l]) => opt(k, r.source, l)).join('')}</select></div>
    <div class="settings-row" id="fwr-cidr-row" ${r.source === 'custom' ? '' : 'hidden'}><label class="settings-label">Addresses${tipHtml('One per line: an address (203.0.113.7) or a network (203.0.113.0/24). IPv6 works too.')}</label>
      <textarea class="settings-input" id="fwr-cidrs" rows="3" style="font-family:monospace">${_esc((r.cidrs || []).join('\n'))}</textarea></div>
    <div id="fwr-warn" class="fm-warn" hidden></div>
    <div id="fwr-msg" style="color:var(--err);font-size:12px;margin-top:6px"></div>`,
    [{label: 'Cancel', onclick: closeModal}, {label: i >= 0 ? 'Save' : 'Add', primary: true, onclick: () => fwSaveRule(i)}]);
  const warn = () => { const w = document.getElementById('fwr-warn'); const any = document.getElementById('fwr-src').value === 'any';
    w.hidden = !any; w.textContent = 'Anyone means anyone who can reach this server — the whole internet if this port is forwarded on your router.'; };
  document.getElementById('fwr-src').addEventListener('change', warn); warn();
}
function fwSaveRule(i) {
  const v = id => document.getElementById(id).value.trim();
  const rule = {...(i >= 0 ? fwS.draft.rules[i] : {enabled: true}), name: v('fwr-name'), ports: v('fwr-ports'),
                proto: v('fwr-proto'), source: v('fwr-src'), cidrs: v('fwr-cidrs').split(/[\s,]+/).filter(Boolean)};
  const msg = document.getElementById('fwr-msg');
  if (!rule.name) { msg.textContent = 'Give the rule a name.'; return; }
  if (!/^\s*\d{1,5}(\s*-\s*\d{1,5})?(\s*,\s*\d{1,5}(\s*-\s*\d{1,5})?)*\s*$/.test(rule.ports)) { msg.textContent = 'Ports: numbers or ranges, comma-separated (e.g. 80, 443, 8000-8010).'; return; }
  if (rule.source === 'custom' && !rule.cidrs.length) { msg.textContent = 'Add at least one address.'; return; }
  if (i >= 0) fwS.draft.rules[i] = rule; else fwS.draft.rules.push(rule);
  closeModal(); fwRules();
}

// ── Exposure ──
async function fwExposure() {
  fwPane().innerHTML = '<div class="vm-dim">Checking what listens on the network…</div>';
  let a; try { a = await fwApi('/audit'); } catch (e) { fwPane().innerHTML = `<div class="vm-note err">${_esc(errText(e))}</div>`; return; }
  const mode = a.mode;
  const rows = a.rows.filter(r => !r.loopback);
  const reachCol = r => r.reach === 'nobody' ? '<span style="color:var(--fg-dim)">blocked</span>'
    : r.reach === 'anyone' ? `<span style="color:${mode === 'enforce' ? 'var(--err)' : 'var(--warn)'}">anyone</span>` : _esc(r.reach);
  const sug = a.suggested || [];
  fwPane().innerHTML = `
    ${mode !== 'enforce' && sug.length ? `<div class="vf-card"><div class="vm-h">Suggested rules</div>
      <p style="font-size:12.5px;margin:0 0 8px">These services listen on the network and no rule covers them yet. Tick the ones you use; each becomes a rule that lets your <b>LAN</b> in.
      Anything left unticked is blocked once protection is on.</p>
      ${sug.map((s, i) => `<label style="display:flex;gap:8px;align-items:center;padding:3px 0"><input type="checkbox" class="fw-sug" data-i="${i}" checked>
        <b>${_esc(s.name)}</b><span class="vm-dim">${_esc(s.ports)} ${s.proto === 'both' ? 'TCP+UDP' : s.proto.toUpperCase()}</span></label>`).join('')}
      <div class="vf-foot" style="gap:8px"><button class="ctr-btn" onclick="fwAddSuggested(false)">Add ticked rules</button>
        <button class="settings-btn primary" onclick="fwAddSuggested(true)">Add ticked rules and start protecting</button></div></div>`
      : mode === 'learn' ? `<div class="vf-card"><div class="vm-h">Ready to protect</div><p style="font-size:12.5px;margin:0 0 8px">Every listening service has a rule.</p>
        <div class="vf-foot"><button class="settings-btn primary" onclick="fwSetMode('enforce')">Start protecting</button></div></div>` : ''}
    <div class="vf-card"><div class="vm-h">What listens on the network</div>
      <p class="vm-dim" style="margin:0 0 8px">${mode === 'enforce' ? '' : 'While the firewall is ' + (mode === 'off' ? 'off' : 'learning') + ', everything here can be reached by anyone who can reach this server.'}</p>
      <div style="overflow-x:auto"><table class="vm-table"><thead><tr><th>Port</th><th>Service</th><th>Reachable by</th><th></th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${r.port}/${r.proto}</td><td>${_esc(r.container ? r.container + ' (container)' : r.process || '?')}${r.sensitive && r.sensitive.toLowerCase() !== String(r.process || '').toLowerCase() ? `<div class="vm-dim">${_esc(r.sensitive)}</div>` : ''}</td>
        <td>${reachCol(r)}${!r.covered && mode !== 'off' ? ' <span class="vm-dim">(no rule)</span>' : ''}</td><td style="color:var(--err);font-size:12px">${r.warn ? '⚠ ' + _esc(r.warn) : ''}</td></tr>`).join('')}
      </tbody></table></div>
      <p class="vm-dim" style="margin:8px 0 0">${a.rows.filter(r => r.loopback).length} more only listen on this machine itself (not reachable from the network).</p></div>`;
  fwS.sug = sug;
}
async function fwAddSuggested(protect) {
  const pick = [...document.querySelectorAll('.fw-sug:checked')].map(b => fwS.sug[+b.dataset.i]);
  const c = fwCfg(); c.rules = c.rules.concat(pick);
  if (protect) c.mode = 'enforce';
  await fwApply(c, protect ? `${pick.length} rules added, protection on` : `${pick.length} rules added`);
}

// ── Blocked ──
async function fwBlocked() {
  let d; try { d = await fwApi('/log?limit=200&hours=24'); } catch (e) { fwPane().innerHTML = `<div class="vm-note err">${_esc(errText(e))}</div>`; return; }
  const mode = (fwS.st.pending || fwS.st.config).mode;
  const verb = mode === 'learn' ? 'Would have been blocked' : 'Blocked';
  const t = ts => ts ? new Date(ts * 1000).toLocaleTimeString() : '';
  fwPane().innerHTML = `<div class="vf-card"><div class="vm-h">${verb} in the last 24 hours: ${d.total}</div>
      ${mode === 'off' ? '<p class="vm-dim">The firewall is off, so nothing is logged.</p>' : ''}
      <div style="display:flex;gap:24px;flex-wrap:wrap;font-size:12.5px">
        <div><b>Top sources</b>${d.top_sources.map(s => `<div>${_esc(s.src)} <span class="vm-dim">× ${s.count}</span></div>`).join('') || '<div class="vm-dim">none</div>'}</div>
        <div><b>Top ports</b>${d.top_ports.map(p => `<div>${p.port}/${_esc(p.proto)} <span class="vm-dim">× ${p.count}</span></div>`).join('') || '<div class="vm-dim">none</div>'}</div>
      </div></div>
    <div class="vf-card"><div class="vm-h">Recent</div><div style="overflow-x:auto"><table class="vm-table"><thead><tr><th>Time</th><th>From</th><th>To port</th><th>Interface</th><th></th></tr></thead><tbody>
      ${d.events.map(e => `<tr><td>${t(e.ts)}</td><td>${_esc(e.src)}</td><td>${e.dport ?? ''}/${_esc(e.proto)}</td><td>${_esc(e.iface)}</td><td class="vm-dim">${_esc(e.kind)}</td></tr>`).join('')
        || '<tr><td colspan="5" class="vm-dim">Nothing yet.</td></tr>'}</tbody></table></div></div>`;
}
