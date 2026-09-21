/* users.js — admin user management: table, create/edit/reset/delete modals */
"use strict";

(() => {
  const { api, esc, pill, fmtDate, timeAgo, openModal, closeModal, confirmModal, toast, initials } = App;
  let users = [];

  function el(id) { return document.getElementById(id); }

  function render() {
    const tbody = el("users-tbody");
    const empty = el("users-empty");
    if (!users.length) {
      tbody.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";
    tbody.innerHTML = users
      .map((u) => `
      <tr data-id="${u.id}">
        <td>
          <div class="flex">
            <span class="avatar">${esc(initials(u.name))}</span>
            <div><div class="cell-main">${esc(u.name)}</div>
            <div class="cell-sub">${esc(u.email)}</div></div>
          </div>
        </td>
        <td>${pill(u.role)}</td>
        <td>${pill(u.status)}</td>
        <td class="cell-sub">${fmtDate(u.created_at)}</td>
        <td class="cell-sub">${u.last_login ? `${timeAgo(u.last_login)}` : "never"}</td>
        <td class="right">
          <div class="flex" style="justify-content:flex-end;gap:4px">
            <button class="btn btn-sm btn-ghost" data-act="edit" title="Edit">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
            </button>
            <button class="btn btn-sm btn-ghost" data-act="reset" title="Reset password">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
            </button>
            <button class="btn btn-sm btn-ghost" data-act="toggle" title="${u.status === "ACTIVE" ? "Disable" : "Enable"}">
              ${u.status === "ACTIVE"
                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>'
                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" width="14" height="14"><path d="M20 6 9 17l-5-5"/></svg>'}
            </button>
            <button class="btn btn-sm btn-ghost" data-act="delete" title="Delete" style="color:var(--red)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
            </button>
          </div>
        </td>
      </tr>`)
      .join("");
    bindRows();
  }

  function bindRows() {
    el("users-tbody").querySelectorAll("tr").forEach((tr) => {
      const u = users.find((x) => String(x.id) === tr.dataset.id);
      if (!u) return;
      tr.querySelectorAll("[data-act]").forEach((btn) =>
        btn.addEventListener("click", () => act(btn.dataset.act, u))
      );
    });
  }

  // ------------------------------------------------------------ modals
  function userFormFields(u) {
    return `
      <div class="field"><label for="f-name">Name</label>
        <input class="input" id="f-name" value="${esc(u.name || "")}" required></div>
      <div class="field"><label for="f-email">Email</label>
        <input class="input" id="f-email" type="email" value="${esc(u.email || "")}" required></div>
      <div class="form-row">
        <div class="field"><label for="f-role">Role</label>
          <select class="input" id="f-role">
            <option value="USER" ${u.role === "USER" ? "selected" : ""}>USER</option>
            <option value="ADMIN" ${u.role === "ADMIN" ? "selected" : ""}>ADMIN</option>
          </select></div>
        <div class="field"><label for="f-status">Status</label>
          <select class="input" id="f-status">
            <option value="ACTIVE" ${u.status === "ACTIVE" ? "selected" : ""}>ACTIVE</option>
            <option value="DISABLED" ${u.status === "DISABLED" ? "selected" : ""}>DISABLED</option>
          </select></div>
      </div>`;
  }

  function openCreate() {
    const overlay = openModal(
      "Create user",
      userFormFields({ name: "", email: "", role: "USER", status: "ACTIVE" }) +
        `<div class="field"><label for="f-pass">Password</label>
        <input class="input" id="f-pass" type="password" required minlength="8" placeholder="min 8 chars, letters + numbers">
        <span class="hint">Strong password: at least 8 characters with letters and a number.</span></div>`,
      `<button class="btn" type="button" id="m-cancel">Cancel</button>
       <button class="btn btn-primary" type="button" id="m-save">Create user</button>`
    );
    overlay.querySelector("#m-cancel").addEventListener("click", closeModal);
    overlay.querySelector("#m-save").addEventListener("click", async () => {
      const body = {
        name: overlay.querySelector("#f-name").value,
        email: overlay.querySelector("#f-email").value,
        role: overlay.querySelector("#f-role").value,
        status: overlay.querySelector("#f-status").value,
        password: overlay.querySelector("#f-pass").value,
      };
      try {
        await api("/api/users", { method: "POST", body });
        toast("User created", `${body.name} can now sign in.`, "success");
        closeModal();
        load();
      } catch (e) {
        toast("Could not create user", e.message, "error");
      }
    });
  }

  function openEdit(u) {
    const overlay = openModal(
      `Edit user — ${u.email}`,
      userFormFields(u),
      `<button class="btn" type="button" id="m-cancel">Cancel</button>
       <button class="btn btn-primary" type="button" id="m-save">Save changes</button>`
    );
    overlay.querySelector("#m-cancel").addEventListener("click", closeModal);
    overlay.querySelector("#m-save").addEventListener("click", async () => {
      const body = {
        name: overlay.querySelector("#f-name").value,
        email: overlay.querySelector("#f-email").value,
        role: overlay.querySelector("#f-role").value,
        status: overlay.querySelector("#f-status").value,
      };
      try {
        await api(`/api/users/${u.id}`, { method: "PUT", body });
        toast("User updated", `${u.email} saved.`, "success");
        closeModal();
        load();
      } catch (e) {
        toast("Update failed", e.message, "error");
      }
    });
  }

  function openReset(u) {
    const overlay = openModal(
      `Reset password — ${u.email}`,
      `<div class="field"><label for="f-pass">New password</label>
        <input class="input" id="f-pass" type="password" required minlength="8" placeholder="min 8 chars, letters + numbers">
        <span class="hint">All existing sessions for this user will be invalidated.</span></div>`,
      `<button class="btn" type="button" id="m-cancel">Cancel</button>
       <button class="btn btn-primary" type="button" id="m-save">Reset password</button>`
    );
    overlay.querySelector("#m-cancel").addEventListener("click", closeModal);
    overlay.querySelector("#m-save").addEventListener("click", async () => {
      try {
        await api(`/api/users/${u.id}/reset-password`, {
          method: "POST",
          body: { password: overlay.querySelector("#f-pass").value },
        });
        toast("Password reset", `New password set for ${u.email}.`, "success");
        closeModal();
      } catch (e) {
        toast("Reset failed", e.message, "error");
      }
    });
  }

  async function act(action, u) {
    switch (action) {
      case "edit":
        openEdit(u);
        break;
      case "reset":
        openReset(u);
        break;
      case "toggle": {
        const next = u.status === "ACTIVE" ? "DISABLED" : "ACTIVE";
        const ok = await confirmModal(
          next === "DISABLED" ? "Disable user?" : "Enable user?",
          next === "DISABLED"
            ? `${u.email} will no longer be able to sign in.`
            : `${u.email} will be able to sign in again.`
        );
        if (!ok) return;
        try {
          await api(`/api/users/${u.id}`, { method: "PUT", body: { name: u.name, email: u.email, role: u.role, status: next } });
          toast(next === "ACTIVE" ? "User enabled" : "User disabled", u.email, "success");
          load();
        } catch (e) {
          toast("Action failed", e.message, "error");
        }
        break;
      }
      case "delete": {
        const ok = await confirmModal("Delete user?", `${u.email} will be permanently deleted. Their submissions and audit events are kept for auditing.`, { confirmLabel: "Delete user", danger: true });
        if (!ok) return;
        try {
          await api(`/api/users/${u.id}`, { method: "DELETE" });
          toast("User deleted", u.email, "success");
          load();
        } catch (e) {
          toast("Delete failed", e.message, "error");
        }
        break;
      }
    }
  }

  async function load() {
    try {
      const data = await api("/api/users");
      users = data.users;
      render();
    } catch (e) {
      toast("Failed to load users", e.message, "error");
    }
  }

  function start() {
    const btn = el("user-create-btn");
    if (btn) btn.addEventListener("click", openCreate);
    load();
  }

  document.addEventListener("DOMContentLoaded", start);
})();
