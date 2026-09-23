/* Real vacancies: public pages, employer source links and evidence-based Google status. */
(() => {
  const form = document.getElementById("real-job-form");
  const rows = document.getElementById("real-job-rows");
  const note = document.getElementById("real-job-note");
  const importResult = document.getElementById("real-job-import-result");
  let items = [], editId = null;
  const esc = App.esc;

  function reset() {
    editId = null;
    form.reset();
    form.elements.date_posted.readOnly = false;
    document.getElementById("form-heading").textContent = "Publish a real vacancy";
    document.getElementById("publish-job").textContent = "Publish & queue Google notification";
  }

  function error(e) {
    const details = e.data && e.data.errors;
    App.toast("Action failed", details ? details.map(x => x.error || x.msg).join("; ") : e.message, "error");
  }

  function pillText(value) {
    return esc(String(value || "UNKNOWN").replaceAll("_", " "));
  }

  function render(items) {
    rows.innerHTML = items.map(p => {
      const source = p.sourceUrl
        ? `<a href="${esc(p.sourceUrl)}" target="_blank" rel="noopener">${esc(new URL(p.sourceUrl).hostname)}</a>`
        : "—";
      const queue = p.queue && p.queue.status
        ? `${pillText(p.queue.status)}<div class="small faint">${p.queue.maxAttempts == null ? esc((p.queue.attempts || 0) + " attempts · auto retry") : esc((p.queue.attempts || 0) + "/" + (p.queue.maxAttempts || 0))}</div>`
        : "—";
      const gsc = p.gsc || {};
      const gscEvidence = gsc.evidence || {};
      const crawl = gsc.crawlStatus || "UNKNOWN";
      const index = gsc.indexStatus || "UNKNOWN";
      const indexDetail = gscEvidence.coverage_state ? `<div class="small faint">${esc(gscEvidence.coverage_state)}</div>` : "";
      return `<tr>
        <td><a href="${esc(p.path)}" target="_blank" rel="noopener"><strong>${esc(p.job.title || "Untitled")}</strong></a><div class="small faint">${esc(p.job.company || "")} · #${esc(p.number)}</div></td>
        <td><a href="${esc(p.publicUrl)}" target="_blank" rel="noopener">Open page</a><div class="small faint">${esc(p.publicUrl)}</div></td>
        <td>${source}</td>
        <td>${pillText(p.notificationStatus)}<div class="small faint">${esc(p.notificationResult.message || "")}</div></td>
        <td>${queue}${p.queue && p.queue.error ? `<div class="small recent-failure">${esc(p.queue.error)}</div>` : ""}</td>
        <td>${pillText(crawl)}${gsc.lastCheckedAt ? `<div class="small faint">${esc(gsc.lastCheckedAt)}</div>` : ""}</td>
        <td>${pillText(index)}${indexDetail}</td>
        <td><div class="flex wrap">
          ${p.status === "OPEN" ? `<button class="btn btn-sm" data-action="edit" data-id="${p.id}">Edit</button><button class="btn btn-sm" data-action="close" data-id="${p.id}">Close</button>` : ""}
          <button class="btn btn-sm" data-action="inspect" data-id="${p.id}">Inspect GSC</button>
        </div></td>
      </tr>`;
    }).join("");
  }

  async function load() {
    try {
      const data = await App.api("/api/real-jobs/dashboard");
      items = data.items || [];
      const c = data.counts || {};
      for (const [id, key] of [
        ["rj-total","total"],["rj-sent","sent"],["rj-queued","queued"],["rj-indexed","indexed"],
        ["rj-not-indexed","notIndexed"],["rj-unknown","unknown"]
      ]) document.getElementById(id).textContent = c[key] ?? 0;
      render(items);
      note.textContent = items.length
        ? "Google accepted = notification delivery only. Indexed/Not Indexed appears only when Search Console provides evidence for your own public job page."
        : "No real vacancies published yet.";
    } catch (e) {
      note.textContent = e.message;
    }
  }

  form.addEventListener("submit", async e => {
    e.preventDefault();
    const button = document.getElementById("publish-job");
    button.disabled = true;
    try {
      const body = Object.fromEntries(new FormData(form));
      body.authorized_real_vacancy = document.getElementById("job-attestation").checked;
      const result = await App.api("/api/real-jobs" + (editId ? "/" + editId : ""), {
        method: editId ? "PUT" : "POST",
        body
      });
      App.toast(result.duplicate ? "Already published" : "Vacancy saved", "Google status: " + result.notificationStatus + ". Transient failures retry automatically.", "success");
      reset();
      await load();
    } catch (err) {
      error(err);
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("import-real-jobs").addEventListener("click", async () => {
    const input = document.getElementById("real-job-sheet");
    if (!input.files.length) return App.toast("Select a sheet", "Choose a CSV or XLSX file first.", "error");
    if (!document.getElementById("bulk-attestation").checked) return App.toast("Authorization required", "Confirm that every vacancy in the sheet is genuine and authorized.", "error");
    const button = document.getElementById("import-real-jobs");
    button.disabled = true;
    importResult.textContent = "Importing vacancies and queueing Google notifications…";
    try {
      const fd = new FormData();
      fd.append("file", input.files[0]);
      fd.append("authorized_bulk", "true");
      const result = await App.api("/api/real-jobs/import", {method: "POST", body: fd});
      importResult.textContent = `Rows: ${result.totalRows}. Created: ${result.created.length}. Duplicates: ${result.duplicates.length}. Errors: ${result.errors.length}.`;
      if (result.errors.length) console.warn("Real-job import errors", result.errors);
      input.value = "";
      await load();
    } catch (err) {
      importResult.textContent = err.message;
      error(err);
    } finally {
      button.disabled = false;
    }
  });

  rows.addEventListener("click", async e => {
    const button = e.target.closest("button[data-action]");
    if (!button) return;
    const row = items.find(p => String(p.id) === button.dataset.id);
    if (!row) return;

    if (button.dataset.action === "edit") {
      editId = row.id;
      for (const [key, value] of Object.entries(row.job || {})) if (form.elements[key]) form.elements[key].value = value || "";
      document.getElementById("job-attestation").checked = false;
      form.elements.date_posted.readOnly = true;
      document.getElementById("form-heading").textContent = "Edit vacancy #" + row.number;
      document.getElementById("publish-job").textContent = "Save & queue update";
      form.scrollIntoView({behavior: "smooth"});
      return;
    }

    if (button.dataset.action === "close") {
      const ok = await App.confirmModal(
        "Close this vacancy?",
        "The public page remains available but becomes noindex, Apply is disabled, sitemap/RSS inclusion stops, and URL_DELETED is queued.",
        {confirmLabel: "Close vacancy", danger: true}
      );
      if (!ok) return;
    }

    button.disabled = true;
    try {
      const action = button.dataset.action;
      await App.api(`/api/real-jobs/${row.id}/${action}`, {method: "POST"});
      await load();
    } catch (err) {
      error(err);
      button.disabled = false;
    }
  });

  document.getElementById("reset-job").addEventListener("click", reset);
  document.getElementById("refresh-jobs").addEventListener("click", load);
  load();
  setInterval(() => { if (!document.hidden) load(); }, 10000);
})();
