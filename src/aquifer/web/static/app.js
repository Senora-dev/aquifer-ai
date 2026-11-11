// Aquifer UI — a small single-page app with no build step and no dependencies.
// Every piece of source text is inserted as text (never as HTML): indexed content is untrusted.

import { LOGOS } from "./logos.js";

const main = document.getElementById("main");
const topbar = document.getElementById("topbar");
const sidebar = document.getElementById("sidebar");
const banner = document.getElementById("banner");
const state = { session: null, overview: null };
let timers = [];

// --- utilities ------------------------------------------------------------------------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v; // CSSOM: allowed by the page's CSP
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") throw new Error("no raw HTML");
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ICONS = {
  plus: "M12 5v14M5 12h14",
  arrow: "M5 12h14M13 6l6 6-6 6",
  back: "M19 12H5M11 6l-6 6 6 6",
  ext: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h6",
  pr: "M6 3v12M6 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM6 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM18 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM18 17V9a3 3 0 0 0-3-3h-4M13 3l-2 3 2 3",
  issue: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2z",
  book: "M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2zM4 5v16M8 7h7",
  chat: "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z",
  tag: "M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9zM8 8h.01",
  alert: "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16v-4M12 8h.01",
  check: "M20 6 9 17l-5-5",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  plug: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  refresh: "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
  question: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01",
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  bot: "M12 3v3M5 8h14v11H5zM9 13h.01M15 13h.01M9 16.5h6M2 12v4M22 12v4",
  logout: "M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 16l-4-4 4-4M6 12h11",
  flask: "M9 3h6M10 3v6L4.5 19a1 1 0 0 0 .9 1.5h13.2a1 1 0 0 0 .9-1.5L14 9V3M7 15h10",
  chevron: "M9 6l6 6-6 6",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  key: "M15 7a4 4 0 1 1-3.9 4.9L3 20v-3h3v-3h3l1.1-1.1A4 4 0 0 1 15 7zM16 10h.01",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
};
function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICONS[name] || ICONS.info);
  s.append(p);
  return s;
}
// The official Aquifer logo (aquifer-ai.dev) and wordmark: "Aquifer" plus a mono "AI".
function brandLink(href = "#/") {
  return h("a", { class: "brand", href, "aria-label": "Aquifer AI — home" },
    h("img", { src: "/assets/logo-96.png", srcset: "/assets/logo-96.png 1x, /assets/logo-192.png 2x", alt: "", width: "28", height: "28" }),
    h("span", { class: "word" }, h("span", { class: "name" }, "Aquifer"), h("span", { class: "ai", "aria-hidden": "true" }, "AI")));
}
function logo(name) {
  const box = h("span", { class: "logo", "aria-hidden": "true" });
  const data = LOGOS[name];
  if (!data) { box.append(icon("plug")); return box; }
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", data.path);
  s.append(p);
  box.append(s);
  return box;
}

let clockSkew = 0;  // server time minus local time, from the Date header of API responses
async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: { accept: "application/json" }, credentials: "same-origin" };
  if (method !== "GET") {
    opts.headers["content-type"] = "application/json";
    opts.headers["x-aquifer-csrf"] = "1";
    opts.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw Object.assign(new Error("Aquifer did not respond. Is the server running?"), { code: "offline" });
  }
  // Relative times ("5 min ago") use the server's clock: a laptop clock can be minutes off.
  const served = Date.parse(res.headers.get("date") || "");
  if (!Number.isNaN(served)) clockSkew = served + 500 - Date.now();
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/api/session") {
    state.session = { ...(state.session || {}), signed_in: false };
    renderSignIn();
    throw Object.assign(new Error("Signed out"), { code: "signed_out", silent: true });
  }
  if (!res.ok) throw Object.assign(new Error(data.message || `Request failed (${res.status})`), { status: res.status, code: data.error, data });
  return data;
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  return { path: path || "/", params: new URLSearchParams(query || "") };
}
function go(path, params) {
  const q = params ? "?" + new URLSearchParams(params).toString() : "";
  location.hash = "#" + path + q;
}
function every(ms, fn) { const t = setInterval(fn, ms); timers.push(() => clearInterval(t)); }
function clearTimers() { timers.forEach((stop) => stop()); timers = []; }

function ago(iso) {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "unknown";
  const s = Math.max(0, (Date.now() + clockSkew - t) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en", { month: "short", day: "numeric", year: s > 300 * 86400 ? "numeric" : undefined });
}
function date(iso) {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString("en", { year: "numeric", month: "short", day: "numeric" });
}
function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }
function toast(text) {
  const el = document.getElementById("toast");
  el.replaceChildren(h("div", { class: "t" }, text));
  setTimeout(() => { if (el.textContent === text) el.replaceChildren(); }, 3200);
}
function alertBox(kind, title, ...body) {
  return h("div", { class: `alert ${kind}`, role: kind === "bad" ? "alert" : null },
    icon(kind === "ok" ? "check" : kind === "info" ? "info" : "alert"),
    h("div", {}, title ? h("span", { class: "title" }, title) : null, ...body));
}
function chip(kind, text, busy) {
  return h("span", { class: `chip ${kind}${busy ? " busy" : ""}` }, h("span", { class: "dot" }), text);
}
function copyButton(text) {
  return h("button", { class: "btn small", type: "button", onclick: async (e) => {
    try { await navigator.clipboard.writeText(text); e.currentTarget.textContent = "Copied"; } catch { toast("Copy failed: select the text instead"); }
  } }, icon("copy"), "Copy");
}
function errorView(err) {
  if (err && err.silent) return null;
  return h("div", { class: "wrap" }, h("div", { class: "empty-state card" },
    h("div", { class: "icon" }, icon("alert")),
    h("h2", {}, err && err.status === 403 ? "You don't have access to this" : "Something went wrong"),
    h("p", {}, (err && err.message) || "Try again."),
    h("div", { class: "row" }, h("a", { class: "btn", href: "#/" }, "Back to Aquifer"))));
}

const KIND = {
  pr: ["pr", "Pull request"], issue: ["issue", "Issue"], readme: ["book", "README"],
  file: ["file", "File"], discussion: ["chat", "Discussion"], release: ["tag", "Release"],
  ticket: ["issue", "Ticket"], page: ["file", "Page"], record: ["file", "Record"],
};
function kindLabel(kind) { return (KIND[kind] || ["file", kind || "Document"])[1]; }
function kindBadge(kind) {
  const [ic, label] = KIND[kind] || ["file", kind || "Document"];
  return h("span", { class: "kind" }, icon(ic), label);
}
function itemNumber(docId) { const m = /:(?:pr|issue):(\d+)$/.exec(docId || ""); return m ? `#${m[1]}` : null; }

function highlight(text, terms) {
  if (!terms || !terms.length) return [text];
  const esc = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length);
  const re = new RegExp(`(${esc.join("|")})[\\w-]*`, "gi");
  const out = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(h("mark", {}, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// --- chrome ---------------------------------------------------------------------------

function crumbsFor(route, demo) {
  const ws = demo ? "Demo workspace" : "Workspace";
  const home = demo ? "#/demo" : "#/";
  const p = route.path;
  if (p === "/" || p === "/demo") return [[ws, null], ["Ask", null]];
  if (p === "/ask") return [[ws, home], ["Ask", home], [route.params.get("q") || "", null]];
  if (p === "/doc") {
    const q = route.params.get("q");
    const back = q ? "#/ask?" + new URLSearchParams(demo ? { q, ws: "demo" } : { q }).toString() : home;
    return [[ws, home], ["Ask", back], ["Source", null]];
  }
  if (p === "/sources") return [[ws, home], ["Sources", null]];
  if (p.startsWith("/sources/add")) return [[ws, home], ["Sources", "#/sources"], ["Add a source", null]];
  if (p === "/agents") return [[ws, home], ["Agents", null]];
  if (p === "/settings") return [[ws, home], ["Settings", null]];
  return [[ws, home]];
}

function renderChrome(route) {
  const s = state.session || {};
  const demo = route.params.get("ws") === "demo" || route.path === "/demo";
  document.body.classList.toggle("bare", !s.signed_in);
  if (!s.signed_in) { sidebar.replaceChildren(); topbar.replaceChildren(); banner.replaceChildren(); return; }
  const section = route.path.startsWith("/sources") ? "sources" : route.path.startsWith("/agents") ? "agents" : route.path.startsWith("/settings") ? "settings" : "ask";
  const item = (href, ic, label, key) => h("a", { class: "nav-item", href, "aria-current": section === key ? "page" : null }, icon(ic), h("span", {}, label));
  const demoOn = !state.overview || state.overview.demo.available;
  const initial = (s.principal || "?").slice(0, 1).toUpperCase();
  sidebar.replaceChildren();
  append(sidebar, [
    brandLink(),
    demoOn ? h("div", { class: "ws-switch", role: "group", "aria-label": "Workspace" },
      h("a", { href: "#/", "aria-current": demo ? null : "true" }, "Workspace"),
      h("a", { href: "#/demo", "aria-current": demo ? "true" : null }, icon("flask"), "Demo")) : null,
    h("nav", { class: "nav", "aria-label": "Main" },
      h("div", { class: "nav-label" }, "Context"),
      item(demo ? "#/demo" : "#/", "search", "Ask", "ask"),
      item(demo ? "#/sources?ws=demo" : "#/sources", "db", "Sources", "sources"),
      h("div", { class: "nav-label" }, "Access"),
      item("#/agents", "bot", "Agents", "agents"),
      item("#/settings", "gear", "Settings", "settings")),
    h("div", { class: "side-foot" },
      h("span", { class: "avatar", "aria-hidden": "true" }, initial),
      h("div", { class: "who" }, h("strong", {}, s.principal), h("span", {}, s.auth_required ? "" : "Local · ", s.admin ? "Admin" : "Read access")),
      s.auth_required ? h("button", { class: "btn icon ghost", type: "button", title: "Sign out", "aria-label": "Sign out", onclick: signOut }, icon("logout")) : null)]);
  const crumbs = crumbsFor(route, demo);
  topbar.replaceChildren();
  append(topbar, [
    h("nav", { class: "crumbs-bar", "aria-label": "Breadcrumb" }, crumbs.map(([label, href], i) => [
      i ? h("span", { class: "sep", "aria-hidden": "true" }, "/") : null,
      href ? h("a", { href }, label) : h("span", { class: i === crumbs.length - 1 ? "here" : null, "aria-current": i === crumbs.length - 1 ? "page" : null }, label)])),
    demo ? h("span", { class: "pill demo-data" }, icon("flask"), "Demo data") : null,
    h("button", { class: "searchbtn", type: "button", onclick: focusAsk }, icon("search"), h("span", {}, "Ask anything…"), h("kbd", {}, "/"))]);
  banner.replaceChildren(demo ? h("div", { class: "demo-banner" },
    h("span", {}, "Synthetic data: acme/payments, acme/platform and acme/legacy-billing. Nothing here comes from your organization."),
    h("a", { href: "#/" }, "Exit demo")) : "");
}
function focusAsk() {
  const input = document.querySelector("main .askbox input");
  if (input) { input.focus(); input.select(); return; }
  go(parseHash().params.get("ws") === "demo" ? "/demo" : "/");
}

async function signOut() {
  await api("/api/session", { method: "DELETE" }).catch(() => {});
  state.session = { ...state.session, signed_in: false };
  renderSignIn();
}

function renderSignIn(errorText) {
  clearTimers();
  renderChrome({ path: "/", params: new URLSearchParams() });
  const input = h("input", { type: "password", id: "token", autocomplete: "current-password", required: true, "aria-describedby": "token-hint" });
  const err = h("div", { "aria-live": "assertive" }, errorText ? alertBox("bad", null, errorText) : null);
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const res = await fetch("/api/session", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json", "x-aquifer-csrf": "1" }, body: JSON.stringify({ token: input.value }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || "Sign-in failed.");
      state.session = { ...state.session, ...data, signed_in: true };
      route();
    } catch (ex) {
      err.replaceChildren(alertBox("bad", null, ex.message));
      btn.disabled = false;
      input.select();
    }
  } },
    h("label", { class: "field", for: "token" }, "Access token",
      h("span", { class: "hint", id: "token-hint" }, "Your Aquifer access token: the admin token, or one an admin gave you. Agent credentials work only for MCP clients. On AWS, an admin copies the admin token with ", h("code", {}, "deploy/aws/aquifer-aws token | pbcopy"), ".")),
    input, err,
    h("button", { class: "btn primary large", type: "submit" }, "Sign in"));
  main.replaceChildren(h("div", { class: "wrap" }, h("div", { class: "signin card pad" },
    brandLink(),
    h("h1", {}, "Sign in"),
    h("p", {}, "Aquifer shows what your agents know, where it came from, and who is using it."),
    form)));
  input.focus();
}

// --- home -----------------------------------------------------------------------------

function journey(step) {
  const items = [
    ["Connect a source", "GitHub first: READMEs, issues, pull requests and decision records."],
    ["Ask a real question", "Answers come with the exact lines they rely on."],
    ["Connect an agent", "Your coding agent queries the same index over MCP."],
  ];
  return h("ol", { class: "journey", "aria-label": "Getting started" }, items.map(([t, d], i) =>
    h("li", { class: i < step ? "done" : i === step ? "now" : null, "aria-current": i === step ? "step" : null },
      h("span", { class: "n" }, i < step ? icon("check") : String(i + 1)),
      h("div", {}, h("strong", {}, t), h("span", {}, d)))));
}

function askBox({ value = "", workspace, compact = false, autofocus = false } = {}) {
  const input = h("input", { type: "search", name: "q", value, placeholder: workspace === "demo" ? "Ask about the acme demo systems…" : "Ask about your systems, decisions and history…", "aria-label": "Question", autocomplete: "off", maxlength: 500, enterkeyhint: "search" });
  input.value = value;
  const form = h("form", { role: "search", onsubmit: (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) { input.focus(); return; }
    go("/ask", workspace === "demo" ? { q, ws: "demo" } : { q });
  } }, input, h("button", { class: "btn primary", type: "submit" }, "Ask", icon("arrow")));
  const box = h("div", { class: `askbox${compact ? " compact" : ""}` }, form);
  if (autofocus) setTimeout(() => input.focus(), 0);
  return box;
}
function suggestions(list, workspace) {
  if (!list || !list.length) return null;
  return h("div", { class: "suggest", "aria-label": "Suggested questions" }, list.map((q) =>
    h("button", { type: "button", onclick: () => go("/ask", workspace === "demo" ? { q, ws: "demo" } : { q }) }, q)));
}

async function viewHome() {
  const ov = state.overview = await api("/api/overview");
  if (ov.phase === "ready") return homeReady(ov);
  if (ov.phase === "indexing") { every(2000, refreshHomeIfChanged); return homeIndexing(ov); }
  if (ov.phase === "failed" || ov.phase === "nothing_indexed") return homeProblem(ov);
  return homeEmpty(ov);
}
async function refreshHomeIfChanged() {
  const ov = await api("/api/overview").catch(() => null);
  if (!ov || parseHash().path !== "/") return;
  if (ov.phase !== state.overview.phase) { if (ov.phase === "ready") toast("Your first source is searchable"); route(); }
  else if (ov.phase === "indexing") { state.overview = ov; main.replaceChildren(homeIndexing(ov)); }
}

function homeEmpty(ov) {
  const admin = ov.admin;
  return h("div", { class: "wrap" }, h("section", { class: "hero" },
    h("div", {},
      h("div", { class: "eyebrow" }, "New installation"),
      h("h1", {}, "Give your agents the context your team already wrote down."),
      h("p", { class: "lede" }, "Aquifer indexes your repositories, decisions and tickets, then answers questions with the exact source behind them, for you and for your coding agents."),
      admin ? null : h("div", { style: "margin-top:20px" }, alertBox("info", "Read access", `You are signed in as ${ov.principal}. Connecting sources needs an admin token; until then, explore the demo.`)),
      h("div", { class: "row cta" },
        admin ? h("a", { class: "btn primary large", href: "#/sources/add" }, icon("plus"), "Add a source") : null,
        ov.demo.available ? h("a", { class: `btn large${admin ? "" : " primary"}`, href: "#/demo" }, "Explore the demo") : null),
      ov.demo.available ? h("p", { class: "cta-note" }, "The demo uses synthetic repositories and is clearly labeled. Nothing private is needed.") : null),
    h("div", { class: "card pad" }, h("div", { class: "eyebrow", style: "margin-bottom:6px" }, "Your first session"), journey(0))));
}

function homeIndexing(ov) {
  const repos = ov.github.repos;
  const done = repos.filter((r) => r.documents > 0).length;
  return h("div", { class: "wrap" }, h("section", { class: "hero" },
    h("div", {},
      h("div", { class: "eyebrow" }, "Indexing"),
      h("h1", {}, "Reading your first repositories."),
      h("p", { class: "lede" }, "Aquifer is fetching READMEs, issues, pull requests and decision files. Small repositories are ready in under a minute; this page updates on its own."),
      h("div", { class: "card pad", style: "margin-top:26px" },
        h("ul", { class: "progress-list" }, repos.map(repoProgress)),
        h("div", { class: "bar indeterminate", role: "progressbar", "aria-label": "Indexing", "aria-valuetext": `${done} of ${repos.length} repositories have documents` }, h("span"))),
      ov.demo.available ? h("p", { class: "cta-note" }, "While you wait: ", h("a", { href: "#/demo" }, "try the demo questions"), ".") : null),
    h("div", { class: "card pad" }, journey(0))));
}
function repoProgress(r) {
  const states = {
    indexing: () => chip("accent", "Indexing", true),
    queued: () => chip("accent", "Queued", true),
    waiting: () => chip("", "Waiting for the first sync", true),
    ready: () => chip("ok", "Ready"),
    failed: () => chip("bad", "Failed"),
    empty: () => chip("warn", "Nothing to index"),
  };
  return h("li", {},
    h("span", { class: "name" }, r.repo),
    (states[r.state] || states.waiting)(),
    h("span", { class: "muted small" }, r.documents ? `${plural(r.documents, "document")} indexed` : "", r.jobs ? ` · ${plural(r.jobs, "page")} left` : ""),
    r.error ? h("div", { style: "width:100%" }, alertBox("bad", r.error.message, h("div", {}, r.error.action))) : null);
}

function homeProblem(ov) {
  const failed = ov.phase === "failed";
  const err = failed ? (ov.github.sync.error || (ov.github.repos.find((r) => r.error) || {}).error) : null;
  return h("div", { class: "wrap" }, h("div", { class: "empty-state card" },
    h("div", { class: "icon" }, icon(failed ? "alert" : "info")),
    h("h2", {}, failed ? "The first sync failed" : "Connected, but nothing was indexed"),
    h("p", {}, failed ? (err ? err.message : "A source reported an error.") : "The connected repositories have no README, issues, pull requests or decision files that Aquifer reads yet."),
    failed && err ? h("p", { class: "muted" }, err.action) : null,
    h("div", { class: "row" },
      h("a", { class: "btn primary", href: "#/sources" }, "Open Sources"),
      failed && err && err.code === "token_rejected" && ov.admin ? h("a", { class: "btn", href: "#/sources/add/github" }, "Replace the token") : null,
      ov.demo.available ? h("a", { class: "btn", href: "#/demo" }, "Explore the demo") : null)));
}

// What the sources' synchronization state actually supports saying.
function syncState(ov) {
  const sync = ov.github.sync || {};
  const last = sync.last_success;
  const failing = (ov.github.repos || []).some((r) => r.state === "failed") || ov.failed > 0 || !!sync.error;
  if (!ov.scopes) return { kind: "", label: "No sources", detail: null };
  if (failing) return { kind: "bad", label: "Sync failed", detail: last ? `last successful sync ${ago(last)}` : "no successful sync yet" };
  if (ov.indexing) return { kind: "accent", busy: true, label: "Syncing", detail: last ? `last successful sync ${ago(last)}` : "first sync in progress" };
  if (!last && (ov.github.repos || []).length) return { kind: "warn", label: "Not synced yet", detail: "waiting for the first sync" };
  return { kind: "ok", label: "Up to date", detail: last ? `synced ${ago(last)}` : "imported sources only" };
}

function homeReady(ov) {
  const ready = ov.github.repos.filter((r) => r.state === "ready").length + ov.mcp_sources.filter((m) => m.enabled).length;
  const sync = syncState(ov);
  const ag = ov.agents;
  return h("div", { class: "wrap" },
    ov.failed ? h("div", { style: "margin-top:20px" }, alertBox("bad", `${plural(ov.failed, "source")} need attention`, h("a", { href: "#/sources" }, "See what to fix"))) : null,
    h("section", { class: "askhero" },
      h("h1", {}, "What do you need to know?"),
      h("p", { class: "sub" }, `Aquifer answers from ${plural(ov.documents, "document")} in ${plural(ready, "source")}, and shows the exact lines it relies on.`),
      askBox({ autofocus: true }),
      suggestions(ov.suggestions),
      h("p", { class: "kbd-hint" }, "Press ", h("kbd", {}, "/"), " to ask from anywhere."),
      h("div", { class: "stats" },
        h("a", { class: "stat", href: "#/sources" }, h("span", { class: "k" }, "Sources"), h("strong", {}, ready),
          chip(sync.kind, sync.label, sync.busy), sync.detail ? h("span", { class: "muted small" }, sync.detail) : null),
        h("div", { class: "stat" }, h("span", { class: "k" }, "Documents"), h("strong", {}, ov.documents), h("span", { class: "muted small" }, "searchable")),
        h("a", { class: "stat", href: "#/settings" }, h("span", { class: "k" }, "Answers"),
          ov.answers.available ? chip("ok", "With citations") : chip("", "Search only"),
          h("span", { class: "muted small" }, ov.answers.available ? "citations show sources, not proof" : "cited excerpts, no generated text"),
          h("span", { class: "small stat-link" }, "Model details")),
        h("a", { class: "stat", href: "#/agents", title: "Agent credentials: MCP-only tokens. “Used” means an authenticated tool call in the last 7 days." },
          h("span", { class: "k" }, "Agent credentials"), h("strong", {}, ag.credentials),
          ag.credentials ? (ag.active_7d ? chip("ok", `${ag.active_7d} used in 7 days`) : chip("warn", "None used yet")) : null,
          ag.credentials ? null : (ov.admin ? h("span", { class: "btn primary small" }, "Connect an agent") : h("span", { class: "muted small" }, "none configured"))))));
}

async function viewDemo() {
  const ov = state.overview = await api("/api/overview");
  if (!ov.demo.available) return errorView({ message: "The demo workspace is turned off on this server." });
  return h("div", { class: "wrap" }, h("section", { class: "askhero" },
    h("div", { class: "eyebrow" }, "Demo preview"),
    h("h1", {}, "Ask the acme engineering team's history."),
    h("p", { class: "sub" }, "Three synthetic repositories with decision records, Terraform, Compose files, issues and pull requests. Try a question a new engineer would ask a teammate."),
    askBox({ workspace: "demo", autofocus: true }),
    suggestions(ov.demo.suggestions, "demo"),
    h("p", { class: "kbd-hint" }, ov.demo.note, " ", h("a", { href: "#/sources?ws=demo" }, "See the demo sources"), ".")));
}

// --- ask ------------------------------------------------------------------------------

async function viewAsk(params) {
  const q = params.get("q") || "";
  const ws = params.get("ws") === "demo" ? "demo" : null;
  const top = h("div", { class: "ask-top" }, askBox({ value: q, workspace: ws, compact: true }));
  const body = h("div", { "aria-live": "polite", "aria-busy": "true" },
    h("div", { class: "row muted", style: "margin-top:28px" }, h("span", { class: "spinner" }), "Searching the index…"));
  const page = h("div", { class: "wrap" }, top, body);
  main.replaceChildren(page);
  if (!q) { body.replaceChildren(); return page; }
  let res;
  try {
    res = await api("/api/ask", { method: "POST", body: { question: q, workspace: ws } });
  } catch (err) {
    body.replaceChildren(alertBox("bad", "The question could not be answered", err.message));
    return page;
  }
  body.setAttribute("aria-busy", "false");
  body.replaceChildren(renderResult(res, ws));
  return page;
}

function renderResult(res, ws) {
  if (res.status === "no_sources") {
    return h("div", { class: "empty-state card" },
      h("div", { class: "icon" }, icon("db")),
      h("h2", {}, "Nothing is indexed yet"),
      h("p", {}, "Connect a source first, or explore the demo to see how answers work."),
      h("div", { class: "row" },
        state.session.admin ? h("a", { class: "btn primary", href: "#/sources/add" }, "Add a source") : null,
        h("a", { class: "btn", href: "#/demo" }, "Explore the demo")));
  }
  if (res.status === "insufficient") return insufficient(res, ws);
  const ev = res.evidence;
  // In search mode the lead card already shows source 1; the list shows the others.
  const listed = res.status === "answered" ? ev : ev.slice(1);
  const evList = listed.length
    ? h("ol", { class: "evidence-list", "aria-label": "Sources" }, listed.map((e) => h("li", {}, evidenceCard(e, res, ws))))
    : h("p", { class: "muted" }, "No other indexed source mentions enough of your question.");
  let lead;
  if (res.status === "answered") {
    lead = h("div", { class: "card answer" },
      h("div", { class: "label" }, h("span", { class: "eyebrow" }, "Answer"), chip("ok", "With citations")),
      h("p", { class: "text" }, citedText(res.answer.text)),
      h("p", { class: "basis" }, `Generated from the ${plural(ev.length, "excerpt")} on the right only; every sentence cites one. A citation shows where a statement comes from, not that it is correct: check the source before acting on it. `, h("a", { href: "#/settings" }, "Model details")));
  } else {
    const best = ev[0];
    const num = itemNumber(best.document_id);
    lead = h("div", { class: "card answer" },
      h("div", { class: "label" }, h("span", { class: "eyebrow" }, res.status === "not_answered" ? "No answer: these sources mention your terms but do not answer it" : "Strongest evidence"),
        res.status === "not_answered" ? chip("warn", "Not enough evidence for an answer") : chip("", "Source-backed search")),
      h("p", { class: "lead-fact" },
        `${kindLabel(best.kind)} `, num ? `${num} ` : "",
        h("span", { class: "q" }, `“${best.title}”`),
        ` in ${best.repo}`,
        best.state ? ` (${best.state}` + (best.updated_at ? `, ${date(best.updated_at)})` : ")") : best.updated_at ? ` (updated ${date(best.updated_at)})` : "",
        ` says, at line${best.line_start === best.line_end ? "" : "s"} ${best.line_start}${best.line_end !== best.line_start ? "–" + best.line_end : ""}:`),
      excerptBlock(best, res.terms),
      h("div", { class: "row", style: "margin-top:16px" },
        h("a", { class: "btn primary", href: docHref(best, res, ws) }, "Open the source at these lines", icon("arrow")),
        externalLink(best)),
      h("p", { class: "basis" }, res.generation_note || (res.generation && res.generation.available === false
        ? `${res.generation.reason} Sources that mention more of your question's terms come first.`
        : "Sources that mention more of your question's terms come first, then by search relevance.")));
  }
  return h("div", { class: "results" },
    h("div", { class: "answer-col" }, lead),
    h("section", { "aria-label": "Supporting sources" },
      h("div", { class: "section-head" }, h("h2", { style: "font-size:20px" }, res.status === "answered" ? "Sources" : "Also relevant"), h("span", { class: "muted small" }, `${plural(ev.length, "matching source")} · searched in ${res.elapsed_ms} ms`)),
      evList));
}

function citedText(text) {
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(/\[(\d{1,2})\]/g)) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const n = m[1];
    parts.push(h("button", { class: "cite", type: "button", "aria-label": `Source ${n}`, onclick: () => {
      const card = document.getElementById(`ev-${n}`);
      if (!card) return;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
      card.querySelector("a")?.focus({ preventScroll: true });
    } }, n));
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function docHref(e, res, ws) {
  const p = { id: e.document_id, s: e.line_start, e: e.line_end };
  if (res && res.question) p.q = res.question;
  if (ws === "demo") p.ws = "demo";
  return "#/doc?" + new URLSearchParams(p).toString();
}
function externalLink(e) {
  const href = e.link || e.url;
  if (!href) return null;
  const label = e.source_type === "github" ? "View on GitHub" : "Open in the source tool";
  if (e.link_is_fictional) {
    return h("span", { class: "fictional", title: href }, icon("info"), "GitHub link is fictional in the demo");
  }
  return h("a", { class: "ext", href, target: "_blank", rel: "noopener noreferrer" }, label, icon("ext"), h("span", { class: "sr" }, "(opens in a new tab)"));
}

function lineRange(a, b) { return a === b ? `L${a}` : `L${a}–${b}`; }
function excerptBlock(e, terms) {
  const where = e.path || itemNumber(e.document_id) || kindLabel(e.kind);
  return h("div", { class: "excerpt", "aria-label": `Excerpt from ${e.repo} ${where}, ${lineRange(e.line_start, e.line_end)}` },
    h("div", { class: "excerpt-head" },
      icon((KIND[e.kind] || ["file"])[0]),
      h("span", { class: "repo" }, e.repo), h("span", { class: "sep" }, "/"), h("span", { class: "path" }, where),
      h("span", { class: "spacer" }),
      h("span", { class: "range" }, lineRange(e.line_start, e.line_end))),
    h("div", { class: "excerpt-body" }, e.excerpt.lines.map((l) =>
      h("div", { class: `ln${l.focus ? " focus" : ""}` }, h("span", {}, l.n), h("span", {}, highlight(l.text, terms))))));
}

function evidenceCard(e, res, ws) {
  const num = itemNumber(e.document_id);
  return h("article", { class: "card ev", id: e.rank ? `ev-${e.rank}` : null },
    h("div", { class: "ev-head" },
      e.rank ? h("span", { class: "ev-num", "aria-label": `Source ${e.rank}` }, e.rank) : null,
      h("div", { style: "min-width:0" },
        h("a", { class: "ev-title", href: docHref(e, res, ws) }, num ? `${num} ` : "", e.title),
        h("div", { class: "ev-meta" },
          kindBadge(e.kind),
          e.state ? stateChip(e.state) : null,
          e.updated_at ? h("span", {}, `updated ${date(e.updated_at)}`) : null,
          e.imported_via ? h("span", {}, `via ${e.imported_via.mcp_server || "MCP"}`) : null))),
    excerptBlock(e, res.terms),
    h("div", { class: "ev-foot" },
      h("a", { class: "btn small", href: docHref(e, res, ws) }, "Open source", icon("arrow")),
      externalLink(e),
      e.matched_terms && e.matched_terms.length ? h("span", { class: "terms" }, "Mentions: ", e.matched_terms.join(", ")) : null));
}
function stateChip(s) {
  const kind = { merged: "ok", closed: "", open: "accent", accepted: "ok" }[s] ?? "";
  return chip(kind, s);
}

function insufficient(res, ws) {
  const terms = res.terms.length ? res.terms.join(", ") : "those words";
  return h("div", {},
    h("div", { class: "empty-state card" },
      h("div", { class: "icon" }, icon("question")),
      h("h2", {}, "Not enough evidence to answer"),
      h("p", {}, `No indexed source covers enough of your question (${terms}). Aquifer does not guess: no evidence in the index does not mean the thing doesn't exist.`),
      h("p", { class: "muted small" }, "Try other words (a service name, a ticket number), check what is indexed, or connect the tool where this was decided."),
      h("div", { class: "row" },
        h("a", { class: "btn", href: "#/sources" }, "See what is indexed"),
        ws !== "demo" && state.session.admin ? h("a", { class: "btn", href: "#/sources/add" }, "Add a source") : null)),
    res.closest && res.closest.length ? h("details", { class: "closest" },
      h("summary", {}, `Closest matches (${res.closest.length}): they mention few or none of your terms`),
      h("ol", { class: "evidence-list", style: "margin-top:14px" }, res.closest.map((e) => h("li", {}, evidenceCard(e, res, ws))))) : null);
}

// --- document -------------------------------------------------------------------------

async function viewDoc(params) {
  const ws = params.get("ws") === "demo" ? "demo" : "";
  const s = params.get("s"), e = params.get("e");
  const q = new URLSearchParams({ id: params.get("id") || "", workspace: ws });
  if (s) { q.set("start", s); q.set("end", e || s); }
  const doc = await api("/api/documents?" + q.toString());
  const focus = doc.focus;
  const lines = h("div", { class: "code-body", role: "region", tabindex: "0", "aria-label": `Source text of ${doc.title}` },
    doc.lines.map((text, i) => {
      const n = i + 1;
      const on = focus && n >= focus.start && n <= focus.end;
      return h("div", { class: `ln${on ? " focus" : ""}`, id: `L${n}` }, h("span", {}, n), h("span", {}, text || " "));
    }));
  const back = params.get("q")
    ? h("a", { href: "#/ask?" + new URLSearchParams(ws ? { q: params.get("q"), ws } : { q: params.get("q") }).toString() }, "← Back to the answer")
    : h("a", { href: ws ? "#/demo" : "#/" }, "← Back");
  const rev = doc.revision ? doc.revision.slice(0, 10) : null;
  const meta = [
    ["Repository", doc.repo],
    doc.path ? ["Path", doc.path] : null,
    ["Revision", rev || (doc.kind === "pr" || doc.kind === "issue" ? "Not versioned (live item)" : "Unknown")],
    doc.state ? ["State", doc.state] : null,
    doc.updated_at ? ["Updated at the source", date(doc.updated_at)] : null,
    doc.indexed_at ? ["Indexed by Aquifer", ago(doc.indexed_at)] : null,
    focus ? ["Supporting lines", focus.start === focus.end ? `${focus.start}` : `${focus.start}–${focus.end}`] : null,
    doc.imported_via ? ["Imported via", `${doc.imported_via.mcp_server || "MCP"} · ${doc.imported_via.tool || ""}`] : null,
  ].filter(Boolean);
  const ext = externalLink({ ...doc, link_is_fictional: doc.demo });
  const page = h("div", { class: "wrap" },
    h("div", { class: "crumbs" }, back),
    h("header", { class: "doc-head" },
      h("div", { class: "row" }, kindBadge(doc.kind), doc.state ? stateChip(doc.state) : null, itemNumber(doc.document_id) ? h("span", { class: "muted" }, itemNumber(doc.document_id)) : null),
      h("h1", {}, doc.title)),
    h("div", { class: "doc-grid" },
      h("div", { class: "code" },
        h("div", { class: "code-head" },
          icon((KIND[doc.kind] || ["file"])[0]),
          h("span", { class: "repo" }, doc.repo), h("span", { class: "sep" }, "/"), h("span", { class: "path" }, doc.path || doc.title),
          h("span", { class: "spacer" }),
          h("span", { class: "range" }, plural(doc.lines.length, "line"))),
        lines),
      h("aside", { class: "card props", "aria-label": "Properties" },
        h("div", { class: "props-title" }, "Properties"),
        h("dl", { class: "meta-grid" }, meta.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v)))),
        h("div", { class: "props-foot" }, ext, doc.link && !doc.demo ? copyButton(doc.link) : null),
        h("p", { class: "muted small" }, "Shown exactly as indexed. Source text is data, not instructions."))));
  main.replaceChildren(page);
  if (focus) {
    const target = document.getElementById(`L${focus.start}`);
    if (target) target.scrollIntoView({ block: "center" });
  }
  return page;
}

// --- sources --------------------------------------------------------------------------

const REPO_STATE = {
  ready: ["ok", "Ready"], indexing: ["accent", "Indexing"], queued: ["accent", "Queued"],
  waiting: ["", "Waiting for sync"], failed: ["bad", "Failed"], empty: ["warn", "Nothing to index"],
};
const MCP_STATE = {
  active: ["ok", "Active"], ready: ["accent", "Ready to enable"], paused: ["", "Paused"],
  needs_authorization: ["warn", "Needs authorization"], needs_reauth: ["bad", "Needs re-authorization"], error: ["bad", "Error"],
};

async function viewSources() {
  const data = await api("/api/sources");
  const gh = data.github;
  const busy = gh.sync.running || gh.repos.some((r) => r.jobs) || data.mcp_sources.some((m) => (m.last_run || {}).status === "running");
  if (busy) every(2500, () => { if (parseHash().path === "/sources") rerender(); });
  const admin = data.admin;
  const head = h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Sources"), h("p", {}, "What Aquifer reads, when it last synced, and anything that needs attention.")),
    h("div", { class: "actions" }, admin
      ? h("a", { class: "btn primary", href: "#/sources/add" }, icon("plus"), "Add a source")
      : h("span", { class: "muted small" }, icon("lock"), " Read access: an admin changes sources")));
  const cards = [];
  if (gh.repos.length) cards.push(githubCard(gh, admin));
  for (const m of data.mcp_sources) cards.push(mcpCard(m, admin));
  if (!cards.length) {
    cards.push(h("div", { class: "empty-state card" },
      h("div", { class: "icon" }, icon("db")), h("h2", {}, "No sources yet"),
      h("p", {}, "Connect GitHub to index READMEs, issues, pull requests and decision files."),
      h("div", { class: "row" }, admin ? h("a", { class: "btn primary", href: "#/sources/add" }, "Add a source") : null, h("a", { class: "btn", href: "#/demo" }, "Explore the demo"))));
  }
  return h("div", { class: "wrap" }, head, h("div", { style: "margin-top:20px" }, cards),
    h("div", { class: "access-note" }, alertBox("info", "Who can read this", data.access)));
}
const KIND_LABEL = { readme: ["README", "READMEs"], file: ["file", "files"], issue: ["issue", "issues"], pr: ["pull request", "pull requests"] };
async function viewDemoSources() {
  const data = await api("/api/sources?workspace=demo");
  const total = data.repos.reduce((n, r) => n + r.documents, 0);
  const rows = data.repos.map((r) => h("tr", {},
    h("td", {}, h("span", { class: "mono", style: "font-weight:600" }, r.repo)),
    h("td", {}, chip("ok", "Ready")),
    h("td", {}, plural(r.documents, "document")),
    h("td", { class: "small muted" }, Object.entries(r.kinds).map(([k, n]) => { const [one, many] = KIND_LABEL[k] || [k, k + "s"]; return plural(n, one, many); }).join(" · "))));
  return h("div", { class: "wrap" },
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Sources"), h("p", {}, "The synthetic repositories the demo workspace has indexed.")),
      h("div", { class: "actions" }, h("a", { class: "btn primary", href: "#/demo" }, icon("search"), "Ask the demo"))),
    h("div", { style: "margin-top:20px" }, h("section", { class: "card source-card", "aria-label": "GitHub (demo)" },
      h("div", { class: "src-head" }, logo("github"),
        h("div", {}, h("h3", {}, "GitHub ", h("span", { class: "pill demo-data" }, "Demo data")),
          h("div", { class: "facts" }, `${plural(data.repos.length, "repository", "repositories")} · ${plural(total, "document")} · indexed in this server's memory`))),
      h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", {}, "Repository"), h("th", {}, "Status"), h("th", {}, "Indexed"), h("th", {}, "Contents"))),
        h("tbody", {}, rows)))),
    h("p", { class: "kbd-hint" }, data.note));
}
async function rerender() {
  clearTimers();
  const node = await currentView().catch(() => null);
  if (node) main.replaceChildren(node);
}

function githubCard(gh, admin) {
  const sync = gh.sync;
  const facts = [
    gh.token_source === "deployment" ? "Token from the deployment" : gh.token_type ? `${gh.token_type} token${gh.login ? " of @" + gh.login : ""}` : gh.token_configured ? "Token configured" : "No token (public repositories only)",
    gh.connected_by ? `connected by ${gh.connected_by} ${ago(gh.connected_at)}` : null,
    `last successful sync ${ago(sync.last_success)}`,
    `syncs every ${Math.round(sync.interval_s / 60)} min`,
  ].filter(Boolean).join(" · ");
  const rows = gh.repos.map((r) => {
    const [k, label] = REPO_STATE[r.state] || ["", r.state];
    return h("tr", {},
      h("td", {}, h("span", { class: "mono", style: "font-weight:600" }, r.repo),
        r.origin === "deployment" ? h("div", { class: "act" }, "Configured by the deployment") : null,
        r.error ? h("div", {}, h("div", { class: "err" }, r.error.message), h("div", { class: "act" }, r.error.action)) : null),
      h("td", {}, chip(k, label, r.state === "indexing" || r.state === "queued")),
      h("td", {}, r.documents ? plural(r.documents, "document") : "—", r.jobs ? h("div", { class: "act" }, `${plural(r.jobs, "page")} left`) : null),
      h("td", {}, r.last_indexed_at ? ago(r.last_indexed_at) : "—"),
      h("td", { style: "text-align:right" }, admin && r.origin === "ui" ? h("button", { class: "btn small ghost danger", type: "button", onclick: () => removeRepo(r.repo) }, "Remove") : null));
  });
  return h("section", { class: "card source-card", "aria-label": "GitHub" },
    h("div", { class: "src-head" }, logo("github"),
      h("div", {}, h("h3", {}, "GitHub"), h("div", { class: "facts" }, facts)),
      h("div", { class: "spacer" }),
      sync.running ? chip("accent", "Syncing", true) : null,
      admin ? h("button", { class: "btn small", type: "button", onclick: syncGithub }, icon("refresh"), "Sync now") : null,
      admin ? h("a", { class: "btn small", href: "#/sources/add/github" }, "Add repositories") : null),
    sync.error ? h("div", { style: "margin-top:16px" }, alertBox("bad", "Last sync failed", h("div", {}, sync.error.message), h("div", {}, sync.error.action),
      admin && sync.error.code === "token_rejected" ? h("a", { href: "#/sources/add/github?replace=1" }, "Replace the token") : null)) : null,
    h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Repository"), h("th", {}, "Status"), h("th", {}, "Indexed"), h("th", {}, "Last indexed"), h("th", {}, h("span", { class: "sr" }, "Actions")))),
      h("tbody", {}, rows)));
}
async function syncGithub() {
  try { const r = await api("/api/github/sync", { method: "POST", body: {} }); toast(r.queued ? "Sync queued" : "A sync is already queued"); rerender(); }
  catch (err) { if (!err.silent) toast(err.message); }
}
async function removeRepo(repo) {
  if (!confirm(`Stop indexing ${repo} and delete its documents from Aquifer?`)) return;
  try { await api(`/api/github/repos/${repo}`, { method: "DELETE" }); toast(`${repo} removed`); rerender(); }
  catch (err) { if (!err.silent) toast(err.message); }
}

function mcpCard(m, admin) {
  const [k, label] = MCP_STATE[m.state] || ["", m.state];
  const last = m.last_run || {};
  const action = async (what) => {
    try { await api(`/api/mcp/${m.name}/${what}`, { method: "POST", body: {} }); toast(`${m.name}: ${what} done`); rerender(); }
    catch (err) { if (!err.silent) toast(err.message); }
  };
  return h("section", { class: "card source-card", "aria-label": m.name },
    h("div", { class: "src-head" }, logo(m.provider),
      h("div", {}, h("h3", {}, m.name), h("div", { class: "facts" },
        `MCP Integrator · ${m.provider} · last successful run ${ago(m.last_success)} · ${plural(m.documents || 0, "document")} · every ${m.schedule_hours} h`)),
      h("div", { class: "spacer" }), chip(k, label, last.status === "running"),
      admin && m.state === "ready" ? h("a", { class: "btn small primary", href: "#/sources/add/github-mcp?step=preview" }, "Preview and enable") : null,
      admin && m.enabled ? h("button", { class: "btn small", type: "button", onclick: () => action("sync") }, "Sync now") : null,
      admin && m.enabled ? h("button", { class: "btn small ghost", type: "button", onclick: () => action("pause") }, "Pause") : null),
    m.action ? h("div", { style: "margin-top:14px" }, alertBox(m.state === "paused" ? "info" : "warn", null, m.action)) : null,
    last.error ? h("div", { style: "margin-top:10px" }, alertBox("bad", "Last run failed", last.error)) : null,
    h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Stream"), h("th", {}, "Detects changes"), h("th", {}, "Detects deletions"))),
      h("tbody", {}, m.streams.map((s) => h("tr", {}, h("td", { class: "mono" }, s.stream), h("td", { class: "small" }, s.changes), h("td", { class: "small" }, s.deletions))))));
}

// --- catalog --------------------------------------------------------------------------

async function viewCatalog() {
  const [cat, ov] = await Promise.all([api("/api/catalog"), api("/api/overview")]);
  const githubConnected = ov.github.repos.length > 0 && ov.github.token_configured;
  const ready = cat.items.filter((c) => c.status === "available");
  const early = cat.items.filter((c) => c.status === "early");
  const card = (c) => {
    const blocked = c.requires === "github" && !githubConnected;
    const href = c.id === "github" ? "#/sources/add/github" : "#/sources/add/github-mcp";
    return h("article", { class: "card connector" },
      h("div", { class: "top" }, logo(c.logo), h("div", {}, h("h3", {}, c.name), h("div", { class: "via" }, c.via))),
      h("p", {}, c.what),
      h("div", { class: "foot" }, chip("ok", "Available"),
        h("span", { class: "spacer" }),
        !cat.admin ? h("span", { class: "muted small" }, icon("lock"), " Admin only")
          : blocked ? h("span", { class: "muted small" }, "Connect GitHub first")
            : h("a", { class: "btn primary small", href }, c.id === "github" && githubConnected ? "Add repositories" : "Connect")));
  };
  const earlyCard = (c) => h("article", { class: "card connector early" },
    h("div", { class: "top" }, logo(c.logo), h("div", {}, h("h3", {}, c.name), h("div", { class: "via" }, c.via))),
    h("p", {}, c.what),
    h("div", { class: "foot" }, chip("warn", "Early access")),
    h("details", { class: "cli-help" }, h("summary", {}, "Set up from the command line"),
      h("p", { class: "small muted", style: "margin-bottom:8px" }, c.verified || ""),
      h("div", { class: "cli" }, c.id === "custom"
        ? "aquifer sources add NAME --url https://… --auth oauth\naquifer sources authorize NAME\naquifer sources tools NAME\naquifer sources propose NAME --stream … --tool …\naquifer sources preview NAME\naquifer sources enable NAME"
        : `aquifer sources add ${c.id} --provider ${c.id}${c.id === "slack" ? " --client-id … --token-stdin < secret" : ""}\naquifer sources authorize ${c.id}\naquifer sources tools ${c.id}\naquifer sources propose ${c.id} --stream … --tool …\naquifer sources preview ${c.id}\naquifer sources enable ${c.id}`),
      c.notes ? h("p", { class: "small muted", style: "margin-top:8px" }, c.notes) : null));
  return h("div", { class: "wrap" },
    h("div", { class: "crumbs" }, h("a", { href: "#/sources" }, "← Sources")),
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Add a source"), h("p", {}, "Connect where your team writes things down. Aquifer only reads, and you review what it will read before anything is indexed."))),
    h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Ready to connect"), h("span", { class: "muted" }, "Set up here, verified end to end")),
      h("div", { class: "catalog" }, ready.map(card))),
    cat.integrator ? h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Early access"),
      h("span", { class: "muted" }, "Sign-in is verified; importing content is not yet verified with real accounts. Set up from the command line, with the same preview and explicit enablement.")),
      h("div", { class: "catalog" }, early.map(earlyCard))) : null);
}

// --- GitHub wizard --------------------------------------------------------------------

function steps(labels, current) {
  return h("ol", { class: "steps", "aria-label": "Progress" }, labels.map((l, i) =>
    h("li", { class: i < current ? "done" : null, "aria-current": i === current ? "step" : null },
      h("span", { class: "n" }, i < current ? "✓" : String(i + 1)), l)));
}

async function viewGithub(params) {
  const ov = await api("/api/overview");
  if (!ov.admin) return errorView({ status: 403, message: `Signed in as ${ov.principal}, which can read but not connect sources. Ask an Aquifer admin.` });
  const wiz = { step: 0, probe: null, selected: new Set(), error: null, busy: false, replace: params.get("replace") === "1", gh: ov.github, started: null };
  const root = h("div", { class: "wrap wizard" });
  const labels = ["Token", "Repositories", "Preview", "Review access", "Indexing"];

  function draw() {
    const body = [stepToken, stepRepos, stepPreview, stepReview, stepIndexing][wiz.step]();
    root.replaceChildren();
    append(root, [
      h("div", { class: "crumbs" }, h("a", { href: "#/sources/add" }, "← Add a source")),
      h("div", { class: "page-head" }, h("div", { class: "row" }, logo("github"), h("h1", {}, "Connect GitHub"))),
      steps(labels, wiz.step), body]);
  }
  async function check(payload) {
    wiz.busy = true; wiz.error = null; draw();
    try {
      wiz.probe = await api("/api/github/check", { method: "POST", body: payload });
      wiz.step = 1;
    } catch (err) {
      if (err.silent) return;
      wiz.error = err.message;
    }
    wiz.busy = false; draw();
  }
  function stepToken() {
    const deployment = wiz.gh.token_source === "deployment";
    const stored = wiz.gh.token_source === "ui" && !wiz.replace;
    const input = h("input", { type: "password", id: "gh-token", autocomplete: "off", spellcheck: "false", "aria-describedby": "gh-token-hint" });
    const err = wiz.error ? alertBox("bad", "GitHub connection failed", wiz.error) : null;
    if (deployment || stored) {
      return [h("div", { class: "card pad" },
        h("h2", {}, deployment ? "This server already has a GitHub token" : "Use the token you connected"),
        h("p", { class: "lead" }, deployment ? "Your deployment configured a GitHub token (AQUIFER_GITHUB__TOKEN_SECRET). Aquifer will list what it can read." : `GitHub is connected${wiz.gh.login ? " as @" + wiz.gh.login : ""}. Continue to add repositories with the same token.`),
        err,
        h("div", { class: "wizard-actions" },
          h("button", { class: "btn primary", type: "button", disabled: wiz.busy, onclick: () => check(deployment ? {} : { use_stored: true }) }, wiz.busy ? h("span", { class: "spinner" }) : null, "List repositories"),
          stored ? h("button", { class: "btn ghost", type: "button", onclick: () => { wiz.replace = true; draw(); } }, "Use a different token") : null))];
    }
    const form = h("form", { onsubmit: (e) => { e.preventDefault(); if (input.value.trim()) check({ token: input.value }); else input.focus(); } },
      h("label", { class: "field", for: "gh-token" }, "GitHub token",
        h("span", { class: "hint", id: "gh-token-hint" }, "Stored on this server as a secret reference. It is never shown again and never sent to agents.")),
      input, err ? h("div", { style: "margin-top:12px" }, err) : null,
      h("div", { class: "wizard-actions" },
        h("button", { class: "btn primary", type: "submit", disabled: wiz.busy }, wiz.busy ? h("span", { class: "spinner" }) : null, wiz.busy ? "Checking with GitHub…" : "Check token")));
    setTimeout(() => input.focus(), 0);
    return [h("div", { class: "card pad" },
      h("h2", {}, "Give Aquifer read access"),
      h("p", { class: "lead" }, "Create a ", h("a", { href: "https://github.com/settings/personal-access-tokens/new", target: "_blank", rel: "noopener noreferrer" }, "fine-grained personal access token", icon("ext")), " limited to the repositories you want indexed, with read-only permissions:"),
      h("ul", { class: "perm-list" }, h("li", {}, "Contents: read"), h("li", {}, "Issues: read"), h("li", {}, "Pull requests: read"), h("li", {}, "Metadata: read (added automatically)")),
      h("p", { class: "small muted", style: "margin:12px 0 18px" }, "Aquifer never writes to GitHub. Classic tokens work too; you will see their write scopes before connecting."),
      form)];
  }
  function stepRepos() {
    const p = wiz.probe;
    const connected = new Set(p.connected);
    const filter = h("input", { type: "search", placeholder: "Filter repositories", "aria-label": "Filter repositories", oninput: () => {
      const q = filter.value.toLowerCase();
      list.querySelectorAll("li").forEach((li) => { li.hidden = !li.dataset.name.includes(q); });
    } });
    const count = h("span", { class: "muted small", "aria-live": "polite" }, `${wiz.selected.size} selected`);
    const next = h("button", { class: "btn primary", type: "button", disabled: !wiz.selected.size, onclick: () => { wiz.step = 2; draw(); } }, "Preview content", icon("arrow"));
    const list = h("ul", { class: "repo-list" }, p.repos.map((r) => {
      const already = connected.has(r.full_name);
      const box = h("input", { type: "checkbox", checked: already || wiz.selected.has(r.full_name), disabled: already, onchange: (e) => {
        if (e.target.checked) wiz.selected.add(r.full_name); else wiz.selected.delete(r.full_name);
        count.textContent = `${wiz.selected.size} selected`;
        next.disabled = !wiz.selected.size;
      } });
      return h("li", { "data-name": r.full_name.toLowerCase() }, h("label", {}, box,
        h("div", {}, h("div", { class: "rn" }, r.full_name), r.description ? h("div", { class: "rd" }, r.description) : null,
          h("div", { class: "rd" }, r.pushed_at ? `pushed ${ago(r.pushed_at)}` : "", r.archived ? " · archived" : "")),
        h("div", { class: "row", style: "gap:6px" }, already ? chip("ok", "Connected") : null, chip("", r.private ? "Private" : "Public"))));
    }));
    return [h("div", { class: "card pad" },
      h("h2", {}, "Choose repositories"),
      h("p", { class: "lead" }, p.login ? `Connected to GitHub as @${p.login}` : "Connected to GitHub", ` with a ${p.token_type} token. It can read ${plural(p.repos.length, "repository", "repositories")}${p.truncated ? " (first 500 shown)" : ""}.`),
      p.repos.length ? [filter, list] : alertBox("warn", "This token cannot read any repository", "Give the token access to at least one repository, then check it again."),
      h("div", { class: "wizard-actions" }, h("button", { class: "btn ghost", type: "button", onclick: () => { wiz.step = 0; draw(); } }, "Back"), h("span", { class: "spacer" }), count, next))];
  }
  function stepReview() {
    const rv = wiz.probe.review;
    const box = h("input", { type: "checkbox", id: "ack" });
    const connect = h("button", { class: "btn primary large", type: "button", disabled: true, onclick: async () => {
      connect.disabled = true;
      try {
        await api("/api/github/connect", { method: "POST", body: { repos: [...wiz.selected] } });
        wiz.started = Date.now(); wiz.step = 4; draw();
        every(1500, pollIndexing);
      } catch (err) { if (!err.silent) { wiz.error = err.message; connect.disabled = false; errBox.replaceChildren(alertBox("bad", "Could not connect", err.message)); } }
    } }, "Enable indexing");
    const errBox = h("div", { style: "margin-top:12px" });
    box.addEventListener("change", () => { connect.disabled = !box.checked; });
    return [h("div", { class: "card pad" },
      h("h2", {}, "Review access"),
      h("p", { class: "lead" }, `Before anything is indexed: what Aquifer will do with ${plural(wiz.selected.size, "repository", "repositories")}.`),
      rv.notes.map((n) => h("div", { style: "margin-bottom:12px" }, alertBox(n.level === "warning" ? "warn" : "info", null, n.text))),
      h("div", { class: "review" },
        h("div", { class: "card" }, h("h3", {}, "Aquifer will read"), h("ul", {}, rv.reads.map((r) => h("li", {}, r)))),
        h("div", { class: "card" }, h("h3", {}, "Aquifer will never"), h("ul", {}, h("li", {}, rv.writes), h("li", {}, "Act on what it reads: indexed text is treated as data")),
          h("h3", { style: "margin-top:12px" }, "Token"), h("ul", {}, h("li", {}, rv.stored), wiz.probe.scopes ? h("li", {}, `Scopes: ${wiz.probe.scopes.join(", ") || "none"}`) : null)),
        h("div", { class: "card wide" }, h("h3", {}, "Who will be able to read it"), h("p", { class: "small" }, rv.visibility),
          h("p", { class: "small muted", style: "margin-top:6px" }, `Selected: ${[...wiz.selected].join(", ")}. Synced every ${Math.round(rv.schedule_s / 60)} minutes.`))),
      h("label", { class: "confirm", for: "ack" }, box, h("span", {}, "I understand that everyone with an Aquifer token can read these repositories through Aquifer.")),
      errBox,
      h("div", { class: "wizard-actions" }, h("button", { class: "btn ghost", type: "button", onclick: () => { wiz.step = 2; draw(); } }, "Back"), h("span", { class: "spacer" }), connect))];
  }
  function stepPreview() {
    const key = [...wiz.selected].sort().join(",");
    const body = h("div", { "aria-live": "polite" });
    const next = h("button", { class: "btn primary", type: "button", disabled: true, onclick: () => { wiz.step = 3; draw(); } }, "Review access", icon("arrow"));
    const render = (pv) => {
      next.disabled = false;
      const count = (x) => `${x.at_least ? "at least " : ""}${x.count}`;
      const sample = (items, fmt) => items.length ? h("ul", { class: "pv-sample" }, items.map((i) => h("li", {}, fmt(i)))) : null;
      body.replaceChildren();
      append(body, [pv.repos.map((r) => h("section", { class: "card pv-repo", "aria-label": r.repo },
        h("div", { class: "pv-head" }, logo("github"), h("span", { class: "mono", style: "font-weight:600" }, r.repo),
          r.branch ? h("span", { class: "muted small" }, `default branch ${r.branch}`) : null),
        r.error ? alertBox("bad", null, r.error) : h("div", { class: "pv-grid" },
          h("div", {}, h("div", { class: "pv-k" }, "README"), h("strong", {}, r.readme ? "1" : "0")),
          h("div", {}, h("div", { class: "pv-k" }, "Files"), h("strong", {}, r.files.count),
            r.files.capped ? h("div", { class: "muted small" }, `${r.files.matched} match; the first ${r.files.count} are indexed`) : null,
            sample(r.files.sample, (f) => h("span", { class: "mono" }, f))),
          h("div", {}, h("div", { class: "pv-k" }, "Issues"), h("strong", {}, count(r.issues)),
            sample(r.issues.sample, (i) => `#${i.number} ${i.title}`)),
          h("div", {}, h("div", { class: "pv-k" }, "Pull requests"), h("strong", {}, count(r.pulls)),
            sample(r.pulls.sample, (i) => `#${i.number} ${i.title}`))))),
        pv.omitted ? h("p", { class: "muted small" }, `${plural(pv.omitted, "more repository", "more repositories")} not previewed; they are read the same way.`) : null]);
    };
    if (wiz.preview && wiz.preview.key === key) setTimeout(() => render(wiz.preview.data), 0);
    else {
      body.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), "Reading from GitHub (nothing is stored or indexed)…"));
      api("/api/github/preview", { method: "POST", body: { repos: [...wiz.selected] } })
        .then((data) => { wiz.preview = { key, data }; if (wiz.step === 2) render(data); })
        .catch((err) => { if (!err.silent) body.replaceChildren(alertBox("bad", "Preview failed", err.message)); });
    }
    return [h("div", { class: "card pad" },
      h("h2", {}, "Preview what will be indexed"),
      h("p", { class: "lead" }, "Read-only: the README, files matching Aquifer's patterns at the default branch (decision records, Terraform, Compose, Backstage), issues and pull requests. Nothing is stored until you enable indexing."),
      body,
      h("div", { class: "wizard-actions" }, h("button", { class: "btn ghost", type: "button", onclick: () => { wiz.step = 1; draw(); } }, "Back"), h("span", { class: "spacer" }), next))];
  }
  async function pollIndexing() {
    if (!parseHash().path.startsWith("/sources/add/github")) return;
    const o = await api("/api/overview").catch(() => null);
    if (o) { wiz.gh = o.github; wiz.ov = o; if (wiz.step === 4) draw(); }
  }
  function stepIndexing() {
    const repos = (wiz.gh.repos || []).filter((r) => wiz.selected.has(r.repo));
    const ready = repos.filter((r) => r.documents > 0);
    const failed = repos.filter((r) => r.state === "failed");
    const allDone = repos.length && repos.every((r) => r.state === "ready" || r.state === "failed" || r.state === "empty");
    if (allDone) clearTimers();
    return [h("div", { class: "card pad" },
      h("h2", {}, allDone ? (failed.length === repos.length ? "Indexing failed" : "Ready to ask") : "Indexing"),
      h("p", { class: "lead" }, allDone ? (ready.length ? `${plural(ready.reduce((n, r) => n + r.documents, 0), "document")} indexed. Ask the question you would otherwise take to a teammate.` : "") : "Fetching READMEs, issues, pull requests and decision files. This page updates on its own."),
      h("ul", { class: "progress-list" }, repos.map(repoProgress)),
      allDone ? null : h("div", { class: "bar indeterminate", role: "progressbar", "aria-label": "Indexing" }, h("span")),
      h("div", { class: "wizard-actions" },
        ready.length ? h("a", { class: "btn primary large", href: "#/" }, "Ask your first question", icon("arrow")) : null,
        h("a", { class: "btn", href: "#/sources" }, "Open Sources")))];
  }
  draw();
  return root;
}

// --- GitHub MCP (Integrator) wizard ---------------------------------------------------

async function viewGithubMcp(params) {
  const ov = await api("/api/overview");
  if (!ov.admin) return errorView({ status: 403, message: "Only an admin can connect sources." });
  const repos = ov.github.repos.map((r) => r.repo);
  const existing = ov.mcp_sources.find((m) => m.name === "github-mcp");
  const wiz = { step: params.get("step") === "preview" && existing ? 1 : 0, targets: new Set(), streams: new Set(["discussions", "releases"]), preview: null, error: null, busy: false };
  const root = h("div", { class: "wrap wizard" });
  const draw = () => root.replaceChildren(
    h("div", { class: "crumbs" }, h("a", { href: "#/sources/add" }, "← Add a source")),
    h("div", { class: "page-head" }, h("div", { class: "row" }, logo("github"), h("h1", {}, "GitHub Discussions & Releases"))),
    steps(["Choose content", "Preview", "Enabled"], wiz.step),
    wiz.step === 0 ? choose() : wiz.step === 1 ? previewStep() : done());
  const run = async (fn) => { wiz.busy = true; wiz.error = null; draw(); try { await fn(); } catch (e) { if (!e.silent) wiz.error = e.message; } wiz.busy = false; draw(); };
  function choose() {
    const next = h("button", { class: "btn primary", type: "button", disabled: wiz.busy, onclick: () => run(async () => {
      if (!wiz.targets.size || !wiz.streams.size) throw new Error("Choose at least one repository and one kind of content.");
      await api("/api/mcp/github", { method: "POST", body: { targets: [...wiz.targets], streams: [...wiz.streams] } });
      wiz.preview = await api("/api/mcp/github-mcp/preview", { method: "POST", body: {} });
      wiz.step = 1;
    }) }, wiz.busy ? h("span", { class: "spinner" }) : null, wiz.busy ? "Connecting to GitHub's MCP server…" : "Preview");
    return h("div", { class: "card pad" },
      h("h2", {}, "Choose what to import"),
      h("p", { class: "lead" }, "Imported through GitHub's own MCP server, with read-only tools only, using the GitHub token you connected. Nothing is indexed until you enable it after the preview."),
      h("fieldset", { style: "border:0;padding:0;margin:0" }, h("legend", { class: "field" }, "Content"),
        ["discussions", "releases"].map((s) => h("label", { class: "row", style: "margin-top:8px" }, h("input", { type: "checkbox", checked: wiz.streams.has(s), onchange: (e) => e.target.checked ? wiz.streams.add(s) : wiz.streams.delete(s) }), s === "discussions" ? "Discussions" : "Release notes"))),
      h("fieldset", { style: "border:0;padding:0;margin:18px 0 0" }, h("legend", { class: "field" }, "Repositories"),
        repos.map((r) => h("label", { class: "row", style: "margin-top:8px" }, h("input", { type: "checkbox", checked: wiz.targets.has(r), onchange: (e) => e.target.checked ? wiz.targets.add(r) : wiz.targets.delete(r) }), h("span", { class: "mono" }, r)))),
      wiz.error ? h("div", { style: "margin-top:14px" }, alertBox("bad", "Could not prepare the import", wiz.error)) : null,
      h("div", { class: "wizard-actions" }, next));
  }
  function previewStep() {
    if (!wiz.preview && !wiz.busy && !wiz.error) run(async () => { wiz.preview = await api("/api/mcp/github-mcp/preview", { method: "POST", body: {} }); });
    const pv = wiz.preview;
    const box = h("input", { type: "checkbox", id: "ack-mcp" });
    const enable = h("button", { class: "btn primary large", type: "button", disabled: true, onclick: () => run(async () => {
      await api("/api/mcp/github-mcp/enable", { method: "POST", body: {} }); wiz.step = 2;
    }) }, "Enable import");
    box.addEventListener("change", () => { enable.disabled = !box.checked; });
    return h("div", { class: "card pad" },
      h("h2", {}, "Preview: nothing has been indexed"),
      wiz.error ? alertBox("bad", "Preview failed", wiz.error) : null,
      !pv ? h("div", { class: "row muted" }, h("span", { class: "spinner" }), "Sampling the first page of each stream…") : [
        pv.streams.map((s) => h("div", { class: "card", style: "padding:16px;margin-top:14px;box-shadow:none" },
          h("div", { class: "row" }, h("strong", { class: "mono" }, s.stream), s.error ? chip("bad", "Error") : chip("", `${s.items_on_first_page ?? 0} items on the first page`)),
          s.error ? h("div", { style: "margin-top:8px" }, alertBox("bad", null, s.error)) : null,
          h("p", { class: "small", style: "margin-top:8px" }, h("strong", {}, "Changes: "), s.coverage.changes),
          h("p", { class: "small" }, h("strong", {}, "Deletions: "), s.coverage.deletions),
          s.samples.map((x) => h("div", { class: "small", style: "margin-top:10px;padding-top:10px;border-top:1px solid var(--line)" },
            h("strong", {}, x.title || x.document_id), h("div", { class: "muted" }, `${x.characters} characters · ${x.author || "unknown author"}`),
            h("div", { class: "mono", style: "margin-top:4px;white-space:pre-wrap" }, x.excerpt))))),
        h("div", { style: "margin-top:16px" }, alertBox("info", "Who will be able to read it", pv.access)),
        h("label", { class: "confirm", for: "ack-mcp" }, box, h("span", {}, "I reviewed the samples and coverage, and everyone with an Aquifer token may read this content.")),
        h("div", { class: "wizard-actions" }, enable)]);
  }
  function done() {
    return h("div", { class: "card pad" }, h("h2", {}, "Import enabled"),
      h("p", { class: "lead" }, "The first import is running; after that it syncs daily. Follow it in Sources."),
      h("div", { class: "wizard-actions" }, h("a", { class: "btn primary", href: "#/sources" }, "Open Sources")));
  }
  draw();
  return root;
}

// --- agents ---------------------------------------------------------------------------

async function viewAgents() {
  const root = h("div", { class: "wrap" });
  const flow = { issued: null, client: "claude-code", reveal: false };
  async function refresh() {
    const d = await api("/api/agents").catch(() => null);
    if (d && parseHash().path === "/agents") draw(d);
  }
  function draw(d) {
    const agentRows = d.agents.filter((a) => a.role === "agent" && a.status === "active");
    root.replaceChildren();
    append(root, [
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "Agents"), h("p", {}, "MCP clients retrieve context from this workspace with their own credential. Agent credentials are read-only: they search and read indexed context and cannot sign in to this UI or change sources.")),
        d.can_issue && !agentRows.length && !flow.issued ? h("div", { class: "actions" }, h("a", { class: "btn primary", href: "#connect" , onclick: (e) => { e.preventDefault(); const n = document.getElementById("agent-name"); if (n) n.focus(); } }, icon("plus"), "Connect an agent")) : null),
      endpointCard(d),
      connectFlow(d, flow, refresh),
      credentialsCard(d),
      h("p", { class: "small muted", style: "margin-top:14px" }, "“Used” means Aquifer recorded an authenticated MCP tool call from that credential. Creating a credential or copying a configuration is not a connection. ", d.identity_note)]);
  }
  const data = await api("/api/agents");
  every(4000, refresh);
  draw(data);
  return root;
}

function kv(label, value) { return h("div", {}, h("dt", {}, label), h("dd", {}, value)); }

function endpointCard(d) {
  const net = d.network || {};
  let where;
  if (net.access === "ssm-tunnel") {
    where = [
      alertBox("info", "This address is the SSM tunnel on this computer",
        "Agents on this computer use it while the tunnel is open. The server has no open ports; the tunnel forwards localhost to it. Reopen the tunnel any time with ", h("code", {}, "deploy/aws/aquifer-aws connect"), "."),
      h("p", { class: "small", style: "margin-top:10px" }, h("strong", {}, "Agents running elsewhere"), " (another computer, CI, a hosted agent) cannot use 127.0.0.1. Give them network access first: run the same SSM port forward where they run (needs AWS credentials allowed ", h("code", {}, "ssm:StartSession"), " on this server), or switch the deployment to HTTPS access with a domain and allowed source ranges (",
        h("code", {}, "aquifer install aws --access https --domain …"), "). Tokens are required either way.")];
  } else if (net.access === "https") {
    where = [alertBox("info", "Public HTTPS endpoint", "Reachable from the source ranges the deployment allows. Every request needs a credential."),
      net.remote_mcp_url && net.remote_mcp_url !== d.mcp_url ? h("p", { class: "small", style: "margin-top:8px" }, "Remote clients: ", h("code", {}, net.remote_mcp_url)) : null];
  } else {
    where = [h("p", { class: "small muted" }, net.loopback ? "This server runs on this computer; clients on this computer use this address." : "Clients that can reach this host use this address.")];
  }
  return h("section", { class: "card pad", "aria-labelledby": "ep-h", style: "margin-top:20px" },
    h("h2", { id: "ep-h" }, "Endpoint"),
    h("div", { class: "endpoint" }, h("code", { class: "ep-url" }, d.mcp_url), copyButton(d.mcp_url)),
    h("dl", { class: "ep-facts" },
      kv("Transport", "Streamable HTTP (stateless)"),
      kv("Authentication", d.auth_mode === "none" ? "none (local mode, this machine only)" : "Authorization: Bearer <credential>"),
      kv("Tools", "search_context, get_document, find_related, list_entities, list_sources (read-only)")),
    where);
}

function connectFlow(d, flow, refresh) {
  const card = h("section", { class: "card pad", id: "connect", "aria-labelledby": "connect-h", style: "margin-top:16px" }, h("h2", { id: "connect-h" }, "Connect an agent"));
  if (d.auth_mode === "none") {
    card.append(h("p", { class: "lead" }, "Local mode: MCP clients on this machine connect without a credential."), clientSetup(d, d.setup, flow, true));
    return card;
  }
  if (!d.can_issue) {
    card.append(h("p", { class: "lead" }, d.admin ? d.issue_reason : "Each agent needs its own credential. Ask an Aquifer admin to create one for you; then follow the setup below."), clientSetup(d, d.setup, flow, false));
    return card;
  }
  if (!flow.issued) {
    const input = h("input", { type: "text", id: "agent-name", placeholder: "e.g. claude-code-alex", autocomplete: "off", "aria-describedby": "agent-hint" });
    const err = h("div");
    card.append(steps(["Create a credential", "Store it", "Configure the client", "Verify"], 0),
      h("form", { onsubmit: async (e) => {
        e.preventDefault();
        try { flow.issued = await api("/api/agents", { method: "POST", body: { name: input.value.trim() } }); flow.reveal = false; refresh(); }
        catch (ex) { if (!ex.silent) err.replaceChildren(h("div", { style: "margin-top:10px" }, alertBox("bad", null, ex.message))); }
      } },
        h("label", { class: "field", for: "agent-name" }, "Name the agent", h("span", { class: "hint", id: "agent-hint" }, "One credential per agent, so you can see which one calls and revoke it alone. Lowercase letters, digits and dashes.")),
        input, err, h("div", { class: "wizard-actions" }, h("button", { class: "btn primary", type: "submit" }, icon("key"), "Create credential"))));
    return card;
  }
  const t = flow.issued;
  const row = d.agents.find((a) => a.name === t.name);
  const seen = row && row.observed;
  const secret = h("code", { class: "secret" }, flow.reveal ? t.token : "•".repeat(24));
  card.append(steps(["Create a credential", "Store it", "Configure the client", "Verify"], seen ? 4 : 1),
    alertBox("ok", `Credential “${t.name}” created`, "Read-only MCP access. Aquifer shows it only on this page: store it now."),
    h("h3", { class: "step-h" }, "2 · Store the credential"),
    h("div", { class: "endpoint" }, secret,
      h("button", { class: "btn small", type: "button", onclick: () => { flow.reveal = !flow.reveal; secret.textContent = flow.reveal ? t.token : "•".repeat(24); } }, icon("eye"), flow.reveal ? "Hide" : "Show"),
      copyButton(t.token)),
    h("p", { class: "small muted" }, "Keep it in your password manager or keychain and expose it to the client as the AQUIFER_TOKEN environment variable. Never paste it into a URL, a shared config file or a chat."),
    h("div", { class: "cli" }, t.setup.env.macos),
    h("h3", { class: "step-h" }, "3 · Configure the client"),
    clientSetup(d, t.setup, flow, false),
    h("h3", { class: "step-h" }, "4 · Verify with a first tool call"),
    h("p", { class: "small" }, "Ask your agent something that uses Aquifer (for example “Use Aquifer to list its sources”), or run this from the same computer:"),
    h("div", { class: "copyable" }, h("div", { class: "cli" }, t.setup.verify), copyButton(t.setup.verify)),
    h("div", { style: "margin-top:14px", "aria-live": "polite" }, seen
      ? alertBox("ok", `Verified: ${t.name} made its first tool call ${ago(seen.last_call_at)}`, `Tools called: ${Object.keys(seen.tools).join(", ")}.`)
      : h("div", { class: "row muted" }, h("span", { class: "spinner" }), `Waiting for the first tool call from ${t.name}. Created is not connected: this turns green only when Aquifer receives an authenticated call.`)),
    h("div", { class: "wizard-actions" }, h("button", { class: "btn ghost", type: "button", onclick: () => { flow.issued = null; refresh(); } }, seen ? "Connect another agent" : "Done for now")));
  return card;
}

function clientSetup(d, setup, flow, local) {
  const clients = d.clients;
  const pane = h("div");
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const text = (id) => id === "claude-code" ? (local ? setup["claude-code"].replace(/ --header .*$/, "") : setup["claude-code"]) : setup[id];
  const note = {
    "claude-code": "Single quotes keep ${AQUIFER_TOKEN} unexpanded in the stored configuration; Claude Code reads the variable when it connects, so start it from a shell where AQUIFER_TOKEN is set. Check with: claude mcp list",
    "python-sdk": "pip install mcp. Reads AQUIFER_TOKEN from the environment.",
    "json": "For clients that accept an HTTP MCP server with custom headers and expand environment variables (e.g. a project .mcp.json for Claude Code). Not tested here with other clients: check that yours sends the header.",
  };
  const show = (id) => {
    flow.client = id;
    tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.id === id)));
    const c = clients.find((x) => x.id === id);
    pane.replaceChildren(h("div", { class: "copyable", style: "margin-top:10px" }, h("div", { class: "cli", role: "tabpanel" }, text(id)), copyButton(text(id))),
      h("p", { class: "small muted", style: "margin-top:6px" }, c.tested ? "Tested with this deployment. " : "Not tested. ", note[id]));
  };
  for (const c of clients) tabs.append(h("button", { type: "button", role: "tab", "data-id": c.id, onclick: () => show(c.id) }, c.name, c.tested ? "" : " (untested)"));
  show(flow.client || "claude-code");
  return h("div", {}, tabs, pane, h("p", { class: "small muted", style: "margin-top:8px" }, d.oauth_note));
}

function credentialsCard(d) {
  const role = { admin: "Admin · UI and MCP", agent: "Agent · MCP, read-only", reader: "Reader · UI and MCP, read-only", local: "Local" };
  const rows = d.agents.map((a) => h("tr", {},
    h("td", {}, h("span", { class: "mono", style: "font-weight:600" }, a.name), a.you ? h("span", { class: "muted small" }, " (you)") : null),
    h("td", { class: "small" }, role[a.role] || a.role),
    h("td", {}, a.status === "revoked" ? chip("bad", "Revoked") : a.observed && a.observed.calls_recent ? chip("ok", "Used") : a.observed ? chip("", "Idle 7+ days") : chip("warn", "Never used")),
    h("td", { class: "small" }, a.observed ? `${ago(a.observed.last_call_at)}` : "—", a.observed ? h("div", { class: "muted small" }, Object.entries(a.observed.tools).sort((x, y) => y[1] - x[1]).map(([t, n]) => `${t} ×${n}`).join(", ")) : null),
    h("td", { class: "small muted" }, a.created_at ? `${ago(a.created_at)}${a.created_by ? " by " + a.created_by : ""}` : a.status === "revoked" && a.revoked_at ? `revoked ${ago(a.revoked_at)}` : "deployment"),
    h("td", {}, d.admin && a.status === "active" && a.kind === "token" && !a.admin && !a.you ? h("button", { class: "btn small danger", type: "button", onclick: async () => {
      if (!confirm(`Revoke ${a.name}? Clients using it are rejected immediately.`)) return;
      try { await api(`/api/agents/${a.name}`, { method: "DELETE" }); toast(`${a.name} revoked`); route(); } catch (e) { if (!e.silent) toast(e.message); }
    } }, "Revoke") : null)));
  return h("section", { class: "card source-card", "aria-labelledby": "cred-h", style: "margin-top:16px" },
    h("div", { class: "src-head" }, h("div", {}, h("h3", { id: "cred-h" }, "Credentials"), h("div", { class: "facts" }, "Every credential this server accepts. Revoking takes effect immediately."))),
    h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Can"), h("th", {}, "Status"), h("th", {}, "Last tool call"), h("th", {}, "Created"), h("th", {}, h("span", { class: "sr" }, "Actions")))),
      h("tbody", {}, rows)));
}

async function viewSettings() {
  const s = await api("/api/settings");
  const net = { "ssm-tunnel": "Private: SSM tunnel (no inbound ports)", https: "Public HTTPS (tokens required)", local: "This machine" }[s.network_access] || s.network_access;
  return h("div", { class: "wrap" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"), h("p", {}, "How this server is configured. Change these through the deployment (terraform.tfvars, then aquifer install aws)."))),
    h("div", { class: "agents-grid" },
      h("section", { class: "card pad" }, h("h2", {}, "Models"),
        h("dl", { class: "meta-grid", style: "margin-top:12px" },
          kv("Embeddings", `${s.embedding.model} (${s.embedding.provider})`),
          kv("Answers with citations", s.answers.available ? s.answers.model : `off: ${s.answers.reason || "no model"}`),
          kv("LLM extraction", s.extraction.enabled && s.extraction.model ? s.extraction.model : "off"))),
      h("section", { class: "card pad" }, h("h2", {}, "Server"),
        h("dl", { class: "meta-grid", style: "margin-top:12px" },
          kv("Access", net),
          kv("MCP authentication", s.auth_mode === "token" ? "Bearer credentials" : "None (local mode)"),
          kv("Admin principals", s.admin_principals.join(", ")),
          kv("Sync interval", `every ${Math.round(s.sync_interval_s / 60)} minutes`),
          kv("Deployment", s.environment)))));
}

// --- router ---------------------------------------------------------------------------

function currentView() {
  const r = parseHash();
  switch (r.path) {
    case "/": return viewHome();
    case "/demo": return viewDemo();
    case "/ask": return viewAsk(r.params);
    case "/doc": return viewDoc(r.params);
    case "/sources": return r.params.get("ws") === "demo" ? viewDemoSources() : viewSources();
    case "/sources/add": return viewCatalog();
    case "/sources/add/github": return viewGithub(r.params);
    case "/sources/add/github-mcp": return viewGithubMcp(r.params);
    case "/agents": return viewAgents();
    case "/settings": return viewSettings();
    default: return Promise.resolve(errorView({ message: "This page does not exist." }));
  }
}

async function route() {
  clearTimers();
  if (!state.session || !state.session.signed_in) {
    state.session = await fetch("/api/session", { credentials: "same-origin" }).then((r) => r.json()).catch(() => null);
    if (!state.session) { main.replaceChildren(errorView({ message: "Aquifer did not respond. Is the server running?" })); return; }
    if (!state.session.signed_in) { renderSignIn(); return; }
  }
  const r = parseHash();
  renderChrome(r);
  const keepFocus = r.path === "/ask";
  try {
    const node = await currentView();
    if (node && !(r.path === "/ask" || r.path === "/doc")) main.replaceChildren(node);
  } catch (err) {
    const v = errorView(err);
    if (v) main.replaceChildren(v);
  }
  const title = { "/": "Aquifer", "/ask": "Ask · Aquifer", "/doc": "Source · Aquifer", "/sources": "Sources · Aquifer", "/agents": "Agents · Aquifer", "/settings": "Settings · Aquifer", "/demo": "Demo · Aquifer" }[r.path] || "Aquifer";
  document.title = r.path === "/ask" && r.params.get("q") ? `${r.params.get("q")} · Aquifer` : title;
  if (!keepFocus && r.path !== "/" && r.path !== "/demo") main.focus({ preventScroll: true });
}

window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  e.preventDefault();
  focusAsk();
});
route();
