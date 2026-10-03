const PHASES = [["connectivity", "Connectivity"], ["configuration", "Configuration"], ["testing", "Testing"]];
const SVG_NS = "http://www.w3.org/2000/svg";
const WIRE_COLORS = { UART_TX: "#e0a100", UART_RX: "#2f9e44", VCC_5V: "#d6336c", GND: "#222" };

const comps = {};          // component id -> {panel, li, phases: {name: el}, done}
let activeId = null, shownId = null, hasKey = false;
const tests = {}, wiring = {};

const $ = (s, r = document) => r.querySelector(s);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function svg(tag, attrs, text) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}

const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); (handlers[m.type] || (() => {}))(m); };
ws.onclose = () => { const b = $("#banner"); b.textContent = "Disconnected from server. Reload the page."; b.classList.remove("hidden"); };
const send = (m) => ws.send(JSON.stringify(m));

// ---- layout helpers ----
function ensureComponent(id, index) {
  if (comps[id]) return comps[id];
  $("#empty")?.remove();
  const panel = el("section", "panel");
  const h1 = el("h1"); h1.append(el("span", "idx", id + " "), el("span", "name", "New Sensor"));
  const phasesEl = el("div", "phases"), phases = {};
  for (const [key, label] of PHASES) {
    const p = el("div", "phase pending"); p.dataset.label = label; p.dataset.phase = key;
    const head = el("button", "phase-head");
    head.append(el("span", "label", label), el("span", "chev", "v"));
    head.onclick = () => p.classList.contains("complete") && p.classList.toggle("open");
    const body = el("div", "phase-body"); body.append(el("div", "feed"));
    p.append(head, body); phasesEl.append(p); phases[key] = p;
  }
  const next = el("div", "next hidden", "Let’s move onto the next sensor");
  const nextBtn = el("button", null, "Add next sensor"); nextBtn.onclick = () => send({ type: "add_component" });
  next.append(nextBtn);
  panel.append(h1, phasesEl, next);
  $("#panels").append(panel);

  const li = el("li"); li.append(el("span", "cname", "New Sensor"));
  const dots = el("span", "dots");
  for (const [key] of PHASES) { const d = el("span", "dot"); d.dataset.phase = key; dots.append(d); }
  li.append(dots); li.onclick = () => show(id);
  $("#inputs").append(li);
  comps[id] = { panel, li, phases, next, done: false };
  return comps[id];
}

function show(id) {
  shownId = id;
  for (const [cid, c] of Object.entries(comps)) {
    c.panel.classList.toggle("hidden", cid !== id);
    c.li.classList.toggle("shown", cid === id);
  }
  updateComposer();
}

function updateComposer() {
  const running = activeId && !comps[activeId].done;
  for (const b of $("#mode-seg").children) b.disabled = !!running;
  $("#mode-seg").title = running ? "Switch modes between components" : "";
  const live = running && shownId === activeId;
  $("#text").disabled = $("#send").disabled = !live;
  if (live) $("#text").focus();
}

function feed(m) { return $(".feed", comps[m.component_id].phases[m.phase]); }
function append(m, node) {
  const f = feed(m), thinking = $(".thinking", f);
  thinking ? f.insertBefore(node, thinking) : f.append(node);
  scrollDown();
  return node;
}
function scrollDown() { const p = $("#panels"); p.scrollTop = p.scrollHeight; }

// ---- event handlers ----
const handlers = {
  hello(m) {
    $("#board-name").textContent = m.board + (m.transport === "sim" ? " · simulated board" : "");
    $("#sim-toggle").classList.toggle("hidden", m.transport !== "sim");
  },
  mode(m) {
    const online = m.mode === "online", badge = $("#mode-badge");
    badge.className = `badge ${m.mode}`;
    badge.textContent = online ? `Online mode · ${m.model}` : "Offline mode · scripted, no LLM";
    for (const b of $("#mode-seg").children) b.classList.toggle("on", b.dataset.mode === m.mode);
    hasKey = m.has_key;
    $("#key-form").classList.add("hidden"); $("#mode-error").textContent = ""; $("#api-key").value = "";
  },
  mode_error(m) { $("#key-form").classList.remove("hidden"); $("#mode-error").textContent = m.text; },
  component(m) {
    const c = ensureComponent(m.component_id, m.index);
    $(".name", c.panel).textContent = m.name; $(".cname", c.li).textContent = m.name;
    activeId = m.component_id; show(m.component_id);
  },
  phase(m) {
    const c = comps[m.component_id], p = c.phases[m.phase];
    p.classList.remove("pending", "active", "complete", "open"); p.classList.add(m.status);
    $(".label", p).textContent = m.status === "active" ? `${p.dataset.label} in progress` : `${p.dataset.label} (Complete)`;
    $(`.dot[data-phase="${m.phase}"]`, c.li).className = `dot ${m.status}`;
    scrollDown();
  },
  message(m) {
    const p = el("p", `msg ${m.role}`);
    p.append(el("b", null, m.role === "probe" ? "Probe: " : "User: "), document.createTextNode(m.text));
    append(m, p);
  },
  thinking(m) {
    const f = feed(m);
    $(".thinking", f)?.remove();
    if (m.on) { f.append(el("p", "thinking", "Probe is thinking…")); scrollDown(); }
  },
  error(m) {
    if (m.component_id) append(m, el("p", "error", m.text));
    else alert(m.text);
  },
  schematic(m) { append(m, renderSchematic(m)); },
  wiring_step(m) {
    const card = el("div", "card wiring");
    card.append(el("div", null, m.instruction));
    const actions = el("div", "actions"), btn = el("button", null, "Completed");
    btn.onclick = () => { btn.disabled = true; send({ type: "user_message", text: "completed" }); };
    actions.append(btn); card.append(actions);
    wiring[`${m.component_id}:${m.pin}`] = actions;
    append(m, card);
  },
  wiring_confirmed(m) {
    const a = wiring[`${m.component_id}:${m.pin}`];
    if (a) a.replaceChildren(el("span", "ok", "✓ Connected"));
  },
  board_io(m) {
    const f = feed(m);
    let block = [...f.children].filter((n) => !n.classList.contains("thinking")).pop();
    if (!block || !block.classList.contains("io")) block = append(m, el("div", "io"));
    const err = m.error ?? /^ERR/.test(m.line);
    const arrow = { tx: "→", rx: "←", sensor: "⇠" }[m.direction] || "·";
    block.append(el("div", m.direction === "tx" ? "tx" : `${m.direction}${err ? " err" : ""}`, `${arrow} ${m.line}`));
    scrollDown();
  },
  config(m) {
    const card = el("div", "card"), table = el("table", "config");
    card.append(el("div", null, "Configuration applied:"));
    for (const [k, v] of Object.entries(m.settings)) {
      const tr = el("tr"); tr.append(el("td", null, k.replace(/_/g, " ")), el("td", null, String(v))); table.append(tr);
    }
    card.append(table); append(m, card);
  },
  test_step(m) {
    const card = el("div", "card test"), status = el("div", "status"), values = el("div", "values"), result = el("div", "result");
    card.append(el("div", "instr", m.instruction), status, values, result);
    tests[m.test_id] = { status, values, result, seconds: m.seconds };
    let left = Math.round(m.lead);
    const tick = () => {
      if (tests[m.test_id].sampling) return;
      status.textContent = left > 0 ? `Starting in ${left}…` : "Starting…";
      if (left-- > 0) setTimeout(tick, 1000);
    };
    tick(); append(m, card);
  },
  test_status(m) {
    const t = tests[m.test_id]; if (!t) return;
    if (m.status === "sampling") { t.sampling = true; t.status.textContent = `Measuring for ${t.seconds} s…`; }
    else t.status.textContent = `Measured ${m.samples} samples.`;
  },
  reading(m) {
    const t = tests[m.test_id]; if (!t) return;
    t.values.replaceChildren(...Object.entries(m.values).map(([k, v]) => el("span", null, `${k} ${v.toFixed(3)}`)));
  },
  test_result(m) {
    const t = tests[m.test_id]; if (!t) return;
    t.result.replaceChildren(el("span", m.passed ? "ok" : "bad", m.passed ? "✓ Passed " : "✗ Failed "), document.createTextNode(m.summary));
    scrollDown();
  },
  done(m) {
    const c = comps[m.component_id]; c.done = true;
    c.next.classList.remove("hidden"); updateComposer(); scrollDown();
  },
};

function renderSchematic(m) {
  const rows = m.connections, rowH = 46, top = 56, h = top + rows.length * rowH + 16;
  const s = svg("svg", { viewBox: `0 0 620 ${h}` });
  const box = (x, label) => {
    s.append(svg("rect", { x, y: top - 14, width: 170, height: rows.length * rowH + 4, rx: 10, fill: "#fff", stroke: "#666" }));
    s.append(svg("text", { x: x + 85, y: top - 26, "text-anchor": "middle", "font-size": 15, fill: "#333" }, label));
  };
  box(20, m.sensor); box(430, "Probe Cube");
  rows.forEach((w, i) => {
    const y = top + 14 + i * rowH, color = WIRE_COLORS[w.signal] || "#777";
    s.append(svg("text", { x: 176, y: y + 5, "text-anchor": "end", "font-size": 14 }, w.sensor_pin));
    s.append(svg("circle", { cx: 190, cy: y, r: 5, fill: color }));
    if (w.cube_port) {
      s.append(svg("path", { d: `M190 ${y} C 300 ${y}, 320 ${y}, 430 ${y}`, stroke: color, "stroke-width": 3, fill: "none" }));
      s.append(svg("circle", { cx: 430, cy: y, r: 5, fill: color }));
      s.append(svg("text", { x: 444, y: y + 1, "font-size": 14 }, `Port ${w.cube_port}`));
      s.append(svg("text", { x: 444, y: y + 16, "font-size": 11, fill: "#777" }, `${w.mcu_pin} · ${w.mcu_function}`));
    }
  });
  const card = el("div", "card schematic"); card.append(s);
  return card;
}

// ---- controls ----
for (const b of $("#mode-seg").children) b.onclick = () => {
  if (b.classList.contains("on")) return;
  if (b.dataset.mode === "online" && !hasKey) { $("#key-form").classList.remove("hidden"); $("#api-key").focus(); return; }
  send({ type: "set_mode", mode: b.dataset.mode });
};
$("#key-form").onsubmit = (e) => {
  e.preventDefault();
  $("#mode-error").textContent = "Checking key…";
  send({ type: "set_mode", mode: "online", api_key: $("#api-key").value });
};
$("#key-cancel").onclick = () => { $("#key-form").classList.add("hidden"); $("#mode-error").textContent = ""; };
$("#add").onclick = () => send({ type: "add_component" });
$("#sim-fault").onchange = (e) => send({ type: "sim_fault", on: e.target.checked });
$("#composer").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#text").value.trim();
  if (!text) return;
  send({ type: "user_message", text }); $("#text").value = "";
};
