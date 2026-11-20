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

const FW_TABS = ['overview', 'rules', 'protection', 'containers', 'exposure', 'blocked'];
function fwTabFromHash() { const m = (location.hash || '').match(/^#firewall\/(\w+)/); return m && FW_TABS.includes(m[1]) ? m[1] : 'overview'; }
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#firewall')) loadFirewall(); });

async function loadFirewall() {
  clearInterval(fwS.timer);
  fwS.tab = fwTabFromHash();
  document.querySelectorAll('.fw-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === fwS.tab));
  FW_TABS.forEach(t => { const p = document.getElementById('fw-pane-' + t); if (p) p.hidden = t !== fwS.tab; });
  await fwRefresh();
  // Tabs with a form are not redrawn under the user's hands.
  fwS.timer = setInterval(() => { if (location.hash.startsWith('#firewall') && !document.hidden && ['overview', 'exposure', 'blocked'].includes(fwS.tab)) fwRefresh(); }, 10000);
}

async function fwRefresh() {
  try { fwS.st = await fwApi('/status'); } catch (e) { fwPane().innerHTML = `<div class="vm-note err">${_esc(errText(e))}</div>`; return; }
  fwPendingBar();
  if (!fwS.st.available) { fwPane().innerHTML = `<div class="vm-note warn">${_esc(fwS.st.reason || 'The firewall is not available on this system.')}</div>`; return; }
  if (fwS.tab === 'overview') fwOverview();
  else if (fwS.tab === 'rules') fwRules();
  else if (fwS.tab === 'protection') fwProtection();
  else if (fwS.tab === 'containers') fwContainers();
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
    <div class="vf-card"><div class="vm-h">Notifications</div>
      <p class="vm-dim" style="margin:0 0 6px">Sent to the bell and to the channels set up in Settings &gt; Notifications.</p>
      ${Object.entries(FW_NOTIFY).map(([k, l]) => `<div class="settings-toggle-row"><span class="settings-label">${l}</span>
        <label class="toggle-switch"><input type="checkbox" ${(cfg.notify || {})[k] !== false ? 'checked' : ''} onchange="fwSetNotify('${k}', this.checked)"><span class="toggle-track"></span></label></div>`).join('')}
    </div>
    <div class="vf-card"><div class="vm-h">If you ever lock yourself out</div>
      <p style="font-size:12.5px;margin:0">SSH and the web UI cannot be blocked from your LAN or VPN, and every change that switches protection on is undone after ${st.confirm_secs} s unless you press Keep.
      As a last resort, run <code>sudo helix-firewall off</code> on the server's console.</p></div>`;
}
const FW_NOTIFY = {bans: 'An address was banned', reverted: 'A change was undone because Keep was not pressed',
                   lists: 'A blocklist or country list failed to update', new_service: 'A new service is listening without a rule'};
async function fwSetNotify(key, on) { const c = fwCfg(); c.notify = {...(c.notify || {}), [key]: on}; await fwApply(c, 'Notification setting saved'); }
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
      <td>${_esc(r.name)}${r.learned ? ' <span class="vm-dim">(learned)</span>' : ''}${r.rate_per_min ? `<div class="vm-dim">max ${r.rate_per_min} new connections/min per address</div>` : ''}</td><td>${_esc(r.ports)}</td><td>${r.proto === 'both' ? 'TCP+UDP' : r.proto.toUpperCase()}</td>
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
    <div class="settings-row"><label class="settings-label">Limit new connections${tipHtml('Per address, per minute. An address that opens more new connections than this to these ports is dropped for the rest of the minute: slows down password guessing on SSH-like services. Leave empty for no limit.')}</label>
      <input class="settings-input" id="fwr-rate" type="number" min="0" max="10000" placeholder="No limit" value="${r.rate_per_min || ''}"></div>
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
                proto: v('fwr-proto'), source: v('fwr-src'), cidrs: v('fwr-cidrs').split(/[\s,]+/).filter(Boolean),
                rate_per_min: parseInt(v('fwr-rate') || '0', 10) || 0};
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


// ── Protection: bans, blocklists, countries, limits (enforce mode only) ──
const FW_COUNTRIES = ('AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW').split(' ');
const fwCountryName = (() => { let dn = null; try { dn = new Intl.DisplayNames([navigator.language || 'en'], {type: 'region'}); } catch (_) {}
  return cc => { try { return dn ? dn.of(cc) : cc; } catch (_) { return cc; } }; })();
const fwFmtTime = ts => ts ? new Date(ts * 1000).toLocaleString() : '';

async function fwProtection() {
  const cfg = fwS.draft || fwCfg(); fwS.draft = cfg;
  const enforcing = cfg.mode === 'enforce';
  let bans = {bans: []}, ls = {};
  try { [bans, ls] = await Promise.all([fwApi('/bans'), fwApi('/lists/status')]); } catch (_) {}
  const b = cfg.bans, g = cfg.geo, lim = cfg.limits;
  const changed = JSON.stringify(cfg) !== JSON.stringify(fwS.st.pending || fwS.st.config);
  const tog = (on, js) => `<label class="toggle-switch"><input type="checkbox" ${on ? 'checked' : ''} onchange="${js}"><span class="toggle-track"></span></label>`;
  const num = (val, js, min, max) => `<input class="settings-input" type="number" style="max-width:110px" min="${min}" max="${max}" value="${val}" onchange="${js}">`;
  const SRC = {ssh: 'SSH', webui: 'Web UI', ftp: 'FTP', smb: 'SMB file shares'};
  const lstat = id => { const x = (ls.lists || {})[id]; if (!x) return '<span class="vm-dim">not downloaded yet</span>';
    return x.ok ? `<span class="vm-dim">${x.count.toLocaleString()} network${x.count === 1 ? '' : 's'} · ${fwFmtTime(x.at)}</span>` : `<span style="color:var(--err)">${_esc(x.error)}</span>${x.count ? ` <span class="vm-dim">(using the last copy: ${x.count})</span>` : ''}`; };
  const gstat = cc => { const x = (ls.geo || {})[cc]; return !x ? 'not downloaded' : x.ok ? `${x.count.toLocaleString()} network${x.count === 1 ? '' : 's'}` : (x.error || 'failed'); };
  fwPane().innerHTML = `
    ${enforcing ? '' : `<div class="vm-note warn">These protections work once the firewall is <b>Protecting</b> (they are off while ${cfg.mode === 'off' ? 'the firewall is off' : 'learning'}). You can set them up now.</div>`}
    <div class="vf-card"><div class="vm-h">Ban repeated failed logins</div>
      <div class="settings-toggle-row"><span class="settings-label">Ban addresses that keep failing to log in${tipHtml('Watches SSH, the web UI, FTP and SMB. An address that fails too often is blocked from everything on this server for a while.')}</span>${tog(b.enabled, 'fwS.draft.bans.enabled=this.checked;fwProtection()')}</div>
      <div class="settings-row"><label class="settings-label">Ban after</label><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${num(b.max_retries, 'fwS.draft.bans.max_retries=+this.value', 2, 100)} failed logins within ${num(b.window_min, 'fwS.draft.bans.window_min=+this.value', 1, 1440)} minutes</div></div>
      <div class="settings-row"><label class="settings-label">Ban for</label><div style="display:flex;gap:8px;align-items:center">${num(b.ban_min, 'fwS.draft.bans.ban_min=+this.value', 1, 525600)} minutes</div></div>
      <div class="settings-row"><label class="settings-label">Watch</label><div style="display:flex;gap:14px;flex-wrap:wrap">${Object.entries(SRC).map(([k, l]) =>
        `<label><input type="checkbox" ${b.sources.includes(k) ? 'checked' : ''} onchange="fwS.draft.bans.sources=this.checked?[...new Set([...fwS.draft.bans.sources,'${k}'])]:fwS.draft.bans.sources.filter(x=>x!=='${k}')"> ${l}</label>`).join('')}</div></div>
      <div class="settings-toggle-row"><span class="settings-label">Also ban LAN and VPN addresses${tipHtml('Off by default so a mistyped password at home can never lock your own computer out.')}</span>${tog(b.ban_lan, 'fwS.draft.bans.ban_lan=this.checked')}</div>
      <div class="settings-row"><label class="settings-label">Never ban${tipHtml('Addresses or networks, one per line.')}</label><textarea class="settings-input" rows="2" style="font-family:monospace" onchange="fwS.draft.bans.never=this.value.split(/[\\s,]+/).filter(Boolean)">${_esc(b.never.join('\n'))}</textarea></div>
      <div style="margin-top:10px"><b style="font-size:12.5px">Banned now (${bans.bans.length})</b>
        <table class="vm-table" style="margin-top:4px"><tbody>${bans.bans.map(x => `<tr><td>${_esc(x.ip)}</td><td class="vm-dim">${_esc(x.reason)}</td><td class="vm-dim">until ${fwFmtTime(x.until)}</td>
          <td><button class="ctr-btn" onclick="fwUnban('${_esc(x.ip)}')">Unban</button></td></tr>`).join('') || '<tr><td class="vm-dim">Nobody.</td></tr>'}</tbody></table>
        <div style="display:flex;gap:8px;margin-top:6px"><input class="settings-input" id="fw-banip" placeholder="Ban an address by hand, e.g. 203.0.113.9" style="max-width:280px"><button class="ctr-btn" onclick="fwBanByHand()">Ban for ${b.ban_min} min</button></div></div>
    </div>
    <div class="vf-card"><div class="vm-h" style="display:flex;align-items:center">Blocklists<span style="flex:1"></span>
        <button class="ctr-btn" ${ls.running ? 'disabled' : ''} onclick="fwUpdateLists()">${ls.running ? 'Updating…' : 'Update now'}</button></div>
      <p class="vm-dim" style="margin:0 0 8px">Known-bad addresses, refreshed every day. Your LAN and VPN ranges are always taken out of a list, so it can never block your own network.</p>
      ${cfg.blocklists.map((x, i) => `<div class="settings-toggle-row"><span class="settings-label">${_esc(x.name)}<div>${lstat(x.id)}</div></span>${tog(x.enabled, `fwS.draft.blocklists[${i}].enabled=this.checked`)}</div>`).join('')}
      <details style="margin-top:6px"><summary class="vm-dim" style="cursor:pointer">Add your own list</summary>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px"><input class="settings-input" id="fw-bl-name" placeholder="Name" style="max-width:180px"><input class="settings-input" id="fw-bl-url" placeholder="https://… (one address or network per line)" style="flex:1;min-width:220px"><button class="ctr-btn" onclick="fwAddList()">Add</button></div></details>
    </div>
    <div class="vf-card"><div class="vm-h">Countries</div>
      <div class="settings-row"><label class="settings-label">Filter by country${tipHtml('Uses free per-country address lists (ipdeny.com), refreshed daily. Your LAN and VPN are never filtered. Country data is approximate: VPNs and cloud servers can appear to be anywhere.')}</label>
        <select class="settings-select" onchange="fwS.draft.geo.mode=this.value;fwProtection()">${[['off', 'Off'], ['block', 'Block the countries below'], ['allow', 'Only allow the countries below']].map(([k, l]) => `<option value="${k}" ${g.mode === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      ${g.mode === 'allow' ? '<div class="fm-warn">Everyone outside these countries is blocked, except your LAN and VPN. If you travel, add where you go or use the VPN.</div>' : ''}
      ${g.mode !== 'off' ? `<div style="margin:6px 0">${g.countries.map(cc => `<span class="ctr-btn" style="display:inline-flex;gap:6px;margin:2px">${_esc(fwCountryName(cc))} <span class="vm-dim">${gstat(cc)}</span><a href="javascript:void 0" onclick="fwS.draft.geo.countries=fwS.draft.geo.countries.filter(c=>c!=='${cc}');fwProtection()">✕</a></span>`).join('') || '<span class="vm-dim">No countries picked.</span>'}</div>
        <select class="settings-select" onchange="if(this.value){fwS.draft.geo.countries=[...new Set([...fwS.draft.geo.countries,this.value])].sort();fwProtection()}"><option value="">Add a country…</option>
          ${FW_COUNTRIES.filter(c => !g.countries.includes(c)).map(c => [c, fwCountryName(c)]).sort((a, b) => a[1].localeCompare(b[1])).map(([c, n]) => `<option value="${c}">${_esc(n)}</option>`).join('')}</select>` : ''}
    </div>
    <div class="vf-card"><div class="vm-h">Flood protection</div>
      <div class="settings-toggle-row"><span class="settings-label">Limit new connections per address${tipHtml('An address outside your LAN that opens more new connections per second than this is dropped for a moment. Stops SYN floods and fast port scans.')}</span>${tog(lim.syn_flood, 'fwS.draft.limits.syn_flood=this.checked;fwProtection()')}</div>
      ${lim.syn_flood ? `<div class="settings-row"><label class="settings-label">Per second</label>${num(lim.syn_per_sec, 'fwS.draft.limits.syn_per_sec=+this.value', 5, 100000)}</div>` : ''}
      <div class="settings-row"><label class="settings-label">Pings per second${tipHtml('0 = no limit.')}</label>${num(lim.ping_per_sec, 'fwS.draft.limits.ping_per_sec=+this.value', 0, 10000)}</div>
    </div>
    <div class="vf-foot" style="gap:8px"><span class="vm-dim">${changed ? 'Unsaved changes' : ''}</span><span style="flex:1"></span>
      <button class="ctr-btn" ${changed ? '' : 'disabled'} onclick="fwS.draft=null;fwProtection()">Reset</button>
      <button class="settings-btn primary" ${changed ? '' : 'disabled'} onclick="fwSaveProtection()">Apply</button></div>`;
  clearTimeout(fwS.lsTimer);
  if (ls.running) fwS.lsTimer = setTimeout(() => { if (fwS.tab === 'protection') fwProtection(); }, 4000);
}
async function fwSaveProtection() {
  const c = fwS.draft; fwS.draft = null;
  const needLists = c.blocklists.some(b => b.enabled) || c.geo.mode !== 'off';
  await fwApply(c, 'Protection settings applied');
  if (needLists) { try { await fwPost('/lists/update'); showToast('Downloading the lists…', 'ok'); } catch (_) {} }
  if (fwS.tab === 'protection') fwProtection();
}
async function fwUpdateLists() { try { await fwPost('/lists/update'); } catch (e) { showToast(errText(e), 'warn'); } fwProtection(); }
async function fwUnban(ip) { try { await fwPost('/bans/unban', {ip}); showToast(`${ip} unbanned`, 'ok'); } catch (e) { showToast(errText(e), 'warn'); } fwProtection(); }
async function fwBanByHand() {
  const ip = document.getElementById('fw-banip').value.trim();
  if (!ip) return;
  try { await fwPost('/bans/ban', {ip, minutes: fwS.draft.bans.ban_min}); showToast(`${ip} banned`, 'ok'); } catch (e) { showToast(errText(e), 'warn'); }
  fwProtection();
}
function fwAddList() {
  const name = document.getElementById('fw-bl-name').value.trim(), url = document.getElementById('fw-bl-url').value.trim();
  if (!/^https:\/\/[A-Za-z0-9.-]+(\/[A-Za-z0-9._~%\/+-]*)?$/.test(url)) { showToast('Enter a plain https:// address.', 'warn'); return; }
  let id = 'custom_' + (name || 'list').toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 20); while (fwS.draft.blocklists.some(b => b.id === id)) id += '_';
  fwS.draft.blocklists.push({id, name: name || url, url, enabled: true}); fwProtection();
}

// ── Containers: per-container outbound rules ──
async function fwContainers() {
  const cfg = fwS.draft || fwCfg(); fwS.draft = cfg;
  let d = {available: false, containers: []};
  try { d = await fwApi('/containers'); } catch (_) {}
  const changed = JSON.stringify(cfg) !== JSON.stringify(fwS.st.pending || fwS.st.config);
  const POL = {internet: 'Internet (normal)', lan: 'LAN only, no internet', none: 'Nothing outside Docker', vpn: 'Only through the PIA VPN (kill switch)'};
  fwPane().innerHTML = `${cfg.mode === 'enforce' ? '' : '<div class="vm-note warn">Outbound rules work once the firewall is <b>Protecting</b>.</div>'}
    <div class="vf-card"><div class="vm-h">Where each container may connect to</div>
      <p class="vm-dim" style="margin:0 0 8px">Containers can always talk to each other. "VPN only" lets a container out only through the PIA tunnel, so it loses the internet instead of leaking when the VPN drops.
      Containers on the host network cannot be controlled here.</p>
      ${!d.available ? '<div class="vm-note warn">Docker is not running (start the array).</div>' : `<div style="overflow-x:auto"><table class="vm-table"><thead><tr><th>Container</th><th>State</th><th>Outbound</th></tr></thead><tbody>
      ${d.containers.map(c => `<tr><td>${_esc(c.name)}</td><td class="vm-dim">${_esc(c.state)}</td><td>${c.supported ? `<select class="settings-select" data-ctr="${_esc(c.name)}" onchange="fwSetEgress(this)">
          ${Object.entries(POL).map(([k, l]) => `<option value="${k}" ${(cfg.egress[c.name] || 'internet') === k ? 'selected' : ''}>${l}</option>`).join('')}</select>` : '<span class="vm-dim">host network: not controllable</span>'}</td></tr>`).join('')}
      </tbody></table></div>`}
    </div>
    <div class="vf-foot" style="gap:8px"><span class="vm-dim">${changed ? 'Unsaved changes' : ''}</span><span style="flex:1"></span>
      <button class="ctr-btn" ${changed ? '' : 'disabled'} onclick="fwS.draft=null;fwContainers()">Reset</button>
      <button class="settings-btn primary" ${changed ? '' : 'disabled'} onclick="fwSaveContainers()">Apply</button></div>`;
}
function fwSetEgress(sel) {
  const n = sel.dataset.ctr;
  if (sel.value === 'internet') delete fwS.draft.egress[n]; else fwS.draft.egress[n] = sel.value;
  fwContainers();
}
async function fwSaveContainers() { const c = fwS.draft; fwS.draft = null; await fwApply(c, 'Outbound rules applied'); if (fwS.tab === 'containers') fwContainers(); }
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#firewall')) fwS.draft = null; });

// ── Dashboard tile ──
async function pollFirewallTile() {
  const row = document.getElementById('stats-row');
  if (!row || document.hidden) return;
  let d; try { d = await fetchJSON('/api/firewall/summary'); } catch (_) { return; }
  let tile = row.querySelector('[data-tile-id="firewall"]');
  if (!d.available) { if (tile) tile.remove(); return; }
  if (!tile) {
    tile = document.createElement('div');
    tile.className = 'stat-tile zfs-tile';
    tile.dataset.tileId = 'firewall';
    tile.onclick = () => { if (!(typeof _customizeActive !== 'undefined' && _customizeActive)) location.hash = '#firewall'; };
    row.appendChild(tile);
    try { _restoreTileOrder(); if (typeof _customizeActive !== 'undefined' && _customizeActive) _attachDragListeners(); } catch (_) {}
  }
  const [label, colour] = FW_MODE[d.mode] || FW_MODE.off;
  const tone = d.mode === 'enforce' ? 'ok' : d.mode === 'learn' ? 'warn' : 'err';
  const parts = [`${d.rules} rule${d.rules === 1 ? '' : 's'}`];
  if (d.mode === 'enforce' && d.blocked_today != null) parts.push(`${d.blocked_today.toLocaleString()} blocked today`);
  if (d.bans) parts.push(`${d.bans} banned`);
  tile.title = 'Open the firewall';
  tile.innerHTML = `<div class="tile-drag-handle" title="Drag to reorder">⠿</div>
    <div class="stat-label">Firewall</div>
    <div class="zfs-name" style="color:${colour}">${label}</div>
    <div class="stat-sub">${_esc(parts.join(' · '))}</div>
    ${d.mode === 'off' ? '<div class="stat-sub" style="color:var(--err)">Everything is reachable</div>' : ''}
    <div class="accent-corner" style="background:var(--${tone})"></div>`;
}
window.addEventListener('load', () => { pollFirewallTile(); setInterval(pollFirewallTile, 30000); });
