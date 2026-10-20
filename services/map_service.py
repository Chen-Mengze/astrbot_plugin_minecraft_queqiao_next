"""方块地图数据服务（Web 面板游戏顶层方块地图）：真实数据链路（无演示数据）。

真实数据来自服务端 mod「QueQiao-Map-Web」，经鹊桥事件桥以 5 类 notice 事件
推送；main 层按 sub_type 拦截后把事件对象交给 MapService.handle_event
（载荷经 event.payload 原始字典透传）：

- 「map_dimensions」：维度清单（registry key），连接后一次 + 清单变化重发；
- 「map_palette」：调色板（id + ≤256 个 #RRGGBB，colors[0] 固定 #000000 表示
  未设置）；本次连接内冻结，服务端重启/修订递增会带新 id —— 收到新 id ⇒
  替换调色板并清空该服务器全部已存瓦片（旧瓦片按旧调色板量化，混用错色）；
- 「map_tile」：「zlib-pal8」瓦片：b64 → zlib 解压 → side*side 字节
  （side=size*16，行优先 index=pz*side+px，左上原点，px 向东 x+、pz 向南 z+），
  每字节为调色板下标，下标 0 = 未设置；cx/cz 为瓦片左上角区块坐标（可负），
  size 为每边区块数（勿硬编码白名单，按解码长度校验兜底）；sha1 为
  **zlib 压缩字节**（b64 之前）的摘要，作去重键；按 (dim, cx, cz, size)
  位置键覆盖写幂等（重连可能重放）；
- 「map_region」：「zlib-pal8」区域（side 固定 512，32×32 区块 = 512×512
  像素，rx/rz 为 MC 区域坐标可负）：初次连接全量补发已加载的二维区域 +
  断点续传——服务端按 (dim,rx,rz)+xor 内容指纹（Xaero 式，未压缩库内容）
  决定重发，消费端只留档 xor（记日志/排障），去重仍按自算 sha1（zlib
  压缩字节摘要）；区域到达 = 整块替换该 512×512；与 map_tile 叠加幂等：
  合成先画区域做底层、瓦片局部覆盖，与事件到达顺序无关；
- 「player_move」：玩家位置批量 upsert（按 uuid 键，uuid 缺失退 nickname）；
  无离线事件，超过 MAP_PLAYER_STALE_SECONDS 未更新即在快照 players 中缺席。

前向兼容铁律：未知 sub_type / 未知字段一律忽略不报错；解析异常绝不向事件
循环抛出（handle_event 内部 try/except + logger.warning，带
「[{PLUGIN_NAME}][{server_name}]」前缀）。

**无数据语义**：未收到过真实流（调色板 + 至少存过 1 片瓦片或区域）的服务器
不生成任何演示数据——status/snapshot 返回 source=none，reason 说明原因
（未收到事件 MAP_NO_DATA_REASON / 开关关闭 MAP_DISABLED_REASON），快照
bbox=null、tiles 空串、players 空；有真实流但该维度零瓦片且零区域时给
「未探索」全黑小窗（以该维度最新鲜玩家为中心 ±112）。

**磁盘持久化**（解决「palette 每连接只发一次、AstrBot 重启后收不到重发」）：
<data_dir>/map/<safe_server>/meta.json（调色板 id、colors 原 hex 串、上报
维度、各维度更新时间）+ 每维度 <dim>.json（瓦片存 zlib 压缩字节 b64）
+ 每维度 <dim>.regions.json（区域压缩字节 b64，键 "rx,rz"，值
{"x":xor 留档,"s":自算 sha1,"b":b64}）；
写入为 tmp 文件 + os.replace 原子替换（threading.Lock 保护），脏标记 +
节流（距上次落盘 ≥5s 且再有事件才同步落盘），flush() 立即落盘
（main terminate 接线）。

仅用标准库（base64 / hashlib / json / math / os / re / struct / threading /
time / zlib），无新第三方依赖。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
import time
import zlib
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..core.constants import (
    MAP_DEFAULT_DIMENSION,
    MAP_DIMENSION_NAMES,
    MAP_DIMENSIONS,
    MAP_DIM_SHORT_RE,
    MAP_DISABLED_REASON,
    MAP_DYNAMIC_DIMENSIONS_MAX,
    MAP_NO_DATA_REASON,
    MAP_PLAYER_STALE_SECONDS,
    MAP_REGISTRY_DIM_SHORT,
    MAP_REGISTRY_KEY_RE,
    MAP_SOURCE_NONE,
    MAP_SOURCE_REAL,
    MAP_SUB_DIMENSIONS,
    MAP_SUB_PALETTE,
    MAP_SUB_PLAYER_MOVE,
    MAP_SUB_REGION,
    MAP_SUB_TILE,
    MAP_TILE_FORMAT,
    PLUGIN_NAME,
)

# 本模块消费的 5 类 sub_type（禁止 import MAP_EVENT_SUB_TYPES——main 层专用）
_HANDLED_SUB_TYPES = (
    MAP_SUB_DIMENSIONS,
    MAP_SUB_PALETTE,
    MAP_SUB_TILE,
    MAP_SUB_REGION,
    MAP_SUB_PLAYER_MOVE,
)

# ---- 真实链路阈值（map_service 专用模块级常量，依据与取舍见各条注释）----
# 每 (server, dim) 瓦片上限：依据上游实测 8×8 瓦片（128×128 pal8）zlib 后
# 0.5–2 KB、摊销约 20 B/区块；20000 片 = 128 万区块、压缩态约 10–40 MB，
# 已覆盖超大地图；超出按插入序淘汰最旧（一次性告警）
MAP_TILES_MAX_PER_DIM = 20000
# 每 (server, dim) 区域上限：单区域压缩后典型 15–60 KB（512×512 pal8 原始
# 256 KB → zlib，INTEGRATION.md §3.5 预算）；2048 块 = 2048×1024 区块 ≈
# 2.1M 区块（≈536M 方块列）覆盖，与瓦片上限 MAP_TILES_MAX_PER_DIM=20000
# （全按 8×8 片计 1.28M 区块）覆盖面积同一量级，压缩态约 30–120 MB；
# 超出按插入序淘汰最旧（一次性告警）
MAP_REGIONS_MAX_PER_DIM = 2048
# map_region 区域位图边长：32×32 区块 = 512×512 像素（1 像素 = 1 方块列）；
# 解码与 zlib-pal8 瓦片完全同构，仅 side 固定为此值
MAP_REGION_SIDE = 512
# 快照合成窗口边长上限：576 = 4.5 个 8×8 瓦片（128 方块/片）；RGB 上限
# 576*576*3 ≈ 1 MB/快照，base64 后约 1.3 MB，面板轮询带宽可接受
MAP_SNAPSHOT_BBOX_MAX = 576
# 协议硬上限：瓦片字节为 1 字节调色板下标 ⇒ 调色板至多 256 项
MAP_PALETTE_MAX_COLORS = 256
# 落盘节流：距上次落盘 ≥5s 且再次有事件才同步落盘——瓦片推送高频且单文件
# 全量重写，逐事件写盘会拖慢事件循环；退出前由 flush() 兜底
MAP_FLUSH_MIN_INTERVAL_SECONDS = 5.0

# 调色板单项：#RRGGBB（大小写均可）
_HEX6_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# (0,0,0) 是「未探索/未覆盖」的占位色：调色板里合法的纯黑项抬 1 个蓝分量，
# 使真实纯黑区域不会被前端渲染成未探索雾（肉眼几乎无差别）
_BLACK_FIX = (0, 0, 1)
# server_name 转安全文件名：与 services/monitor.py::MonitorStore 同一写法
# （刻意复制而非 import monitor——避免把 monitor 的重依赖链拉进地图模块）
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.\-]+")


def safe_server_name(server_name: str) -> str:
    """把 server_name 转为安全的目录/文件名（保留可读性，避免路径注入）。"""
    cleaned = _SAFE_NAME_RE.sub("_", server_name or "").strip("._")
    return cleaned or "server"


def _as_int(value: Any) -> int | None:
    """防御式取整：排除 bool（True 不是 1 号坐标），float 仅取整数值。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    """防御式取 float；bool / NaN / Inf 视为非法。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    return None


# ---- 真实流「未探索小窗」参数 ----
MAP_BBOX_HALF = 112  # bbox 以 (0,0) 为中心 ±112 方块（W=H=224）；亦用于真实流「未探索小窗」


class _ServerMapState:
    """单台服务器的真实地图状态（内存态；瓦片始终存 zlib 压缩字节）。"""

    def __init__(self) -> None:
        # 调色板：id + RGB 列表；另保留原 hex 串用于落盘（避免往返转换误差）
        self.palette_id: str = ""
        self.palette_colors: list[tuple[int, int, int]] = []
        self.palette_hex: list[str] = []
        # 瓦片：(dim, cx, cz, size) -> (自算 sha1, zlib 压缩字节)；
        # dict 保持插入序，供超限时按插入序淘汰最旧
        self.tiles: dict[tuple[str, int, int, int], tuple[str, bytes]] = {}
        self.dim_tile_count: dict[str, int] = {}
        # 区域：(dim, rx, rz) -> (xor 留档, 自算 sha1, zlib 压缩字节)；dict
        # 保持插入序，供超限时按插入序淘汰最旧。xor 是服务端地图库内容校验
        # （Xaero 式，续传重发判定在服务端做），消费端只留档排障不参与去重
        self.regions: dict[tuple[str, int, int], tuple[int, str, bytes]] = {}
        self.dim_region_count: dict[str, int] = {}
        # 每维度 revision：瓦片实际变化才 bump，供快照合成缓存失效
        self.revision: dict[str, int] = {}
        # 每维度最近一次瓦片变化时间（落盘 meta，排障用）
        self.dim_updated_at: dict[str, float] = {}
        # map_dimensions 上报的 registry key 集合（清单重发即整体替换）
        self.reported_dimensions: set[str] = set()
        # 模组维度动态短名：registry key -> 内部短名（插入序 = 页签顺序），
        # 由 map_dimensions / map_tile / player_move 里的未知 registry key 登记
        self.dyn_dims: dict[str, str] = {}
        # 玩家：key=uuid（缺失退 nickname）→ {name,x,y,z,dimension,updated_at}
        self.players: dict[str, dict[str, Any]] = {}
        self.last_event_at: float = 0.0
        # 曾存过至少 1 片瓦片或区域的历史标记（status/snapshot 真实流判定门槛之一；
        # 变量名沿用 ever_tile，语义已扩为瓦片∪区域）
        self.ever_tile: bool = False
        # 落盘状态
        self.dirty: bool = False
        self.last_flush_at: float = 0.0
        # 去重告警：未映射维度 / 瓦片超限淘汰 / 区域超限淘汰各只告警一次
        # （瓦片与区域淘汰告警互相独立，不共用集合）
        self.warned_dims: set[str] = set()
        self.warned_evict: set[str] = set()
        self.warned_evict_region: set[str] = set()
        # 快照合成缓存：dim -> (bbox, 该维度 revision, tiles_b64)，每维度单条
        self.snapshot_cache: dict[
            str, tuple[tuple[int, int, int, int], int, str]
        ] = {}


# 全黑「未探索」tiles 惰性单例（224*224*3 零字节 base64）
_BLANK_TILES_B64: str | None = None


def _blank_tiles_b64() -> str:
    global _BLANK_TILES_B64
    if _BLANK_TILES_B64 is None:
        side = MAP_BBOX_HALF * 2
        _BLANK_TILES_B64 = base64.b64encode(bytes(side * side * 3)).decode("ascii")
    return _BLANK_TILES_B64


class MapService:
    """每服务器一份方块地图状态：真实流优先；关闭或无真实流时快照为 source=none 空态。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        # data_dir=None 为纯内存（tests_offline 旧用法）；提供时启动加载已
        # 持久化状态（损坏文件告警跳过）
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._states: dict[str, _ServerMapState] = {}
        # 功能开关（map_enabled=false 的 server_name 集合）：默认启用，
        # set_enabled 由 main 装载/软更新配置时同步
        self._disabled: set[str] = set()
        # 落盘锁：flush() 来自 terminate（可能与事件循环不同线程），
        # 事件循环内的节流落盘也可能并发，统一用一把锁串行化
        self._lock = threading.Lock()
        if self._data_dir is not None:
            self._load_all()

    # ---- 功能开关（每台服务器 map_enabled，main 层装载/软更新时同步） ----

    def set_enabled(self, server_name: str, enabled: bool) -> None:
        """同步 map_enabled：True 移出禁用集合，False 加入（默认视为启用）。"""
        if enabled:
            self._disabled.discard(server_name)
        else:
            self._disabled.add(server_name)

    # ---- 事件接收（main 层按 sub_type 拦截后透传事件对象） ----

    def handle_event(self, server_name: str, event: Any) -> bool:
        """消费 5 类地图 notice 事件；未知 sub_type 返回 False，否则恒 True。

        载荷取 getattr(event, "payload", None) or {}（原始字典，含 sub_type）；
        任何解析异常都在内部吞掉并告警，绝不向事件循环抛出。
        """
        sub_type: Any = None
        try:
            payload = getattr(event, "payload", None) or {}
            if isinstance(payload, dict):
                sub_type = payload.get("sub_type")
            if not sub_type:
                # 兼容退化：payload 非 dict 或缺 sub_type 时用模型字段兜底
                sub_type = getattr(event, "sub_type", "")
            if sub_type == MAP_SUB_DIMENSIONS:
                self._apply_dimensions(server_name, payload)
            elif sub_type == MAP_SUB_PALETTE:
                self._apply_palette(server_name, payload)
            elif sub_type == MAP_SUB_TILE:
                self._apply_tile(server_name, payload)
            elif sub_type == MAP_SUB_REGION:
                self._apply_region(server_name, payload)
            elif sub_type == MAP_SUB_PLAYER_MOVE:
                self._apply_player_move(server_name, payload)
            else:
                return False
        except Exception as exc:
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] 地图事件处理异常"
                f"（sub_type={sub_type!r}，已忽略不抛出）: {exc!r}"
            )
        return True

    def _lookup(self, server_name: str) -> _ServerMapState | None:
        """按原名查状态；命中磁盘加载的安全名键则升级为原名键。

        持久化目录名是 safe_server_name(server_name)，重启加载后原名可能不同
        （含被替换字符）；status/snapshot/事件任一路径首次触达即完成升级，
        保证重启后无需等事件也能读到磁盘真实数据。
        """
        st = self._states.get(server_name)
        if st is not None:
            return st
        safe = safe_server_name(server_name)
        if safe != server_name:
            st = self._states.pop(safe, None)
            if st is not None:
                self._states[server_name] = st
        return st

    def _state(self, server_name: str) -> _ServerMapState:
        """取（或建）某服务器状态。"""
        st = self._lookup(server_name)
        if st is None:
            st = _ServerMapState()
            self._states[server_name] = st
        return st

    def _touch(self, st: _ServerMapState, dirty: bool = True) -> float:
        """记录事件时间并按需标脏；返回事件时间。"""
        now = time.time()
        st.last_event_at = now
        if dirty:
            st.dirty = True
        return now

    # ---- 维度映射（内置别名 + 模组维度动态登记） ----

    @staticmethod
    def _known_short(st: _ServerMapState, registry_key: Any) -> str | None:
        """registry key → 已登记短名（内置别名优先）；不登记、不告警。"""
        if not isinstance(registry_key, str):
            return None
        known = MAP_REGISTRY_DIM_SHORT.get(registry_key)
        if known:
            return known
        return st.dyn_dims.get(registry_key)

    def _dim_short(
        self, server_name: str, st: _ServerMapState, registry_key: Any
    ) -> str | None:
        """registry key → 面板短名；未知命名空间按需动态登记。

        内置三维度走 MAP_REGISTRY_DIM_SHORT；其余形如 namespace:path 的
        registry key 以 path 清洗成短名登记（与内置/已登记短名冲突时拼命名
        空间或补序号），每台上限 MAP_DYNAMIC_DIMENSIONS_MAX（防页签被撑爆）。
        非法形态或超上限返回 None，调用方按「无法映射」忽略并告警一次。
        """
        known = self._known_short(st, registry_key)
        if known:
            return known
        if not isinstance(registry_key, str) or not MAP_REGISTRY_KEY_RE.match(
            registry_key
        ):
            return None
        namespace, _, path = registry_key.partition(":")
        base = re.sub(r"[^a-z0-9_-]+", "_", path.lower()).strip("_")[:32]
        if not base:
            return None
        if len(st.dyn_dims) >= MAP_DYNAMIC_DIMENSIONS_MAX:
            if registry_key not in st.warned_dims:
                st.warned_dims.add(registry_key)
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] 模组维度数已达上限"
                    f" {MAP_DYNAMIC_DIMENSIONS_MAX}，{registry_key!r} 及其数据被忽略"
                )
            return None
        taken = set(MAP_DIMENSIONS) | set(st.dyn_dims.values())
        short = base
        if short in taken:
            short = re.sub(
                r"[^a-z0-9_-]+", "_", f"{namespace}_{base}".lower()
            ).strip("_")[:40]
        serial = 2
        while short in taken:
            short = f"{base[:40]}_{serial}"
            serial += 1
        st.dyn_dims[registry_key] = short
        logger.info(
            f"[{PLUGIN_NAME}][{server_name}] 登记模组维度 {registry_key!r}"
            f" → 面板短名 {short!r}"
        )
        return short

    def _warn_unmapped_dim(
        self, server_name: str, st: _ServerMapState, dim_raw: Any
    ) -> None:
        """无法映射的维度：每维度只告警一次（不建立瓦片，仅刷新时间戳）。"""
        shown = dim_raw if isinstance(dim_raw, str) else repr(dim_raw)
        if shown not in st.warned_dims:
            st.warned_dims.add(shown)
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] 无法映射的维度 {shown!r}"
                " 的瓦片已忽略（每维度只告警一次）"
            )
        self._touch(st, dirty=False)

    # ---- 五类事件的解析落地 ----

    def _apply_dimensions(self, server_name: str, payload: dict[str, Any]) -> None:
        """map_dimensions：整体替换上报维度清单，模组维度按需登记短名。"""
        dims = payload.get("dimensions")
        if not isinstance(dims, list):
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] map_dimensions 载荷缺"
                " dimensions 或非列表，忽略"
            )
            return
        st = self._state(server_name)
        reported: set[str] = set()
        for d in dims:
            if not isinstance(d, str) or not d:
                continue
            reported.add(d)
            if self._known_short(st, d) is not None:
                continue
            if self._dim_short(server_name, st, d) is None:
                self._warn_unmapped_dim(server_name, st, d)
        st.reported_dimensions = reported
        self._touch(st)
        self._maybe_flush(server_name, st)

    def _apply_palette(self, server_name: str, payload: dict[str, Any]) -> None:
        """map_palette：解析并（在 id 变化时）替换调色板。

        - 非法色项 ⇒ 整盘拒绝并告警（宁缺勿错色）；
        - 超过 256 项 ⇒ 截断保留前 256 并告警；
        - id 变化**或颜色内容变化**（部分实现 id 为空/固定）⇒ 替换调色板并清空
          该服务器全部已存瓦片与区域（两者同按旧调色板量化，混用会错色），
          并对瓦片∪区域涉及的各维度 bump revision 使快照缓存失效。
        """
        pid = payload.get("id")
        pid = pid if isinstance(pid, str) else ""
        colors = payload.get("colors")
        if not isinstance(colors, list) or not colors:
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] map_palette 载荷缺 colors"
                " 或为空，忽略"
            )
            return
        if len(colors) > MAP_PALETTE_MAX_COLORS:
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] 调色板 {len(colors)} 项超过"
                f" {MAP_PALETTE_MAX_COLORS}，截断保留前 {MAP_PALETTE_MAX_COLORS} 项"
            )
            colors = colors[:MAP_PALETTE_MAX_COLORS]
        parsed: list[tuple[int, int, int]] = []
        for i, c in enumerate(colors):
            if not isinstance(c, str) or not _HEX6_RE.match(c):
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] 调色板第 {i} 项非法"
                    f"（{c!r}），整盘拒绝"
                )
                return
            parsed.append((int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)))
        st = self._state(server_name)
        # 颜色内容变化同样必须换盘：部分实现 palette id 为空或不修订，
        # 只认 id 会让新旧量化混用而错色。
        colors_changed = parsed != st.palette_colors
        if pid != st.palette_id or colors_changed:
            stale_tiles = len(st.tiles)
            stale_regions = len(st.regions)
            if stale_tiles or stale_regions:
                # 旧瓦片/区域同按旧调色板量化，必须整服清空
                for dim in {k[0] for k in st.tiles} | {k[0] for k in st.regions}:
                    st.revision[dim] = st.revision.get(dim, 0) + 1
                st.tiles.clear()
                st.dim_tile_count.clear()
                st.regions.clear()
                st.dim_region_count.clear()
            st.snapshot_cache.clear()
            logger.info(
                f"[{PLUGIN_NAME}][{server_name}] 收到调色板"
                f"（{st.palette_id or '无'} → {pid or '?'}），清空旧瓦片"
                f" {stale_tiles} 片 / 旧区域 {stale_regions} 块"
                + ("（颜色内容变化）" if colors_changed else "")
            )
        st.palette_id = pid
        st.palette_colors = parsed
        st.palette_hex = [str(c) for c in colors]
        self._touch(st)
        self._maybe_flush(server_name, st)

    def _apply_tile(self, server_name: str, payload: dict[str, Any]) -> None:
        """map_tile：解码校验 zlib-pal8 瓦片，sha1 去重后按位置键覆盖写。

        - 维度为内置短名或模组维度（未知 registry key 首次出现即登记短名）；
          非法形态 ⇒ 忽略整批，每 (server, dimension) 只告警一次；
        - 调色板未到 ⇒ 丢弃整批并告警（上游保证 palette 先于 tile；AstrBot
          侧重启场景由磁盘持久化兜底）；
        - format 非 MAP_TILE_FORMAT ⇒ 跳过该瓦片并告警（新编码会用新 format 串）；
        - sha1 是 zlib 压缩字节（b64 之前）的摘要：自算作去重键，与声称不符
          ⇒ 告警但按自算值继续；同位置同摘要 ⇒ 跳过（不 bump revision）；
        - 解码长度必须等于 side*side（side=size*16），否则丢弃并告警；
        - 按位置键覆盖写幂等（重连可能重放同一批瓦片）。
        """
        dim_raw = payload.get("dimension")
        st = self._state(server_name)
        dim = self._dim_short(server_name, st, dim_raw)
        if dim is None:
            self._warn_unmapped_dim(server_name, st, dim_raw)
            return
        tiles = payload.get("tiles")
        if not isinstance(tiles, list):
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] map_tile 载荷缺 tiles"
                " 或非列表，忽略"
            )
            return
        if not st.palette_colors:
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] 调色板尚未收到，丢弃本次"
                f" {len(tiles)} 片 {dim} 瓦片（上游保证 palette 先于 tile）"
            )
            self._touch(st, dirty=False)
            return
        changed = False
        for item in tiles:
            if not isinstance(item, dict):
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片条目非 dict，跳过"
                )
                continue
            fmt = item.get("format")
            if fmt != MAP_TILE_FORMAT:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片编码 format="
                    f"{fmt!r} 不受支持（期望 {MAP_TILE_FORMAT!r}），跳过"
                )
                continue
            cx = _as_int(item.get("cx"))
            cz = _as_int(item.get("cz"))
            size = _as_int(item.get("size"))
            if cx is None or cz is None or size is None or size <= 0:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片坐标/尺寸非法"
                    f"（cx={item.get('cx')!r}, cz={item.get('cz')!r},"
                    f" size={item.get('size')!r}），跳过"
                )
                continue
            b64 = item.get("b64")
            if not isinstance(b64, str) or not b64:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片"
                    f" ({cx},{cz},{size}) 缺 b64，跳过"
                )
                continue
            try:
                # validate=True：非字母表字符直接报错而非静默丢字符；
                # binascii.Error 是 ValueError 子类
                comp = base64.b64decode(b64, validate=True)
            except ValueError:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片"
                    f" ({cx},{cz},{size}) b64 解码失败，跳过"
                )
                continue
            digest = hashlib.sha1(comp).hexdigest()
            claimed = item.get("sha1")
            if not isinstance(claimed, str):
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片"
                    f" ({cx},{cz},{size}) 缺 sha1，按自算摘要去重"
                )
            elif claimed.lower() != digest:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片"
                    f" ({cx},{cz},{size}) 声称 sha1 {claimed!r} 与自算 {digest}"
                    " 不符，按自算值继续"
                )
            side = size * 16
            try:
                raw = zlib.decompress(comp)
            except zlib.error as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片"
                    f" ({cx},{cz},{size}) zlib 解压失败，跳过: {exc}"
                )
                continue
            if len(raw) != side * side:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片"
                    f" ({cx},{cz},{size}) 解码长度 {len(raw)} ≠ {side * side}，丢弃"
                )
                continue
            pos = (dim, cx, cz, size)
            prev = st.tiles.get(pos)
            if prev is not None and prev[0] == digest:
                continue  # 同位置同摘要：跳过，不 bump revision
            st.tiles[pos] = (digest, comp)
            if prev is None:
                st.dim_tile_count[dim] = st.dim_tile_count.get(dim, 0) + 1
            changed = True
            # 每 (server, dim) 超限：按插入序淘汰最旧（一次性告警）
            while st.dim_tile_count.get(dim, 0) > MAP_TILES_MAX_PER_DIM:
                victim = next(
                    (k for k in st.tiles if k[0] == dim and k != pos), pos
                )
                del st.tiles[victim]
                st.dim_tile_count[dim] -= 1
                if dim not in st.warned_evict:
                    st.warned_evict.add(dim)
                    logger.warning(
                        f"[{PLUGIN_NAME}][{server_name}] {dim} 瓦片数超过上限"
                        f" {MAP_TILES_MAX_PER_DIM}，按插入序淘汰最旧瓦片"
                        "（后续淘汰不再告警）"
                    )
        if changed:
            now = self._touch(st)
            st.ever_tile = True
            st.revision[dim] = st.revision.get(dim, 0) + 1
            st.dim_updated_at[dim] = now
        else:
            self._touch(st)
        self._maybe_flush(server_name, st)

    def _apply_region(self, server_name: str, payload: dict[str, Any]) -> None:
        """map_region：解码 zlib-pal8 区域（side 固定 MAP_REGION_SIDE），按位置键整块覆盖。

        - 解码与 map_tile 完全一致，仅 side 固定 512（32×32 区块 = 512×512
          像素）；rx/rz 为 MC 区域坐标（可负，无 size 字段）；
        - 区域到达 = 整块替换该 512×512；合成时区域为底层、瓦片局部覆盖
          （叠加幂等，与到达顺序无关）；
        - xor 是服务端地图库内容指纹（Xaero 式）：断点续传的重发判定在服务端
          （xor 不变不重发），消费端只留档（记日志/排障），去重用自算 sha1
          （zlib 压缩字节摘要）；缺失按 0 存；
        - 其余校验链与 _apply_tile 完全对齐：调色板未到 ⇒ 整批丢弃；条目非
          dict 跳过；未知 format 跳过告警；b64 validate=True；解压后长度必须
          等于 MAP_REGION_SIDE² 否则丢弃；同位置同自算 sha1 跳过不 bump
          revision（xor 变了只更新留档 tuple）；按位置键覆盖写幂等。
        """
        dim_raw = payload.get("dimension")
        st = self._state(server_name)
        dim = self._dim_short(server_name, st, dim_raw)
        if dim is None:
            self._warn_unmapped_dim(server_name, st, dim_raw)
            return
        regions = payload.get("regions")
        if not isinstance(regions, list):
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] map_region 载荷缺 regions"
                " 或非列表，忽略"
            )
            return
        if not st.palette_colors:
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] 调色板尚未收到，丢弃本次"
                f" {len(regions)} 块 {dim} 区域（上游保证 palette 先于 region/tile）"
            )
            self._touch(st, dirty=False)
            return
        changed = False
        for item in regions:
            if not isinstance(item, dict):
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域条目非 dict，跳过"
                )
                continue
            fmt = item.get("format")
            if fmt != MAP_TILE_FORMAT:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域编码 format="
                    f"{fmt!r} 不受支持（期望 {MAP_TILE_FORMAT!r}），跳过"
                )
                continue
            rx = _as_int(item.get("rx"))
            rz = _as_int(item.get("rz"))
            if rx is None or rz is None:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域坐标非法"
                    f"（rx={item.get('rx')!r}, rz={item.get('rz')!r}），跳过"
                )
                continue
            b64 = item.get("b64")
            if not isinstance(b64, str) or not b64:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域"
                    f" ({rx},{rz}) 缺 b64，跳过"
                )
                continue
            try:
                # validate=True：非字母表字符直接报错而非静默丢字符
                comp = base64.b64decode(b64, validate=True)
            except ValueError:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域"
                    f" ({rx},{rz}) b64 解码失败，跳过"
                )
                continue
            digest = hashlib.sha1(comp).hexdigest()
            claimed = item.get("sha1")
            if not isinstance(claimed, str):
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域"
                    f" ({rx},{rz}) 缺 sha1，按自算摘要去重"
                )
            elif claimed.lower() != digest:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域"
                    f" ({rx},{rz}) 声称 sha1 {claimed!r} 与自算 {digest}"
                    " 不符，按自算值继续"
                )
            try:
                raw = zlib.decompress(comp)
            except zlib.error as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域"
                    f" ({rx},{rz}) zlib 解压失败，跳过: {exc}"
                )
                continue
            if len(raw) != MAP_REGION_SIDE * MAP_REGION_SIDE:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dim} 区域"
                    f" ({rx},{rz}) 解码长度 {len(raw)} ≠"
                    f" {MAP_REGION_SIDE * MAP_REGION_SIDE}，丢弃"
                )
                continue
            xor_v = _as_int(item.get("xor"))
            xor = 0 if xor_v is None else xor_v
            pos = (dim, rx, rz)
            prev = st.regions.get(pos)
            if prev is not None and prev[1] == digest:
                # 同位置同压缩摘要：内容一致，不 bump revision；
                # 服务端指纹 xor 变了则只更新留档 tuple（续传排障用）
                if prev[0] != xor:
                    st.regions[pos] = (xor, digest, prev[2])
                continue
            st.regions[pos] = (xor, digest, comp)
            if prev is None:
                st.dim_region_count[dim] = st.dim_region_count.get(dim, 0) + 1
            changed = True
            # 每 (server, dim) 超限：按插入序淘汰最旧（一次性告警）
            while st.dim_region_count.get(dim, 0) > MAP_REGIONS_MAX_PER_DIM:
                victim = next(
                    (k for k in st.regions if k[0] == dim and k != pos), pos
                )
                del st.regions[victim]
                st.dim_region_count[dim] -= 1
                if dim not in st.warned_evict_region:
                    st.warned_evict_region.add(dim)
                    logger.warning(
                        f"[{PLUGIN_NAME}][{server_name}] {dim} 区域数超过上限"
                        f" {MAP_REGIONS_MAX_PER_DIM}，按插入序淘汰最旧区域"
                        "（后续淘汰不再告警）"
                    )
        if changed:
            now = self._touch(st)
            st.ever_tile = True
            st.revision[dim] = st.revision.get(dim, 0) + 1
            st.dim_updated_at[dim] = now
        else:
            self._touch(st)
        self._maybe_flush(server_name, st)

    def _apply_player_move(self, server_name: str, payload: dict[str, Any]) -> None:
        """player_move：按 uuid（缺失退 nickname）批量 upsert 玩家位置。

        name 取 nickname；dimension 为 registry key，映射存短名（无法映射
        原样存，永不匹配面板维度）。无离线事件：快照按
        MAP_PLAYER_STALE_SECONDS 判缺席；玩家为瞬时态，不落盘。
        """
        players = payload.get("players")
        if not isinstance(players, list):
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] player_move 载荷缺 players"
                " 或非列表，忽略"
            )
            return
        st = self._state(server_name)
        now = time.time()
        for item in players:
            if not isinstance(item, dict):
                continue
            uuid = item.get("uuid")
            uuid = uuid if isinstance(uuid, str) and uuid else ""
            nickname = item.get("nickname")
            nickname = nickname if isinstance(nickname, str) and nickname else ""
            if not uuid and not nickname:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] 玩家条目 uuid/nickname"
                    " 皆缺失，跳过"
                )
                continue
            x = _as_float(item.get("x"))
            y = _as_float(item.get("y"))
            z = _as_float(item.get("z"))
            if x is None or y is None or z is None:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] 玩家 {uuid or nickname!r}"
                    " 坐标非法，跳过"
                )
                continue
            dim_raw = item.get("dimension")
            # 内置维度映射短名；模组维度首次出现即登记；非法形态退原文
            dim = self._dim_short(server_name, st, dim_raw) or (
                dim_raw if isinstance(dim_raw, str) else ""
            )
            st.players[uuid or nickname] = {
                "name": nickname or uuid,
                "x": x,
                "y": y,
                "z": z,
                "dimension": dim,
                "updated_at": now,
            }
        self._touch(st, dirty=False)
        self._maybe_flush(server_name, st)

    # ---- WebApiController 消费的外向接口 ----

    def status(self, server_name: str) -> dict[str, Any]:
        """某台服务器的地图数据源状态（无任何演示数据）。

        map_enabled=false ⇒ source=none + MAP_DISABLED_REASON；
        收到过调色板且至少存过 1 片瓦片或区域 ⇒ 真实流（source=MAP_SOURCE_REAL）；
        其余（未收到真实事件）⇒ source=none + MAP_NO_DATA_REASON。

        dimensions 恒为「内置三维度 + 已登记模组维度」（前端据此生成页签），
        dimension_names 给短名到展示名的映射。
        """
        st = self._lookup(server_name)
        dims = list(MAP_DIMENSIONS) if st is None else self._mapped_dimensions(st)
        names = self._dimension_names(st)
        if server_name in self._disabled:
            return {
                "server_name": server_name,
                "enabled": False,
                "source": MAP_SOURCE_NONE,
                "reason": MAP_DISABLED_REASON,
                "dimensions": dims,
                "dimension_names": names,
                "updated_at": time.time(),
            }
        if st is not None and st.palette_colors and st.ever_tile:
            return {
                "server_name": server_name,
                "enabled": True,
                "source": MAP_SOURCE_REAL,
                "reason": "",
                "dimensions": dims,
                "dimension_names": names,
                "updated_at": st.last_event_at,
            }
        return {
            "server_name": server_name,
            "enabled": True,
            "source": MAP_SOURCE_NONE,
            "reason": MAP_NO_DATA_REASON,
            "dimensions": dims,
            "dimension_names": names,
            "updated_at": time.time(),
        }

    def snapshot(
        self,
        server_name: str,
        dimension: str = MAP_DEFAULT_DIMENSION,
        at: float | None = None,
    ) -> dict[str, Any] | None:
        """生成某维度方块地图快照；非法维度返回 None（控制层据此回 400）。

        功能关闭或无真实数据 ⇒ source=none 空态（无任何演示数据）；否则
        真实流按瓦片合成。「at」为显式参考时间（秒，测试/复现用），缺省取
        当前时间；真实流下它同时作为玩家新鲜度判定基准。
        """
        st = self._lookup(server_name)
        known = dimension in MAP_DIMENSIONS or (
            st is not None and dimension in st.dyn_dims.values()
        )
        if not known:
            return None
        now = time.time() if at is None else float(at)
        if server_name in self._disabled:
            return self._none_snapshot(
                server_name, dimension, now, MAP_DISABLED_REASON
            )
        if st is not None and st.palette_colors and st.ever_tile:
            return self._real_snapshot(server_name, st, dimension, now)
        return self._none_snapshot(server_name, dimension, now, MAP_NO_DATA_REASON)

    def _none_snapshot(
        self, server_name: str, dimension: str, now: float, reason: str
    ) -> dict[str, Any]:
        """source=none 空态快照：无瓦片、无玩家，reason 说明原因。"""
        return {
            "server_name": server_name,
            "dimension": dimension,
            "updated_at": now,
            "source": MAP_SOURCE_NONE,
            "reason": reason,
            # revision 恒 0：无数据态指纹稳定，前端可跳过重解码
            "revision": 0,
            "cell": {"size": 1, "unit": "block"},
            "bbox": None,
            "tiles": "",
            "players": [],
        }

    def players(self, server_name: str, at: float | None = None) -> dict[str, Any]:
        """跨维度新鲜玩家位置清单（前端「定位玩家」下拉的唯一数据源）。

        snapshot.players 只含被请求维度的玩家——本方法跨全部维度列清单，
        玩家在其他维度也能被选中并跳转定位。新鲜度与快照同一标准（超过
        MAP_PLAYER_STALE_SECONDS 即缺席）；且不依赖瓦片流：无调色板 / 无
        瓦片时只要有 player_move 就能定位。功能关闭 ⇒ 空清单 +
        MAP_DISABLED_REASON。按 name 排序：前端以签名比对避免 DOM 重建，
        清单顺序必须稳定。「at」为显式参考时间（秒，测试/复现用），
        与 snapshot 同款语义。
        """
        st = self._lookup(server_name)
        now = time.time() if at is None else float(at)
        if server_name in self._disabled:
            return {
                "server_name": server_name,
                "updated_at": now,
                "reason": MAP_DISABLED_REASON,
                "players": [],
            }
        fresh = [
            p
            for p in (st.players.values() if st is not None else ())
            if now - p["updated_at"] <= MAP_PLAYER_STALE_SECONDS
        ]
        fresh.sort(key=lambda p: p["name"])
        return {
            "server_name": server_name,
            "updated_at": st.last_event_at if st is not None else now,
            "reason": "",
            "players": self._snapshot_players(fresh),
        }

    def flush(self) -> None:
        """立即落盘全部脏状态（main terminate 接线调用）。"""
        if self._data_dir is None:
            return
        with self._lock:
            for name, st in list(self._states.items()):
                if st.dirty:
                    self._write_server(name, st)

    # ---- 真实快照合成 ----

    @staticmethod
    def _dimension_names(st: _ServerMapState | None) -> dict[str, str]:
        """面板维度短名 → 展示名：内置中文名 + 模组维度用其 registry key。"""
        names = dict(MAP_DIMENSION_NAMES)
        if st is not None:
            for key, short in st.dyn_dims.items():
                names.setdefault(short, key)
        return names

    def known_dimensions(self, server_name: str) -> list[str]:
        """该服务器当前合法的面板维度短名（内置 + 已登记模组），供控制层提示。"""
        st = self._lookup(server_name)
        if st is None:
            return list(MAP_DIMENSIONS)
        return self._mapped_dimensions(st)

    def _mapped_dimensions(self, st: _ServerMapState) -> list[str]:
        """上报维度映射为短名：内置按 MAP_DIMENSIONS 顺序，模组按登记顺序。

        未上报清单时给「内置三维度 + 已登记模组维度」，保证有数据的模组
        维度也不会从页签里消失（清单事件可能晚于瓦片事件到达）。
        """
        if not st.reported_dimensions:
            return list(MAP_DIMENSIONS) + [
                s for s in st.dyn_dims.values() if s not in MAP_DIMENSIONS
            ]
        vanilla = [
            d
            for d in MAP_DIMENSIONS
            if any(
                MAP_REGISTRY_DIM_SHORT.get(k) == d for k in st.reported_dimensions
            )
        ]
        dynamic = [
            short for key, short in st.dyn_dims.items() if key in st.reported_dimensions
        ]
        return vanilla + dynamic

    def _fresh_player_entries(
        self, st: _ServerMapState, dimension: str, now: float
    ) -> list[dict[str, Any]]:
        """该维度的新鲜玩家原始条目（缺席 = 超 MAP_PLAYER_STALE_SECONDS）。"""
        return [
            p
            for p in st.players.values()
            if p["dimension"] == dimension
            and now - p["updated_at"] <= MAP_PLAYER_STALE_SECONDS
        ]

    def _snapshot_players(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """新鲜玩家 → 快照 players 条目（explored_radius 为契约占位 0）。"""
        return [
            {
                "name": p["name"],
                "x": p["x"],
                "y": p["y"],
                "z": p["z"],
                "dimension": p["dimension"],
                # 真实链路无「已探索」语义（上游按区块推全量瓦片）；
                # 前端只用 name/x/z，explored_radius 保留契约占位
                "explored_radius": 0,
            }
            for p in entries
        ]

    def _dim_tiles(
        self, st: _ServerMapState, dimension: str
    ) -> list[tuple[int, int, int, tuple[str, bytes]]]:
        """该维度全部瓦片 (cx, cz, size, (sha1, 压缩字节))，插入序。"""
        return [
            (cx, cz, size, val)
            for (d, cx, cz, size), val in st.tiles.items()
            if d == dimension
        ]

    def _dim_regions(
        self, st: _ServerMapState, dimension: str
    ) -> list[tuple[int, int, tuple[int, str, bytes]]]:
        """该维度全部区域 (rx, rz, (xor, sha1, 压缩字节))，插入序。"""
        return [
            (rx, rz, val)
            for (d, rx, rz), val in st.regions.items()
            if d == dimension
        ]

    def _real_snapshot(
        self,
        server_name: str,
        st: _ServerMapState,
        dimension: str,
        now: float,
    ) -> dict[str, Any]:
        """真实流快照：有瓦片/区域按并集 bbox 合成；双零维度给全黑未探索小窗。"""
        fresh = self._fresh_player_entries(st, dimension, now)
        tiles = self._dim_tiles(st, dimension)
        regions = self._dim_regions(st, dimension)
        if tiles or regions:
            bbox, tiles_b64 = self._compose_tiles(
                server_name, st, dimension, tiles, fresh, regions
            )
        else:
            bbox, tiles_b64 = self._blank_window(fresh)
        return {
            "server_name": server_name,
            "dimension": dimension,
            "updated_at": st.last_event_at,
            "source": MAP_SOURCE_REAL,
            # 该维度瓦片修订号：前端据此跳过无变化的重解码
            "revision": st.revision.get(dimension, 0),
            "cell": {"size": 1, "unit": "block"},
            "bbox": list(bbox),
            "tiles": tiles_b64,
            "players": self._snapshot_players(fresh),
        }

    def _blank_window(
        self, fresh: list[dict[str, Any]]
    ) -> tuple[tuple[int, int, int, int], str]:
        """未探索小窗：以该维度最新鲜玩家（无则 (0,0)）为中心 ±MAP_BBOX_HALF。"""
        if fresh:
            freshest = max(fresh, key=lambda p: p["updated_at"])
            cx0 = int(round(freshest["x"]))
            cz0 = int(round(freshest["z"]))
        else:
            cx0 = cz0 = 0
        half = MAP_BBOX_HALF
        return (cx0 - half, cz0 - half, cx0 + half, cz0 + half), _blank_tiles_b64()

    @staticmethod
    def _window_lo(center: float, lo_bound: int, hi_bound: int) -> int:
        """以 center 为中心取 MAP_SNAPSHOT_BBOX_MAX 窗口起点，并夹在并集内。"""
        half = MAP_SNAPSHOT_BBOX_MAX // 2
        lo = int(round(center)) - half
        return max(lo_bound, min(lo, hi_bound - MAP_SNAPSHOT_BBOX_MAX))

    def _compose_tiles(
        self,
        server_name: str,
        st: _ServerMapState,
        dimension: str,
        tiles: list[tuple[int, int, int, tuple[str, bytes]]],
        fresh: list[dict[str, Any]],
        regions: list[tuple[int, int, tuple[int, str, bytes]]],
    ) -> tuple[tuple[int, int, int, int], str]:
        """按 bbox 合成 W*H*3 行优先 RGB 并 base64。

        bbox = 该维度全部瓦片块级矩形（x∈[cx*16,(cx+size)*16)）与区域矩形
        （x∈[rx*512,(rx+1)*512)）的并集；并集 w 或 h 超上限时，以新鲜玩家
        均值（无则并集质心）为中心取 ≤MAP_SNAPSHOT_BBOX_MAX 窗口并夹在
        并集内。绘制顺序 = 区域先做底层、瓦片后局部覆盖（叠加幂等，与事件
        到达顺序无关）；区域先做窗口相交判定、相交才解压（单区域解压出
        256 KB，禁止全维无脑解压）。命中区域/瓦片且下标 >0 → 调色板色；
        下标 0 / 未覆盖 / 下标越界（调色板被截断）→ (0,0,0)。(0,0,0) 同时
        是「未探索」的占位色，因此调色板里合法的纯黑项抬为 (0,0,1)：肉眼
        看仍是黑，但能与未探索像素区分（否则整片纯黑区域会被前端当作雾
        盖掉）。
        结果按 (dimension, revision, bbox) 缓存，每维度单条。
        """
        minx = min(
            [cx * 16 for cx, _, _, _ in tiles]
            + [rx * MAP_REGION_SIDE for rx, _, _ in regions]
        )
        maxx = max(
            [(cx + size) * 16 for cx, _, size, _ in tiles]
            + [(rx + 1) * MAP_REGION_SIDE for rx, _, _ in regions]
        )
        minz = min(
            [cz * 16 for _, cz, _, _ in tiles]
            + [rz * MAP_REGION_SIDE for _, rz, _ in regions]
        )
        maxz = max(
            [(cz + size) * 16 for _, cz, size, _ in tiles]
            + [(rz + 1) * MAP_REGION_SIDE for _, rz, _ in regions]
        )
        win_minx, win_minz, win_maxx, win_maxz = minx, minz, maxx, maxz
        if maxx - minx > MAP_SNAPSHOT_BBOX_MAX:
            cx0 = (
                sum(p["x"] for p in fresh) / len(fresh)
                if fresh
                else (minx + maxx) / 2.0
            )
            win_minx = self._window_lo(cx0, minx, maxx)
            win_maxx = win_minx + MAP_SNAPSHOT_BBOX_MAX
        if maxz - minz > MAP_SNAPSHOT_BBOX_MAX:
            cz0 = (
                sum(p["z"] for p in fresh) / len(fresh)
                if fresh
                else (minz + maxz) / 2.0
            )
            win_minz = self._window_lo(cz0, minz, maxz)
            win_maxz = win_minz + MAP_SNAPSHOT_BBOX_MAX
        bbox = (win_minx, win_minz, win_maxx, win_maxz)
        revision = st.revision.get(dimension, 0)
        cached = st.snapshot_cache.get(dimension)
        if cached is not None and cached[0] == bbox and cached[1] == revision:
            return bbox, cached[2]
        w = win_maxx - win_minx
        h = win_maxz - win_minz
        out = bytearray(w * h * 3)  # 默认 (0,0,0)：未覆盖 / 未设置
        colors = st.palette_colors
        n_colors = len(colors)
        # 先画区域做底层；窗口不相交直接跳过，禁止无脑解压（单区域 256 KB）
        for rx, rz, (_, _, comp) in regions:
            rx0 = rx * MAP_REGION_SIDE
            rz0 = rz * MAP_REGION_SIDE
            px_lo = max(0, win_minx - rx0)
            px_hi = min(MAP_REGION_SIDE, win_maxx - rx0)
            pz_lo = max(0, win_minz - rz0)
            pz_hi = min(MAP_REGION_SIDE, win_maxz - rz0)
            if px_lo >= px_hi or pz_lo >= pz_hi:
                continue
            try:
                raw = zlib.decompress(comp)
            except zlib.error as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dimension} 区域"
                    f" ({rx},{rz}) 合成时解压失败，跳过: {exc}"
                )
                continue
            if len(raw) != MAP_REGION_SIDE * MAP_REGION_SIDE:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dimension} 区域"
                    f" ({rx},{rz}) 合成时长度 {len(raw)} ≠"
                    f" {MAP_REGION_SIDE * MAP_REGION_SIDE}，跳过"
                )
                continue
            n = px_hi - px_lo
            for pz in range(pz_lo, pz_hi):
                row_cells = (rz0 + pz - win_minz) * w + (rx0 + px_lo - win_minx)
                src = pz * MAP_REGION_SIDE + px_lo
                for k in range(n):
                    idx = raw[src + k]
                    if idx == 0 or idx >= n_colors:
                        continue
                    color = colors[idx]
                    if color == (0, 0, 0):
                        # 合法纯黑 ≠ 未探索：抬 1 个蓝分量避免被前端当雾
                        color = _BLACK_FIX
                    o = (row_cells + k) * 3
                    out[o] = color[0]
                    out[o + 1] = color[1]
                    out[o + 2] = color[2]
        # 后画瓦片：局部覆盖在区域之上（瓦片胜出，叠加幂等）
        for cx, cz, size, (_, comp) in tiles:
            try:
                raw = zlib.decompress(comp)
            except zlib.error as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dimension} 瓦片"
                    f" ({cx},{cz},{size}) 合成时解压失败，跳过: {exc}"
                )
                continue
            side = size * 16
            if len(raw) != side * side:
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] {dimension} 瓦片"
                    f" ({cx},{cz},{size}) 合成时长度 {len(raw)} ≠ {side * side}，跳过"
                )
                continue
            tx0 = cx * 16
            tz0 = cz * 16
            px_lo = max(0, win_minx - tx0)
            px_hi = min(side, win_maxx - tx0)
            pz_lo = max(0, win_minz - tz0)
            pz_hi = min(side, win_maxz - tz0)
            if px_lo >= px_hi or pz_lo >= pz_hi:
                continue
            n = px_hi - px_lo
            for pz in range(pz_lo, pz_hi):
                row_cells = (tz0 + pz - win_minz) * w + (tx0 + px_lo - win_minx)
                src = pz * side + px_lo
                for k in range(n):
                    idx = raw[src + k]
                    if idx == 0 or idx >= n_colors:
                        continue
                    color = colors[idx]
                    if color == (0, 0, 0):
                        # 合法纯黑 ≠ 未探索：抬 1 个蓝分量避免被前端当雾
                        color = _BLACK_FIX
                    o = (row_cells + k) * 3
                    out[o] = color[0]
                    out[o + 1] = color[1]
                    out[o + 2] = color[2]
        tiles_b64 = base64.b64encode(out).decode("ascii")
        st.snapshot_cache[dimension] = (bbox, revision, tiles_b64)
        return bbox, tiles_b64

    # ---- 磁盘持久化（palette 每连接只发一次，重启后靠磁盘兜底） ----

    def _server_dir(self, server_name: str) -> Path | None:
        """该服务器的持久化目录（data_dir 未提供时 None）。"""
        if self._data_dir is None:
            return None
        return self._data_dir / "map" / safe_server_name(server_name)

    def _load_all(self) -> None:
        """启动时加载 <data_dir>/map/ 下全部已持久化状态；损坏文件告警跳过。"""
        if self._data_dir is None:  # 双保险
            return
        root = self._data_dir / "map"
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取地图持久化目录 {root} 失败: {exc}")
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                st = self._load_server_dir(entry)
            except Exception as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}][{entry.name}] 地图持久化状态加载失败，跳过:"
                    f" {exc!r}"
                )
                continue
            if st is not None:
                self._states[entry.name] = st

    def _load_server_dir(self, entry: Path) -> _ServerMapState | None:
        """加载单台服务器的持久化目录；无任何有效内容时返回 None。

        瓦片/区域不校验 sha1/解压（启动性能），只校验 b64 可解码；内容级
        损坏由快照合成时的解压/长度校验兜住。
        """
        st = _ServerMapState()
        meta_path = entry / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:  # ValueError 覆盖 JSONDecodeError
                logger.warning(
                    f"[{PLUGIN_NAME}][{entry.name}] meta.json 损坏，跳过: {exc!r}"
                )
                meta = {}
            if isinstance(meta, dict):
                pid = meta.get("palette_id")
                colors = meta.get("palette_colors")
                if isinstance(pid, str) and pid and isinstance(colors, list):
                    parsed: list[tuple[int, int, int]] = []
                    ok = True
                    for c in colors[:MAP_PALETTE_MAX_COLORS]:
                        if not isinstance(c, str) or not _HEX6_RE.match(c):
                            ok = False
                            break
                        parsed.append(
                            (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
                        )
                    if ok and parsed:
                        st.palette_id = pid
                        st.palette_colors = parsed
                        st.palette_hex = [
                            str(c) for c in colors[:MAP_PALETTE_MAX_COLORS]
                        ]
                    else:
                        logger.warning(
                            f"[{PLUGIN_NAME}][{entry.name}] meta.json 调色板"
                            " 含非法项，本次加载不启用调色板"
                        )
                dims = meta.get("dimensions")
                if isinstance(dims, list):
                    st.reported_dimensions = {
                        d for d in dims if isinstance(d, str) and d
                    }
                dyn = meta.get("dyn_dims")
                if isinstance(dyn, dict):
                    for key, short in dyn.items():
                        if (
                            isinstance(key, str)
                            and isinstance(short, str)
                            and MAP_REGISTRY_KEY_RE.match(key)
                            and MAP_DIM_SHORT_RE.match(short)
                        ):
                            st.dyn_dims[key] = short
                dus = meta.get("dim_updated_at")
                if isinstance(dus, dict):
                    for k, v in dus.items():
                        f = _as_float(v)
                        if isinstance(k, str) and f is not None:
                            st.dim_updated_at[k] = f
        known_dims = set(MAP_DIMENSIONS) | set(st.dyn_dims.values())
        st.dim_updated_at = {
            k: v for k, v in st.dim_updated_at.items() if k in known_dims
        }
        for f in sorted(entry.glob("*.json")):
            if f.name == "meta.json":
                continue
            stem = f.stem
            # 区域文件名 <dim>.regions.json，stem 是 "<dim>.regions"——先剥
            # 后缀再校验 known_dims（"overworld.regions" ≠ 合法维度短名）
            is_region = stem.endswith(".regions")
            dim = stem[: -len(".regions")] if is_region else stem
            if dim not in known_dims:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}][{entry.name}] 持久化文件 {f.name} 损坏，"
                    f"跳过: {exc!r}"
                )
                continue
            if not isinstance(data, dict):
                continue
            if is_region:
                for key_s, val in data.items():
                    pos = self._parse_region_key(key_s)
                    if pos is None or not isinstance(val, dict):
                        continue
                    sha1v = val.get("s")
                    b64v = val.get("b")
                    if not isinstance(sha1v, str) or not isinstance(b64v, str):
                        continue
                    try:
                        comp = base64.b64decode(b64v, validate=True)
                    except ValueError:
                        continue
                    xors = _as_int(val.get("x"))
                    st.regions[(dim, *pos)] = (
                        0 if xors is None else xors,
                        sha1v,
                        comp,
                    )
                    st.dim_region_count[dim] = (
                        st.dim_region_count.get(dim, 0) + 1
                    )
                continue
            for key_s, val in data.items():
                pos = self._parse_tile_key(key_s)
                if pos is None or not isinstance(val, dict):
                    continue
                sha1v = val.get("s")
                b64v = val.get("b")
                if not isinstance(sha1v, str) or not isinstance(b64v, str):
                    continue
                try:
                    comp = base64.b64decode(b64v, validate=True)
                except ValueError:
                    continue
                st.tiles[(dim, *pos)] = (sha1v, comp)
                st.dim_tile_count[dim] = st.dim_tile_count.get(dim, 0) + 1
        if st.tiles or st.regions:
            st.ever_tile = True
        if not (
            st.tiles
            or st.regions
            or st.palette_colors
            or st.reported_dimensions
            or st.dyn_dims
        ):
            return None
        return st

    @staticmethod
    def _parse_tile_key(key_s: Any) -> tuple[int, int, int] | None:
        """持久化瓦片键 "cx,cz,size" → (cx, cz, size)；非法返回 None。"""
        if not isinstance(key_s, str):
            return None
        parts = key_s.split(",")
        if len(parts) != 3:
            return None
        try:
            cx, cz, size = (int(p) for p in parts)  # 持久化键是数字字符串
        except ValueError:
            return None
        if size <= 0:
            return None
        return cx, cz, size

    @staticmethod
    def _parse_region_key(key_s: Any) -> tuple[int, int] | None:
        """持久化区域键 "rx,rz" → (rx, rz)；非法返回 None（坐标可负）。"""
        if not isinstance(key_s, str):
            return None
        parts = key_s.split(",")
        if len(parts) != 2:
            return None
        try:
            rx, rz = (int(p) for p in parts)  # 持久化键是数字字符串
        except ValueError:
            return None
        return rx, rz

    @staticmethod
    def _all_dims(st: _ServerMapState) -> list[str]:
        """需落盘的维度短名：内置三维度 + 已登记模组维度 + 现存瓦片/区域维度。"""
        ordered: list[str] = list(MAP_DIMENSIONS)
        for short in (
            list(st.dyn_dims.values())
            + [k[0] for k in st.tiles]
            + [k[0] for k in st.regions]
        ):
            if short not in ordered:
                ordered.append(short)
        return ordered

    def _write_server(self, server_name: str, st: _ServerMapState) -> None:
        """原子落盘单台服务器（调用方持锁）；失败告警不抛出。"""
        d = self._server_dir(server_name)
        if d is None:
            return
        try:
            d.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(
                d / "meta.json",
                {
                    "palette_id": st.palette_id,
                    "palette_colors": st.palette_hex,
                    "dimensions": sorted(st.reported_dimensions),
                    "dim_updated_at": st.dim_updated_at,
                    "dyn_dims": st.dyn_dims,
                },
            )
            for dim in self._all_dims(st):
                dim_tiles: dict[str, dict[str, str]] = {}
                for (dd, cx, cz, size), (sha1v, comp) in st.tiles.items():
                    if dd != dim:
                        continue
                    dim_tiles[f"{cx},{cz},{size}"] = {
                        "s": sha1v,
                        "b": base64.b64encode(comp).decode("ascii"),
                    }
                # 瓦片与区域两个文件各自独立落盘：同维度只有其一时
                # 另一类也必须正常写（不得互相 continue 短路）
                if dim_tiles or (d / f"{dim}.json").is_file():
                    self._atomic_write_json(d / f"{dim}.json", dim_tiles)
                # 无内容且无旧文件：不产生空文件（两类同逻辑）
                dim_regions: dict[str, dict[str, Any]] = {}
                for (dd, rx, rz), (xor_v, sha1v, comp) in st.regions.items():
                    if dd != dim:
                        continue
                    dim_regions[f"{rx},{rz}"] = {
                        "x": xor_v,  # 服务端内容指纹留档（续传排障）
                        "s": sha1v,
                        "b": base64.b64encode(comp).decode("ascii"),
                    }
                if dim_regions or (d / f"{dim}.regions.json").is_file():
                    self._atomic_write_json(d / f"{dim}.regions.json", dim_regions)
            st.dirty = False
            st.last_flush_at = time.time()
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] 地图持久化写入失败: {exc!r}"
            )

    @staticmethod
    def _atomic_write_json(path: Path, obj: Any) -> None:
        """tmp 文件 + os.replace 原子替换（避免读到半截 JSON）。"""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _maybe_flush(self, server_name: str, st: _ServerMapState) -> None:
        """节流落盘：距上次落盘 ≥MAP_FLUSH_MIN_INTERVAL_SECONDS 且仍有脏数据。"""
        if self._data_dir is None or not st.dirty:
            return
        if time.time() - st.last_flush_at < MAP_FLUSH_MIN_INTERVAL_SECONDS:
            return
        with self._lock:
            if st.dirty:
                self._write_server(server_name, st)
