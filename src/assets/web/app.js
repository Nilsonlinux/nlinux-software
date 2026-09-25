"use strict";

/* ============================== Estado ================================== */
const state = { data: null, cat: "all", q: "", busy: false };
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// localStorage can be unavailable inside embedded WebKitGTK windows
// (enable-local-storage off by default). Fall back to in-memory storage.
const store = (() => {
  try {
    localStorage.setItem("__nlix_probe", "1");
    localStorage.removeItem("__nlix_probe");
    return localStorage;
  } catch (e) {
    const m = new Map();
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => m.set(k, String(v)),
      removeItem: (k) => m.delete(k),
    };
  }
})();

// Report JavaScript errors to the local backend so the native window can be
// debugged from the server log (no browser developer tools inside WebKitGTK).
function reportErr(msg) {
  try {
    fetch("/api/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg: String(msg).slice(0, 500) }),
    }).catch(() => {});
  } catch (e) { /* ignore */ }
}
window.addEventListener("error", (ev) =>
  reportErr(`${ev.message} @ ${(ev.filename || "").split("/").pop() || "?"}:${ev.lineno || "?"}`));
window.addEventListener("unhandledrejection", (ev) =>
  reportErr(`unhandledrejection: ${(ev.reason && ev.reason.message) || ev.reason || "?"}`));

const CATEGORY_LABELS = {
  all: "Todos",
  accessibility: "Acessibilidade",
  accessories: "Utilitários",
  development: "Desenvolvimento",
  education: "Educação",
  games: "Jogos",
  graphics: "Gráficos",
  internet: "Internet",
  "more-software": "Mais software",
  multimedia: "Multimídia",
  office: "Escritório",
  server: "Servidor",
  system: "Sistema",
};

const SOURCE_LABELS = {
  arch: "Repositório oficial",
  aur: "AUR",
  manual: "Site oficial",
};

const CAT_ICONS = {
  all: '<path d="M12 3l2.1 5.6L20 10.5l-5.9 1.9L12 18l-2.1-5.6L4 10.5l5.9-1.9L12 3z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z"/>',
  accessibility: '<circle cx="12" cy="5" r="2.2"/><path d="M4 8.6h16M12 8.6v5.8m0 0l-3.4 5.6M12 14.4l3.4 5.6"/>',
  accessories: '<path d="M14.3 6.1a3.6 3.6 0 1 0-5 5l-4.6 4.6a1.8 1.8 0 0 0 2.6 2.5l4.6-4.5a3.6 3.6 0 0 0 5-5l-2.3 2.3-2.5-2.5 2.2-2.4z"/><path d="M15.5 8.5 20 4"/>',
  development: '<path d="M7.5 8.5 3.5 12l4 3.5M16.5 8.5l4 3.5-4 3.5M13.2 6l-2.4 12"/>',
  education: '<path d="M2.5 8.2 12 4l9.5 4.2L12 12.4 2.5 8.2z"/><path d="M6.5 10.6v4c0 1.7 2.4 3.2 5.5 3.2s5.5-1.5 5.5-3.2v-4"/><path d="M21.5 8.2v6"/>',
  games: '<rect x="2.5" y="7" width="19" height="10" rx="5"/><path d="M7.5 10v4M5.5 12h4M15 11h.01M17.5 13h.01"/>',
  graphics: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8.5" cy="9" r="1.6"/><path d="M21 15.5 16 10.5 8 18.5 4.5 15"/>',
  internet: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5a13 13 0 0 1 0 17M12 3.5a13 13 0 0 0 0 17"/>',
  "more-software": '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
  multimedia: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5L10 9.5z"/>',
  office: '<rect x="3" y="7.5" width="18" height="12.5" rx="2.5"/><path d="M8 7.5V6a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v1.5M3 12h18"/>',
  server: '<rect x="3" y="4" width="18" height="6.5" rx="2"/><rect x="3" y="13.5" width="18" height="6.5" rx="2"/><path d="M7 7.2h.01M7 16.7h.01"/>',
  system: '<rect x="3" y="4" width="18" height="11.5" rx="2.5"/><path d="M8.5 20h7M12 15.5V20"/>',
};

const THEME_LABELS = { dark: "Tema: escuro", light: "Tema: claro" };

/* ============================== Utilidades ============================== */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function stripTags(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = String(html ?? "");
  return tpl.content.textContent || "";
}

function sanitize(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = String(html ?? "");
  const allow = ["B", "I", "U", "EM", "STRONG", "BR", "P", "CODE", "SMALL", "UL", "OL", "LI", "A"];
  for (const el of tpl.content.querySelectorAll("*")) {
    if (!allow.includes(el.tagName)) {
      el.replaceWith(document.createTextNode(el.textContent));
      continue;
    }
    if (el.tagName === "A") {
      const href = el.getAttribute("href") || "";
      if (!/^https?:/i.test(href)) el.removeAttribute("href");
      else { el.target = "_blank"; el.rel = "noopener"; el.className = "m-link"; }
    }
  }
  return tpl.innerHTML;
}

function highlight(text, q) {
  const e = esc(text);
  if (!q) return e;
  const escq = esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  try { return e.replace(new RegExp("(" + escq + ")", "ig"), "<mark>$1</mark>"); }
  catch { return e; }
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

/* ============================== Toasts =================================== */
function toast(msg, kind = "info") {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 320);
  }, 3400);
}

/* ============================== App icons ================================ */
function mountIcon(img, fallback) {
  img.addEventListener("error", function handler() {
    img.removeEventListener("error", handler);
    const s = document.createElement("span");
    s.className = "icon-fallback";
    s.textContent = (fallback || "?").charAt(0).toUpperCase();
    img.replaceWith(s);
  });
}

/* ============================== Tema ===================================== */
function applyTheme(mode) {
  store.setItem("theme", mode);
  document.documentElement.setAttribute("data-theme", mode);
  renderThemeGlyph();
}

function renderThemeGlyph() {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const ic = $("#theme-ic");
  if (ic) ic.className = dark ? "ti ti-sun" : "ti ti-moon";
  const btn = $("#theme-toggle");
  if (btn) btn.dataset.tip = THEME_LABELS[dark ? "dark" : "light"];
}

function themeInit() {
  const cur = document.documentElement.getAttribute("data-theme");
  if (cur !== "light" && cur !== "dark") applyTheme("dark");
  else renderThemeGlyph();
  $("#theme-toggle").addEventListener("click", () => {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    applyTheme(isDark ? "light" : "dark");
  });
}

/* ============================== Header =================================== */
function renderHeader(info) {
  $("#arch-badge").textContent = info.system.arch;
  $("#brand-name").textContent = "NLinux";
  $("#foot-info").textContent =
    `Nlinux-Software · ${info.total} aplicativos`;
  const al = $("#admin-link");
  if (al) al.style.display = info.admin ? "" : "none";
}

/* ============================== Categorias =============================== */
function renderCats(cats) {
  const nav = $("#cats");
  const list = [{ id: "all", count: state.data.total }, ...cats];
  nav.innerHTML = list.map((c, i) => {
    const label = CATEGORY_LABELS[c.id] || c.id;
    return `<button class="cat tip ${c.id === "all" ? "active" : ""}" data-cat="${c.id}" data-tip="${esc(
      label.toLowerCase()
    )}" style="animation:fadeUp .4s ${i * 25}ms both">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${CAT_ICONS[c.id] || CAT_ICONS.all}</svg>
      <span>${label}</span><b>${c.count}</b>
    </button>`;
  }).join("");
  nav.querySelectorAll(".cat").forEach((b) =>
    b.addEventListener("click", () => {
      state.cat = b.dataset.cat;
      nav.querySelectorAll(".cat").forEach((x) => x.classList.toggle("active", x === b));
      render(true);
    })
  );
}

/* ============================== Grid ===================================== */
function skeletonCards(n = 8) {
  $("#grid").innerHTML = Array.from({ length: n }, () =>
    '<div class="skeleton"><div class="sk-line sk-ic"></div><div class="sk-line sk-t1"></div><div class="sk-line sk-t2"></div><div class="sk-line sk-t3"></div></div>').join("");
}

function visibleProducts() {
  const q = state.q.trim().toLowerCase();
  const qs = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = qs ? new RegExp(qs, "i") : null;
  return state.data.products.filter((p) => {
    if (state.cat !== "all" && p.category !== state.cat) return false;
    if (!re) return true;
    return re.test(p.name) || re.test(p.summary) || re.test(p.description) ||
           re.test(p.developer || "") || re.test(p.category);
  });
}

function installActionHtml(p) {
  if (p.source === "aur") {
    const url = `https://aur.archlinux.org/packages/${encodeURIComponent(p.packages[0] || "")}`;
    return `<a class="btn btn-ghost tip" data-tip="Página no AUR" href="${esc(url)}" target="_blank" rel="noopener"><i class="ti ti-package"></i> AUR</a>` +
      `<button class="btn btn-primary btn-install tip" data-action="install" data-key="${esc(p.key)}" data-tip="AUR: ${esc(p.packages.join(", "))}" ${state.busy ? "disabled" : ""}><i class="ti ti-download"></i> ${p.installed ? "Reinstalar" : "Instalar"}</button>`;
  }
  if (p.source === "manual") {
    if (p.website) return `<a class="btn btn-ghost tip" data-tip="Baixar no site oficial" href="${esc(p.website)}" target="_blank" rel="noopener"><i class="ti ti-external-link"></i> Site oficial</a>`;
    return `<button class="btn btn-ghost" disabled><i class="ti ti-tools"></i> Manual</button>`;
  }
  const label = p.installed ? "Reinstalar" : "Instalar";
  return `<button class="btn btn-primary btn-install tip" data-action="install" data-key="${esc(p.key)}" data-tip="Pacote: ${esc(p.packages.join(", "))}" ${state.busy ? "disabled" : ""}><i class="ti ti-download"></i> ${label}</button>`;
}

function chipFor(p) {
  if (p.source !== "arch") return `<span class="chip">${SOURCE_LABELS[p.source] || p.source}</span>`;
  if (p.proprietary) return `<span class="chip">Proprietário</span>`;
  return `<span class="chip chip-official">Oficial</span>`;
}

function cardHtml(p, i) {
  const installedChip = p.installed
    ? '<span class="installed-chip">✓ instalado</span>' : "";
  const chip = chipFor(p);
  return `
  <article class="card" data-key="${esc(p.key)}" style="--i:${Math.min(i, 18)}">
    ${installedChip}
    <div class="card-top">
      <div class="icon-wrap tip" data-tip="${esc(p.name)} — ver detalhes">
        <img src="${esc(p.icon)}" alt="" loading="lazy" data-fb="${esc(p.name)}">
      </div>
      <div>
        <h3>${highlight(p.name, state.q)}</h3>
        <p class="dev">${esc(p.developer || "")}</p>
      </div>
    </div>
    <div class="desc">${highlight(stripTags(p.description), state.q)}</div>
    ${chip ? `<div class="chip-row">${chip}</div>` : ""}
    <div class="actions">
      <button class="btn btn-ghost" data-action="details" data-key="${esc(p.key)}"><i class="ti ti-info-circle"></i> Detalhes</button>
      ${installActionHtml(p)}
    </div>
  </article>`;
}

function render(animate = true) {
  const grid = $("#grid");
  const products = visibleProducts();
  $("#results-count").textContent =
    `Mostrando ${products.length} de ${state.data.total} aplicativos`;
  $("#empty").hidden = products.length > 0;

  grid.innerHTML = products.map(cardHtml).join("");
  grid.querySelectorAll("img").forEach((img) => mountIcon(img, img.dataset.fb));
  if (!animate) {
    grid.querySelectorAll(".card").forEach((c) => (c.style.animation = "none"));
  }
}

/* ============================== Modal ==================================== */
function openModal(product) {
  $("#modal").hidden = false;
  const desc = sanitize(product.description);
  const meta = `
    <div class="meta">
      <div><dt>Licença</dt><dd>${product.proprietary ? "Proprietária" : "Open Source"}</dd></div>
      <div><dt>Plataforma</dt><dd>${product.arches.map((a) =>
        `<span class="${a === state.data.system.arch ? "arch-cur" : "arch-oth"}">${a}</span>`).join("")}</dd></div>
      <div><dt>Pacote${product.packages.length > 1 ? "s" : ""}</dt><dd class="m-pkgs">${esc(product.packages.join(", "))}</dd></div>
      <div><dt>Fonte</dt><dd class="m-src">${SOURCE_LABELS[product.source] || product.source}${product.source === "arch" && !product.proprietary ? `<span class="chip chip-official">Oficial</span>` : ""}</dd></div>
      <div><dt>Desenvolvedor</dt><dd>${esc(product.developer || "—")}</dd></div>
    </div>`;
  const gallery = product.screenshots.length
    ? `<div class="gallery"><h4>Capturas de tela</h4><div class="gallery-strip">${product.screenshots.map((s, i) =>
        `<img src="${esc(s)}" alt="Captura ${i + 1}" loading="lazy" data-gallery="${esc(product.key)}" data-i="${i}">`).join("")}</div></div>`
    : '<p class="no-gallery">Sem capturas de tela.</p>';

  $("#modal-content").innerHTML = `
    <div class="m-head">
      <div class="m-icon"><img src="${esc(product.icon)}" alt="" data-fb="${esc(product.name)}"></div>
      <div class="m-titles">
        <h2>${esc(product.name)}</h2>
        <p class="m-dev">${esc(product.developer || "")}</p>
        <p class="m-sum">${esc(product.summary)}</p>
        <div class="m-actions">${modalActionsHtml(product)}</div>
      </div>
    </div>
    <div class="m-body"><p>${desc}</p></div>
    ${meta}
    ${gallery}`;
  $("#modal-content").querySelectorAll("img").forEach((img) => mountIcon(img, img.dataset.fb));

  $("#modal-content").querySelectorAll("[data-action=more-screens]").forEach((a) =>
    a.addEventListener("click", () => openLightbox(product, 0)));
  $("#modal-content").querySelectorAll(".gallery-strip img").forEach((img) =>
    img.addEventListener("click", () => openLightbox(product, +img.dataset.i)));
  bindModalActions(true);
}

function modalActionsHtml(p) {
  if (p.source !== "arch") {
    if (p.source === "aur") {
      const url = `https://aur.archlinux.org/packages/${encodeURIComponent(p.packages[0] || "")}`;
      return `<a class="btn btn-ghost tip" data-tip="Página do AUR" href="${esc(url)}" target="_blank" rel="noopener"><i class="ti ti-package"></i> AUR</a>` +
        `<button class="btn btn-primary btn-install" data-action="install" data-key="${esc(p.key)}" ${state.busy ? "disabled" : ""}><i class="ti ti-download"></i> ${p.installed ? "Reinstalar" : "Instalar"}</button>`;
    }
    if (p.website) return `<a class="btn btn-primary" href="${esc(p.website)}" target="_blank" rel="noopener"><i class="ti ti-external-link"></i> Site oficial</a>`;
    return `<button class="btn btn-primary" disabled><i class="ti ti-alert-triangle"></i> Sem pacote</button>`;
  }
  return `<button class="btn btn-primary btn-install" data-action="install" data-key="${esc(p.key)}" ${state.busy ? "disabled" : ""}><i class="ti ti-download"></i> ${p.installed ? "Reinstalar" : "Instalar"}</button>`;
}

function fromKey(key) {
  return state.data.products.find((p) => p.key === key);
}

/* ============================== Lightbox ================================= */
let lbKey = null, lbIdx = 0;
function openLightbox(product, idx) {
  const shots = product.screenshots;
  if (!shots.length) return;
  lbKey = product.key; lbIdx = idx;
  $("#lightbox-img").src = shots[idx];
  $("#lightbox").hidden = false;
}
function lbStep(d) {
  const p = fromKey(lbKey);
  if (!p || !p.screenshots.length) return;
  lbIdx = (lbIdx + d + p.screenshots.length) % p.screenshots.length;
  $("#lightbox-img").src = p.screenshots[lbIdx];
}

/* ============================== Instalação =============================== */
function statusCard(title, line, mode) {
  const sc = $("#install-status");
  sc.hidden = false;
  sc.classList.toggle("done", mode === "done");
  sc.classList.toggle("error", mode === "error");
  $("#status-title").textContent = title;
  $("#status-line").textContent = line || "";
}

function setBusy(busy) {
  state.busy = busy;
  $$("#grid [data-action=install]").forEach((b) => (b.disabled = busy));
}

async function startInstall(product, btn) {
  if (state.busy) { toast("Aguarde a instalação atual terminar.", "info"); return; }
  if (!product.packages.length) { toast("Este aplicativo não tem pacotes definidos.", "err"); return; }

  state.busy = true;
  setBusy(true);
  const card = btn.closest(".card");
  if (card) card.classList.add("installing-card");
  btn.classList.add("is-running");

  statusCard(`Instalando ${product.name}…`, "Aguardando autorização do sistema…", "");

  let job;
  try {
    job = await api("/api/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ packages: product.packages, source: product.source }),
    });
  } catch (e) {
    finishInstall(false, product, btn, card);
    toast("Não foi possível iniciar a instalação.", "err");
    return;
  }

  (async function poll() {
    const st = await api("/api/status?id=" + job.id).catch(() => null);
    if (!st) { statusCard(`Instalando ${product.name}…`, "Verificando…", ""); }
    else if (st.state === "pending") {
      const waiting = st.lines.length <= 1;
      const last = waiting
        ? "Aguardando autorização do sistema…"
        : st.lines[st.lines.length - 1];
      statusCard(`Instalando ${product.name}…`, last, "");
      setTimeout(poll, 1400);
    } else {
      const ok = st.done && st.success;
      finishInstall(ok, product, btn, card);
      if (ok) {
        const last = st.lines[st.lines.length - 1] || "";
        statusCard(`${product.name} instalado`, last, "done");
        toast(`${product.name} foi instalado com sucesso.`, "succ");
      } else {
        const last = st.lines[st.lines.length - 1] || "";
        statusCard(`Falha ao instalar ${product.name}`, last, "error");
        toast(`Falha ao instalar ${product.name}.`, "err");
      }
      setTimeout(() => { $("#install-status").hidden = true; }, 4200);
    }
  })();
}

function finishInstall(ok, product, btn, card) {
  btn.classList.remove("is-running");
  if (card) card.classList.remove("installing-card");
  if (ok) {
    const p = fromKey(product.key);
    if (p) p.installed = true;
  }
  state.busy = false;
  setBusy(false);
  render(false);
}

/* ============================== Delegation =============================== */
function bindDelegates() {
  $("#grid").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const product = fromKey(btn.dataset.key);
    if (!product) return;
    if (btn.dataset.action === "install") startInstall(product, btn);
    else if (btn.dataset.action === "details") openModal(product);
  });

  $("#grid").addEventListener("click", (ev) => {
    const wrap = ev.target.closest(".icon-wrap");
    if (!wrap) return;
    const card = ev.target.closest(".card");
    if (card) openModal(fromKey(card.dataset.key));
  });
}

function bindModalActions() {
  const content = $("#modal-content");
  content.querySelectorAll("[data-action=install]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const product = fromKey(btn.dataset.key);
      if (product) startInstall(product, btn);
    }));
}

function bindGlobal() {
  $("#search").addEventListener("input", (ev) => {
    const esc2 = ev.target.value;
    clearTimeout(state._dt);
    state._dt = setTimeout(() => { state.q = esc2; render(false); }, 120);
  });

  // External links: open in the system browser (works inside the native WebView).
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[target=_blank]");
    if (a && window.pywebview && window.pywebview.api
        && typeof window.pywebview.api.open_external === "function"
        && a.href && /^https?:/i.test(a.href)) {
      ev.preventDefault();
      window.pywebview.api.open_external(a.href);
    }
  });

  document.addEventListener("keydown", (ev) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName);
    if (ev.key === "/" && !typing && !$("#lightbox").hidden) return;
    if (ev.key === "/" && !typing) {
      ev.preventDefault();
      $("#search").focus();
    }
    if (ev.key === "Escape") {
      if (!$("#lightbox").hidden) closeLightbox();
      else if (!$("#modal").hidden) $("#modal").hidden = true;
    }
    if ($("#lightbox").hidden) return;
    if (ev.key === "ArrowRight") lbStep(1);
    if (ev.key === "ArrowLeft") lbStep(-1);
  });

  $$(".modal").forEach((m) => m.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-close]")) m.hidden = true;
  }));
  $$(".lightbox").forEach((lb) => lb.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-close]") || ev.target.closest("#lightbox-img") || ev.target.id === "lightbox") closeLightbox();
  }));
  $("#lb-prev").addEventListener("click", (ev) => { ev.stopPropagation(); lbStep(-1); });
  $("#lb-next").addEventListener("click", (ev) => { ev.stopPropagation(); lbStep(1); });
  $("#status-close").addEventListener("click", () => { $("#install-status").hidden = true; });
}

function closeLightbox() {
  $("#lightbox").hidden = true;
}

/* ============================== Tooltips ================================= */
function setupTips() {
  const layer = document.createElement("div");
  layer.id = "tip-layer";
  document.body.appendChild(layer);

  let tipEl = null, timer = null;

  function showTip(el) {
    const text = el.getAttribute("data-tip");
    if (!text) { hideTip(); return; }
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "js-tip";
      layer.appendChild(tipEl);
    }
    tipEl.textContent = text;
    tipEl.style.left = "0px";
    tipEl.style.top = "0px";
    tipEl.style.opacity = "0";
    const r = el.getBoundingClientRect();
    const tw = tipEl.offsetWidth;
    const th = tipEl.offsetHeight;
    let left = Math.round(r.left + r.width / 2 - tw / 2);
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    let top = Math.round(r.bottom + 8);
    if (top + th > window.innerHeight - 8) top = Math.round(r.top - th - 8);
    tipEl.style.left = left + "px";
    tipEl.style.top = top + "px";
    requestAnimationFrame(() => { if (tipEl) tipEl.classList.add("on"); });
  }

  function hideTip() {
    clearTimeout(timer);
    if (tipEl) {
      tipEl.classList.remove("on");
      const dead = tipEl;
      tipEl = null;
      setTimeout(() => dead.remove(), 200);
    }
  }

  document.addEventListener("mouseover", (e) => {
    const el = e.target && e.target.closest ? e.target.closest("[data-tip]") : null;
    clearTimeout(timer);
    if (el) timer = setTimeout(() => showTip(el), 50);
    else hideTip();
  });
  document.addEventListener("scroll", hideTip, true);
}

/* ============================== Boot ===================================== */
function watchRevision() {
  let last = state.data && state.data.stats && state.data.stats.revision;
  let lastHash = state.data && state.data.stats && state.data.stats.catalog_hash;
  setInterval(async () => {
    if (state.busy) return;
    try {
      const meta = await fetch("/api/index", { cache: "no-store" });
      const fresh = await meta.json();
      const rev = fresh.stats && fresh.stats.revision;
      const hash = fresh.stats && fresh.stats.catalog_hash;
      if (hash && hash !== lastHash) { location.reload(); return; }
      if (typeof rev === "number" && typeof last === "number" && rev !== last) location.reload();
      else if (typeof rev === "number") last = rev;
      if (hash) lastHash = hash;
    } catch (e) { /* offline momentâneo */ }
  }, 4000);
}

async function boot() {
  try {
    themeInit();
    skeletonCards();
    const data = await api("/api/index");
    state.data = data;
    renderHeader(data);
    renderCats(data.categories);
    render();
    bindDelegates();
    bindModalActions();
    bindGlobal();
    setupTips();
    watchRevision();
  } catch (e) {
    console.error(e);
    $("#grid").innerHTML = "";
    $("#empty").hidden = false;
    $("#empty h2").textContent = "Erro ao carregar a loja";
    $("#empty p").textContent = "Verifique se o servidor local está rodando.";
    toast("Não foi possível carregar a loja.", "err");
  }
}

boot();