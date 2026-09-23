/* Keys are uploaded once; never displayed or persisted in browser storage. */
(() => {
  const note = document.getElementById('google-note');
  const fileInput = document.getElementById('google-key-file');
  function show(data) {
    const fields = {origin: data.origin, email: data.clientEmail || '—', project: data.projectId || '—',
      'key-status': data.configured ? 'Stored on server' : 'Not configured', enabled: data.enabled ? 'Enabled (eligible real jobs only)' : 'Paused',
      limits: `${data.minuteLimit}/minute · ${data.dailyLimit}/rolling 24 hours`};
    for (const [id, value] of Object.entries(fields)) document.getElementById('google-' + id).textContent = value;
    note.textContent = data.note;
  }
  async function action(button, fn) {
    button.disabled = true;
    try { show(await fn()); App.toast('Google setup updated', 'No indexing claim is made.', 'success'); }
    catch (err) { note.textContent = err.message; App.toast('Google setup failed', err.message, 'error'); }
    finally { button.disabled = false; }
  }
  document.getElementById('google-key-form').addEventListener('submit', e => {
    e.preventDefault();
    action(e.currentTarget.querySelector('button'), async () => {
      try {
        const file = fileInput.files[0];
        if (!file || file.size > 32768) throw new Error('Choose a service-account JSON file up to 32 KiB.');
        let body;
        try { body = JSON.parse(await file.text()); } catch (_) { throw new Error('The selected file is not valid JSON.'); }
        return await App.api('/api/google-indexing/credentials', {method: 'POST', body});
      } finally { fileInput.value = ''; document.getElementById('google-approval').checked = false; }
    });
  });
  document.getElementById('google-enable').addEventListener('click', e => action(e.currentTarget, () => {
    if (!document.getElementById('google-approval').checked) throw new Error('Confirm required API enablement and Google approval/quota first.');
    return App.api('/api/google-indexing/enable', {method: 'POST', body: {approved_api_usage: true}});
  }));
  document.getElementById('google-pause').addEventListener('click', e => action(e.currentTarget, () => App.api('/api/google-indexing/pause', {method: 'POST'})));
  document.getElementById('google-remove').addEventListener('click', async e => {
    const button = e.currentTarget;
    if (await App.confirmModal('Remove service-account key?', 'Pauses delivery and removes the local key. Revoke it separately in Google Cloud if required.', {confirmLabel: 'Remove', danger: true})) {
      action(button, () => App.api('/api/google-indexing/credentials', {method: 'DELETE'}));
    }
  });
  App.api('/api/google-indexing').then(show).catch(err => { note.textContent = err.message; });
})();
