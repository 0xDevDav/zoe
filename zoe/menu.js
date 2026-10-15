// Zoe's menu in Telegram: asks the service for a view, draws it, and acts on taps.
// Everything shown is set as text, never as markup.
"use strict";

const tg = window.Telegram.WebApp;
const main = document.getElementById("view");
const toast = document.getElementById("toast");
// Every view is asked for again each second, and drawn anew only when it changed.
const EVERY = 1000;
let shown = null;
let drawn = "";
let asked = 0;
let timer = 0;
// The server's clock minus this phone's, in seconds: running times tick against the server.
let offset = 0;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function notify(text) {
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { toast.hidden = true; }, 2500);
}

// Asks for a view or an action. An answer overtaken by a newer request is dropped, and the
// view on screen is asked for again a second later, skipped while the app is in the background.
async function go(request, refresh) {
  clearTimeout(timer);
  const mine = ++asked;
  let view = null;
  if (!(refresh && document.hidden)) {
    try {
      const response = await fetch("api", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": tg.initData },
        body: JSON.stringify(request),
      });
      view = await response.json();
    } catch (error) {
      if (!refresh) notify("⚠️ " + error.message);
    }
  }
  if (mine !== asked) return;
  if (view) draw(view, refresh);
  const next = shown && shown.at ? { view: shown.at } : request.view ? request : null;
  if (next) timer = setTimeout(() => go(next, true), EVERY);
}

// Whether the person is writing in a field: a refresh never takes it away from them.
function typing() {
  return [...main.querySelectorAll("input")].some((field) => field === document.activeElement || field.value);
}

// A refresh keeps the scroll and the open texts, and draws nothing when the view did not change.
function draw(view, refresh) {
  if (view.now) offset = view.now - Date.now() / 1000;
  const content = JSON.stringify({ ...view, now: 0, notice: "" });
  if (refresh && (content === drawn || typing())) return;
  drawn = content;
  const open = refresh ? [...main.querySelectorAll("details")].map((d) => d.open) : [];
  const top = refresh ? window.scrollY : 0;
  document.getElementById("brand").textContent = view.brand || "";
  document.getElementById("title").textContent = view.title;
  main.replaceChildren(...view.blocks.map(block));
  if (refresh) main.querySelectorAll("details").forEach((d, i) => { d.open = Boolean(open[i]); });
  window.scrollTo(0, top);
  if (view.back) tg.BackButton.show(); else tg.BackButton.hide();
  if (view.notice) notify(view.notice);
  shown = view;
  tick();
}

// A running time as a clock: 4:05, 1:04:05, or with days in front.
function clock(seconds, day) {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor(total / 3600) % 24;
  const minutes = Math.floor(total / 60) % 60;
  const two = (n) => String(n).padStart(2, "0");
  const time = days || hours ? `${hours}:${two(minutes)}:${two(total % 60)}` : `${minutes}:${two(total % 60)}`;
  return days ? `${days} ${day} ${time}` : time;
}

// A text with a running time keeps its pattern and the moment the time started.
function timed(node, since) {
  if (since) {
    node.dataset.since = since;
    node.dataset.pattern = node.textContent;
  }
  return node;
}

function tick() {
  const now = Date.now() / 1000 + offset;
  for (const node of main.querySelectorAll("[data-since]")) {
    node.textContent = node.dataset.pattern.replace("{elapsed}", clock(now - Number(node.dataset.since), shown.day));
  }
}

function block(part) {
  if ("text" in part) return timed(el("p", "", part.text), part.since);
  if ("heading" in part) return el("h2", "", part.heading);
  if ("code" in part) return el("code", "", part.code);
  if ("stats" in part) {
    const grid = el("div", "stats cols-" + (part.columns || 2));
    for (const stat of part.stats) {
      const card = el("div", ["stat", stat.tone, stat.wide ? "wide" : ""].join(" ").trim());
      card.append(el("small", "", stat.label), timed(el("strong", "", stat.value), stat.since));
      if (stat.detail) card.append(el("span", "", stat.detail));
      grid.append(card);
    }
    return grid;
  }
  if ("meters" in part) {
    // A bar for each figure, filled to its share of 100 and coloured by its tone.
    const card = el("div", "meters");
    for (const meter of part.meters) {
      const row = el("div", ["meter", meter.tone].join(" ").trim());
      const top = el("div", "top");
      top.append(el("span", "", meter.label), el("strong", "", meter.value));
      const bar = el("div", "bar");
      const fill = el("i");
      fill.style.width = meter.fill + "%";
      bar.append(fill);
      row.append(top, bar);
      if (meter.detail) row.append(el("small", "", meter.detail));
      card.append(row);
    }
    return card;
  }
  if ("sections" in part) {
    // One section open at a time: opening one closes the others.
    const group = el("div", "sections");
    for (const section of part.sections) {
      const details = el("details", "section");
      const summary = el("summary");
      const title = el("span", "", section.title);
      if (section.detail) title.append(el("small", "", section.detail));
      summary.append(title);
      const inner = el("div", "inner");
      inner.append(...section.blocks.map(block));
      details.append(summary, inner);
      details.open = Boolean(section.open);
      details.ontoggle = () => {
        if (!details.open) return;
        for (const other of group.children) if (other !== details) other.open = false;
      };
      group.append(details);
    }
    return group;
  }
  if ("input" in part) {
    const form = el("form", "add");
    const field = el("input");
    field.placeholder = part.input.placeholder;
    field.inputMode = "numeric";
    form.append(field, el("button", "button success", part.input.label));
    form.onsubmit = (event) => {
      event.preventDefault();
      const value = field.value.trim();
      if (value) go({ action: part.input.act + ":" + value });
    };
    return form;
  }
  if ("quote" in part) {
    const details = el("details", "quote");
    const full = el("div", "", part.quote);
    full.onclick = () => { details.open = false; };
    details.append(el("summary", "", part.quote.split("\n")[0].slice(0, 90) + " …"), full);
    return details;
  }
  if ("items" in part) {
    const list = el("div", "list");
    for (const item of part.items) {
      const row = el(item.to || item.act ? "button" : "div", item.to ? "row go" : "row");
      const label = el("span", "", item.label);
      if (item.detail) label.append(timed(el("small", "", item.detail), item.since));
      row.append(label);
      if (item.count) row.append(el("b", "count", String(item.count)));
      if (item.side) row.append(el("em", "side", item.side));
      if (item.to) row.onclick = () => go({ view: item.to });
      else if (item.act) row.onclick = () => act(item.act, item.confirm || "");
      list.append(row);
    }
    return list;
  }
  const button = el("button", "button " + part.button.style, part.button.label);
  button.onclick = () => act(part.button.act, part.button.confirm);
  return button;
}

function act(action, confirm) {
  if (!confirm) {
    go({ action });
    return;
  }
  tg.showConfirm(confirm, (ok) => { if (ok) go({ action }); });
}

// The page's background follows the theme into Telegram's own header and bars.
const background = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
if (/^#[0-9a-f]{6}$/i.test(background)) {
  tg.setHeaderColor(background);
  tg.setBackgroundColor(background);
}
// Nothing shown can be copied, also where a webview ignores the style: only a field's own text can.
for (const kind of ["copy", "cut", "contextmenu", "dragstart"]) {
  document.addEventListener(kind, (event) => { if (event.target.tagName !== "INPUT") event.preventDefault(); });
}
tg.BackButton.onClick(() => { if (shown && shown.back) go({ view: shown.back }); });
setInterval(tick, 1000);
tg.ready();
tg.expand();
go({ view: new URLSearchParams(location.search).get("view") || "home" });
