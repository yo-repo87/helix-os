// ── Virtual machines: the VM table, menus, dialogs and detail rows ──────────────────────────────
// Modelled on Unraid's VM Manager. The add/edit form and the settings page are in vmform.js.
// Backend: routers/vms.py (libvirt via virsh). Browser console: noVNC at /novnc with the WebSocket in routers/vmconsole.py.
const vmx = {data: null, host: null, tpl: null, expanded: new Set(), details: {}, stats: {}, timer: null, jobs: [], lastRender: ''};

const vmApi = (path, opts) => fetchJSON('/api/vms' + path, opts);
const vmPost = (path, body) => vmApi(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
const vmErr = (e) => errText(e).replace(/^HTTP \d+: ?/, '');
const vmSize = (bytes) => bytes >= 2 ** 40 ? (bytes / 2 ** 40).toFixed(1) + ' TB' : bytes >= 2 ** 30 ? (bytes / 2 ** 30).toFixed(bytes >= 10 * 2 ** 30 ? 0 : 1) + ' GB' : (bytes / 2 ** 20).toFixed(0) + ' MB';
const vmMem = (mb) => mb >= 1024 ? (mb / 1024) + ' GB' : mb + ' MB';
const VM_STATE_LABEL = {running: 'Running', 'shut off': 'Stopped', paused: 'Paused', 'in shutdown': 'Shutting down', crashed: 'Crashed', suspended: 'Suspended', hibernated: 'Hibernated', unknown: 'Unknown'};

function vmIcon(state) {
  // shape and colour together, like the disk orbs: play = running, pause = paused/suspended, square = stopped, moon = hibernated
  const s = {running: ['▶', 'var(--ok)'], paused: ['❚❚', 'var(--warn)'], suspended: ['❚❚', 'var(--warn)'], 'shut off': ['■', 'var(--err)'],
             hibernated: ['☾', 'var(--accent)'], 'in shutdown': ['…', 'var(--warn)'], crashed: ['✖', 'var(--err)']}[state] || ['?', 'var(--fg-dim)'];
  return `<span class="vm-orb" style="color:${s[1]};border-color:${s[1]}" title="${_esc(VM_STATE_LABEL[state] || state)}">${s[0]}</span>`;
}

// ── page load / refresh ─────────────────────────────────────────────────────────────────────────
async function loadVMs() {
  clearInterval(vmx.timer);
  await vmRefresh(true);
  vmx.timer = setInterval(() => { if (location.hash === '#vms' && !document.hidden && !document.getElementById('modal-overlay').style.display.includes('flex')) vmRefresh(false); }, 4000);
}

async function vmRefresh(first) {
  try {
    vmx.data = await vmApi('/');
    const [stats, jobs] = await Promise.all([vmx.data.running ? vmApi('/stats').catch(() => ({stats: {}})) : {stats: {}}, vmApi('/jobs').catch(() => ({jobs: []}))]);
    vmx.stats = stats.stats || {}; vmx.jobs = jobs.jobs || [];
    if (first || !vmx.host) vmApi('/host').then(h => { vmx.host = h; vmRender(); }).catch(() => {});
    for (const n of vmx.expanded) { try { vmx.details[n] = await vmApi('/' + encodeURIComponent(n)); } catch (_) { vmx.expanded.delete(n); } }
    vmRender();
  } catch (e) {
    document.getElementById('vm-banner').innerHTML = `<div class="vm-note err">Could not read the VM list: ${_esc(vmErr(e))}</div>`;
  }
}

function vmGpuName(addr) { const d = vmx.host && vmx.host.pci.find(p => p.address === addr); return d ? d.name.replace(/\[.*?\]/g, '').trim().slice(0, 38) : addr; }

function vmBanner() {
  const d = vmx.data; if (!d) return '';
  if (!d.installed) return `<div class="vm-note warn"><b>VMs are not available.</b> ${_esc(d.reason)}<br>Update Helix OS to a version that includes the hypervisor (3.0.0 or newer).</div>`;
  if (!d.kvm) return `<div class="vm-note warn"><b>Hardware virtualization is not available.</b> ${_esc(d.reason)}</div>`;
  if (!d.enabled) return `<div class="vm-note"><b>VM Manager is off.</b> Turn it on to create and run virtual machines. It starts and stops with the array.
    <div style="margin-top:8px"><button class="settings-btn primary" onclick="vmEnableManager()">Enable VM Manager</button> <a href="#vm-settings" class="panel-action">Settings…</a></div></div>`;
  if (!d.running) return `<div class="vm-note warn"><b>VM Manager is waiting for the array.</b> ${_esc(d.reason)} <a href="#main" class="panel-action">Open Main</a></div>`;
  return '';
}

async function vmEnableManager() {
  try { const r = await vmApi('/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: true})}); if (r.message) showToast(r.message, 'warn'); }
  catch (e) { alert(vmErr(e)); }
  vmRefresh(true);
}

function vmRender() {
  const d = vmx.data, tb = document.getElementById('vm-tbody'); if (!d || !tb) return;
  document.getElementById('vm-banner').innerHTML = vmBanner();
  const usable = d.running && d.kvm;
  ['vm-add-btn', 'vm-startall-btn', 'vm-stopall-btn'].forEach(id => { const b = document.getElementById(id); if (b) b.disabled = !usable; });
  document.getElementById('vm-table-wrap').hidden = !usable;
  if (!usable) { document.getElementById('vm-jobs').innerHTML = ''; return; }
  if (!d.vms.length) {
    tb.innerHTML = `<tr><td colspan="8" class="vm-empty">No virtual machines yet. <a href="#vm/new" class="panel-action">Add a VM</a> to get started.</td></tr>`;
  } else {
    tb.innerHTML = d.vms.map(v => {
      const open = vmx.expanded.has(v.name), st = vmx.stats[v.name];
      const gfx = (v.graphics || []).map(g => `<span class="vm-chip" title="Open the console">${g.type.toUpperCase()}${g.port ? ':' + g.port : ''}</span>`).join('')
                + (v.gpus || []).map(a => `<span class="vm-chip gpu" title="${_esc(a)}">${_esc(vmGpuName(a))}</span>`).join('') || '<span class="vm-dim">—</span>';
      const ip = (v.ips || [])[0];
      const n = _esc(v.name);
      const live = v.state === 'running' && st ? `<div class="vm-sub">${st.cpu_pct}% CPU · ${vmMem(st.mem_mb)}</div>` : '';
      return `<tr class="vm-row" data-vm="${n}">
        <td><button class="vm-iconbtn" data-act="menu" title="Actions">${vmIcon(v.state)}</button></td>
        <td><span class="vm-name" data-act="toggle">${n}</span><div class="vm-sub">${_esc(VM_STATE_LABEL[v.state] || v.state)}${v.unsupported && v.unsupported.length ? ' · custom XML' : ''}</div>${live}</td>
        <td class="vm-desc">${_esc(v.description || '')}${v.webui ? ` <a href="${_esc(v.webui)}" target="_blank" rel="noopener" class="panel-action">WebUI</a>` : ''}</td>
        <td class="vm-num" title="${(v.pins || []).length ? 'pinned to host CPUs ' + v.pins.join(', ') : 'not pinned'}">${v.vcpus}${(v.pins || []).length ? ' <span class="vm-dim">pinned</span>' : ''}</td>
        <td class="vm-num">${vmMem(v.memory_mb)}${v.max_memory_mb > v.memory_mb ? `<div class="vm-sub">up to ${vmMem(v.max_memory_mb)}</div>` : ''}</td>
        <td class="vm-num">${v.disks} / ${vmSize(v.disks_capacity)}<div class="vm-sub">${v.cdroms} CD${v.cdroms === 1 ? '' : 's'}${v.snapshots ? ` · ${v.snapshots} snapshot${v.snapshots === 1 ? '' : 's'}` : ''}</div></td>
        <td data-act="console">${gfx}</td>
        <td class="vm-num">${ip ? _esc(ip.ip) : (v.state === 'running' ? '<span class="vm-dim" title="Needs a DHCP lease on the libvirt network, or the guest agent in the VM">waiting…</span>' : '<span class="vm-dim">—</span>')}</td>
        <td><label class="toggle-switch" title="Start this VM when the array starts"><input type="checkbox" data-act="autostart" ${v.autostart ? 'checked' : ''}><span class="toggle-track"></span></label></td></tr>
        ${open ? `<tr class="vm-detail"><td></td><td colspan="8">${vmDetailHtml(v.name)}</td></tr>` : ''}`;
    }).join('');
  }
  const jobs = (vmx.jobs || []).filter(j => j.state === 'running' || (j.finished && Date.now() / 1000 - j.finished < 60));
  document.getElementById('vm-jobs').innerHTML = jobs.map(j => `<div class="vm-note ${j.state === 'failed' ? 'err' : ''}"><b>${j.kind === 'clone' ? 'Cloning to ' + _esc(j.vm) : j.kind === 'virtio' ? 'VirtIO drivers ISO' : _esc(j.kind)}</b>:
    ${j.state === 'running' ? '<span class="vm-dim">working…</span> ' + _esc(j.message) : j.state === 'done' ? 'done' : 'failed: ' + _esc(j.message)}</div>`).join('');
}

// event delegation for the table
(function vmWire() {
  document.addEventListener('DOMContentLoaded', () => {
    const tb = document.getElementById('vm-tbody'); if (!tb) return;
    tb.addEventListener('click', (ev) => {
      const tr = ev.target.closest('tr.vm-row'); if (!tr) return;
      const name = tr.dataset.vm, act = (ev.target.closest('[data-act]') || {}).dataset?.act;
      if (act === 'menu') return vmMenu(ev, name);
      if (act === 'toggle') return vmToggleDetail(name);
      if (act === 'console') return vmConsole(name);
    });
    tb.addEventListener('change', async (ev) => {
      const cb = ev.target.closest('[data-act="autostart"]'); if (!cb) return;
      const name = cb.closest('tr.vm-row').dataset.vm;
      try { await vmApi(`/${encodeURIComponent(name)}/autostart`, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: cb.checked})}); }
      catch (e) { cb.checked = !cb.checked; alert(vmErr(e)); }
    });
    tb.addEventListener('contextmenu', (ev) => { const tr = ev.target.closest('tr.vm-row'); if (tr) { ev.preventDefault(); vmMenu(ev, tr.dataset.vm); } });
  });
})();

async function vmToggleDetail(name) {
  if (vmx.expanded.has(name)) vmx.expanded.delete(name);
  else { vmx.expanded.add(name); try { vmx.details[name] = await vmApi('/' + encodeURIComponent(name)); } catch (e) { vmx.expanded.delete(name); alert(vmErr(e)); } }
  vmRender();
}

// ── detail row: disks (grow), CDs (change), NICs, USB hot-plug, snapshots ───────────────────────
function vmDetailHtml(name) {
  const d = vmx.details[name]; if (!d) return '<span class="vm-dim">Loading…</span>';
  const off = d.state === 'shut off' || d.state === 'hibernated', running = d.state === 'running', n = _esc(name);
  const disks = d.disks_detail.map((k, i) => `<tr><td class="mono">${_esc(k.path)}${k.exists ? '' : ' <b style="color:var(--err)">missing</b>'}</td><td>${_esc(k.serial || '—')}</td><td>${_esc(k.bus)}</td>
      <td>${k.type === 'file' && off ? `<a class="panel-action" title="Click to make this vDisk larger" onclick="vmGrowDisk('${n}', ${i})">${vmSize(k.capacity)}</a>` : vmSize(k.capacity)}</td>
      <td>${vmSize(k.allocation)}</td><td>${k.boot || '—'}</td></tr>`).join('') || '<tr><td colspan="6" class="vm-dim">No vDisks</td></tr>';
  const cds = d.spec.cdroms.map((c, i) => {
    const dev = (d.disks_detail.length ? '' : '') + vmCdDev(d, i);
    return `<div class="vm-cd"><span class="mono">${_esc(dev)}</span> ${c.path ? `<span class="mono">${_esc(c.path.split('/').pop())}</span> <a class="panel-action" onclick="vmEject('${n}','${_esc(dev)}')">Eject</a>` : '<span class="vm-dim">No CD image inserted</span>'}
      <a class="panel-action" onclick="vmInsert('${n}','${_esc(dev)}')">${c.path ? 'Change…' : 'Insert…'}</a></div>`;
  }).join('') || '<span class="vm-dim">No CD drives</span>';
  const nics = d.nics_detail.map(x => `<tr><td class="mono">${_esc(x.mac)}</td><td>${_esc(x.type)}: ${_esc(x.source)}</td><td>${_esc(x.model)}</td><td>${(x.ips || []).map(i => _esc(i.ip + (i.prefix ? '/' + i.prefix : ''))).join(', ') || '<span class="vm-dim">—</span>'}</td></tr>`).join('');
  const usb = vmx.host ? vmx.host.usb.map(u => {
    const on = d.spec.usb.some(x => x.vendor === u.vendor && x.product === u.product);
    return `<div class="vm-cd">${_esc(u.name)} <span class="mono vm-dim">${u.vendor}:${u.product}</span>${u.storage ? ' <span style="color:var(--warn)" title="Do not pass the Helix boot drive to a VM">storage</span>' : ''}
      ${running ? `<a class="panel-action" onclick="vmUsb('${n}','${on ? 'detach' : 'attach'}','${u.vendor}','${u.product}')">${on ? 'Unplug' : 'Plug in'}</a>` : (on ? '<span class="vm-dim">attached at start</span>' : '')}</div>`;
  }).join('') : '';
  const tree = vmSnapTree(d);
  return `<div class="vm-dgrid">
    <div><div class="vm-h">vDisks</div><table class="vm-mini"><thead><tr><th>Path</th><th>Serial</th><th>Bus</th><th>Capacity</th><th>Allocated</th><th>Boot</th></tr></thead><tbody>${disks}</tbody></table>
      <div class="vm-h">CD drives</div>${cds}</div>
    <div><div class="vm-h">Network</div><table class="vm-mini"><thead><tr><th>MAC</th><th>Network</th><th>Model</th><th>IP address</th></tr></thead><tbody>${nics || '<tr><td colspan="4" class="vm-dim">No network</td></tr>'}</tbody></table>
      ${usb ? `<div class="vm-h">USB devices</div>${usb}` : ''}</div>
    <div style="grid-column:1/-1"><div class="vm-h">Snapshots <a class="panel-action" onclick="vmSnapshotDialog('${n}')">+ Create</a></div>${tree}</div></div>`;
}

function vmCdDev(d, i) {
  // the guest device name of the i-th CD drive, matching how vmxml.py names them (sd*/hd* after the disks on the same bus)
  const used = {}; const pref = {virtio: 'vd', sata: 'sd', scsi: 'sd', usb: 'sd', ide: 'hd'};
  d.spec.disks.forEach(x => { const p = pref[x.bus] || 'vd'; used[p] = (used[p] || 0) + 1; });
  let dev = '';
  d.spec.cdroms.forEach((c, k) => { const p = pref[c.bus] || 'sd'; const n = used[p] || 0; used[p] = n + 1; if (k === i) dev = p + String.fromCharCode(97 + n); });
  return dev;
}

function vmSnapTree(d) {
  const snaps = d.snapshot_list || []; if (!snaps.length) return '<span class="vm-dim">No snapshots</span>';
  const kids = {}; snaps.forEach(s => (kids[s.parent || ''] = kids[s.parent || ''] || []).push(s));
  const rows = [];
  const walk = (parent, depth) => (kids[parent] || []).forEach(s => {
    rows.push(`<tr><td style="padding-left:${depth * 18 + 6}px">${depth ? '└ ' : ''}<b>${_esc(s.name)}</b></td><td>${_esc(s.created)}</td><td>${_esc(s.state)}</td><td>${_esc(s.description)}</td>
      <td><a class="panel-action" onclick="vmSnapRevert('${_esc(d.name)}','${_esc(s.name)}')">Revert</a> <a class="panel-action" style="color:var(--err)" onclick="vmSnapRemove('${_esc(d.name)}','${_esc(s.name)}')">Remove</a></td></tr>`);
    walk(s.name, depth + 1);
  });
  walk('', 0);
  return `<table class="vm-mini"><thead><tr><th>Name</th><th>Created</th><th>State</th><th>Description</th><th></th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
}

// ── the click menu (changes with the VM's state, like Unraid's) ─────────────────────────────────
function vmMenuHide() { document.getElementById('fm-menu')?.remove(); }
function vmMenu(ev, name) {
  ev.stopPropagation(); vmMenuHide();
  const v = (vmx.data.vms || []).find(x => x.name === name); if (!v) return;
  const st = v.state, n = name, items = [];
  const I = (label, fn, opts = {}) => items.push({label, fn, ...opts});
  if (st === 'running') {
    if (v.graphics.length) I('Console', () => vmConsole(n));
    if (v.webui) I('Open WebUI', () => window.open(v.webui, '_blank', 'noopener'));
    items.push(null);
    I('Stop (shut down)', () => vmDo(n, 'stop')); I('Pause', () => vmDo(n, 'pause')); I('Restart', () => vmDo(n, 'reboot'));
    I('Hibernate', () => vmDo(n, 'hibernate')); I('Force stop', () => vmDo(n, 'force-stop', 'Force stop is like pulling the plug: the guest does not get to shut down properly. Continue?'), {danger: true});
    items.push(null); I('Create snapshot…', () => vmSnapshotDialog(n)); I('Logs', () => vmLogs(n));
    I('Edit', null, {disabled: 'Shut the VM down to edit it'});
  } else if (st === 'paused' || st === 'suspended') {
    I('Resume', () => vmDo(n, 'resume')); I('Force stop', () => vmDo(n, 'force-stop', 'Force stop the VM?'), {danger: true}); items.push(null); I('Logs', () => vmLogs(n));
  } else if (st === 'in shutdown') {
    I('Force stop', () => vmDo(n, 'force-stop', 'The guest has not shut down yet. Force it off?'), {danger: true}); I('Logs', () => vmLogs(n));
  } else {
    const dis = v.unsupported && v.unsupported.length ? 'This VM uses settings the form cannot edit; use the XML editor' : '';
    I(st === 'hibernated' ? 'Resume from hibernation' : 'Start', () => vmDo(n, 'start'));
    I('Start with console', async () => { if (await vmDo(n, 'start')) setTimeout(() => vmConsole(n), 1500); });
    if (st === 'hibernated') I('Discard hibernation image', () => vmDo(n, 'discard-hibernation', 'Discard the saved state? The VM will boot fresh next time.'), {danger: true});
    items.push(null);
    I('Edit', () => { location.hash = '#vm/' + encodeURIComponent(n); }, dis ? {disabled: dis} : {});
    I('Edit XML', () => { location.hash = '#vm/' + encodeURIComponent(n) + '/xml'; });
    I('Clone…', () => vmCloneDialog(n)); I('Save as template…', () => vmSaveTemplate(n)); I('Create snapshot…', () => vmSnapshotDialog(n)); I('Logs', () => vmLogs(n));
    items.push(null);
    I('Remove VM', () => vmRemoveDialog(n, false), {danger: true}); I('Remove VM and disks', () => vmRemoveDialog(n, true), {danger: true});
  }
  const m = document.createElement('div'); m.id = 'fm-menu'; m.className = 'fm-menu';
  items.forEach(it => {
    if (!it) { m.appendChild(document.createElement('hr')); return; }
    const b = document.createElement('button'); b.textContent = it.label;
    if (it.disabled) { b.disabled = true; b.title = it.disabled; } else b.onclick = () => { vmMenuHide(); it.fn && it.fn(); };
    if (it.danger) b.style.color = 'var(--err)';
    m.appendChild(b);
  });
  document.body.appendChild(m);
  const r = m.getBoundingClientRect(), x = ev.clientX || ev.target.getBoundingClientRect().left, y = ev.clientY || ev.target.getBoundingClientRect().bottom;
  m.style.left = Math.min(x, innerWidth - r.width - 8) + 'px'; m.style.top = Math.min(y, innerHeight - r.height - 8) + 'px';
}
document.addEventListener('click', (e) => { if (!e.target.closest('#fm-menu')) vmMenuHide(); });

async function vmDo(name, action, confirmText) {
  if (confirmText && !confirm(confirmText)) return false;
  try {
    await vmPost(`/${encodeURIComponent(name)}/${action}`);
    showToast(({stop: 'Shutting down ', 'force-stop': 'Stopped ', start: 'Starting ', pause: 'Paused ', resume: 'Resumed ', reboot: 'Restarting ', hibernate: 'Hibernating ', 'discard-hibernation': 'Discarded the hibernation image of '}[action] || '') + name, 'ok');
    setTimeout(() => vmRefresh(false), 800); setTimeout(() => vmRefresh(false), 3500);
    return true;
  } catch (e) { alert(`${name}: ${vmErr(e)}`); return false; }
}
async function vmAll(what) {
  try { const r = await vmPost('/' + what); showToast(what === 'start-all' ? `Started ${r.started.length} VM(s)${r.failed.length ? '; ' + r.failed.join('; ') : ''}` : `Shutting down ${r.stopping.length} VM(s)`, r.failed && r.failed.length ? 'warn' : 'ok'); }
  catch (e) { alert(vmErr(e)); }
  setTimeout(() => vmRefresh(false), 1000);
}

// ── console ─────────────────────────────────────────────────────────────────────────────────────
async function vmConsole(name) {
  let info;
  try { info = await vmApi(`/${encodeURIComponent(name)}/console`); } catch (e) { alert(vmErr(e)); return; }
  const g = info.graphics.find(x => x.type === 'vnc');
  if (g) {
    const url = `/novnc/vnc.html?path=${encodeURIComponent('api/vms/' + encodeURIComponent(name) + '/console/ws')}&autoconnect=true&resize=scale&reconnect=true&reconnect_delay=1500`;
    window.open(url, 'vmconsole-' + name, 'width=1100,height=760');
    return;
  }
  const s = info.graphics.find(x => x.type === 'spice');
  showModal('SPICE console', `<p style="font-size:12.5px">This VM uses SPICE. Open it with a SPICE client such as <b>virt-viewer</b> / <b>remote-viewer</b>:</p>
    <pre class="vm-pre">spice://${_esc(location.hostname)}:${s ? s.port : ''}</pre><p style="font-size:11.5px;color:var(--fg-muted)">The browser console needs VNC graphics. Change the graphics type in Edit to use it.</p>`,
    '<button class="settings-btn primary" onclick="closeModal()">Close</button>');
}

async function vmLogs(name) {
  let d; try { d = await vmApi(`/${encodeURIComponent(name)}/log`); } catch (e) { alert(vmErr(e)); return; }
  showModal(`Log: ${name}`, `<pre class="vm-pre" style="max-height:60vh">${_esc(d.lines.join('\n') || d.error || '(empty)')}</pre>`, '<button class="settings-btn primary" onclick="closeModal()">Close</button>');
  fmWide(true);
}

// ── dialogs: grow disk, CD media, USB, snapshots, clone, remove ─────────────────────────────────
function vmGrowDisk(name, i) {
  const k = vmx.details[name].disks_detail[i], cur = Math.round(k.capacity / 2 ** 30);
  showModal('Grow vDisk', `<p style="font-size:12.5px" class="mono">${_esc(k.path)}</p><p style="font-size:12px;color:var(--fg-muted)">A vDisk can only grow, and only while the VM is shut off. After growing it, extend the partition inside the guest.</p>
    <div class="form-label">New size (GB), currently ${cur} GB</div><input id="vmg-size" class="settings-input" type="number" min="${cur + 1}" value="${cur + 10}"><div id="vmg-err" class="fm-err"></div>`,
    '<button class="ctr-btn" onclick="closeModal()">Cancel</button><button class="settings-btn primary" id="vmg-go">Grow</button>');
  document.getElementById('vmg-go').onclick = async () => {
    try { await vmPost(`/${encodeURIComponent(name)}/disks/resize`, {path: k.path, size_gb: +document.getElementById('vmg-size').value}); closeModal(); vmRefresh(false); }
    catch (e) { document.getElementById('vmg-err').textContent = vmErr(e); }
  };
}
async function vmEject(name, dev) { try { await vmPost(`/${encodeURIComponent(name)}/cdrom`, {dev, path: ''}); vmRefresh(false); } catch (e) { alert(vmErr(e)); } }
function vmInsert(name, dev) {
  const isos = (vmx.host && vmx.host.isos) || [];
  showModal('Insert CD image', `<div class="form-label">Image</div><select id="vmi-iso" class="settings-input">${isos.map(i => `<option value="${_esc(i.path)}">${_esc(i.name)} (${vmSize(i.size)})</option>`).join('')}<option value="">Other path…</option></select>
    <input id="vmi-path" class="settings-input" placeholder="/mnt/user/isos/file.iso" style="margin-top:6px" hidden><div id="vmi-err" class="fm-err"></div>`,
    '<button class="ctr-btn" onclick="closeModal()">Cancel</button><button class="settings-btn primary" id="vmi-go">Insert</button>');
  const sel = document.getElementById('vmi-iso'); sel.onchange = () => { document.getElementById('vmi-path').hidden = sel.value !== ''; };
  sel.dispatchEvent(new Event('change'));
  document.getElementById('vmi-go').onclick = async () => {
    const p = sel.value || document.getElementById('vmi-path').value.trim();
    try { await vmPost(`/${encodeURIComponent(name)}/cdrom`, {dev, path: p}); closeModal(); vmRefresh(false); } catch (e) { document.getElementById('vmi-err').textContent = vmErr(e); }
  };
}
async function vmUsb(name, op, vendor, product) { try { await vmPost(`/${encodeURIComponent(name)}/usb/${op}`, {vendor, product}); setTimeout(() => vmRefresh(false), 500); } catch (e) { alert(vmErr(e)); } }

function vmSnapshotDialog(name) {
  const running = (vmx.data.vms.find(v => v.name === name) || {}).state === 'running';
  showModal('Create snapshot', `<div class="form-label">Name</div><input id="vms-name" class="settings-input" placeholder="leave empty for a date-time name" autocomplete="off">
    <div class="form-label" style="margin-top:8px">Description</div><input id="vms-desc" class="settings-input" autocomplete="off">
    <p class="fm-warn" style="margin-top:8px">${running ? 'The VM is running, so the snapshot includes its memory and the VM pauses briefly.' : 'The VM is shut off, so the snapshot covers its disks.'} Snapshots live inside the vDisk (qcow2). UEFI VMs cannot be snapshotted this way.</p><div id="vms-err" class="fm-err"></div>`,
    '<button class="ctr-btn" onclick="closeModal()">Cancel</button><button class="settings-btn primary" id="vms-go">Create</button>');
  document.getElementById('vms-go').onclick = async () => {
    const b = document.getElementById('vms-go'); b.disabled = true; b.textContent = 'Working…';
    try { await vmPost(`/${encodeURIComponent(name)}/snapshots`, {name: document.getElementById('vms-name').value.trim(), description: document.getElementById('vms-desc').value.trim()}); closeModal(); vmx.expanded.add(name); vmRefresh(false); }
    catch (e) { document.getElementById('vms-err').textContent = vmErr(e); b.disabled = false; b.textContent = 'Create'; }
  };
}
async function vmSnapRevert(name, snap) {
  if (!confirm(`Revert ${name} to the snapshot "${snap}"? Changes made since then are lost.`)) return;
  try { await vmPost(`/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snap)}/revert`); showToast('Reverted', 'ok'); vmRefresh(false); } catch (e) { alert(vmErr(e)); }
}
async function vmSnapRemove(name, snap) {
  if (!confirm(`Remove the snapshot "${snap}"? Removing a snapshot in the middle of a chain can break the snapshots built on it.`)) return;
  try { await vmApi(`/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snap)}`, {method: 'DELETE'}); vmRefresh(false); } catch (e) { alert(vmErr(e)); }
}

function vmCloneDialog(name) {
  showModal('Clone VM', `<p style="font-size:12.5px">Copies the vDisks of <b>${_esc(name)}</b> into a new VM.</p><div class="form-label">New name</div><input id="vmc-name" class="settings-input" value="${_esc(name)}_clone" autocomplete="off">
    <label style="font-size:12.5px;display:block;margin-top:8px"><input type="checkbox" id="vmc-mac" checked> Give the clone new MAC addresses</label>
    <label style="font-size:12.5px;display:block"><input type="checkbox" id="vmc-over"> Overwrite vDisks of the same name</label><div id="vmc-err" class="fm-err"></div>`,
    '<button class="ctr-btn" onclick="closeModal()">Cancel</button><button class="settings-btn primary" id="vmc-go">Clone</button>');
  document.getElementById('vmc-go').onclick = async () => {
    try { await vmPost(`/${encodeURIComponent(name)}/clone`, {new_name: document.getElementById('vmc-name').value.trim(), regenerate_macs: document.getElementById('vmc-mac').checked, overwrite: document.getElementById('vmc-over').checked}); closeModal(); vmRefresh(false); showToast('Cloning started: it runs in the background', 'ok'); }
    catch (e) { document.getElementById('vmc-err').textContent = vmErr(e); }
  };
}

function vmRemoveDialog(name, withDisks) {
  showModal(withDisks ? 'Remove VM and disks' : 'Remove VM', `<p style="font-size:12.5px">${withDisks ? `This permanently deletes <b>${_esc(name)}</b> <b style="color:var(--err)">and its vDisk files</b>. This cannot be undone.` : `This removes the definition of <b>${_esc(name)}</b>. Its vDisks stay where they are.`}</p>
    <div class="form-label">To confirm type: ${_esc(name)}</div><input id="vmr-confirm" class="settings-input" autocomplete="off"><div id="vmr-err" class="fm-err"></div>`,
    '<button class="ctr-btn" onclick="closeModal()">Cancel</button><button class="settings-btn primary" id="vmr-go" style="background:var(--err);border-color:var(--err)">Remove</button>');
  document.getElementById('vmr-go').onclick = async () => {
    if (document.getElementById('vmr-confirm').value !== name) { document.getElementById('vmr-err').textContent = 'Incorrect confirmation'; return; }
    try { const r = await vmApi(`/${encodeURIComponent(name)}?remove_disks=${withDisks}`, {method: 'DELETE'}); closeModal(); vmx.expanded.delete(name); showToast(withDisks ? `Removed (${r.removed_disks.length} vDisk file(s) deleted${r.kept_disks.length ? ', ' + r.kept_disks.length + ' kept: in use elsewhere' : ''})` : 'Removed', 'ok'); vmRefresh(false); }
    catch (e) { document.getElementById('vmr-err').textContent = vmErr(e); }
  };
}

async function vmSaveTemplate(name) {
  const t = prompt('Template name', name + ' template'); if (!t) return;
  try { await vmPost('/templates', {name: t, from_vm: name}); showToast('Saved: it appears in Add VM under "Your templates"', 'ok'); } catch (e) { alert(vmErr(e)); }
}
