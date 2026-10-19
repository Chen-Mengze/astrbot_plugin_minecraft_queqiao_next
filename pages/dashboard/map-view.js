/* Minecraft 鹊桥互通 - 方块地图面板（「功能设置」下方的「🗺️ 方块地图」）
 * 通过 AstrBot Plugin Page Bridge 调用后端 Web API（真实数据由服务端 QueQiao-Map-Web mod
 * 经鹊桥事件推送；无真实数据时后端返回 source:"none" + 安装引导 reason，不再有占位演示数据）：
 *   GET  map/<server>/status → {server_name, enabled, source:"queqiao-map-web"|"none",
 *                               reason, dimensions:[...], updated_at}
 *        （enabled=false ⇒ 该服务器 map_enabled=false：隐藏整个面板并停止轮询）
 *   GET  map/<server>/snapshot?dimension=<overworld|nether|the_end>
 *        → {server_name, dimension, updated_at, source,
 *           cell:{size,unit}, bbox:[minx,minz,maxx,maxz],
 *           tiles:<base64 W*H*3 RGB 行优先, W=maxx-minx, H=maxz-minz>,
 *           players:[{name,x,y,z,dimension,explored_radius}]}
 *        RGB 全 0 = 未探索（画暗色遮罩）；服务器不存在 404；dimension 非法 400
 *        无数据/禁用 → {server_name, dimension, updated_at, source:"none", reason,
 *        cell:{size:1,unit:"block"}, bbox:null, tiles:"", players:[]}：清屏 + 安装引导空态
 *   GET  map/<server>/players → {server_name, updated_at, reason,
 *           players:[{name,x,y,z,dimension,explored_radius}]}
 *        （跨维度新鲜玩家清单：「定位玩家」下拉数据源；与 snapshot.players 同一新鲜度
 *         标准；不依赖瓦片流——无调色板/瓦片也能定位；禁用时 players=[] + reason）
 *   POST map/<server>/teleport {player:"A", target:"B"} | {player:"A", x, z, y?}
 *        → {success, command, output, channel}；success=false 按失败在状态行提示
 * 面板显隐由 app.js activateServerTab 派发的 CustomEvent('queqiao:server-view-changed',
 * {detail:{serverName|null}}) 驱动：serverName 为 null（全局/图床/配置视图）时隐藏
 * 面板并停止轮询；否则显示、按 map_dimension 恢复维度并每 7s 轮询 snapshot。
 * map_dimension 持久化走后端 panel_prefs（AGENTS §4.7：沙箱 iframe 禁本地存储）。
 * 右键为自绘 context-menu 浮层（受限 iframe 里 window.alert/confirm 被拦截）：
 *   命中玩家标记 → 「将 A 传送到 → 其他玩家」；命中空白 → 「传送到 (x, z) → 在线玩家」。
 * 对外暴露 window.__mcqqMapTeleportMenu(player, anchorEl, serverName) 供 app.js
 * 服务器卡片玩家头像右键接线，两个入口共用同一菜单与请求实现。
 */
const PLUGIN_PATH = 'astrbot_plugin_minecraft_queqiao_next';

// 默认三个原版维度及其中文名；运行时以 status.dimensions / dimension_names 为准
// （已登记 mod 维度会追加进来，见 applyDimensions）
const DIMENSIONS = ['overworld', 'nether', 'the_end'];
const DIM_LABELS = { overworld: '主世界', nether: '下界', the_end: '末地' };
const MAP_SOURCE_REAL = 'queqiao-map-web';  // status/snapshot 的 source：真实推送（其余按无数据空态处理）
const POLL_MS = 7000;          // snapshot 轮询间隔（契约要求 5~10s）
const MIN_PX_PER_CELL = 0.25;  // 缩放下限：每方块像素
const MAX_PX_PER_CELL = 16;    // 缩放上限：每方块像素
const MOVE_ANIM_MS = 700;      // 玩家位置线性插值时长（体现实时移动）
const HIT_RADIUS_PX = 16;      // 玩家标记命中半径（屏幕像素）

// ?mapdebug=1 开启地图诊断日志（排查闪烁/轮询异常；默认关闭，零开销）
const MAP_DEBUG = new URLSearchParams(window.location.search).get('mapdebug') === '1';
const mapDbg = (...args) => { if (MAP_DEBUG) console.log('[map-debug]', ...args); };

const el = (id) => document.getElementById(id);
const enc = encodeURIComponent;

// Bridge 可能在模块加载后稍晚注入，每次调用现读（与 app.js 行为一致）
function getBridge() {
  return window.AstrBotPluginPage || null;
}

async function apiGet(endpoint, params = {}) {
  const bridge = getBridge();
  if (bridge && typeof bridge.apiGet === 'function') {
    return await bridge.apiGet(endpoint, params);
  }
  const query = new URLSearchParams(params).toString();
  const url = `/api/v1/plugins/extensions/${PLUGIN_PATH}/${endpoint}${query ? '?' + query : ''}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP error ${res.status}`);
  return await res.json();
}

async function apiPost(endpoint, body = {}) {
  const bridge = getBridge();
  if (bridge && typeof bridge.apiPost === 'function') {
    return await bridge.apiPost(endpoint, body);
  }
  const url = `/api/v1/plugins/extensions/${PLUGIN_PATH}/${endpoint}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP error ${res.status}`);
  return await res.json();
}

/* ---------- 状态 ---------- */
const state = {
  serverName: null,      // 当前视图服务器；null = 面板隐藏
  dimension: 'overworld',
  dims: DIMENSIONS.slice(),      // 可选维度（status.dimensions：内置 + 已登记 mod 维度）
  dimLabels: { ...DIM_LABELS },  // 短名 → 展示名（status.dimension_names）
  dimSig: '',                    // 维度清单签名：相同则零 DOM 重建（避免轮询期闪烁）
  prefDim: null,                 // 后端恢复的维度偏好（等它进入可选清单后再落地）
  dimTouched: false,     // 已从后端恢复或用户手动切过维度（恢复不再覆盖手动值）
  mapPlayers: [],        // map/<server>/players 最近清单（跨维度新鲜玩家定位数据）
  playerSig: '',         // 玩家清单签名（name+维度展示名）：相同则零 DOM 重建（防闪烁）
  locatedPlayer: '',     // 最近选中的定位玩家（下拉开关文案 + 选项选中态）
  centerOn: null,        // 待居中玩家名：进入其维度快照后居中一次即清空
  status: null,          // map/<server>/status 最近响应
  mapDisabled: false,    // 当前服务器 map_enabled=false：面板隐藏、轮询已停
  snapshot: null,        // 最近成功的 snapshot（原始对象）
  lastSnapshotKey: '',   // 快照指纹（revision 优先，否则 updated_at+bbox+tiles 长度）：相同跳过解码
  tileCanvas: null,      // 瓦片像素解码后的离屏 canvas
  tileError: '',
  viewReady: false,      // 视图已按当前 (server, dimension) 适配过
  pxPerCell: 4,          // 缩放：每方块像素
  centerX: 0,            // 画布中心对应的世界方块坐标
  centerZ: 0,
  players: new Map(),    // name → {name, fromX/fromZ→toX/toZ 线性插值, curX/curZ, animStart}
  avatars: new Map(),    // name → {img, failed}（crafatar 头像缓存）
  pollTimer: null,
  rafId: null,
  statusTimer: null,
};

/* ---------- 面板状态行 / 空态遮罩 ---------- */
function setMapStatus(text, kind = '') {
  const node = el('map-status');
  if (!node) return;
  const want = text || '';
  const curKind = node.classList.contains('ok') ? 'ok'
    : (node.classList.contains('err') ? 'err' : '');
  if (node.textContent === want && curKind === (kind || '')) return; // 同值不碰 DOM、不重置自动清除计时
  node.textContent = want;
  node.classList.remove('ok', 'err');
  if (kind) node.classList.add(kind);
  if (state.statusTimer) clearTimeout(state.statusTimer);
  if (want) {
    state.statusTimer = setTimeout(() => {
      node.textContent = '';
      node.classList.remove('ok', 'err');
    }, 10000);
  }
}

/* 数据源徽标跟随 status/snapshot.source：真实推送 / 无地图数据（禁用情形由整面板隐藏覆盖） */
function updateSourceBadge(resp) {
  const badge = el('map-source-badge');
  if (!badge) return;
  let text;
  let title;
  if (resp && resp.source === MAP_SOURCE_REAL) {
    text = '真实数据：QueQiao-Map-Web';
    title = '地图数据由服务端 QueQiao-Map-Web mod 经鹊桥实时推送';
  } else {
    text = '无地图数据';
    title = (resp && resp.reason)
      || '尚未收到该服务器的地图事件：服务端安装 QueQiao-Map-Web mod 后可查看真实地图';
  }
  if (badge.textContent === text && badge.title === title) return; // 同值不写：避免每轮轮询替换文本节点
  badge.textContent = text;
  badge.title = title;
}

function setMapEmpty(text) {
  const box = el('map-empty');
  if (!box) return;
  if (text) {
    if (box.textContent !== text) box.textContent = text;
    box.classList.remove('hidden');
  } else if (!box.classList.contains('hidden')) {
    box.textContent = '';
    box.classList.add('hidden');
  }
}

function formatUpdatedAt(value) {
  if (value === null || value === undefined || value === '') return '';
  let date;
  if (typeof value === 'number' && Number.isFinite(value)) {
    date = new Date(value < 1e12 ? value * 1000 : value);
  } else {
    date = new Date(value);
  }
  if (Number.isNaN(date.getTime())) return `更新 ${String(value)}`;
  const pad = (n) => String(n).padStart(2, '0');
  return `更新 ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/* 时钟文本幂等写入：秒值未变时不替换文本节点（减少轮询期的无谓 mutation） */
function setUpdatedText(value) {
  const node = el('map-updated');
  if (!node) return;
  const want = formatUpdatedAt(value);
  if (node.textContent !== want) node.textContent = want;
}

/* ---------- 视图与坐标 ---------- */
function canvasCssSize() {
  const canvas = el('map-canvas');
  if (!canvas) return { w: 0, h: 0 };
  const rect = canvas.getBoundingClientRect();
  return { w: rect.width, h: rect.height };
}

function fitView() {
  const bbox = state.snapshot && state.snapshot.bbox;
  if (!Array.isArray(bbox) || bbox.length !== 4) return;
  const [minx, minz, maxx, maxz] = bbox.map(Number);
  if (![minx, minz, maxx, maxz].every(Number.isFinite)) return;
  const { w, h } = canvasCssSize();
  const worldW = Math.max(1, maxx - minx);
  const worldH = Math.max(1, maxz - minz);
  const px = Math.min(w / worldW, h / worldH);
  state.pxPerCell = Math.max(MIN_PX_PER_CELL, Math.min(MAX_PX_PER_CELL, px));
  state.centerX = (minx + maxx) / 2;
  state.centerZ = (minz + maxz) / 2;
  state.viewReady = true;
}

function screenToWorld(clientX, clientY) {
  const canvas = el('map-canvas');
  const rect = canvas.getBoundingClientRect();
  const x = state.centerX + (clientX - rect.left - rect.width / 2) / state.pxPerCell;
  const z = state.centerZ + (clientY - rect.top - rect.height / 2) / state.pxPerCell;
  return { x, z };
}

function worldToScreenX(x, cw) { return (x - state.centerX) * state.pxPerCell + cw / 2; }
function worldToScreenZ(z, ch) { return (z - state.centerZ) * state.pxPerCell + ch / 2; }

function zoomAt(clientX, clientY, factor) {
  const before = screenToWorld(clientX, clientY);
  const next = Math.max(MIN_PX_PER_CELL, Math.min(MAX_PX_PER_CELL, state.pxPerCell * factor));
  if (next === state.pxPerCell) return;
  state.pxPerCell = next;
  // 缩放后把锚点世界坐标挪回指针下，实现「以指针为中心缩放」
  const rect = el('map-canvas').getBoundingClientRect();
  state.centerX = before.x - (clientX - rect.left - rect.width / 2) / state.pxPerCell;
  state.centerZ = before.z - (clientY - rect.top - rect.height / 2) / state.pxPerCell;
  requestDraw();
}

/* ---------- 瓦片解码（base64 → 独立离屏 canvas；RGB 全 0 = 透明，露出暗色底）
   绝不触碰可见画布：调用方在解码成功后一次性换入成品 canvas，旧帧全程保持（防刷新闪烁） ---------- */
function decodeTiles(base64, bbox) {
  if (!base64 || typeof base64 !== 'string') throw new Error('tiles 字段缺失');
  if (!Array.isArray(bbox) || bbox.length !== 4) throw new Error('bbox 字段缺失');
  const w = Math.round(bbox[2] - bbox[0]);
  const h = Math.round(bbox[3] - bbox[1]);
  if (w <= 0 || h <= 0) throw new Error('地图范围为空');
  const binary = atob(base64);
  if (binary.length < w * h * 3) throw new Error(`tiles 长度不足（${binary.length} < ${w * h * 3}）`);
  const data = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    const r = binary.charCodeAt(i * 3);
    const g = binary.charCodeAt(i * 3 + 1);
    const b = binary.charCodeAt(i * 3 + 2);
    const o = i * 4;
    if (r === 0 && g === 0 && b === 0) {
      data[o + 3] = 0; // 未探索：透明，露出暗色遮罩底
    } else {
      data[o] = r; data[o + 1] = g; data[o + 2] = b; data[o + 3] = 255;
    }
  }
  const off = document.createElement('canvas');
  off.width = w;
  off.height = h;
  off.getContext('2d').putImageData(new ImageData(data, w, h), 0, 0);
  return off;
}

/* ---------- 玩家标记：头像缓存 + 线性插值 ---------- */
function avatarFor(name) {
  let rec = state.avatars.get(name);
  if (rec) return rec;
  rec = { img: new Image(), failed: false };
  state.avatars.set(name, rec);
  rec.img.onload = () => requestDraw();
  rec.img.onerror = () => { rec.failed = true; requestDraw(); };
  rec.img.src = `https://crafatar.com/avatars/${enc(name)}?size=32&default=MHF_Steve&overlay`;
  return rec;
}

function mergePlayers(snapshot) {
  const now = performance.now();
  const seen = new Set();
  for (const raw of snapshot.players || []) {
    if (!raw || typeof raw.name !== 'string' || !raw.name) continue;
    const nx = Number(raw.x);
    const nz = Number(raw.z);
    if (!Number.isFinite(nx) || !Number.isFinite(nz)) continue;
    seen.add(raw.name);
    const prev = state.players.get(raw.name);
    if (prev) {
      prev.fromX = Number.isFinite(prev.curX) ? prev.curX : prev.toX;
      prev.fromZ = Number.isFinite(prev.curZ) ? prev.curZ : prev.toZ;
      prev.toX = nx;
      prev.toZ = nz;
      prev.animStart = now;
    } else {
      state.players.set(raw.name, {
        name: raw.name, fromX: nx, fromZ: nz, toX: nx, toZ: nz,
        curX: nx, curZ: nz, animStart: now,
      });
    }
  }
  for (const name of Array.from(state.players.keys())) {
    if (!seen.has(name)) state.players.delete(name);
  }
  // 玩家定位：下拉选中的玩家出现在本维度快照后，一次性居中到其最新坐标
  if (state.centerOn && seen.has(state.centerOn)) {
    const target = state.players.get(state.centerOn);
    if (target) {
      state.centerX = target.toX;
      state.centerZ = target.toZ;
    }
    state.centerOn = null;
  }
}

/* ---------- 绘制（单帧同步段：clearRect + 瓦片 + 玩家标记一次画完，中间无 await，无空帧闪烁） ---------- */
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v && v.trim()) || fallback;
}

function drawMap() {
  const canvas = el('map-canvas');
  if (!canvas) return false;
  const ctx = canvas.getContext('2d');
  const rect = canvas.getBoundingClientRect();
  const cw = rect.width;
  const ch = rect.height;
  ctx.clearRect(0, 0, cw, ch);
  ctx.fillStyle = cssVar('--map-bg', '#161d29');
  ctx.fillRect(0, 0, cw, ch);

  const snap = state.snapshot;
  const bbox = snap && Array.isArray(snap.bbox) ? snap.bbox.map(Number) : null;
  let animating = false;

  if (bbox && bbox.every(Number.isFinite) && state.tileCanvas) {
    const [minx, minz, maxx, maxz] = bbox;
    const dx = worldToScreenX(minx, cw);
    const dz = worldToScreenZ(minz, ch);
    const dw = (maxx - minx) * state.pxPerCell;
    const dh = (maxz - minz) * state.pxPerCell;
    // 未探索区域：bbox 范围先铺暗色遮罩，瓦片里的透明像素露出它
    ctx.fillStyle = cssVar('--map-unexplored', '#2a3446');
    ctx.fillRect(dx, dz, dw, dh);
    if (dw > 0.5 && dh > 0.5) {
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(state.tileCanvas, 0, 0, state.tileCanvas.width, state.tileCanvas.height,
        dx, dz, dw, dh);
    }
    ctx.imageSmoothingEnabled = true;
    ctx.strokeStyle = cssVar('--map-border', 'rgba(148,163,184,0.25)');
    ctx.lineWidth = 1;
    ctx.strokeRect(dx, dz, dw, dh);
  }

  const now = performance.now();
  for (const p of state.players.values()) {
    const t = Math.min(1, (now - p.animStart) / MOVE_ANIM_MS);
    p.curX = p.fromX + (p.toX - p.fromX) * t;
    p.curZ = p.fromZ + (p.toZ - p.fromZ) * t;
    if (t < 1) animating = true;
    const sx = worldToScreenX(p.curX, cw);
    const sz = worldToScreenZ(p.curZ, ch);
    if (sx < -40 || sx > cw + 40 || sz < -40 || sz > ch + 40) continue;
    const r = 11;
    // 头像圆片：crafatar 皮肤头，加载失败回落首字母圆形
    ctx.save();
    ctx.beginPath();
    ctx.arc(sx, sz, r, 0, Math.PI * 2);
    ctx.closePath();
    ctx.fillStyle = cssVar('--bg-tertiary', '#1f2937');
    ctx.fill();
    ctx.clip();
    const av = avatarFor(p.name);
    if (!av.failed && av.img.complete && av.img.naturalWidth > 0) {
      ctx.drawImage(av.img, sx - r, sz - r, r * 2, r * 2);
    } else {
      ctx.fillStyle = cssVar('--text-primary', '#e2e8f0');
      ctx.font = 'bold 11px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(p.name.slice(0, 2).toUpperCase(), sx, sz + 0.5);
    }
    ctx.restore();
    ctx.beginPath();
    ctx.arc(sx, sz, r + 1.5, 0, Math.PI * 2);
    ctx.strokeStyle = cssVar('--accent', '#38bdf8');
    ctx.lineWidth = 1.5;
    ctx.stroke();
    // 名字标签（深底描边保证两种主题可读）
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.55)';
    ctx.strokeText(p.name, sx, sz + r + 4);
    ctx.fillStyle = '#f8fafc';
    ctx.fillText(p.name, sx, sz + r + 4);
  }
  return animating;
}

function requestDraw() {
  if (state.rafId !== null) return;
  state.rafId = requestAnimationFrame(() => {
    state.rafId = null;
    const panel = el('map-panel');
    if (state.serverName && panel && !panel.classList.contains('hidden')) {
      const animating = drawMap();
      if (animating) requestDraw();
    }
  });
}

function resizeCanvas() {
  const canvas = el('map-canvas');
  const wrap = el('map-canvas-wrap');
  if (!canvas || !wrap) return;
  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  if (w <= 0 || h <= 0) return;
  const bw = Math.round(w * dpr);
  const bh = Math.round(h * dpr);
  // 仅在 CSS 尺寸真正变化时才写 width/height：赋值本身会清空画布缓冲
  const resized = canvas.width !== bw || canvas.height !== bh;
  if (resized) {
    mapDbg('canvas buffer recreated ' + canvas.width + 'x' + canvas.height + ' -> ' + bw + 'x' + bh);
    canvas.width = bw;
    canvas.height = bh;
  }
  canvas.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0); // 之后按 CSS 像素作图
  if (state.snapshot && !state.viewReady) fitView();
  if (resized) {
    // 关键防闪烁：缓冲刚被重建（写入即清空），若走 requestDraw 会等到下一帧才补画，
    // 中间浏览器可能绘出一帧空白画布（露出面板亮底色）——地图区「闪一下黑/亮」的正主。
    // 这里在同一同步任务里立即重画，空白帧永远不可能被合成上屏。
    const panel = el('map-panel');
    if (state.serverName && panel && !panel.classList.contains('hidden')) drawMap();
  } else {
    requestDraw();
  }
}

/* ---------- API 拉取 ---------- */
async function fetchStatus() {
  if (!state.serverName) return;
  const serverAtFetch = state.serverName;
  try {
    const resp = await apiGet(`map/${enc(serverAtFetch)}/status`);
    if (state.serverName !== serverAtFetch) return;
    state.status = resp;
    const panel = el('map-panel');
    if (resp && resp.enabled === false) {
      // 该服务器 map_enabled=false：整个面板隐藏、停止轮询（契约：不再收地图事件）
      state.mapDisabled = true;
      stopPolling();
      if (panel) panel.classList.add('hidden');
      return;
    }
    const wasDisabled = state.mapDisabled;
    state.mapDisabled = false;
    // 此前被禁用隐藏、现在功能恢复且仍在服务器视图：面板回显并恢复轮询
    if (wasDisabled && state.serverName) {
      if (panel) panel.classList.remove('hidden');
      if (!state.pollTimer) startPolling();
    }
    updateSourceBadge(resp);
    // 维度页签动态化：内置三维度 + 已登记 mod 维度（清单不变时零 DOM 操作）
    applyDimensions(resp && resp.dimensions, resp && resp.dimension_names);
  } catch (e) {
    if (state.serverName !== serverAtFetch) return;
    state.status = null;
    if (!state.tileCanvas) setMapEmpty(`地图状态获取失败：${e.message}`);
  }
}

/* 快照指纹：响应带 revision 优先用它，否则 updated_at + bbox + tiles 长度。
   指纹与上次相同 = 地图未变：跳过 decodeTiles，只更新玩家 overlay（消除轮询重解码闪烁） */
function snapshotKey(resp) {
  if (!resp) return '';
  if (resp.revision !== undefined && resp.revision !== null) return `r:${resp.revision}`;
  const stamp = (resp.updated_at === undefined || resp.updated_at === null) ? '' : String(resp.updated_at);
  const bbox = Array.isArray(resp.bbox) ? resp.bbox.join(',') : '';
  const tilesLen = typeof resp.tiles === 'string' ? String(resp.tiles.length) : '';
  if (!stamp && !bbox && !tilesLen) return '';
  return `${stamp}|${bbox}|${tilesLen}`;
}

async function fetchSnapshot() {
  if (!state.serverName) return;
  const serverAtFetch = state.serverName;
  const dimAtFetch = state.dimension;
  try {
    const resp = await apiGet(`map/${enc(serverAtFetch)}/snapshot`, { dimension: dimAtFetch });
    // 快速切维度/切服务器时丢弃过期响应
    if (state.serverName !== serverAtFetch || state.dimension !== dimAtFetch) return;
    if (resp && resp.dimension && resp.dimension !== dimAtFetch) return;
    if (resp && resp.source === 'none') {
      // 无真实数据（无地图事件源）：清屏 + 安装引导空态，不做任何演示渲染
      state.snapshot = resp;
      state.tileCanvas = null;
      state.tileError = '';
      state.lastSnapshotKey = '';
      state.viewReady = false;
      state.players.clear();
      updateSourceBadge(resp);
      setMapEmpty(resp.reason || '暂无地图数据');
      setUpdatedText(resp.updated_at);
      requestDraw();
      return;
    }
    state.snapshot = resp;
    if (!resp.tiles) {
      // 真实流也可能给出空瓦片（维度刚登记 / 瓦片还没到）：保留旧帧、不报错、
      // 不清屏，只在空态遮罩提示——避免状态行变红撑动布局（闪烁诱因）
      mergePlayers(resp);
      setUpdatedText(resp.updated_at);
      setMapStatus('');
      setMapEmpty(resp.reason || '该维度暂无瓦片数据');
      requestDraw();
      return;
    }
    const key = snapshotKey(resp);
    if (key && key === state.lastSnapshotKey && state.tileCanvas) {
      // 快照未变：沿用已解码瓦片（不重新 decode、不产生空帧），玩家照常合并插值
      mergePlayers(resp);
      setUpdatedText(resp.updated_at);
      setMapEmpty('');
      requestDraw();
      return;
    }
    // 防闪烁：decodeTiles 全程在独立离屏 canvas 内完成，成功后一次性替换 state.tileCanvas，
    // 解码期间可见画布保持上一帧，不做任何 clearRect/清屏
    let decoded = null;
    try {
      const t0 = performance.now();
      decoded = decodeTiles(resp.tiles, resp.bbox);
      mapDbg('tiles decoded key=' + key + ' ' + Math.round(performance.now() - t0) + 'ms');
    } catch (e) {
      state.tileError = e.message;
      mapDbg('tiles decode failed key=' + key + ': ' + e.message);
    }
    if (decoded) {
      state.tileCanvas = decoded;
      state.tileError = '';
      state.lastSnapshotKey = key;
    } else {
      // 解码失败：保留旧帧（不清空 tileCanvas），只在状态行提示
      setMapStatus(`瓦片解码失败：${state.tileError}`, 'err');
    }
    if (!state.viewReady) fitView();
    mergePlayers(resp);
    setUpdatedText(resp.updated_at);
    if (state.tileCanvas) setMapEmpty('');
    else setMapEmpty(`瓦片解码失败：${state.tileError}`);
    requestDraw();
  } catch (e) {
    if (state.serverName !== serverAtFetch) return;
    setMapStatus(`快照获取失败：${e.message}`, 'err');
    if (!state.tileCanvas) setMapEmpty(`地图数据获取失败：${e.message}`);
  }
}

/* 玩家定位清单：跨维度新鲜玩家（map/<server>/players），与当前快照维度无关。
   失败静默保留旧清单——定位是辅助功能，不打断地图主流程也不写红色状态行 */
async function fetchPlayers() {
  if (!state.serverName) return;
  const serverAtFetch = state.serverName;
  try {
    const resp = await apiGet(`map/${enc(serverAtFetch)}/players`);
    if (state.serverName !== serverAtFetch) return; // 切服竞态：旧响应作废
    state.mapPlayers = (resp && Array.isArray(resp.players)) ? resp.players : [];
    renderPlayerOptions();
  } catch (e) {
    mapDbg('players fetch failed: ' + e.message);
  }
}

function startPolling() {
  stopPolling();
  fetchStatus();
  fetchSnapshot();
  fetchPlayers();
  state.pollTimer = setInterval(() => {
    if (document.hidden) return; // 页签不可见不轮询
    // status 同步轮询：mod 维度新增/数据源切换无需刷新页面即可反映（幂等，
    // 清单未变时不碰 DOM）
    fetchStatus();
    fetchSnapshot();
    fetchPlayers();
  }, POLL_MS);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

/* ---------- 自绘右键菜单（沙箱 iframe 禁用 window.alert/confirm 与原生菜单） ---------- */
let activeMenu = null;

function closeMenu() {
  if (!activeMenu) return;
  activeMenu.remove();
  activeMenu = null;
  document.removeEventListener('mousedown', onDocMouseDown, true);
  document.removeEventListener('keydown', onDocKeyDown, true);
}

function onDocMouseDown(e) {
  if (activeMenu && !activeMenu.contains(e.target)) closeMenu();
}

function onDocKeyDown(e) {
  if (e.key === 'Escape') closeMenu();
}

// items: [{label, onClick?} | {label, disabled:true}]；x/y 为视口坐标（position: fixed）
function openMenu(title, items, x, y) {
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'mcqq-context-menu';
  const head = document.createElement('div');
  head.className = 'mcqq-context-title';
  head.textContent = title;
  menu.appendChild(head);
  for (const it of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mcqq-context-item';
    btn.textContent = it.label;
    if (it.disabled) {
      btn.disabled = true;
    } else {
      btn.addEventListener('click', () => {
        closeMenu();
        if (it.onClick) it.onClick();
      });
    }
    menu.appendChild(btn);
  }
  document.body.appendChild(menu);
  // 先上屏量尺寸，再钳制到视口内
  const rect = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
  const top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8));
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  activeMenu = menu;
}

/* ---------- 传送（两个右键入口共用同一实现） ---------- */
async function doTeleport(serverName, payload) {
  const desc = typeof payload.target === 'string'
    ? `${payload.player} → ${payload.target}`
    : `${payload.player} → (${Number(payload.x).toFixed(1)}, ${Number(payload.z).toFixed(1)})`;
  setMapStatus(`正在传送 ${desc}…`);
  try {
    const resp = await apiPost(`map/${enc(serverName)}/teleport`, payload);
    const cmd = resp && resp.command ? `；指令: ${resp.command}` : '';
    if (resp && resp.success === true) {
      const ch = resp.channel ? `（通道: ${resp.channel}）` : '';
      setMapStatus(`✓ 传送成功 ${desc}${cmd}${ch}`, 'ok');
      fetchSnapshot();
    } else {
      const why = (resp && (resp.output || resp.message)) || '后端返回 success=false';
      setMapStatus(`✕ 传送失败 ${desc}: ${why}${cmd}`, 'err');
    }
  } catch (e) {
    setMapStatus(`✕ 传送请求失败 ${desc}: ${e.message}`, 'err');
  }
}

// 菜单一：把 subject 传送到其他在线玩家（在线名单来自 snapshot）
function openPlayerTeleportMenu(serverName, subject, x, y) {
  const title = `将 ${subject} 传送到 →`;
  openMenu(title, [{ label: '正在读取在线玩家…', disabled: true }], x, y);
  apiGet(`map/${enc(serverName)}/snapshot`, { dimension: state.dimension })
    .then((snap) => {
      const others = ((snap && snap.players) || []).filter((p) => p && p.name && p.name !== subject);
      const items = others.length
        ? others.map((p) => ({
            label: p.name,
            onClick: () => doTeleport(serverName, { player: subject, target: p.name }),
          }))
        : [{ label: '没有其他在线玩家', disabled: true }];
      openMenu(title, items, x, y);
    })
    .catch((e) => {
      openMenu(title, [{ label: `读取玩家列表失败: ${e.message}`, disabled: true }], x, y);
    });
}

// 菜单二：把在线玩家传送到地图点 (x, z)（点击处方块坐标，1 位小数）
function openPointTeleportMenu(serverName, px, pz, x, y) {
  const xs = px.toFixed(1);
  const zs = pz.toFixed(1);
  const title = `传送到 (${xs}, ${zs}) →`;
  openMenu(title, [{ label: '正在读取在线玩家…', disabled: true }], x, y);
  apiGet(`map/${enc(serverName)}/snapshot`, { dimension: state.dimension })
    .then((snap) => {
      const players = ((snap && snap.players) || []).filter((p) => p && p.name);
      // 契约要求坐标 1 位小数：显示与 POST 一律用四舍五入后的值
      const items = players.length
        ? players.map((p) => ({
            label: p.name,
            onClick: () => doTeleport(serverName, { player: p.name, x: Number(xs), z: Number(zs) }),
          }))
        : [{ label: '当前没有在线玩家', disabled: true }];
      openMenu(title, items, x, y);
    })
    .catch((e) => {
      openMenu(title, [{ label: `读取玩家列表失败: ${e.message}`, disabled: true }], x, y);
    });
}

/* ---------- 维度切换（map_dimension 走后端 panel_prefs，AGENTS §4.7） ---------- */
/* 按 state.dims / state.dimLabels 重建下拉选项（只在清单变化时调用，
   避免每轮轮询重建 DOM 导致下拉闪动） */
function renderDimOptions() {
  const list = el('map-dim-list');
  if (!list) return;
  const frag = document.createDocumentFragment();
  state.dims.forEach((dim) => {
    const opt = document.createElement('button');
    opt.type = 'button';
    opt.className = 'map-dim-option';
    opt.dataset.dim = dim;
    opt.setAttribute('role', 'option');
    opt.textContent = state.dimLabels[dim] || dim;
    opt.addEventListener('click', () => {
      applyDimension(dim);
      setDimsOpen(false);
    });
    frag.appendChild(opt);
  });
  list.replaceChildren(frag);
}

/* 维度清单动态化：后端 status.dimensions = 内置三维度 + 已登记 mod 维度，
   dimension_names 给短名到展示名。清单/名称都未变时直接返回（零 DOM），
   当前维度被移除（mod 卸载/换服）时回落主世界并重新拉快照。 */
function applyDimensions(dims, names) {
  if (!Array.isArray(dims)) return;
  const next = [];
  dims.forEach((d) => {
    if (typeof d === 'string' && d && next.indexOf(d) < 0) next.push(d);
  });
  if (!next.length) return;
  const nextLabels = {};
  next.forEach((d) => {
    const given = names && typeof names[d] === 'string' ? names[d] : '';
    nextLabels[d] = given || DIM_LABELS[d] || d;
  });
  const sig = next.join('|') + '::' + next.map((d) => nextLabels[d]).join('|');
  if (sig === state.dimSig) return; // 未变化：不碰 DOM、不重排
  const dimsChanged = next.length !== state.dims.length
    || next.some((d, i) => d !== state.dims[i]);
  state.dims = next;
  state.dimLabels = nextLabels;
  state.dimSig = sig;
  if (dimsChanged) renderDimOptions();
  if (state.dims.indexOf(state.dimension) < 0) {
    state.dimension = 'overworld';
    state.viewReady = false;
    state.snapshot = null;
    state.tileCanvas = null;
    state.lastSnapshotKey = '';
    state.players.clear();
    if (state.serverName) fetchSnapshot();
  }
  syncDimButtons();
  // 后端偏好里的 mod 维度可能晚于首轮 status 进入清单：进入后立即落地
  if (!state.dimTouched && state.prefDim && state.dims.indexOf(state.prefDim) >= 0) {
    const pref = state.prefDim;
    state.prefDim = null;
    applyDimension(pref, { persist: false });
  }
}

function applyDimension(dim, { persist = true } = {}) {
  if (state.dims.indexOf(dim) < 0) return;
  state.dimTouched = true;
  if (dim === state.dimension) {
    syncDimButtons();
    return;
  }
  state.dimension = dim;
  syncDimButtons();
  // 三个维度是三个世界：切维度清空旧快照并重新适配视图（指纹一并作废）
  state.viewReady = false;
  state.snapshot = null;
  state.tileCanvas = null;
  state.lastSnapshotKey = '';
  state.players.clear();
  setMapEmpty('正在加载地图…');
  requestDraw();
  fetchSnapshot();
  if (persist) {
    apiPost('panel/prefs', { map_dimension: dim }).catch((e) => {
      console.warn('保存地图维度偏好失败:', e);
    });
  }
}

/* 同步可展开下拉：开关按钮显示当前维度文案，选项维护 selected 态（listbox 语义）。
   函数名沿用 syncDimButtons，applyDimension / init 各调用点保持不变 */
function syncDimButtons() {
  const label = el('map-dim-current-label');
  if (label) label.textContent = state.dimLabels[state.dimension] || state.dimension;
  document.querySelectorAll('#map-dim-list .map-dim-option').forEach((opt) => {
    const active = opt.dataset.dim === state.dimension;
    opt.classList.toggle('active', active);
    opt.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

/* 维度下拉开合：hidden class + aria-expanded 同步 */
function setDimsOpen(open) {
  const list = el('map-dim-list');
  const toggle = el('map-dims-toggle');
  if (!list || !toggle) return;
  list.classList.toggle('hidden', !open);
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

/* ---------- 玩家定位下拉（与维度下拉同构：开关按钮 + 绝对定位浮层，
   浮层不占布局位，对画布尺寸零影响） ---------- */
function renderPlayerOptions() {
  const list = el('map-player-list');
  if (!list) return;
  const rows = state.mapPlayers.map((p) => ({
    name: p.name,
    dimLabel: state.dimLabels[p.dimension] || p.dimension,
  }));
  const sig = rows.map((r) => r.name + ':' + r.dimLabel).join('|');
  if (sig === state.playerSig) return; // 清单未变：不碰 DOM
  state.playerSig = sig;
  const frag = document.createDocumentFragment();
  if (!rows.length) {
    const opt = document.createElement('button');
    opt.type = 'button';
    opt.className = 'map-player-option';
    opt.disabled = true;
    opt.textContent = '当前没有在线玩家位置数据';
    frag.appendChild(opt);
  } else {
    rows.forEach((r) => {
      const opt = document.createElement('button');
      opt.type = 'button';
      opt.className = 'map-player-option';
      opt.setAttribute('role', 'option');
      opt.dataset.player = r.name;
      opt.textContent = r.name + '（' + r.dimLabel + '）';
      opt.addEventListener('click', () => {
        locatePlayer(r.name);
        setPlayersOpen(false);
      });
      frag.appendChild(opt);
    });
  }
  list.replaceChildren(frag);
  syncPlayerToggle();
}

/* 定位：跨维度时切到该玩家维度（applyDimension 重取快照），快照落地后由
   mergePlayers 按 centerOn 居中一次——不先在旧维度画布上跳中心，避免跳动；
   同维度直接用已知最新坐标居中。一次性动作，不写 panel_prefs（非持久偏好）。 */
function locatePlayer(name) {
  if (!name || !state.serverName) return;
  const entry = state.mapPlayers.find((p) => p.name === name) || null;
  state.locatedPlayer = name;
  syncPlayerToggle();
  const dim = entry && typeof entry.dimension === 'string' ? entry.dimension : '';
  if (dim && dim !== state.dimension && state.dims.indexOf(dim) >= 0) {
    state.centerOn = name;
    applyDimension(dim);
    return;
  }
  const known = state.players.get(name);
  const x = known && Number.isFinite(known.curX) ? known.curX
    : (entry ? Number(entry.x) : NaN);
  const z = known && Number.isFinite(known.curZ) ? known.curZ
    : (entry ? Number(entry.z) : NaN);
  if (Number.isFinite(x) && Number.isFinite(z)) {
    state.centerX = x;
    state.centerZ = z;
    state.centerOn = null;
    requestDraw();
  } else {
    // 暂无坐标：等下一次该玩家进入快照时由 mergePlayers 居中
    state.centerOn = name;
  }
}

/* 开关文案与选项选中态幂等写入（玩家名定宽 + 省略号，防撑宽工具栏抖动画布） */
function syncPlayerToggle() {
  const label = el('map-player-current-label');
  if (label) {
    const want = state.locatedPlayer || '定位玩家';
    if (label.textContent !== want) label.textContent = want;
  }
  document.querySelectorAll('#map-player-list .map-player-option').forEach((opt) => {
    const active = opt.dataset.player === state.locatedPlayer;
    opt.classList.toggle('active', active);
    opt.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

function setPlayersOpen(open) {
  const list = el('map-player-list');
  const toggle = el('map-players-toggle');
  if (!list || !toggle) return;
  list.classList.toggle('hidden', !open);
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

/* ---------- 视图联动（app.js activateServerTab 派发 queqiao:server-view-changed） ---------- */
function setServer(serverName) {
  closeMenu();
  const panel = el('map-panel');
  if (!panel) return;
  if (!serverName) {
    // 全局 / 图床 / 配置视图：隐藏面板、停止轮询（数据保留，切回即恢复）
    state.serverName = null;
    stopPolling();
    panel.classList.add('hidden');
    return;
  }
  const changed = serverName !== state.serverName;
  mapDbg('setServer ' + serverName + ' changed=' + changed + ' polling=' + !!state.pollTimer + ' disabled=' + state.mapDisabled);
  // 同一服务器视图重复激活且轮询在跑（自动刷新每轮都派发本事件）：
  // 不重启定时器、不立即补拉——消除轮询风暴与随之而来的重复重绘
  if (!changed && state.pollTimer && !state.mapDisabled) {
    panel.classList.remove('hidden');
    return;
  }
  state.serverName = serverName;
  panel.classList.remove('hidden');
  if (changed) {
    state.status = null;
    state.snapshot = null;
    state.tileCanvas = null;
    state.lastSnapshotKey = '';
    // 服务器间 map_enabled 各自独立：切服先回显面板，由新的 status 响应决定是否再隐藏
    state.mapDisabled = false;
    state.players.clear();
    state.mapPlayers = [];
    state.playerSig = '';
    state.locatedPlayer = '';
    state.centerOn = null;
    renderPlayerOptions();
    state.viewReady = false;
    setMapEmpty('正在加载地图…');
    setMapStatus('');
    requestDraw();
  }
  startPolling();
}

/* ---------- 事件绑定 ---------- */
function bindCanvas() {
  const canvas = el('map-canvas');
  const wrap = el('map-canvas-wrap');
  if (!canvas || !wrap) return;

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!state.snapshot) return;
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.2 : 1 / 1.2);
  }, { passive: false });

  let panning = null;
  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    panning = { x: e.clientX, z: e.clientY };
    state.centerOn = null; // 用户手动接管视图：放弃尚未生效的定位居中
    canvas.setPointerCapture(e.pointerId);
    wrap.classList.add('panning');
  });
  canvas.addEventListener('pointermove', (e) => {
    if (panning) {
      state.centerX -= (e.clientX - panning.x) / state.pxPerCell;
      state.centerZ -= (e.clientY - panning.z) / state.pxPerCell;
      panning.x = e.clientX;
      panning.z = e.clientY;
      requestDraw();
    }
    const info = el('map-cursor-info');
    if (info) {
      if (state.snapshot) {
        const w = screenToWorld(e.clientX, e.clientY);
        info.textContent = `(${w.x.toFixed(1)}, ${w.z.toFixed(1)})`;
      } else {
        info.textContent = '';
      }
    }
  });
  const endPan = (e) => {
    if (!panning) return;
    panning = null;
    wrap.classList.remove('panning');
    if (e.pointerId !== undefined && canvas.hasPointerCapture &&
        canvas.hasPointerCapture(e.pointerId)) {
      canvas.releasePointerCapture(e.pointerId);
    }
  };
  canvas.addEventListener('pointerup', endPan);
  canvas.addEventListener('pointercancel', endPan);
  canvas.addEventListener('pointerleave', () => {
    const info = el('map-cursor-info');
    if (info) info.textContent = '';
  });

  // 右键：contextmenu + preventDefault，自绘浮层菜单（点击外部/Esc 关闭）
  canvas.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (!state.serverName || !state.snapshot) return;
    const rect = canvas.getBoundingClientRect();
    // worldToScreenX/Z 返回画布局部坐标（0..宽/高），指针也换算到同一坐标系
    const lx = e.clientX - rect.left;
    const ly = e.clientY - rect.top;
    let hit = null;
    let best = HIT_RADIUS_PX;
    for (const p of state.players.values()) {
      const sx = worldToScreenX(p.curX, rect.width);
      const sz = worldToScreenZ(p.curZ, rect.height);
      const d = Math.hypot(sx - lx, sz - ly);
      if (d <= best) { best = d; hit = p; }
    }
    if (hit) {
      openPlayerTeleportMenu(state.serverName, hit.name, e.clientX, e.clientY);
    } else {
      const w = screenToWorld(e.clientX, e.clientY);
      openPointTeleportMenu(state.serverName, w.x, w.z, e.clientX, e.clientY);
    }
  });

  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(() => resizeCanvas()).observe(wrap);
  }
}

function bindToolbar() {
  // 维度可展开下拉：开关切换列表开合；点选项应用维度（applyDimension 内含持久化）并收起
  el('map-dims-toggle')?.addEventListener('click', () => {
    const list = el('map-dim-list');
    setDimsOpen(!!list && list.classList.contains('hidden'));
  });
  document.querySelectorAll('#map-dim-list .map-dim-option').forEach((opt) => {
    opt.addEventListener('click', () => {
      applyDimension(opt.dataset.dim);
      setDimsOpen(false);
    });
  });
  // 玩家定位下拉：开合切换；打开时立即补拉一次最新位置（不等下一轮轮询）
  el('map-players-toggle')?.addEventListener('click', () => {
    const list = el('map-player-list');
    const open = !!list && list.classList.contains('hidden');
    setPlayersOpen(open);
    if (open && state.serverName) fetchPlayers();
  });
  // 外点关闭（mousedown 早于选项 click，容器内点击不受影响）与 Esc 关闭
  document.addEventListener('mousedown', (e) => {
    const wrap = el('map-dims');
    if (wrap && !wrap.contains(e.target)) setDimsOpen(false);
    const pWrap = el('map-players');
    if (pWrap && !pWrap.contains(e.target)) setPlayersOpen(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      setDimsOpen(false);
      setPlayersOpen(false);
    }
  });
  const canvas = el('map-canvas');
  el('map-zoom-in')?.addEventListener('click', () => {
    const rect = canvas.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1.25);
  });
  el('map-zoom-out')?.addEventListener('click', () => {
    const rect = canvas.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1 / 1.25);
  });
  el('map-reset')?.addEventListener('click', () => {
    if (state.snapshot) { fitView(); requestDraw(); }
  });
  el('map-refresh')?.addEventListener('click', () => {
    if (state.serverName) { fetchStatus(); fetchSnapshot(); fetchPlayers(); }
  });
  window.addEventListener('resize', resizeCanvas);
  // 捕获阶段监听所有滚动：菜单自身是 overflow-y:auto 容器，其内部滚动是正常浏览行为，
  // 不能当成「页面移动」误关菜单——这是玩家列表过长时一滚滚轮就关闭的根因修复
  window.addEventListener('scroll', (e) => {
    if (activeMenu && e.target instanceof Node && activeMenu.contains(e.target)) return;
    closeMenu();
  }, true);
}

/* ---------- 启动（script type=module 在 </body> 前：DOM 已就绪） ---------- */
function init() {
  if (!el('map-panel')) return; // 页面没有地图面板区块时静默退出
  syncDimButtons();
  bindCanvas();
  bindToolbar();
  resizeCanvas();

  window.addEventListener('queqiao:server-view-changed', (e) => {
    setServer(e && e.detail && e.detail.serverName ? e.detail.serverName : null);
  });

  // app.js 服务器卡片玩家头像右键接线的 window 钩子
  window.__mcqqMapTeleportMenu = (player, anchorEl, serverName) => {
    const server = serverName || state.serverName;
    if (!server || !anchorEl) return;
    const rect = anchorEl.getBoundingClientRect();
    openPlayerTeleportMenu(server, player, rect.left, rect.bottom + 4);
  };

  // 启动恢复维度：权威值在后端 panel_prefs（沙箱 iframe 无本地存储可用）
  apiGet('panel/prefs')
    .then((resp) => {
      const dim = resp && resp.prefs && resp.prefs.map_dimension;
      if (!state.dimTouched && typeof dim === 'string' && dim) {
        // mod 维度此时可能还没登记：先记下，等 status 把它纳入清单后落地
        if (state.dims.indexOf(dim) >= 0) applyDimension(dim, { persist: false });
        else state.prefDim = dim;
      }
    })
    .catch((e) => {
      console.warn('拉取地图维度偏好失败:', e);
    });
}

init();
