const ws     = new WebSocket(`ws://${location.host}/ws`);
const status = document.getElementById("status");

ws.onopen  = () => setStatus("ok", "connected");
ws.onerror = () => setStatus("error", "error");
ws.onclose = () => setStatus("error", "disconnected");

ws.onmessage = ({ data }) => {
  const msg = JSON.parse(data);
  switch (msg.event) {
    case "source": renderSource(msg.data.source);   break;
    case "ast":    renderAST(msg.data.tree);        break;
    case "step":   highlightStep(msg.data);         break;
    case "var":    updateVar(msg.data);             break;
    case "call":   updateCallstack(msg.data.stack); break;
    case "ret":    updateCallstack(msg.data.stack); break;
    case "hex":    renderHex(msg.data);             break;
    case "error":  showError(msg.data);             break;
    case "done":   setStatus("ok", "done ✓");       break;
  }
};

function setStatus(cls, text) {
  status.className = `status ${cls}`;
  status.textContent = text;
}

// ── SOURCE ────────────────────────────────────────────────────────────────────

function renderSource(src) {
  const el = document.getElementById("source-view");
  // используем div вместо span чтобы не было лишних пробелов от pre
  el.innerHTML = src.split("\n").map((line, i) =>
    `<div class="source-line" id="sl-${i+1}" data-line="${i+1}">`
    + `<span class="line-num">${String(i+1).padStart(3)}</span>`
    + escHtml(line)
    + `</div>`
  ).join("");   // ← join("") без \n

  el.querySelectorAll(".source-line").forEach(row => {
    row.addEventListener("click", () => {
      const line = parseInt(row.dataset.line);
      activateSourceLine(line);
      highlightASTNode(line);
    });
  });
}

function activateSourceLine(line) {
  _activeLine = line;
  document.querySelectorAll(".source-line.active")
    .forEach(el => el.classList.remove("active"));
  const el = document.getElementById(`sl-${line}`);
  if (el) {
    el.classList.add("active");
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  // синхронизируем hex
  highlightHexLine(line);
}

function highlightStep({ line }) {
  activateSourceLine(line);
  highlightASTNode(line);
}

// ── AST TREE — Canvas ─────────────────────────────────────────────────────────

let _astNodes      = [];
let _astCanvas     = null;
let _astCtx        = null;
let _astAnim       = null;
let _activeASTLine = -1;

// viewport transform
let _panX  = 0;
let _panY  = 20;
let _zoom  = 1.0;
const ZOOM_MIN = 0.2;
const ZOOM_MAX = 3.0;

// layout constants
const NODE_W  = 140;
const NODE_H  = 36;
const NODE_R  = 6;
const LEVEL_H = 90;   // вертикальное расстояние между уровнями
const NODE_GAP = 24;  // минимальный горизонтальный зазор между узлами

const C = {
  bg:              "#0d1117",
  node:            "#161b22",
  nodeBorder:      "#30363d",
  nodeHover:       "#21262d",
  nodeHoverBorder: "#58a6ff",
  nodeActive:      "#1f3a1f",
  nodeActiveBorder:"#3fb950",
  text:            "#c9d1d9",
  textSmall:       "#8b949e",
  edge:            "#30363d",
  edgeActive:      "#3fb950",
};

// ── tooltip ───────────────────────────────────────────────────────────────────

let _tooltip    = null;
let _hoverNode  = null;

function createTooltip() {
  _tooltip = document.createElement("div");
  _tooltip.id = "ast-tooltip";
  _tooltip.style.cssText = `
    position: fixed;
    display: none;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 11px;
    color: #c9d1d9;
    pointer-events: none;
    z-index: 1000;
    max-width: 280px;
    line-height: 1.6;
    box-shadow: 0 4px 16px #00000088;
  `;
  document.body.appendChild(_tooltip);
}

function showTooltip(node, mx, my) {
  if (!_tooltip) createTooltip();
  const raw   = node.raw;
  const lines = [`<b style="color:#58a6ff">${raw._type}</b>`];
  for (const [k, v] of Object.entries(raw)) {
    if (k.startsWith("_")) continue;
    if (typeof v === "object") continue;
    if (v === null || v === undefined) continue;
    lines.push(`<span style="color:#8b949e">${k}:</span> ${escHtml(String(v))}`);
  }
  if (raw._line) lines.push(`<span style="color:#484f58">line ${raw._line}, col ${raw._col || 0}</span>`);
  _tooltip.innerHTML = lines.join("<br>");
  _tooltip.style.display = "block";
  moveTooltip(mx, my);
}

function moveTooltip(mx, my) {
  if (!_tooltip || _tooltip.style.display === "none") return;
  const W = window.innerWidth;
  const H = window.innerHeight;
  const tw = _tooltip.offsetWidth  + 16;
  const th = _tooltip.offsetHeight + 16;
  _tooltip.style.left = (mx + tw > W ? mx - tw : mx + 12) + "px";
  _tooltip.style.top  = (my + th > H ? my - th : my + 12) + "px";
}

function hideTooltip() {
  if (_tooltip) _tooltip.style.display = "none";
  _hoverNode = null;
}

// ── render ────────────────────────────────────────────────────────────────────

function renderAST(tree) {
  const container = document.getElementById("ast-view");
  container.innerHTML = "";
  if (!_tooltip) createTooltip();

  _astCanvas = document.createElement("canvas");
  _astCanvas.style.cssText = "display:block;width:100%;height:100%;cursor:grab;";
  container.appendChild(_astCanvas);

  _astNodes = [];
  let idCounter = 0;

  function buildNode(raw, depth) {
    const id   = idCounter++;
    const node = {
      id,
      label:    nodeLabel(raw),
      line:     raw._line || 0,
      depth,
      x: 0, y: depth * LEVEL_H + 40,
      _ty: depth * LEVEL_H + 40,
      _alpha: 0,
      children: [],
      raw,
    };
    _astNodes.push(node);
    collectChildRaws(raw).forEach(k => {
      node.children.push(buildNode(k, depth + 1).id);
    });
    return node;
  }

  buildNode(tree, 0);

  resizeCanvas();
  layoutTree();
  centerView();
  animateAST();
  bindCanvasEvents();
}

// ── Reingold-Tilford layout ───────────────────────────────────────────────────

function layoutTree() {
  if (!_astNodes.length) return;

  // вычисляем ширину поддерева
  function subtreeW(node) {
    if (!node.children.length) return NODE_W + NODE_GAP;
    return Math.max(
      NODE_W + NODE_GAP,
      node.children.reduce((s, cid) => s + subtreeW(_astNodes[cid]), 0)
    );
  }

  // расставляем узлы
  function place(node, left) {
    const kids = node.children.map(id => _astNodes[id]);
    if (!kids.length) {
      node.x  = left + (NODE_W + NODE_GAP) / 2;
      node._tx = node.x;
      return left + NODE_W + NODE_GAP;
    }
    let cursor = left;
    kids.forEach(k => { cursor = place(k, cursor); });
    node.x  = (kids[0].x + kids[kids.length - 1].x) / 2;
    node._tx = node.x;
    return cursor;
  }

  place(_astNodes[0], 0);

  // y по глубине
  _astNodes.forEach(n => {
    n._ty = n.depth * LEVEL_H + 40;
  });
}

function centerView() {
  if (!_astNodes.length || !_astCanvas) return;
  const xs   = _astNodes.map(n => n.x);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  _panX = _astCanvas.width / 2 - (minX + maxX) / 2;
  _panY = 20;
  _zoom = 1.0;
}

// ── анимация ──────────────────────────────────────────────────────────────────

function animateAST() {
  if (_astAnim) cancelAnimationFrame(_astAnim);

  // стартовые позиции — узлы падают сверху
  _astNodes.forEach(n => {
    n.y      = (n._ty || 0) - 30;
    n._alpha = 0;
  });

  function frame() {
    let done = true;
    _astNodes.forEach(n => {
      if (n._alpha < 1) {
        n._alpha = Math.min(1, n._alpha + 0.05);
        done = false;
      }
      const dy = (n._ty - n.y);
      if (Math.abs(dy) > 0.3) {
        n.y += dy * 0.14;
        done = false;
      } else {
        n.y = n._ty;
      }
    });
    drawAST();
    if (!done) _astAnim = requestAnimationFrame(frame);
  }

  _astAnim = requestAnimationFrame(frame);
}

// ── draw ──────────────────────────────────────────────────────────────────────

function drawAST() {
  if (!_astCtx || !_astNodes.length) return;
  const ctx = _astCtx;
  const W   = _astCanvas.width;
  const H   = _astCanvas.height;

  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(_panX, _panY);
  ctx.scale(_zoom, _zoom);

  // рёбра
  _astNodes.forEach(n => {
    n.children.forEach(cid => {
      const child = _astNodes[cid];
      const alpha = Math.min(n._alpha, child._alpha);
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = (n.line === _activeASTLine && _activeASTLine > 0)
        ? C.edgeActive : C.edge;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(n.x, n.y + NODE_H / 2);
      ctx.bezierCurveTo(
        n.x,     n.y + NODE_H / 2 + LEVEL_H * 0.5,
        child.x, child.y - NODE_H / 2 - LEVEL_H * 0.5,
        child.x, child.y - NODE_H / 2
      );
      ctx.stroke();
    });
  });

  // узлы
  _astNodes.forEach(n => {
    const active  = n.line === _activeASTLine && _activeASTLine > 0;
    const hover   = n === _hoverNode;
    ctx.globalAlpha = n._alpha;

    // тень
    ctx.shadowColor = active ? "#3fb95055" : hover ? "#58a6ff33" : "#00000055";
    ctx.shadowBlur  = active ? 14 : hover ? 10 : 5;

    // фон
    roundRect(ctx, n.x - NODE_W/2, n.y - NODE_H/2, NODE_W, NODE_H, NODE_R);
    ctx.fillStyle = active ? C.nodeActive : hover ? C.nodeHover : C.node;
    ctx.fill();

    ctx.shadowBlur = 0;

    // рамка
    ctx.strokeStyle = active ? C.nodeActiveBorder
                    : hover  ? C.nodeHoverBorder
                    :          C.nodeBorder;
    ctx.lineWidth = active || hover ? 1.5 : 1;
    ctx.stroke();

    // label
    ctx.fillStyle    = C.text;
    ctx.font         = `bold 11px Consolas, monospace`;
    ctx.textAlign    = "center";
    ctx.textBaseline = "middle";
    const lbl = n.label.length > 18 ? n.label.slice(0, 17) + "…" : n.label;
    ctx.fillText(lbl, n.x, n.y - (n.line ? 5 : 0));

    // line hint
    if (n.line) {
      ctx.fillStyle = C.textSmall;
      ctx.font      = `9px Consolas, monospace`;
      ctx.fillText(`line ${n.line}`, n.x, n.y + 9);
    }
  });

  ctx.restore();
  ctx.globalAlpha = 1;
}

// ── события canvas ────────────────────────────────────────────────────────────

let _panning   = false;
let _panStart  = null;

function bindCanvasEvents() {
  const cv = _astCanvas;

  // pan
  cv.addEventListener("mousedown", e => {
    _panning  = true;
    _panStart = { x: e.clientX - _panX, y: e.clientY - _panY };
    cv.style.cursor = "grabbing";
  });
  window.addEventListener("mouseup", () => {
    _panning = false;
    if (cv) cv.style.cursor = "grab";
  });
  cv.addEventListener("mousemove", e => {
    if (_panning) {
      _panX = e.clientX - _panStart.x;
      _panY = e.clientY - _panStart.y;
      drawAST();
      return;
    }
    // hover — определяем узел под курсором
    const n = nodeAtEvent(e);
    if (n !== _hoverNode) {
      _hoverNode = n;
      drawAST();
    }
    if (n) {
      showTooltip(n, e.clientX, e.clientY);
    } else {
      hideTooltip();
    }
    moveTooltip(e.clientX, e.clientY);
  });

  cv.addEventListener("mouseleave", () => {
    hideTooltip();
    _hoverNode = null;
    drawAST();
  });

  // zoom колёсиком
  cv.addEventListener("wheel", e => {
    e.preventDefault();
    const rect   = cv.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;

    const delta  = e.deltaY > 0 ? 0.9 : 1.1;
    const newZoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, _zoom * delta));

    // zoom относительно позиции курсора
    _panX = mouseX - (mouseX - _panX) * (newZoom / _zoom);
    _panY = mouseY - (mouseY - _panY) * (newZoom / _zoom);
    _zoom = newZoom;

    drawAST();
  }, { passive: false });

  // клик — подсветка строки
  cv.addEventListener("click", e => {
    if (_panning) return;
    const n = nodeAtEvent(e);
    if (n && n.line) {
      _activeASTLine = n.line;
      activateSourceLine(n.line);
      drawAST();
    }
  });

  window.addEventListener("resize", () => {
    resizeCanvas();
    layoutTree();
    drawAST();
  });
}

function nodeAtEvent(e) {
  const rect  = _astCanvas.getBoundingClientRect();
  // переводим экранные координаты в мировые (с учётом pan + zoom)
  const wx = (e.clientX - rect.left - _panX) / _zoom;
  const wy = (e.clientY - rect.top  - _panY) / _zoom;
  for (const n of _astNodes) {
    if (wx >= n.x - NODE_W/2 && wx <= n.x + NODE_W/2 &&
        wy >= n.y - NODE_H/2 && wy <= n.y + NODE_H/2) {
      return n;
    }
  }
  return null;
}

function resizeCanvas() {
  if (!_astCanvas) return;
  const rect = _astCanvas.parentElement.getBoundingClientRect();
  _astCanvas.width  = rect.width  || 600;
  _astCanvas.height = rect.height || 400;
  _astCtx = _astCanvas.getContext("2d");
}

function highlightASTNode(line) {
  _activeASTLine = line;
  if (_astNodes.length) drawAST();
}

// ── VARS ──────────────────────────────────────────────────────────────────────

function updateVar({ name, value, type, offset }) {
  const tbody = document.getElementById("vars-body");
  let row = document.getElementById(`var-row-${name}`);
  if (!row) {
    row = document.createElement("tr");
    row.id = `var-row-${name}`;
    tbody.appendChild(row);
  }
  row.className = "updated";
  row.innerHTML = `
    <td>${escHtml(name)}</td>
    <td>${escHtml(type || "—")}</td>
    <td>${value !== null && value !== undefined ? value : "—"}</td>
    <td>${offset !== 0 ? offset : "reg"}</td>
  `;
  setTimeout(() => row.classList.remove("updated"), 500);
}

// ── CALLSTACK ─────────────────────────────────────────────────────────────────

function updateCallstack(stack) {
  document.getElementById("callstack-view").textContent =
    stack.length ? "▶ " + stack.join(" → ") : "(empty)";
}

// ── HEX VIEW ─────────────────────────────────────────────────────────────────

let _offsetMap    = {};   // "byte_index" → line
let _lineToBytes  = {};   // line → [byte_indices]
let _activeLine   = -1;

function renderHex({ bytes, cursor, offset_map }) {
  _offsetMap   = offset_map || {};
  _lineToBytes = {};

  // строим обратную карту line → байты
  for (const [idx, line] of Object.entries(_offsetMap)) {
    if (!_lineToBytes[line]) _lineToBytes[line] = [];
    _lineToBytes[line].push(parseInt(idx));
  }

  const arr = bytes.split(" ");
  let html = "";
  arr.forEach((b, i) => {
    const line = _offsetMap[String(i)];
    const cls = (i === cursor && cursor >= 0) ? "hex-byte active" : "hex-byte";
    html += `<span class="${cls}" id="hb-${i}" data-line="${line || 0}"
                   title="offset 0x${i.toString(16).toUpperCase()}">${b}</span>`;
    if ((i + 1) % 16 === 0) html += "\n";
    else html += " ";
  });

  const el = document.getElementById("hex-view");
  el.innerHTML = html;

  // клик по байту → подсветка строки + AST узла
  el.querySelectorAll(".hex-byte").forEach(span => {
    span.addEventListener("click", () => {
      const line = parseInt(span.dataset.line);
      if (line) {
        activateSourceLine(line);
        highlightASTNode(line);
        highlightHexLine(line);
      }
    });
    // hover
    span.addEventListener("mouseenter", () => {
      const line = parseInt(span.dataset.line);
      if (line) highlightHexLine(line, true);
    });
    span.addEventListener("mouseleave", () => {
      restoreHexHighlight();
    });
  });

  document.getElementById(`hb-${cursor}`)
    ?.scrollIntoView({ block: "nearest" });
}

function highlightHexLine(line, hover = false) {
  // сбрасываем ОБА класса перед новой подсветкой
  document.querySelectorAll(".hex-line-active, .hex-line-hover")
    .forEach(el => {
      el.classList.remove("hex-line-active");
      el.classList.remove("hex-line-hover");
    });

  const indices = _lineToBytes[line] || [];
  indices.forEach(i => {
    const el = document.getElementById(`hb-${i}`);
    if (el) el.classList.add(hover ? "hex-line-hover" : "hex-line-active");
  });
}

function restoreHexHighlight() {
  document.querySelectorAll(".hex-line-hover")
    .forEach(el => el.classList.remove("hex-line-hover"));
}

// ── ERROR ─────────────────────────────────────────────────────────────────────

function showError({ msg, line }) {
  setStatus("error", "error");
  if (line) highlightStep({ line });
  document.getElementById("hex-view").innerHTML =
    `<span class="err-msg">✖ ${escHtml(msg)}</span>`;
}

// ── helpers ───────────────────────────────────────────────────────────────────

function nodeLabel(node) {
  const type = node._type || "?";
  // берём самое информативное поле для лейбла
  if (node.name  != null) return `${type}: ${node.name}`;
  if (node.op    != null) return `${type}: ${node.op}`;
  if (node.value != null && typeof node.value !== "object")
    return `${type}: ${node.value}`;
  return type;
}

function collectChildRaws(node) {
  const kids = [];
  // убираем "value" и "name" из исключений — они нужны как дети
  const SKIP_KEYS = new Set(["_type", "_line", "_col"]);
  for (const [k, v] of Object.entries(node)) {
    if (SKIP_KEYS.has(k)) continue;
    if (typeof v !== "object" || v === null) continue;
    if (Array.isArray(v)) {
      v.forEach(c => { if (c && c._type) kids.push(c); });
    } else if (v._type) {
      kids.push(v);
    }
  }
  return kids;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
