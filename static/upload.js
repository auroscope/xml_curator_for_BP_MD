// Upload boxes (form[data-upload]): drop a file or choose one, and it is sent in the background with a progress bar.
// The server answers with where to go next ({"location": ...}); flash messages are waiting for that page.
(function () {
  const mb = n => (n / 1048576).toFixed(1) + ' MB';

  document.querySelectorAll('form[data-upload]').forEach(form => {
    const input = form.querySelector('input[type=file]');
    const zone = form.querySelector('.dropzone');
    const box = form.querySelector('.upload-progress');
    const bar = box.querySelector('progress');
    const status = box.querySelector('.upload-status');
    const cancel = box.querySelector('.upload-cancel');
    let xhr = null;

    function reset(message) {
      xhr = null;
      box.hidden = true;
      zone.hidden = false;
      input.value = '';
      if (message) {
        const div = document.createElement('div');
        div.className = 'flash flash-danger upload-error';
        div.textContent = message;
        form.querySelectorAll('.upload-error').forEach(e => e.remove());
        form.appendChild(div);
      }
    }

    function send(file) {
      if (!file || xhr) return;
      form.querySelectorAll('.upload-error').forEach(e => e.remove());
      zone.hidden = true;
      box.hidden = false;
      cancel.hidden = false;
      box.querySelector('.upload-name').textContent = file.name;
      bar.max = file.size || 1;
      bar.value = 0;
      status.textContent = 'Starting the upload…';

      const data = new FormData(form);
      data.set('file', file, file.name);
      xhr = new XMLHttpRequest();
      xhr.open('POST', form.action);
      xhr.setRequestHeader('X-Upload', '1');
      xhr.upload.addEventListener('progress', e => {
        if (!e.lengthComputable) return;
        bar.max = e.total;
        bar.value = e.loaded;
        status.textContent = `Uploading: ${mb(e.loaded)} of ${mb(e.total)} · ${Math.floor(100 * e.loaded / e.total)}%`;
      });
      xhr.upload.addEventListener('load', () => {
        bar.removeAttribute('value');                       // indeterminate while the server reads the file
        cancel.hidden = true;
        status.textContent = 'Uploaded. Processing the file…';
      });
      xhr.addEventListener('load', () => {
        let next = null;
        try { next = JSON.parse(xhr.responseText).location; } catch (e) {}
        if (xhr.status === 200 && next) { location.href = next; return; }
        reset(xhr.status === 413 ? 'The file is larger than the upload limit.'
                                 : `The upload failed (${xhr.status || 'no response'}). Please try again.`);
      });
      xhr.addEventListener('error', () => reset('The upload failed: the connection to the server was lost.'));
      xhr.addEventListener('abort', () => reset());
      xhr.send(data);
    }

    cancel.addEventListener('click', () => { if (xhr) xhr.abort(); });
    input.addEventListener('change', () => send(input.files[0]));
    ['dragenter', 'dragover'].forEach(t => zone.addEventListener(t, e => { e.preventDefault(); zone.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => zone.addEventListener(t, () => zone.classList.remove('over')));
    zone.addEventListener('drop', e => { e.preventDefault(); send(e.dataTransfer.files[0]); });
  });

  // A file dropped outside a box would otherwise be opened by the browser, leaving the app
  ['dragover', 'drop'].forEach(t => window.addEventListener(t, e => {
    if (!e.target.closest || !e.target.closest('.dropzone')) e.preventDefault();
  }));
})();
