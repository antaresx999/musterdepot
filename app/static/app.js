"use strict";
// Musterdepot: Dialoge, Bestätigungen, Kursgrafiken und Kostenschätzung beim Handel.

const THIN = "’";
function fmt(v, decimals = 2) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const neg = v < 0;
  const [i, d] = Math.abs(v).toFixed(decimals).split(".");
  return (neg ? "-" : "") + i.replace(/\B(?=(\d{3})+(?!\d))/g, THIN) + (d ? "." + d : "");
}
function fmtPrice(v) {
  const a = Math.abs(v);
  if (a >= 1 || a === 0) return fmt(v, 2);
  if (a >= 0.01) return fmt(v, 4);
  return fmt(v, Math.min(12, 3 - Math.floor(Math.log10(a))));
}
function num(s) {
  if (!s) return NaN;
  return parseFloat(String(s).replace(/[\s'’]/g, "").replace(",", "."));
}
function fmtDate(d) {
  const [y, m, day] = d.split("-");
  return `${day}.${m}.${y}`;
}

// --- Dialoge -------------------------------------------------------------
document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-open]");
  if (open) { document.getElementById(open.dataset.open).showModal(); return; }
  const close = e.target.closest("[data-close]");
  if (close) close.closest("dialog").close();
});
document.addEventListener("submit", (e) => {
  const f = e.target.closest("form[data-confirm]");
  if (f && !confirm(f.dataset.confirm)) e.preventDefault();
});
// Offene Menüs schliessen, wenn daneben geklickt wird
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.menu[open], details.usermenu[open]").forEach((d) => {
    if (!d.contains(e.target)) d.removeAttribute("open");
  });
});

// --- Grafiken ------------------------------------------------------------
const SVGNS = "http://www.w3.org/2000/svg";
function el(name, attrs, parent) {
  const n = document.createElementNS(SVGNS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}
function niceTicks(min, max, count) {
  const span = max - min || Math.abs(max) || 1;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
  const ticks = [];
  for (let t = Math.ceil(min / step) * step; t <= max + step * 1e-9; t += step) ticks.push(t);
  return ticks;
}

function drawChart(box, data) {
  box.innerHTML = "";
  const pts = data.points || [];
  if (pts.length < 2) {
    const m = document.createElement("div");
    m.className = "msg";
    m.textContent = data.error ? "Kursverlauf nicht verfügbar: " + data.error
      : "Noch zu wenig Daten für eine Grafik. Die Werte sammeln sich mit der Zeit.";
    box.appendChild(m);
    return;
  }
  const base = box.dataset.baseline ? data[box.dataset.baseline] || [] : [];
  const W = box.clientWidth, H = box.clientHeight;
  const pad = { l: 8, r: 64, t: 10, b: 24 };
  const vals = pts.map((p) => p[1]).concat(base.map((p) => p[1]));
  let min = Math.min(...vals), max = Math.max(...vals);
  if (min === max) { min -= Math.abs(min) * 0.05 || 1; max += Math.abs(max) * 0.05 || 1; }
  const ticks = niceTicks(min, max, 4);
  min = Math.min(min, ticks[0]); max = Math.max(max, ticks[ticks.length - 1]);
  const x = (i) => pad.l + (i / (pts.length - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - min) / (max - min)) * (H - pad.t - pad.b);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Verlaufsgrafik" }, box);

  const grid = el("g", { class: "grid" }, svg), axis = el("g", { class: "axis" }, svg);
  const decimals = Math.max(...ticks.map(Math.abs)) >= 100 ? 0 : 2;
  ticks.forEach((t) => {
    el("line", { x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t) }, grid);
    el("text", { x: W - pad.r + 8, y: y(t) + 4 }, axis).textContent = t >= 1e6 ? fmt(t / 1e6, 1) + " Mio" : fmt(t, decimals);
  });
  const labels = W < 500 ? [0, pts.length - 1] : [0, Math.floor((pts.length - 1) / 2), pts.length - 1];
  labels.forEach((i, k) => {
    el("text", { x: x(i), y: H - 6, "text-anchor": k === 0 ? "start" : k === labels.length - 1 ? "end" : "middle" }, axis)
      .textContent = fmtDate(pts[i][0]);
  });

  const up = pts[pts.length - 1][1] >= pts[0][1];
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  el("path", { class: "area" + (up ? "" : " neg"), d: `${line}L${x(pts.length - 1)},${H - pad.b}L${x(0)},${H - pad.b}Z` }, svg);
  if (base.length === pts.length) {
    el("path", { class: "base", d: base.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("") }, svg);
  }
  el("path", { class: "line" + (up ? "" : " neg"), d: line }, svg);

  // Fadenkreuz mit Wert
  const cross = el("line", { class: "cross", y1: pad.t, y2: H - pad.b, visibility: "hidden" }, svg);
  const dot = el("circle", { class: "dot", r: 4, visibility: "hidden" }, svg);
  const tip = document.createElement("div");
  tip.className = "tip"; tip.hidden = true; box.appendChild(tip);
  const ccy = box.dataset.currency || "";
  function move(ev) {
    const r = svg.getBoundingClientRect();
    const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - pad.l) / (W - pad.l - pad.r)) * (pts.length - 1))));
    const cx = x(i), cy = y(pts[i][1]);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
    let html = `<b>${ccy} ${fmtPrice(pts[i][1])}</b>${fmtDate(pts[i][0])}`;
    if (base[i]) {
      const d = pts[i][1] - base[i][1];
      html += ` · ${d >= 0 ? "+" : ""}${fmt(d)} ggü. eingezahlt`;
    } else {
      const d = (pts[i][1] / pts[0][1] - 1) * 100;
      html += ` · ${d >= 0 ? "+" : ""}${d.toFixed(2)} %`;
    }
    tip.innerHTML = html; tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(W - tw, cx - tw / 2)) + "px";
    tip.style.top = (cy > 70 ? cy - 56 : cy + 14) + "px";
  }
  function leave() {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
  }
  svg.addEventListener("mousemove", move);
  svg.addEventListener("touchmove", move, { passive: true });
  svg.addEventListener("mouseleave", leave);
  svg.addEventListener("touchend", leave);
}

async function loadChart(box) {
  box.innerHTML = '<div class="msg">Lade Daten…</div>';
  try {
    const r = await fetch(`${box.dataset.src}?range=${encodeURIComponent(box.dataset.range)}`, { credentials: "same-origin" });
    const data = await r.json();
    box._data = data;
    drawChart(box, data);
  } catch (e) {
    box.innerHTML = '<div class="msg">Grafik konnte nicht geladen werden.</div>';
  }
}

document.querySelectorAll(".chart[data-src]").forEach((box) => {
  loadChart(box);
  let t;
  window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => box._data && drawChart(box, box._data), 150); });
});
document.querySelectorAll("[data-chart-range]").forEach((seg) => {
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-range]");
    if (!b) return;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    const box = document.getElementById(seg.dataset.chartRange);
    box.dataset.range = b.dataset.range;
    loadChart(box);
  });
});

// --- Handel: Kostenschätzung ---------------------------------------------
const tf = document.getElementById("trade-form");
if (tf) {
  const d = tf.dataset;
  const price = num(d.price), fx = num(d.fx), cash = num(d.cash), free = num(d.free);
  const fee = (gross) => {
    const f = num(d.feeFixed), p = num(d.feePct), m = num(d.feeMin);
    if (!f && !p && !m) return 0;
    return Math.round(Math.max(m, f + gross * p / 100) * 100) / 100;
  };
  const qty = tf.querySelector("#qty"), amount = tf.querySelector("#amount");
  const type = tf.querySelector("#order-type");
  const side = () => tf.querySelector("input[name=side]:checked").value;
  const execPrice = () => {
    const t = type.value;
    const lim = num(tf.querySelector("[name=limit_price]").value), stop = num(tf.querySelector("[name=stop_price]").value);
    if (t === "limit" && lim > 0) return lim;
    if (t === "stop" && stop > 0) return stop;
    return price;
  };
  function update() {
    const t = type.value;
    tf.querySelectorAll(".only-limit").forEach((x) => (x.hidden = t !== "limit"));
    tf.querySelectorAll(".only-stop").forEach((x) => (x.hidden = t !== "stop"));
    tf.querySelectorAll(".only-pending").forEach((x) => (x.hidden = t === "market"));
    tf.querySelector(".for-amount").hidden = side() === "sell";
    const q = num(qty.value);
    const g = q > 0 && fx > 0 ? q * execPrice() * fx : NaN;
    const f = isNaN(g) ? NaN : fee(g);
    const buy = side() === "buy";
    const total = buy ? g + f : g - f;
    tf.querySelector("#est-gross").textContent = isNaN(g) ? "–" : `${d.currency} ${fmt(g)}`;
    tf.querySelector("#est-fee").textContent = isNaN(f) ? "–" : `${d.currency} ${fmt(f)}`;
    tf.querySelector("#est-label").textContent = buy ? "Belastung (ca.)" : "Gutschrift (ca.)";
    tf.querySelector("#est-total").textContent = isNaN(total) ? "–" : `${d.currency} ${fmt(total)}`;
    let warn = "";
    if (buy && total > cash + 0.005) warn = "Das Bargeld reicht dafür nicht.";
    if (!buy && q > free + 1e-9) warn = "So viele Stück sind nicht frei verfügbar.";
    if (!(price > 0) || !(fx > 0)) warn = "Kein aktueller Kurs oder Wechselkurs – Auftrag nur als Limit/Stop möglich.";
    tf.querySelector("#est-warn").textContent = warn;
    tf.querySelector("#est-warn").classList.toggle("warn", !!warn);
    if (t !== "market") tf.querySelector("#est-label").textContent += " bei Ausführung zum Limit/Stop";
    tf.querySelector("#submit").textContent = buy ? "Kaufauftrag erteilen" : "Verkaufsauftrag erteilen";
  }
  amount.addEventListener("input", () => {
    const a = num(amount.value), p = execPrice();
    if (a > 0 && p > 0 && fx > 0) {
      // Gebühr abziehen, damit der Betrag inkl. Gebühr ungefähr aufgeht
      const g = a - fee(a);
      const q = g / (p * fx);
      qty.value = q >= 1 ? Math.floor(q * 1000) / 1000 : Number(q.toPrecision(6));
    }
    update();
  });
  tf.querySelector("#qty-max").addEventListener("click", () => {
    if (side() === "sell") qty.value = free;
    else {
      const p = execPrice();
      if (p > 0 && fx > 0) {
        let q = (cash - fee(cash)) / (p * fx);
        q = q >= 1 ? Math.floor(q) : Math.floor(q * 1e6) / 1e6;
        while (q > 0 && q * p * fx + fee(q * p * fx) > cash) q = q >= 1 ? q - 1 : Math.floor(q * 0.999 * 1e6) / 1e6;
        qty.value = Math.max(0, q);
      }
    }
    amount.value = "";
    update();
  });
  tf.addEventListener("input", (e) => { if (e.target !== amount) update(); });
  tf.addEventListener("change", update);
  update();
}
