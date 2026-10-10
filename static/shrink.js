(function () {
  const cfg = window.SHRINK;
  const form = document.getElementById('shrink-form');
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const bodies = Array.from(document.querySelectorAll('.tab-body'));
  const store = (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} };
  const load = (k) => { try { return sessionStorage.getItem(k); } catch (e) { return null; } };

  function show(name) {
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    bodies.forEach(b => {
      const on = b.dataset.body === name;
      b.hidden = !on;
      const frame = b.querySelector('iframe[data-src]');
      if (on && frame && !frame.src) frame.src = frame.dataset.src;   // load previews only when opened
    });
    store(cfg.storeKey, name);
  }
  tabs.forEach(t => t.addEventListener('click', () => show(t.dataset.tab)));
  const saved = load(cfg.storeKey);
  show(tabs.some(t => t.dataset.tab === saved) ? saved : 'original');

  // Options with warnings need the "checked the preview" box before Save is enabled
  document.querySelectorAll('[data-needs-confirm]').forEach(btn => {
    const box = document.querySelector(`[data-confirm-for="${btn.value}"]`);
    const sync = () => { btn.disabled = !box.checked; };
    box.addEventListener('change', sync);
    sync();
  });

  // Unsaved details or text: warn before leaving
  const inputs = Array.from(form.querySelectorAll('.meta-grid input[name]'));
  inputs.forEach(i => i.addEventListener('input', () => i.classList.toggle('changed', i.value !== i.defaultValue)));
  const box = document.getElementById('text-box');
  const dirty = () => inputs.some(i => i.value !== i.defaultValue) || (box && box.value !== box.defaultValue);

  // ---- Text only tab ----
  if (box) {
    const preview = document.getElementById('text-preview');
    const status = document.getElementById('text-status');
    const draftNote = document.getElementById('draft-note');
    const wordCount = document.getElementById('word-count');
    const confirmWrap = document.getElementById('text-confirm-wrap');
    const csrf = form.querySelector('input[name=_csrf]').value;
    const fmt = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.round(n / 1024) + ' KB';
    const countWords = () => { wordCount.textContent = (box.value.match(/\S+/g) || []).length + ' words'; };

    // Draft: unsaved edits survive a reload (kept in this browser only)
    try {
      const draft = localStorage.getItem(cfg.draftKey);
      if (draft !== null && draft !== box.defaultValue) {
        box.value = draft;
        draftNote.textContent = 'Unsaved edits restored.';
      }
    } catch (e) {}
    let draftTimer, previewTimer, previewUrl;
    const saveDraft = () => { try {
      if (box.value === box.defaultValue) localStorage.removeItem(cfg.draftKey); else localStorage.setItem(cfg.draftKey, box.value);
    } catch (e) {} };

    async function refresh() {
      status.textContent = 'Updating preview…';
      const body = new FormData();
      body.append('_csrf', csrf);
      body.append('text', box.value);
      try {
        const r = await fetch(cfg.previewUrl, {method: 'POST', body});
        if (!r.ok) { status.textContent = 'Preview failed: ' + (await r.text()).replace(/<[^>]+>/g, ' ').trim().slice(0, 200); return; }
        const xml = Number(r.headers.get('X-Xml-Bytes')), saving = Number(r.headers.get('X-Xml-Saving'));
        const identical = r.headers.get('X-Text-Identical') === '1';
        const edited = Number(r.headers.get('X-Edited-Words'));
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = URL.createObjectURL(await r.blob());
        preview.src = previewUrl;
        document.getElementById('text-xml-size').textContent = fmt(xml);
        document.getElementById('text-saving').textContent = saving > 0 ? 'saves ' + fmt(saving) : '';
        const tabSize = document.getElementById('text-tab-size');
        if (tabSize) tabSize.textContent = fmt(xml);
        status.innerHTML = (identical ? '<span class="ok">✓ The PDF contains exactly this text.</span>'
                                      : '<span class="warn">⚠ The PDF does not contain exactly this text (check the preview).</span>')
          + (edited ? ` <span class="muted">${edited} word(s) changed from the ${box.dataset.source || 'original'} text.</span>` : '');
        confirmWrap.hidden = identical;
      } catch (e) { status.textContent = 'Preview failed.'; }
    }

    box.addEventListener('input', () => {
      countWords();
      clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 500);
      clearTimeout(previewTimer); previewTimer = setTimeout(refresh, 1500);
    });
    document.getElementById('refresh-preview').addEventListener('click', refresh);
    document.getElementById('reset-text').addEventListener('click', () => {
      if (box.value !== box.defaultValue && !confirm('Discard your edits and go back to the original text?')) return;
      box.value = box.defaultValue; saveDraft(); countWords(); draftNote.textContent = ''; refresh();
    });
    document.getElementById('open-original').addEventListener('click', e => {
      // One named window, reused on every click
      const w = window.open(e.currentTarget.dataset.href, 'curator-original', 'width=900,height=1100,scrollbars=yes,resizable=yes');
      if (w) w.focus();
    });
    countWords();
    if (box.value !== box.defaultValue) { preview.dataset.src = ''; setTimeout(refresh, 300); }
  }
  let submitting = false;
  form.addEventListener('submit', () => { submitting = true; });
  window.addEventListener('beforeunload', e => { if (!submitting && dirty()) { e.preventDefault(); e.returnValue = ''; } });

  // While the options are being prepared, poll and reload when ready (unless details are being edited)
  if (cfg.waiting) {
    const poll = async () => {
      try {
        const r = await fetch(cfg.statusUrl, {cache: 'no-store'});
        const s = await r.json();
        if (s.status !== 'running' && s.text !== 'running') {
          if (!dirty()) { submitting = true; location.reload(); return; }
          document.querySelectorAll('.tab .spinner').forEach(el => { el.classList.remove('spinner'); el.textContent = 'ready – reload'; });
          return;
        }
      } catch (e) {}
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 1500);
  }
})();
