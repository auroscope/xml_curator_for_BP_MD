(function () {
  const form = document.getElementById('items-form');
  const store = (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} };
  const load = (k) => { try { return sessionStorage.getItem(k); } catch (e) { return null; } };
  const openKey = `curator:${window.SUMMARY.wid}:open-sections`;
  const secs = Array.from(document.querySelectorAll('.sec'));

  function setOpen(sec, open) {
    sec.classList.toggle('collapsed', !open);
    const t = sec.querySelector('.sec-toggle');
    t.textContent = open ? '▾' : '▸';
    t.setAttribute('aria-expanded', String(open));
  }
  function saveOpen() { store(openKey, JSON.stringify(secs.filter(s => !s.classList.contains('collapsed')).map(s => s.id))); }

  const saved = JSON.parse(load(openKey) || 'null');
  if (saved) secs.forEach(s => setOpen(s, saved.includes(s.id)));
  if (location.hash) { const s = document.getElementById(location.hash.slice(1)); if (s && s.classList.contains('sec')) setOpen(s, true); }

  secs.forEach(sec => sec.querySelector('.sec-head').addEventListener('click', e => {
    if (e.target.closest('a, label, input')) return;
    setOpen(sec, sec.classList.contains('collapsed'));
    saveOpen();
  }));
  document.querySelectorAll('.summary-nav a[href^="#"]').forEach(a => a.addEventListener('click', () => {
    const s = document.getElementById(a.getAttribute('href').slice(1));
    if (s) { setOpen(s, true); saveOpen(); }
  }));
  document.getElementById('expand-all').addEventListener('click', () => { secs.forEach(s => setOpen(s, true)); saveOpen(); });
  document.getElementById('collapse-all').addEventListener('click', () => { secs.forEach(s => setOpen(s, false)); saveOpen(); });

  const boxes = () => Array.from(form.querySelectorAll('input[name="keys"]'));
  const btn = document.getElementById('delete-btn');
  const info = document.getElementById('sel-info');
  function update() {
    const n = boxes().filter(b => b.checked).length;
    btn.disabled = n === 0;
    info.textContent = n ? `${n} selected` : '';
    document.querySelectorAll('[data-select-section]').forEach(all => {
      const own = Array.from(all.closest('.sec').querySelectorAll('input[name="keys"]'));
      const c = own.filter(b => b.checked).length;
      all.checked = own.length > 0 && c === own.length;
      all.indeterminate = c > 0 && c < own.length;
    });
  }
  form.addEventListener('change', e => {
    if (e.target.matches('[data-select-section]')) {
      e.target.closest('.sec').querySelectorAll('input[name="keys"]').forEach(b => { b.checked = e.target.checked; });
    }
    update();
  });
  form.addEventListener('submit', e => {
    const n = boxes().filter(b => b.checked).length;
    if (!confirm(`Delete ${n} item(s) from the working copy?\n\nThe original upload is not changed, and deleted items can be restored.`)) e.preventDefault();
  });
  update();
})();
