/* Real vacancies: all text is escaped; no automatic/random content generation. */
(() => {
  const form = document.getElementById('real-job-form');
  const rows = document.getElementById('real-job-rows');
  const note = document.getElementById('real-job-note');
  let items = [], editId = null;
  const esc = App.esc;
  function reset() {
    editId = null; form.reset(); form.elements.date_posted.readOnly = false;
    document.getElementById('form-heading').textContent = 'Publish a real vacancy';
    document.getElementById('publish-job').textContent = 'Publish & queue notification';
  }
  function error(e) {
    const details = e.data && e.data.errors;
    App.toast('Action failed', details ? details.map(x => x.msg).join('; ') : e.message, 'error');
  }
  async function load() {
    try {
      items = (await App.api('/api/real-jobs')).items;
      rows.innerHTML = items.map(p => `<tr><td><a href="${esc(p.path)}" target="_blank" rel="noopener">${esc(p.job.title)}</a><div class="small faint">${esc(p.job.company)} · #${esc(p.number)}</div></td><td>${esc(p.status)}</td><td>${esc(p.notificationStatus)}<div class="small faint">${esc(p.notificationResult.message || '')}</div></td><td>${esc(p.indexStatus)}</td><td><div class="flex wrap">${p.status === 'OPEN' ? `<button class="btn btn-sm" data-action="edit" data-id="${p.id}">Edit</button><button class="btn btn-sm" data-action="close" data-id="${p.id}">Close vacancy</button>` : ''}${['FAILED','CANCELLED'].includes(p.notificationStatus) ? `<button class="btn btn-sm" data-action="retry" data-id="${p.id}">Retry notification</button>` : ''}</div></td></tr>`).join('');
      note.textContent = items.length ? 'Delivery status only. ACCEPTED means notification received, not indexed. Waiting tasks survive restart.' : 'No real vacancies published yet. Existing generated demos are intentionally not listed here.';
    } catch (e) { note.textContent = e.message; }
  }
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const button = document.getElementById('publish-job'); button.disabled = true;
    try {
      const body = Object.fromEntries(new FormData(form));
      body.authorized_real_vacancy = document.getElementById('job-attestation').checked;
      const result = await App.api('/api/real-jobs' + (editId ? '/' + editId : ''), {method: editId ? 'PUT' : 'POST', body});
      App.toast(result.duplicate ? 'Already published' : 'Vacancy saved', 'Notification status: ' + result.notificationStatus, 'success');
      reset(); await load();
    } catch (err) { error(err); } finally { button.disabled = false; }
  });
  rows.addEventListener('click', async e => {
    const button = e.target.closest('button[data-action]'); if (!button) return;
    const row = items.find(p => String(p.id) === button.dataset.id); if (!row) return;
    if (button.dataset.action === 'edit') {
      editId = row.id;
      for (const [key, value] of Object.entries(row.job)) if (form.elements[key]) form.elements[key].value = value;
      document.getElementById('job-attestation').checked = false;
      form.elements.date_posted.readOnly = true;
      document.getElementById('form-heading').textContent = 'Edit vacancy #' + row.number;
      document.getElementById('publish-job').textContent = 'Save & queue update';
      form.scrollIntoView({behavior: 'smooth'}); return;
    }
    if (button.dataset.action === 'close' && !await App.confirmModal('Close this vacancy?', 'Disables Apply, removes JobPosting markup, marks the page noindex and queues URL_DELETED. This cannot be reopened.', {confirmLabel: 'Close vacancy', danger: true})) return;
    button.disabled = true;
    try { await App.api(`/api/real-jobs/${row.id}/${button.dataset.action}`, {method:'POST'}); await load(); }
    catch (err) { error(err); button.disabled = false; }
  });
  document.getElementById('reset-job').addEventListener('click', reset);
  document.getElementById('refresh-jobs').addEventListener('click', load);
  load(); setInterval(() => { if (!document.hidden) load(); }, 10000);
})();
