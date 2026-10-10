(function () {
  const wid = window.CURATOR.wid;
  const list = document.getElementById('doc-list');
  const form = document.getElementById('delete-form');
  const filter = document.getElementById('filter');
  const sort = document.getElementById('sort');
  const selectAll = document.getElementById('select-all');
  const deleteBtn = document.getElementById('delete-btn');
  const info = document.getElementById('selection-info');
  const store = (k, v) => { try { v === undefined ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (e) {} };
  const load = (k) => { try { return sessionStorage.getItem(k); } catch (e) { return null; } };
  const K = (name) => `curator:${wid}:${name}`;

  const items = () => Array.from(list.querySelectorAll('.doc-item'));
  const boxes = () => Array.from(list.querySelectorAll('input[name="keys"]'));
  const visibleBoxes = () => boxes().filter(b => !b.closest('.doc-item').hidden);

  function fmtSize(n) {
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return Math.round(n / 1024) + ' KB';
    return n + ' B';
  }

  function updateSelection() {
    const checked = boxes().filter(b => b.checked);
    const total = checked.reduce((s, b) => s + Number(b.dataset.size || 0), 0);
    deleteBtn.disabled = checked.length === 0;
    info.textContent = checked.length ? `${checked.length} selected · ${fmtSize(total)}` : '';
    const vis = visibleBoxes();
    const visChecked = vis.filter(b => b.checked).length;
    selectAll.checked = vis.length > 0 && visChecked === vis.length;
    selectAll.indeterminate = visChecked > 0 && visChecked < vis.length;
    store(K('checked'), JSON.stringify(checked.map(b => b.value)));
  }

  function applyFilter() {
    const q = filter.value.trim().toLowerCase();
    items().forEach(it => { it.hidden = q && !it.dataset.text.includes(q); });
    list.querySelectorAll('.section').forEach(sec => {
      sec.hidden = !Array.from(sec.querySelectorAll('.doc-item')).some(it => !it.hidden);
    });
    store(K('filter'), filter.value);
    updateSelection();
  }

  function applySort() {
    const by = sort.value;
    list.querySelectorAll('.section-items').forEach(box => {
      const arr = Array.from(box.children);
      arr.sort((a, b) => by === 'size'
        ? Number(b.dataset.size) - Number(a.dataset.size)
        : (b.dataset.date || '').localeCompare(a.dataset.date || ''));
      arr.forEach(el => box.appendChild(el));
    });
    store(K('sort'), by);
  }

  // Restore state
  const savedChecked = JSON.parse(load(K('checked')) || '[]');
  boxes().forEach(b => { b.checked = savedChecked.includes(b.value); });
  filter.value = load(K('filter')) || '';
  sort.value = load(K('sort')) || 'date';
  applySort();
  applyFilter();
  const savedScroll = load(K('scroll'));
  if (savedScroll) list.scrollTop = Number(savedScroll);
  else { const a = list.querySelector('.doc-item.active'); if (a) a.scrollIntoView({block: 'center'}); }

  // Events
  list.addEventListener('scroll', () => store(K('scroll'), String(list.scrollTop)));
  list.addEventListener('click', e => {
    if (e.target.matches('input[type=checkbox]')) e.stopPropagation();
    const icon = e.target.closest('.shrink-icon');
    if (icon) { e.preventDefault(); e.stopPropagation(); location.href = icon.dataset.href; }
  });
  list.addEventListener('keydown', e => {
    const icon = e.target.closest('.shrink-icon');
    if (icon && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); location.href = icon.dataset.href; }
  });
  boxes().forEach(b => b.addEventListener('change', updateSelection));
  filter.addEventListener('input', applyFilter);
  sort.addEventListener('change', applySort);
  selectAll.addEventListener('change', () => {
    visibleBoxes().forEach(b => { b.checked = selectAll.checked; });
    updateSelection();
  });

  // Metadata editor: toggle, highlight changed fields, warn before leaving with unsaved edits
  const panel = document.getElementById('meta-panel');
  const toggle = document.getElementById('edit-toggle');
  if (panel && toggle) {
    const inputs = Array.from(panel.querySelectorAll('input[name]:not([type=hidden])'));
    const dirty = () => inputs.some(i => i.value !== i.defaultValue);
    const open = (show) => {
      panel.hidden = !show;
      toggle.setAttribute('aria-expanded', String(show));
      toggle.classList.toggle('btn-primary', show);
      if (show) inputs[0].focus();
    };
    toggle.addEventListener('click', () => {
      if (!panel.hidden && dirty() && !confirm('Discard the unsaved changes to the details?')) return;
      if (!panel.hidden) inputs.forEach(i => { i.value = i.defaultValue; i.classList.remove('changed'); });
      open(panel.hidden);
    });
    document.getElementById('meta-cancel').addEventListener('click', () => {
      inputs.forEach(i => { i.value = i.defaultValue; i.classList.remove('changed'); });
      open(false);
    });
    inputs.forEach(i => i.addEventListener('input', () => i.classList.toggle('changed', i.value !== i.defaultValue)));
    panel.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('meta-cancel').click(); });
    let submitting = false;
    panel.addEventListener('submit', () => { submitting = true; });
    window.addEventListener('beforeunload', e => { if (!submitting && dirty()) { e.preventDefault(); e.returnValue = ''; } });
  }

  form.addEventListener('submit', e => {
    const checked = boxes().filter(b => b.checked);
    const total = checked.reduce((s, b) => s + Number(b.dataset.size || 0), 0);
    if (!confirm(`Delete ${checked.length} document(s) (${fmtSize(total)}) from the working copy?\n\n` +
                 'The original upload is not changed, and deleted documents can be restored.')) {
      e.preventDefault();
      return;
    }
    // After deleting, stay near the current position: the first visible unchecked item after the active one.
    const order = items().filter(it => !it.hidden);
    const idx = Math.max(0, order.findIndex(it => it.dataset.key === window.CURATOR.activeKey));
    const next = order.slice(idx).concat(order.slice(0, idx)).find(it => !it.querySelector('input').checked);
    document.getElementById('next-key').value = next ? next.dataset.key : '';
    store(K('checked'));
  });
})();
