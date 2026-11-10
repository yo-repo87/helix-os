// ── VM add/edit form, XML editor and VM Manager settings (companion to vms.js) ───────────────────
// Routes: #vm/new (template grid, then the form) · #vm/<name> (edit) · #vm/<name>/xml (expert editor) · #vm-settings
// The form edits a "spec" (see services/vmxml.py); every field below is a key of it.
const vf = {spec: null, mode: 'new', name: '', adv: false, step: 'templates', tpl: [], host: null, start: true, autostart: false, existingDisks: new Set(), unsupported: []};

const VF_TIPS = {
  cpu: 'Host CPUs the VM may use. Pinning ties each vCPU to a host thread; leave all unticked to let Linux schedule them.',
  mem: 'Initial memory is what the guest starts with. Max memory lets the balloon grow it later.',
  bios: 'SeaBIOS is a classic PC BIOS. OVMF is UEFI (needed for Windows 11, GPU passthrough on newer guests, and secure boot).',
  machine: 'Q35 is a modern chipset with PCIe and is right for almost everything. i440fx is the old PC chipset for very old guests.',
  hyperv: 'Makes the guest see Hyper-V enlightenments, which makes Windows faster. Leave on for Windows guests.',
  vdisk: 'A vDisk is a file on your array holding the virtual hard drive. qcow2 grows as it fills and supports snapshots; raw is a little faster but allocates the space and cannot snapshot.',
  bus: 'virtio is fastest but a Windows installer needs the VirtIO drivers disc. SATA works everywhere.',
  discard: 'Lets the guest tell the host which blocks are free, so a qcow2 vDisk shrinks again after deleting files.',
  share: 'Share a host folder with the guest. virtiofs is fast (needs a driver in the guest); 9p is slow but older kernels have it.',
  vnc: 'VNC works in the browser console. SPICE needs a SPICE client. For a passed-through GPU, graphics can stay on VNC as a second screen or be None.',
  gpu: 'Passing a GPU through gives the VM exclusive use of it, so the host and other VMs cannot use it while the VM runs. It needs IOMMU turned on (VM Manager settings).',
  usbpass: 'USB devices are handed to the VM when it starts. Plug and unplug on a running VM from the VM list. Never pass the Helix boot drive.',
  qemu: 'Extra QEMU arguments, one per line (for example -global ...). A wrong one can stop the VM from starting.',
  agent: 'Adds the channel the QEMU guest agent uses. With the agent installed in the guest, Helix can show its IP address and shut it down cleanly.',
  tpm: 'A virtual TPM 2.0 chip. Windows 11 requires one. Needs OVMF.',
};
const tipI = (k) => `<span class="vm-tip" title="${_esc(VF_TIPS[k] || '')}">ⓘ</span>`;
const vfPath = (obj, p) => p.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
function vfSet(el) {
  const p = el.dataset.p, t = el.dataset.t || 'str', parts = p.split('.'), key = parts.pop();
  const tgt = parts.reduce((o, k) => o[k], vf.spec);
  let v = el.type === 'checkbox' ? el.checked : el.value;
  if (t === 'num') v = v === '' ? 0 : +v;
  tgt[key] = v;
  if (el.dataset.rerender) vfRender();
}
const vfIn = (label, p, o = {}) => {
  const v = vfPath(vf.spec, p);
  const attrs = `data-p="${p}" data-t="${o.t || 'str'}" ${o.rerender ? 'data-rerender="1"' : ''} onchange="vfSet(this)" oninput="${o.live ? 'vfSet(this)' : ''}"`;
  let input;
  if (o.options) input = `<select class="settings-input" ${attrs} ${o.disabled ? 'disabled' : ''}>${o.options.map(([val, lab]) => `<option value="${_esc(val)}" ${String(v) === String(val) ? 'selected' : ''}>${_esc(lab)}</option>`).join('')}</select>`;
  else if (o.bool) input = `<label class="vf-check"><input type="checkbox" ${attrs} ${v ? 'checked' : ''}> ${o.boolLabel || ''}</label>`;
  else input = `<input class="settings-input" ${attrs} type="${o.t === 'num' ? 'number' : 'text'}" ${o.min != null ? `min="${o.min}"` : ''} ${o.max != null ? `max="${o.max}"` : ''} value="${_esc(v ?? '')}" placeholder="${_esc(o.ph || '')}" ${o.disabled ? 'disabled' : ''} autocomplete="off">`;
  return `<div class="vf-field ${o.wide ? 'wide' : ''}"><label>${label}${o.tip ? ' ' + tipI(o.tip) : ''}</label>${input}${o.help ? `<div class="vf-help">${o.help}</div>` : ''}</div>`;
};

// ── routing ─────────────────────────────────────────────────────────────────────────────────────
async function loadVmForm() {
  const parts = (location.hash || '').split('?')[0].replace(/^#vm\//, '').split('/'), name = decodeURIComponent(parts[0] || '');
  const root = document.getElementById('vmform-body'); root.innerHTML = '<div class="vm-dim">Loading…</div>';
  try {
    [vf.host, vf.tpl] = await Promise.all([vmApi('/host'), vmApi('/templates').then(r => r.templates)]);
    vmx.host = vf.host;
    if (name === 'new') { vf.mode = 'new'; vf.name = ''; vf.step = 'templates'; vf.spec = null; vf.unsupported = []; vf.existingDisks = new Set(); return vfRender(); }
    const d = await vmApi('/' + encodeURIComponent(name));
    vf.mode = 'edit'; vf.name = name; vf.spec = d.spec; vf.unsupported = d.spec.unsupported || []; vf.state = d.state;
    vf.existingDisks = new Set(d.spec.disks.map(x => x.path));
    if (parts[1] === 'xml') return vfXmlEditor(name, d.state);
    vf.step = 'form'; vf.adv = false; vfRender();
  } catch (e) { root.innerHTML = `<div class="vm-note err">${_esc(vmErr(e))}</div>`; }
}

// ── step 1: template grid ───────────────────────────────────────────────────────────────────────
function vfTemplateGrid() {
  const groups = {};
  vf.tpl.forEach(t => (groups[t.group] = groups[t.group] || []).push(t));
  return Object.entries(groups).map(([g, list]) => `<div class="vm-h">${_esc(g)}</div><div class="vf-tgrid">${list.map(t => `
    <div class="vf-tcard" onclick="vfPick('${_esc(t.id)}')" title="${_esc(t.note)}"><div class="vf-badge">${_esc(t.badge)}</div><div class="vf-tname">${_esc(t.name)}</div>
      ${t.user ? `<a class="panel-action" style="font-size:10.5px;color:var(--err)" onclick="event.stopPropagation();vfDelTpl('${_esc(t.id)}')">remove</a>` : ''}</div>`).join('')}</div>`).join('');
}
async function vfDelTpl(id) { if (!confirm('Remove this template?')) return; try { await vmApi('/templates/' + encodeURIComponent(id), {method: 'DELETE'}); vf.tpl = (await vmApi('/templates')).templates; vfRender(); } catch (e) { alert(vmErr(e)); } }

function vfPick(id) {
  const t = vf.tpl.find(x => x.id === id), s = JSON.parse(JSON.stringify(t.defaults || {}));
  const def = vf.host.settings.default_network, net = vf.host.networks.find(n => n.source === def) || vf.host.networks[0];
  s.name = ''; s.template = id; s.description = s.description || '';
  s.nics = (s.nics || [{model: 'virtio'}]).map(n => ({model: 'virtio', ...n, type: n.type || (net ? net.type : 'network'), source: n.source || (net ? net.source : 'default')}));
  s.disks = (s.disks || []).map(d => ({format: 'qcow2', bus: 'virtio', discard: true, ssd: false, serial: '', path: '', size_gb: 30, ...d}));
  s.cdroms = s.cdroms || []; s.graphics = s.graphics || [{type: 'vnc', video: 'qxl'}]; s.pci = s.pci || []; s.usb = s.usb || []; s.shares = s.shares || [];
  s.vcpus = s.vcpus || 2; s.threads = s.threads || 1; s.pins = s.pins || []; s.memory_mb = s.memory_mb || 2048; s.max_memory_mb = s.max_memory_mb || 0; s.qemu_args = s.qemu_args || [];
  s.machine = s.machine || 'q35'; s.firmware = s.firmware || 'seabios'; s.clock = s.clock || 'utc'; s.usb_controller = s.usb_controller || 'qemu-xhci'; s.sound = s.sound || 'none';
  s.cpu_mode = s.cpu_mode || 'host-passthrough'; s.migratable = !!s.migratable; s.input_tablet = s.input_tablet !== false; s.agent = s.agent !== false; s.webui = s.webui || '';
  vf.spec = s; vf.step = 'form'; vf.start = true; vf.autostart = false; vf.note = t.note; vfRender();
}

// ── step 2: the form ────────────────────────────────────────────────────────────────────────────
function vfRender() {
  const root = document.getElementById('vmform-body'), s = vf.spec;
  document.getElementById('vmform-title').textContent = vf.mode === 'edit' ? `Edit VM: ${vf.name}` : (vf.step === 'templates' ? 'Add VM' : 'Add VM: ' + (vf.tpl.find(t => t.id === s.template) || {}).name);
  if (vf.step === 'templates') { root.innerHTML = `<p style="font-size:12.5px;color:var(--fg-muted);margin-bottom:6px">Pick what you want to run. This only sets sensible defaults; everything can be changed on the next page.</p>${vfTemplateGrid()}`; return; }
  const h = vf.host, off = vf.mode === 'new' || vf.state === 'shut off';
  if (vf.mode === 'edit' && !off) { root.innerHTML = `<div class="vm-note warn">Shut the VM down to edit it.</div>`; return; }
  const cores = h.cpu.cores, nets = h.networks;
  const win = /^windows/.test(s.template);
  const html = [];
  if (vf.unsupported.length) html.push(`<div class="vm-note warn">This VM uses settings the form cannot show: ${_esc(vf.unsupported.join(', '))}. Saving here would drop them. <a class="panel-action" href="#vm/${encodeURIComponent(vf.name)}/xml">Use the XML editor instead.</a></div>`);
  if (vf.note) html.push(`<div class="vm-note">${_esc(vf.note)}</div>`);
  html.push(`<div class="vf-card"><div class="vf-grid">${vfIn('Name', 'name', {ph: 'My VM', live: true})}${vfIn('Description', 'description', {wide: true})}${vfIn('WebUI (optional)', 'webui', {ph: 'http://[IP]:8080', help: 'Shown as a link in the VM list. [IP] is replaced by the guest IP address.'})}</div></div>`);
  // CPU
  const pinsOn = new Set(s.pins);
  html.push(`<div class="vf-card"><div class="vm-h">CPU ${tipI('cpu')}</div><div class="vf-grid">
    ${vfIn('vCPUs', 'vcpus', {t: 'num', min: 1, max: h.cpu.threads * 4, rerender: true})}
    ${vfIn('CPU mode', 'cpu_mode', {options: [['host-passthrough', 'Host passthrough (best performance)'], ['host-model', 'Host model']]})}
    ${vfIn('Threads per core', 'threads', {t: 'num', options: [[1, '1'], [2, '2 (hyper-threading)']]})}
    ${vfIn('Migratable', 'migratable', {bool: true, boolLabel: 'allow live migration'})}</div>
    <div class="form-label" style="margin-top:8px">Pinned host CPUs <span class="vm-dim">(${s.pins.length} of ${s.vcpus} chosen; pick exactly ${s.vcpus} or none)</span></div>
    <div class="vf-pins">${cores.map(c => `<div class="vf-core">${c.map(n => `<label class="vf-pin"><input type="checkbox" ${pinsOn.has(n) ? 'checked' : ''} onchange="vfPin(${n}, this.checked)"> ${n}</label>`).join('')}</div>`).join('')}</div>
    <div class="vf-help"><a class="panel-action" onclick="vfPinAll(true)">Select all</a> · <a class="panel-action" onclick="vfPinAll(false)">None</a> · ${h.cpu.model ? _esc(h.cpu.model) : ''} (${h.cpu.threads} threads)</div></div>`);
  // memory + machine
  html.push(`<div class="vf-card"><div class="vm-h">Memory and platform ${tipI('mem')}</div><div class="vf-grid">
    ${vfIn('Initial memory (MB)', 'memory_mb', {t: 'num', min: 64, max: h.memory_mb, help: `Host has ${vmMem(h.memory_mb)}. ${vmMem(+s.memory_mb || 0)} chosen.`})}
    ${vfIn('Max memory (MB)', 'max_memory_mb', {t: 'num', min: 0, max: h.memory_mb, help: '0 = same as initial'})}
    ${vfIn('Machine', 'machine', {options: [['q35', 'Q35 (modern)'], ['i440fx', 'i440fx (legacy)']], tip: 'machine'})}
    ${vfIn('BIOS', 'firmware', {options: [['seabios', 'SeaBIOS'], ...(h.ovmf.ovmf ? [['ovmf', 'OVMF (UEFI)']] : []), ...(h.ovmf['ovmf-secure'] ? [['ovmf-secure', 'OVMF (UEFI, secure boot)']] : [])], rerender: true, tip: 'bios'})}
    ${vfIn('Hyper-V enlightenments', 'hyperv', {bool: true, boolLabel: 'on', tip: 'hyperv'})}
    ${vfIn('TPM 2.0', 'tpm', {bool: true, boolLabel: 'virtual TPM', tip: 'tpm', disabled: s.firmware === 'seabios'})}
    ${vfIn('USB controller', 'usb_controller', {options: [['qemu-xhci', 'USB 3 (qemu XHCI)'], ['nec-xhci', 'USB 3 (NEC XHCI)'], ['ich9-ehci1', 'USB 2'], ['none', 'None']]})}
    ${vfIn('Guest agent', 'agent', {bool: true, boolLabel: 'add the agent channel', tip: 'agent'})}</div></div>`);
  // install media
  html.push(`<div class="vf-card"><div class="vm-h">Install media</div>${s.cdroms.map((c, i) => vfCdRow(c, i)).join('') || '<div class="vm-dim" style="font-size:12px">No CD drives.</div>'}
    <div style="margin-top:6px"><a class="panel-action" onclick="vfAdd('cdroms',{path:'',bus:'sata'})">+ Add CD drive</a></div></div>`);
  // disks
  html.push(`<div class="vf-card"><div class="vm-h">vDisks ${tipI('vdisk')}</div>${s.disks.map((d, i) => vfDiskRow(d, i)).join('') || '<div class="vm-dim" style="font-size:12px">No vDisks: the VM would have nothing to boot from.</div>'}
    <div style="margin-top:6px"><a class="panel-action" onclick="vfAdd('disks',{path:'',size_gb:30,format:'qcow2',bus:'virtio',discard:true,ssd:false,serial:''})">+ Add vDisk</a>
    <span class="vm-dim" style="font-size:11px"> · new vDisks go in ${_esc(h.settings.storage)}/&lt;VM name&gt;/ unless you give a location</span>
    ${h.storage_fs === 'btrfs' ? '<div class="fm-warn">That folder is on btrfs. Helix turns off copy-on-write for it, which is what vDisks need.</div>' : ''}</div></div>`);
  // shares
  html.push(`<div class="vf-card"><div class="vm-h">Shared folders ${tipI('share')}</div>${s.shares.map((x, i) => `<div class="vf-row">
      ${vfIn('Mode', `shares.${i}.mode`, {options: [['virtiofs', 'virtiofs'], ['9p', '9p']]})}${vfIn('Source (on the server)', `shares.${i}.source`, {ph: '/mnt/user/media', wide: true})}${vfIn('Mount tag', `shares.${i}.target`, {ph: 'media'})}${vfIn('Read only', `shares.${i}.readonly`, {bool: true, boolLabel: 'yes'})}
      <a class="panel-action vf-rm" onclick="vfDel('shares',${i})">Remove</a></div>`).join('')}
    <a class="panel-action" onclick="vfAdd('shares',{mode:'virtiofs',source:'/mnt/user/',target:'',readonly:false})">+ Add shared folder</a></div>`);
  // graphics
  html.push(`<div class="vf-card"><div class="vm-h">Graphics ${tipI('vnc')}</div>${s.graphics.map((g, i) => `<div class="vf-row">
      ${vfIn('Type', `graphics.${i}.type`, {options: [['vnc', 'VNC (browser console)'], ['spice', 'SPICE']], rerender: true})}
      ${vfIn('Video', `graphics.${i}.video`, {options: [['qxl', 'QXL'], ['virtio', 'Virtio'], ['vga', 'VGA'], ['cirrus', 'Cirrus'], ['none', 'None (GPU only)']]})}
      ${vfIn('Keyboard', `graphics.${i}.keymap`, {options: [['', 'Default'], ...h.keymaps.map(k => [k, k])]})}
      ${vfIn('Password', `graphics.${i}.password`, {ph: 'none', help: g.type === 'vnc' ? 'VNC passwords are cut to 8 characters.' : ''})}
      ${g.type === 'spice' ? vfIn('Copy/paste', `graphics.${i}.copypaste`, {bool: true, boolLabel: 'allow'}) : ''}
      <a class="panel-action vf-rm" onclick="vfDel('graphics',${i})">Remove</a></div>`).join('') || '<div class="vm-dim" style="font-size:12px">No emulated graphics. Use a passed-through GPU below, or add VNC for a console.</div>'}
    <a class="panel-action" onclick="vfAdd('graphics',{type:'vnc',video:'qxl'})">+ Add graphics</a></div>`);
  // sound + gpu + other pci
  const pci = h.pci.filter(d => d.class_id !== '0600');
  const have = new Set(s.pci.map(p => p.address));
  html.push(`<div class="vf-card"><div class="vm-h">Sound and devices</div><div class="vf-grid">${vfIn('Sound card', 'sound', {options: [['none', 'None'], ['ich9', 'Intel HD Audio (ich9)'], ['ac97', 'AC97'], ['ich6', 'ich6'], ['usb', 'USB audio']]})}</div>
    <div class="vm-h" style="margin-top:10px">Graphics cards and other PCI devices ${tipI('gpu')}</div>${!h.cpu.hw_virtualization ? '' : ''}
    ${pci.length ? `<div class="vf-pcilist">${pci.map(d => { const on = have.has(d.address), gp = d.iommu_group; return `<label class="vf-pci ${d.boot_vga ? 'dim' : ''}"><input type="checkbox" ${on ? 'checked' : ''} ${d.boot_vga ? 'disabled' : ''} onchange="vfPci('${d.address}', this.checked)">
      <span class="mono">${d.address}</span> ${d.class ? `<b>${_esc(d.class)}</b>` : ''} ${_esc(d.name.slice(0, 70))} <span class="vm-dim">${gp != null ? 'IOMMU group ' + gp : 'no IOMMU group'}${d.bound_to_vfio ? ' · vfio' : d.driver ? ' · ' + _esc(d.driver) : ''}${d.boot_vga ? ' · used by the host console' : ''}</span></label>`; }).join('')}</div>` : '<div class="vm-dim" style="font-size:12px">No PCI devices found.</div>'}
    ${s.pci.length ? `<div class="vf-pcirom">${s.pci.map((p, i) => vfIn(`ROM BIOS for ${p.address} (optional)`, `pci.${i}.rom`, {ph: '/mnt/user/isos/vbios.rom', wide: true})).join('')}</div>` : ''}
    ${s.pci.length && !h.cpu.hw_virtualization ? '' : ''}
    ${s.pci.some(p => !(pci.find(d => d.address === p.address) || {}).iommu_group) ? '<div class="fm-warn">A chosen device has no IOMMU group, so passthrough will not work. Turn on IOMMU in VM Manager settings and reboot.</div>' : ''}
    <div class="vm-h" style="margin-top:10px">USB devices ${tipI('usbpass')}</div>
    ${h.usb.length ? h.usb.map(u => { const on = s.usb.some(x => x.vendor === u.vendor && x.product === u.product); return `<label class="vf-pci"><input type="checkbox" ${on ? 'checked' : ''} onchange="vfUsb('${u.vendor}','${u.product}',this.checked)"> ${_esc(u.name)} <span class="mono vm-dim">${u.vendor}:${u.product}</span>${u.storage ? ' <span style="color:var(--warn)">storage device: make sure it is not the Helix boot drive</span>' : ''}</label>`; }).join('') : '<div class="vm-dim" style="font-size:12px">No USB devices found.</div>'}</div>`);
  // network
  html.push(`<div class="vf-card"><div class="vm-h">Network</div>${s.nics.map((n, i) => `<div class="vf-row">
      <div class="vf-field"><label>Network source</label><select class="settings-input" onchange="vfNic(${i}, this.value)">${nets.map(x => `<option value="${_esc(x.type + '|' + x.source)}" ${n.type === x.type && n.source === x.source ? 'selected' : ''}>${_esc(x.label)}</option>`).join('')}${nets.some(x => x.type === n.type && x.source === n.source) ? '' : `<option selected>${_esc(n.type + ': ' + n.source)}</option>`}</select></div>
      ${vfIn('Model', `nics.${i}.model`, {options: [['virtio', 'virtio (fast)'], ['e1000', 'e1000'], ['e1000e', 'e1000e'], ['rtl8139', 'rtl8139']]})}
      ${vfIn('MAC address', `nics.${i}.mac`, {ph: 'generated', help: ''})}<a class="panel-action vf-rm" onclick="vfMac(${i})">Generate</a>
      <a class="panel-action vf-rm" onclick="vfDel('nics',${i})">Remove</a></div>`).join('')}
    <a class="panel-action" onclick="vfAdd('nics',{model:'virtio',type:'${nets[0] ? nets[0].type : 'network'}',source:'${nets[0] ? nets[0].source : 'default'}'})">+ Add network card</a>
    ${nets.some(x => x.type === 'bridge') ? '' : '<div class="vf-help">To put VMs on your own network (not behind NAT), bridge a network port on the Network page; it will then be offered here.</div>'}</div>`);
  // advanced
  html.push(`<div class="vf-card"><div class="vm-h"><a class="panel-action" onclick="vf.adv=!vf.adv;vfRender()">${vf.adv ? '▾' : '▸'} Advanced</a></div>${vf.adv ? `<div class="vf-grid">
      ${vfIn('Clock', 'clock', {options: [['utc', 'UTC (Linux)'], ['localtime', 'Local time (Windows)']]})}${vfIn('Tablet input device', 'input_tablet', {bool: true, boolLabel: 'absolute mouse'})}</div>
      <div class="vf-field wide"><label>QEMU command line arguments ${tipI('qemu')}</label><textarea class="settings-input" rows="3" style="font-family:var(--mono,monospace);font-size:12px" onchange="vf.spec.qemu_args=this.value.split('\\n').map(x=>x.trim()).filter(Boolean)">${_esc((s.qemu_args || []).join('\n'))}</textarea></div>
      ${vf.mode === 'edit' ? `<a class="panel-action" href="#vm/${encodeURIComponent(vf.name)}/xml">Edit the raw XML…</a>` : ''}` : ''}</div>`);
  // footer
  html.push(`<div class="vf-foot">${vf.mode === 'new' ? `<label class="vf-check"><input type="checkbox" ${vf.start ? 'checked' : ''} onchange="vf.start=this.checked"> Start VM after creation</label> <label class="vf-check"><input type="checkbox" ${vf.autostart ? 'checked' : ''} onchange="vf.autostart=this.checked"> Start with the array</label>` : ''}
    <span style="flex:1"></span><div id="vf-err" class="fm-err"></div>
    <button class="ctr-btn" onclick="location.hash='#vms'">Cancel</button><button class="settings-btn primary" id="vf-go" onclick="vfSubmit()">${vf.mode === 'edit' ? 'Update' : 'Create'}</button></div>`);
  const y = root.scrollTop; root.innerHTML = html.join(''); root.scrollTop = y;
}

function vfCdRow(c, i) {
  if (c.role === 'virtio') {
    const have = vf.host.virtio_iso;
    return `<div class="vf-row"><div class="vf-field wide"><label>VirtIO drivers disc (for Windows)</label>${have ? `<div class="mono" style="font-size:12px">${_esc(have)} <span class="vm-dim">attached</span></div>` : `<div class="fm-warn" style="margin:0">Not downloaded yet. Windows cannot see a VirtIO disk or network card without these drivers. <a class="panel-action" onclick="vfGetVirtio()">Download virtio-win.iso</a> (about 700 MB, from the Fedora project).</div>`}</div><a class="panel-action vf-rm" onclick="vfDel('cdroms',${i})">Remove</a></div>`;
  }
  const isos = vf.host.isos;
  return `<div class="vf-row"><div class="vf-field wide"><label>CD image ${i + 1}</label><select class="settings-input" onchange="vfIso(${i}, this.value)"><option value="">(empty drive)</option>${isos.map(x => `<option value="${_esc(x.path)}" ${c.path === x.path ? 'selected' : ''}>${_esc(x.name)} (${vmSize(x.size)})</option>`).join('')}${c.path && !isos.some(x => x.path === c.path) ? `<option selected value="${_esc(c.path)}">${_esc(c.path)}</option>` : ''}</select>
    ${isos.length ? '' : `<div class="vf-help">No images found in ${_esc(vf.host.settings.isos)}. Put .iso files there (it is the "isos" share), or type a path below.</div>`}</div>
    ${vfIn('Bus', `cdroms.${i}.bus`, {options: [['sata', 'SATA'], ['ide', 'IDE'], ['usb', 'USB']]})}${vfIn('Or a path', `cdroms.${i}.path`, {ph: '/mnt/user/isos/x.iso', wide: true})}<a class="panel-action vf-rm" onclick="vfDel('cdroms',${i})">Remove</a></div>`;
}
function vfDiskRow(d, i) {
  const ex = vf.existingDisks.has(d.path), blk = d.type === 'block';
  return `<div class="vf-row"><div class="vf-num">${i + 1}</div>
    ${vfIn(blk ? 'Passed-through disk' : 'Location', `disks.${i}.path`, {ph: `${vf.host.settings.storage}/<name>/vdisk${i + 1}.qcow2 (automatic)`, wide: true, disabled: ex, help: ex ? 'existing vDisk (grow it from the VM list)' : ''})}
    ${blk || ex ? '' : vfIn('Size (GB)', `disks.${i}.size_gb`, {t: 'num', min: 0, max: 65536, help: 'only used when the file does not exist yet'})}
    ${blk ? '' : vfIn('Format', `disks.${i}.format`, {options: [['qcow2', 'qcow2'], ['raw', 'raw']], disabled: ex})}
    ${vfIn('Bus', `disks.${i}.bus`, {options: [['virtio', 'VirtIO'], ['sata', 'SATA'], ['scsi', 'SCSI'], ['usb', 'USB'], ['ide', 'IDE']], tip: 'bus'})}
    ${vfIn('Serial', `disks.${i}.serial`, {ph: 'optional'})}${vfIn('Discard', `disks.${i}.discard`, {bool: true, boolLabel: 'TRIM', tip: 'discard'})}${vfIn('SSD', `disks.${i}.ssd`, {bool: true, boolLabel: 'report as SSD'})}
    ${vfIn('Boot order', `disks.${i}.boot`, {t: 'num', min: 0, max: 9, help: '0 = not bootable'})}
    <a class="panel-action vf-rm" onclick="vfDel('disks',${i})">Remove</a></div>`;
}

// ── field helpers ───────────────────────────────────────────────────────────────────────────────
function vfAdd(list, item) { vf.spec[list].push(item); vfRender(); }
function vfDel(list, i) { vf.spec[list].splice(i, 1); vfRender(); }
function vfPin(n, on) { const s = new Set(vf.spec.pins); on ? s.add(n) : s.delete(n); vf.spec.pins = [...s].sort((a, b) => a - b); vfRender(); }
function vfPinAll(on) { vf.spec.pins = on ? vf.host.cpu.cores.flat().sort((a, b) => a - b) : []; vfRender(); }
function vfPci(addr, on) { vf.spec.pci = vf.spec.pci.filter(p => p.address !== addr); if (on) vf.spec.pci.push({address: addr}); vfRender(); }
function vfUsb(v, p, on) { vf.spec.usb = vf.spec.usb.filter(x => !(x.vendor === v && x.product === p)); if (on) vf.spec.usb.push({vendor: v, product: p}); }
function vfNic(i, val) { const [t, ...r] = val.split('|'); vf.spec.nics[i].type = t; vf.spec.nics[i].source = r.join('|'); }
function vfMac(i) { const b = () => Math.floor(Math.random() * 256).toString(16).padStart(2, '0'); vf.spec.nics[i].mac = `52:54:00:${b()}:${b()}:${b()}`; vfRender(); }
function vfIso(i, p) { vf.spec.cdroms[i].path = p; vfRender(); }
async function vfGetVirtio() {
  try { await vmApi('/virtio-iso', {method: 'POST'}); showToast('Downloading the VirtIO drivers in the background', 'ok'); } catch (e) { alert(vmErr(e)); return; }
  const t = setInterval(async () => { try { const j = (await vmApi('/jobs')).jobs.find(x => x.kind === 'virtio'); if (j && j.state !== 'running') { clearInterval(t); vf.host = await vmApi('/host'); if (j.state === 'failed') alert('Download failed: ' + j.message); vfRender(); } } catch (_) {} }, 3000);
}
async function vfSubmit() {
  const b = document.getElementById('vf-go'), err = document.getElementById('vf-err'); err.textContent = '';
  const spec = JSON.parse(JSON.stringify(vf.spec));
  spec.disks.forEach(d => { if (vf.existingDisks.has(d.path)) d.existing = true; });
  if (+spec.max_memory_mb && +spec.max_memory_mb <= +spec.memory_mb) spec.max_memory_mb = 0;   // 0 = same as initial, so raising memory never trips over an old max
  b.disabled = true; b.textContent = 'Working…';
  try {
    const r = vf.mode === 'edit'
      ? await vmApi('/' + encodeURIComponent(vf.name), {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({spec})})
      : await vmPost('/', {spec, start: vf.start, autostart: vf.autostart});
    if (r.start_error) showToast('Created, but it did not start: ' + r.start_error, 'warn');
    location.hash = '#vms';
  } catch (e) { err.textContent = vmErr(e); b.disabled = false; b.textContent = vf.mode === 'edit' ? 'Update' : 'Create'; }
}

// ── XML editor ──────────────────────────────────────────────────────────────────────────────────
async function vfXmlEditor(name, state) {
  const root = document.getElementById('vmform-body'); document.getElementById('vmform-title').textContent = `Edit XML: ${name}`;
  const x = await vmApi(`/${encodeURIComponent(name)}/xml`);
  root.innerHTML = `${state !== 'shut off' ? '<div class="vm-note warn">Shut the VM down before saving changes.</div>' : ''}
    <div class="vm-note">This is the libvirt definition of the VM. libvirt checks it when you save; a wrong edit can make the VM unbootable. The name must stay <b>${_esc(name)}</b>.</div>
    <textarea id="vfx" class="settings-input" spellcheck="false" style="width:100%;height:60vh;font-family:var(--mono,monospace);font-size:12px;white-space:pre">${_esc(x.xml)}</textarea>
    <div class="vf-foot"><div id="vfx-err" class="fm-err" style="flex:1"></div><button class="ctr-btn" onclick="location.hash='#vms'">Cancel</button><button class="settings-btn primary" onclick="vfXmlSave('${_esc(name)}')">Save</button></div>`;
}
async function vfXmlSave(name) {
  try { await vmApi(`/${encodeURIComponent(name)}/xml`, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({xml: document.getElementById('vfx').value})}); location.hash = '#vms'; }
  catch (e) { document.getElementById('vfx-err').textContent = vmErr(e); }
}

// ── VM Manager settings ─────────────────────────────────────────────────────────────────────────
async function loadVmSettings() {
  const root = document.getElementById('vmset-body'); if (!root) return;
  try {
    const [s, st, shr] = await Promise.all([vmApi('/settings'), vmApi('/'), fetchJSON('/api/files/list?path=/mnt/user').catch(() => ({entries: []}))]);
    vs.shares = shr.entries.filter(x => x.is_dir && !x.name.startsWith('.') && x.name !== 'system').map(x => x.name);   // top-level folders of the user share = shares
    const run = st.running;
    root.innerHTML = `<div class="vf-card"><div class="vf-grid">
      <div class="vf-field"><label>Enable VMs</label><label class="vf-check"><input type="checkbox" id="vs-en" ${s.enabled ? 'checked' : ''}> VM Manager on</label>
        <div class="vf-help">VMs only start after the array is started. ${st.kvm ? 'Hardware virtualization: available.' : '<b style="color:var(--err)">Hardware virtualization: not available</b>'}${st.installed ? '' : ' <b style="color:var(--err)">libvirt is not installed.</b>'}</div></div>
      <div class="vf-field"><label>libvirt</label><div style="font-size:12px">${_esc(s.libvirt_version || (run ? '' : 'not running'))}</div></div>
      <div class="vf-field wide"><label>Default VM storage location</label>${vsFolder('vs-storage', s.storage)}<div class="vf-help">New vDisks are created in a folder per VM below this. Pick a share: the Helix boot drive is a small flash drive and is not used.</div></div>
      <div class="vf-field wide"><label>Default ISO storage location</label>${vsFolder('vs-isos', s.isos)}</div>
      <div class="vf-field"><label>VirtIO drivers ISO</label>${s.virtio_iso_present ? `<div class="mono" style="font-size:12px">${_esc(s.virtio_iso)}</div><a class="panel-action" style="color:var(--err)" onclick="vsVirtio('DELETE')">Remove</a>` : `<a class="panel-action" onclick="vsVirtio('POST')">Download virtio-win.iso</a><div class="vf-help">Drivers for Windows guests, from the Fedora project (about 700 MB).</div>`}</div>
      <div class="vf-field"><label>Default network for new VMs</label><input class="settings-input" id="vs-net" value="${_esc(s.default_network)}"></div>
      <div class="vf-field"><label>When the array stops, running VMs</label><select class="settings-input" id="vs-act"><option value="stop" ${s.shutdown_action === 'stop' ? 'selected' : ''}>shut down</option><option value="hibernate" ${s.shutdown_action === 'hibernate' ? 'selected' : ''}>hibernate</option></select></div>
      <div class="vf-field"><label>Time to wait for them (seconds)</label><input class="settings-input" id="vs-to" type="number" min="10" max="600" value="${s.shutdown_timeout}"><div class="vf-help">After this a VM that has not shut down is forced off.</div></div>
      <div class="vf-field"><label>VM usage statistics</label><label class="vf-check"><input type="checkbox" id="vs-us" ${s.usage_stats ? 'checked' : ''}> show CPU and memory per VM</label></div></div>
      <div class="vf-foot"><div id="vs-err" class="fm-err" style="flex:1"></div><button class="ctr-btn" onclick="location.hash='#vms'">Done</button><button class="settings-btn primary" onclick="vsSave()">Apply</button></div></div>`;
  } catch (e) { root.innerHTML = `<div class="vm-note err">${_esc(vmErr(e))}</div>`; }
  loadVfio();
}
async function vsSave() {
  const g = (id) => document.getElementById(id);
  try {
    const r = await vmApi('/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: g('vs-en').checked, storage: vsVal('vs-storage'), isos: vsVal('vs-isos'), default_network: g('vs-net').value.trim(), shutdown_action: g('vs-act').value, shutdown_timeout: +g('vs-to').value, usage_stats: g('vs-us').checked})});
    showToast(r.message || 'VM Manager settings saved', r.message ? 'warn' : 'ok'); loadVmSettings();
  } catch (e) { g('vs-err').textContent = vmErr(e); }
}
async function vsVirtio(method) {
  try { await vmApi('/virtio-iso', {method}); showToast(method === 'POST' ? 'Downloading in the background' : 'Removed', 'ok'); setTimeout(loadVmSettings, 800); } catch (e) { alert(vmErr(e)); }
}

// folder control: a drop-down of the shares, plus "Choose folder…" which opens a folder picker
const vs = {shares: [], custom: {}};
function vsFolder(id, value) {
  const root = '/mnt/user/', isShare = vs.shares.some(n => root + n === value);
  if (!isShare) vs.custom[id] = value;
  const opts = vs.shares.map(n => `<option value="${_esc(root + n)}" ${root + n === value ? 'selected' : ''}>${_esc(n)} (share)</option>`);
  if (!isShare) opts.push(`<option value="${_esc(value)}" selected>${_esc(value)}</option>`);
  opts.push('<option value="__pick">Choose folder…</option>');
  return `<select class="settings-input" id="${id}" data-prev="${_esc(value)}" onchange="vsFolderChanged('${id}')">${opts.join('')}</select>`;
}
const vsVal = (id) => document.getElementById(id).value;
function vsFolderChanged(id) {
  const sel = document.getElementById(id);
  if (sel.value === '__pick') { const prev = sel.dataset.prev; sel.value = prev; vsPick(id, prev || '/mnt/user'); } else sel.dataset.prev = sel.value;
}
async function vsPick(id, path) {
  let d;
  try { d = await fetchJSON('/api/files/list?path=' + encodeURIComponent(path)); } catch (e) { alert(vmErr(e)); return; }
  const dirs = d.entries.filter(x => x.is_dir);
  showModal('Choose a folder', `<div style="font-size:12px;margin-bottom:8px" class="mono">${d.crumbs.map(c => `<a class="panel-action" onclick="vsPick('${id}','${_esc(c.path)}')">${_esc(c.name)}</a>`).join(' / ')}</div>
    <div style="max-height:44vh;overflow:auto;border:1px solid var(--border);border-radius:6px">${dirs.length ? dirs.map(x => `<div class="fm-pickrow" style="padding:6px 10px;cursor:pointer;border-bottom:1px solid var(--border)" onclick="vsPick('${id}','${_esc(x.path)}')">📁 ${_esc(x.name)}</div>`).join('') : '<div class="vm-dim" style="padding:10px">No subfolders</div>'}</div>
    <div class="vf-help" style="margin-top:6px">Selected: <b class="mono">${_esc(d.path)}</b>${d.path.startsWith('/mnt/') && d.path.length > 5 ? '' : ' (pick a folder inside /mnt)'}</div>`,
    `<button class="ctr-btn" onclick="closeModal()">Cancel</button><button class="settings-btn primary" ${d.path.startsWith('/mnt/') ? '' : 'disabled'} onclick="vsChosen('${id}','${_esc(d.path)}')">Use this folder</button>`);
}
function vsChosen(id, path) {
  const sel = document.getElementById(id);
  let o = [...sel.options].find(x => x.value === path);
  if (!o) { o = new Option(path, path); sel.insertBefore(o, sel.lastElementChild); }
  sel.value = path; sel.dataset.prev = path; closeModal();
}
