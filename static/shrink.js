(function () {
  const cfg = window.SHRINK;
  const form = document.getElementById('shrink-form');
  const tabs = () => Array.from(document.querySelectorAll('.tab'));
  const bodies = () => Array.from(document.querySelectorAll('.tab-body'));
  let active = 'original';
  const store = (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} };
  const load = (k) => { try { return sessionStorage.getItem(k); } catch (e) { return null; } };

  function show(name) {
    active = name;
    tabs().forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    bodies().forEach(b => {
      const on = b.dataset.body === name;
      b.hidden = !on;
      const frame = b.querySelector('iframe[data-src]');
      if (on && frame && !frame.src) frame.src = frame.dataset.src;   // load previews only when opened
    });
    store(cfg.storeKey, name);
  }
  document.querySelector('.tabs').addEventListener('click', e => {
    const t = e.target.closest('.tab');
    if (t) show(t.dataset.tab);
  });
  const saved = load(cfg.storeKey);
  show(tabs().some(t => t.dataset.tab === saved) ? saved : 'original');

  // Options with warnings need the "checked the preview" box before Save is enabled
  function bindConfirms(root) {
    root.querySelectorAll('[data-needs-confirm]').forEach(btn => {
      const box = document.querySelector(`[data-confirm-for="${btn.value}"]`);
      if (!box) return;
      const sync = () => { btn.disabled = !box.checked; };
      box.addEventListener('change', sync);
      sync();
    });
  }
  bindConfirms(document);

  // Unsaved details or text: warn before leaving
  const inputs = Array.from(form.querySelectorAll('.meta-grid input[name]'));
  inputs.forEach(i => i.addEventListener('input', () => i.classList.toggle('changed', i.value !== i.defaultValue)));
  const textBox = () => document.getElementById('text-box');
  const dirty = () => inputs.some(i => i.value !== i.defaultValue) || (textBox() && textBox().value !== textBox().defaultValue);

  // ---- Text only tab (set up on load, or when it is swapped in after OCR) ----
  function initText() {
    const box = textBox();
    if (!box || box.dataset.ready) return;
    box.dataset.ready = '1';
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
  initText();
  let submitting = false;
  form.addEventListener('submit', () => { submitting = true; });
  // Delete document: confirmed by the shared data-confirm handler; once confirmed, no "unsaved changes" prompt
  const delForm = document.getElementById('shrink-delete-form');
  if (delForm) delForm.addEventListener('submit', () => { window.curatorLeaving = true; });
  window.addEventListener('beforeunload', e => {
    if (!submitting && !window.curatorLeaving && dirty()) { e.preventDefault(); e.returnValue = ''; }
  });

  // While the options are being prepared: show each tab's progress, and when the options change (one finished,
  // OCR done), swap in just the changed tabs. The Original tab and its preview are never reloaded.
  function showProgress(p) {
    const stage = p.stage;
    const label = {strip: 'Strip images', compress: 'Compressed scan', text: 'Text only', rtfpic: 'Compress pictures',
                   rtfnopic: 'Remove pictures', imgpdf: 'PDF (lossless)', imgjpeg: 'Compressed (keeps colour)'};
    document.querySelectorAll('[data-progress]').forEach(el => {
      const opt = el.dataset.progress;
      const mine = p[opt], all = p['*'];
      let msg = null, done = null, total = null;
      if (stage === opt && mine) ({msg, done, total} = mine);
      else if (stage === '*' && all) msg = all.msg + '…';
      else if (mine) msg = 'Done; shown when the other options are ready.';
      else if (stage && label[stage]) msg = `Queued: starts after ${label[stage]}.`;
      if (msg === null) return;
      el.querySelector('.progress-msg').textContent = msg + (stage === opt && !total ? '…' : '');
      const bar = el.querySelector('.progress-bar');
      bar.hidden = !total;
      if (total) {
        bar.querySelector('progress').max = total;
        bar.querySelector('progress').value = done;
        bar.querySelector('.progress-count').textContent = `${done} / ${total}`;
      }
      const pct = document.querySelector(`[data-pct="${opt}"]`);
      if (pct) pct.textContent = stage === opt && total ? ` ${Math.floor(100 * done / total)}%` : '';
    });
  }

  async function swapTabs() {
    const r = await fetch(location.href, {cache: 'no-store'});
    if (!r.ok) throw new Error('reload failed');
    const fresh = new DOMParser().parseFromString(await r.text(), 'text/html');
    fresh.querySelectorAll('.tab').forEach(t => {
      const old = document.querySelector(`.tab[data-tab="${t.dataset.tab}"]`);
      if (old && t.dataset.tab !== 'original') old.replaceWith(document.importNode(t, true));
    });
    fresh.querySelectorAll('.tab-body').forEach(b => {
      const old = document.querySelector(`.tab-body[data-body="${b.dataset.body}"]`);
      if (!old || b.dataset.body === 'original') return;
      const box = old.querySelector('#text-box');
      if (box && box.value !== box.defaultValue) return;          // never throw away edited text
      const node = document.importNode(b, true);
      old.replaceWith(node);
      bindConfirms(node);
    });
    const title = fresh.querySelector('.shrink-title'), oldTitle = document.querySelector('.shrink-title');
    if (title && oldTitle) oldTitle.replaceWith(document.importNode(title, true));
    show(active);
    initText();
  }

  if (cfg.waiting) {
    let key = cfg.stateKey;
    const poll = async () => {
      let again = true;
      try {
        const r = await fetch(cfg.statusUrl, {cache: 'no-store'});
        const s = await r.json();
        if (s.key !== key) { await swapTabs(); key = s.key; }
        showProgress(s.progress || {});
        again = s.status === 'running' || s.text === 'running';
      } catch (e) {}
      if (again) setTimeout(poll, 1500);
    };
    setTimeout(poll, 300);
  }
})();
