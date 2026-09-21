/* app.js — shared frontend utilities: API wrapper (CSRF), toasts, modals,
   formatting helpers, theme + sidebar behaviour. Vanilla JS, no frameworks. */
"use strict";

const App = (() => {
  // ------------------------------------------------------------ API client
  function csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  // Storage can be unavailable in embedded previews. Always retain the
  // current session in memory; storage is only an optional persistence layer.
  const SESSION_KEY = "bi_session";
  let memorySession = "";
  let sessionCleared = false;
  function sessionToken() {
    if (memorySession || sessionCleared) return memorySession;
    for (const name of ["localStorage", "sessionStorage"]) {
      try {
        const token = window[name].getItem(SESSION_KEY);
        if (token) return (memorySession = token);
      } catch (e) { /* storage denied */ }
    }
    return "";
  }
  function setSession(token) {
    if (!token) return;
    memorySession = token;
    sessionCleared = false;
    for (const name of ["localStorage", "sessionStorage"]) {
      try { window[name].setItem(SESSION_KEY, token); } catch (e) {}
    }
  }
  function clearSession() {
    memorySession = "";
    sessionCleared = true;
    for (const name of ["localStorage", "sessionStorage"]) {
      try { window[name].removeItem(SESSION_KEY); } catch (e) {}
    }
  }

  // A single-use page handoff carries the existing authenticated session
  // into this page even when BOTH cookies and browser storage are blocked.
  const bootstrap = document.getElementById("session-bootstrap");
  if (bootstrap) {
    try { setSession(JSON.parse(bootstrap.textContent)); } finally { bootstrap.remove(); }
  }
  const cleanUrl = new URL(location.href);
  if (cleanUrl.searchParams.has("st")) {
    cleanUrl.searchParams.delete("st");
    history.replaceState(null, "", cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
  }

  async function api(path, options = {}) {
    const opts = {
      method: options.method || "GET",
      headers: { "X-CSRF-Token": csrfToken(), ...(options.headers || {}) },
      credentials: "same-origin",
    };
    if (sessionToken()) opts.headers["Authorization"] = "Bearer " + sessionToken();
    if (options.body !== undefined) {
      if (options.body instanceof FormData) {
        opts.body = options.body; // browser sets multipart boundary
      } else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(options.body);
      }
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      throw new Error("Network error — is the server reachable?");
    }
    let data = null;
    const ct = resp.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      data = await resp.json().catch(() => null);
    } else if (ct.includes("text/xml") || ct.includes("application/xml")) {
      data = await resp.text();
    } else if (ct.includes("text/plain")) {
      data = await resp.text();
    }
    if (!resp.ok) {
      if (resp.status === 401 && sessionToken()) clearSession();
      const detail =
        (data && (data.detail || data.message)) ||
        (typeof data === "string" ? data.slice(0, 300) : null) ||
        `HTTP ${resp.status}`;
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  /** Use a one-time grant for same-origin page navigation, never a bearer
      token in a URL. Do not silently redirect into a login loop on failure. */
  async function gotoWithSession(next) {
    const dest = new URL(next, location.origin);
    if (!next.startsWith("/") || dest.origin !== location.origin) {
      throw new Error("Invalid redirect destination.");
    }
    dest.searchParams.delete("st");
    const h = await api("/api/auth/handoff", { method: "POST" });
    if (!h || !h.st) throw new Error("Could not continue your session. Please sign in again.");
    dest.searchParams.set("st", h.st);
    location.replace(dest.pathname + dest.search + dest.hash);
    return true;
  }

  function initSessionNavigation() {
    document.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !sessionToken()) return;
      const link = event.target.closest("a[href]");
      if (!link || link.target || link.hasAttribute("download") || link.id === "logout-btn") return;
      const url = new URL(link.href, location.href);
      if (url.origin !== location.origin || !/^\/(dashboard|submit|urls|pdfs|queue|monitoring|sitemap|rss|users|settings|integrations|logs|analyzer)(\/|$)/.test(url.pathname)) return;
      event.preventDefault();
      gotoWithSession(url.pathname + url.search + url.hash).catch((error) => {
        toast("Navigation failed", error.message, "error");
      });
    });
  }

  // ------------------------------------------------------------ toasts
  const ICONS = {
    success:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    error:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>',
    warn:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.8 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.8a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>',
    info:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></svg>',
  };

  let toastHost = null;
  function toastHostEl() {
    if (!toastHost) {
      toastHost = document.createElement("div");
      toastHost.className = "toasts";
      document.body.appendChild(toastHost);
    }
    return toastHost;
  }

  function toast(title, message, kind = "info", ttl = 5200) {
    const host = toastHostEl();
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = `<span class="t-icon">${ICONS[kind] || ICONS.info}</span>
      <span><span class="t-title">${esc(title)}</span><br>
      <span class="t-msg">${esc(message || "")}</span></span>`;
    host.appendChild(el);
    setTimeout(() => {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 300);
    }, ttl);
  }

  // ------------------------------------------------------------ modals
  let modalOverlay = null;
  function openModal(title, bodyHtml, footHtml) {
    closeModal(true);
    modalOverlay = document.createElement("div");
    modalOverlay.className = "modal-overlay";
    modalOverlay.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head"><h3>${esc(title)}</h3>
          <button class="icon-btn close" type="button" aria-label="Close">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="modal-body">${bodyHtml}</div>
        ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ""}
      </div>`;
    document.body.appendChild(modalOverlay);
    modalOverlay.addEventListener("mousedown", (e) => {
      if (e.target === modalOverlay) closeModal();
    });
    modalOverlay.querySelector(".close").addEventListener("click", () => closeModal());
    document.addEventListener("keydown", escClose);
    return modalOverlay;
  }

  function escClose(e) {
    if (e.key === "Escape") closeModal();
  }

  function closeModal(silent) {
    if (modalOverlay) {
      modalOverlay.remove();
      modalOverlay = null;
      document.removeEventListener("keydown", escClose);
    }
  }

  function confirmModal(title, message, { confirmLabel = "Confirm", danger = false } = {}) {
    return new Promise((resolve) => {
      const foot = `
        <button class="btn" id="cm-cancel" type="button">Cancel</button>
        <button class="btn ${danger ? "btn-danger" : "btn-primary"}" id="cm-ok" type="button">${esc(confirmLabel)}</button>`;
      const overlay = openModal(title, `<p style="margin:0;color:var(--muted)">${esc(message)}</p>`, foot);
      overlay.querySelector("#cm-cancel").addEventListener("click", () => {
        closeModal();
        resolve(false);
      });
      overlay.querySelector("#cm-ok").addEventListener("click", () => {
        closeModal();
        resolve(true);
      });
    });
  }

  // ------------------------------------------------------------ helpers
  function esc(s) {
    if (s === null || s === undefined) return "";
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  const PILL_MAP = {
    // validation / pdf
    VALID: ["green", "Valid"],
    VALID_PDF: ["green", "Valid"],
    PDF_VALID: ["green", "PDF Valid"],
    RECEIVED: ["blue", "Received"],
    VALIDATING: ["amber", "Validating", true],
    INVALID: ["red", "Invalid"],
    PDF_INVALID: ["red", "Invalid PDF"],
    PDF_ANALYSIS_FAILED: ["red", "Analysis Failed"],
    PDF_ANALYZING: ["amber", "Analyzing", true],
    PAGE_GENERATING: ["amber", "Generating Page", true],
    PAGE_PUBLISHED: ["green", "Published"],
    PAGE_FAILED: ["red", "Page Failed"],
    FAILED: ["red", "Failed"],
    // discovery
    DISCOVERY_PENDING: ["cyan", "Discovery Pending"],
    DISCOVERY_SUBMITTED: ["violet", "Discovery Submitted"],
    // crawl
    UNSUPPORTED: ["slate", "Unsupported — normal discovery"],
    HTML: ["blue", "HTML web page"],
    FETCH_CHECKED: ["blue", "Fetch Checked"],
    SEARCH_ENGINE_CRAWL_EVIDENCE: ["violet", "Search-engine crawl evidence"],
    DONE: ["green", "Done"],
    DISCOVERED: ["green", "Discovered"],
    NOT_SUBMITTED: ["slate", "Not submitted"],
    CRAWL_UNKNOWN: ["slate", "Crawl Unknown"],
    CRAWL_OBSERVED: ["blue", "Crawl Observed"],
    CRAWL_CHECKED: ["violet", "Crawl Checked"],
    // index
    INDEX_UNKNOWN: ["slate", "Unknown"],
    INDEXED: ["green", "Indexed"],
    NOT_INDEXED: ["red", "Not Indexed"],
    // classification
    TEXT_PDF: ["green", "Text PDF"],
    SCANNED_OR_EMPTY_PDF: ["amber", "Scanned / Empty"],
    INVALID_PDF: ["red", "Invalid PDF"],
    ERROR: ["red", "Error"],
    // jobs
    PENDING: ["amber", "Pending", true],
    RUNNING: ["blue", "Running", true],
    RETRY_WAITING: ["amber", "Retry Waiting", true],
    COMPLETED: ["green", "Completed"],
    CANCELLED: ["slate", "Cancelled"],
    // users
    ADMIN: ["violet", "Admin"],
    USER: ["slate", "User"],
    ACTIVE: ["green", "Active"],
    DISABLED: ["red", "Disabled"],
    // integrations
    CONNECTED: ["green", "Connected"],
    "NOT CONNECTED": ["slate", "Not Connected"],
    "NOT CONFIGURED": ["slate", "Not Configured"],
    NOT_AUTHORIZED: ["amber", "Not Authorized"],
    ERROR_STATE: ["red", "Error"],
    OBSERVED: ["blue", "Observed"],
    NOT_OBSERVED: ["amber", "Not Observed"],
    UNKNOWN: ["slate", "Unknown"],
  };

  function pill(status) {
    if (!status) return '<span class="pill slate"><span class="dot"></span>N/A</span>';
    const key = String(status).toUpperCase().replace(/\s+/g, "_");
    const spec = PILL_MAP[key] || ["slate", String(status)];
    const [color, label, pulse] = spec;
    return `<span class="pill ${color}"><span class="dot ${pulse ? "pulse" : ""}"></span>${esc(label)}</span>`;
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return esc(iso);
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function timeAgo(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return "—";
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function fmtBytes(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let v = Number(n);
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function shortUrl(u, n = 52) {
    if (!u) return "—";
    return u.length > n ? u.slice(0, n - 1) + "…" : u;
  }

  function initials(name) {
    return (name || "?")
      .split(/\s+/)
      .slice(0, 2)
      .map((p) => p[0])
      .join("")
      .toUpperCase();
  }

  // ------------------------------------------------------------ theme
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("bi-theme"); } catch (e) {}
    if (saved) document.documentElement.setAttribute("data-theme", saved);
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        const cur = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", cur);
        try { localStorage.setItem("bi-theme", cur); } catch (e) {}
      });
    }
  }

  // ------------------------------------------------------------ sidebar (mobile)
  function initSidebar() {
    const toggle = document.getElementById("menu-toggle");
    const sidebar = document.getElementById("sidebar");
    const backdrop = document.getElementById("sidebar-backdrop");
    if (!toggle || !sidebar) return;
    toggle.addEventListener("click", () => {
      sidebar.classList.toggle("open");
      if (backdrop) backdrop.classList.toggle("show", sidebar.classList.contains("open"));
    });
    if (backdrop) {
      backdrop.addEventListener("click", () => {
        sidebar.classList.remove("open");
        backdrop.classList.remove("show");
      });
    }
  }

  // ------------------------------------------------------------ logout
  function initLogout() {
    const btn = document.getElementById("logout-btn");
    if (!btn) return;
    btn.addEventListener("click", async (event) => {
      event.preventDefault();
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch (e) {
        /* ignore */
      }
      clearSession();
      window.location.href = "/login";
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initSidebar();
    initLogout();
    initSessionNavigation();
  });

  return {
    api, toast, openModal, closeModal, confirmModal, esc, pill, fmtDate, timeAgo,
    fmtBytes, shortUrl, initials, csrfToken, sessionToken, setSession, clearSession,
    gotoWithSession,
  };
})();
