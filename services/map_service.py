"""方块地图数据服务（Web 面板游戏顶层方块地图）：真实数据链路（无演示数据）。

真实数据来自服务端 mod「QueQiao-Map-Web」，经鹊桥事件桥以 4 类 notice 事件
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
- 「player_move」：玩家位置批量 upsert（按 uuid 键，uuid 缺失退 nickname）；
  无离线事件，超过 MAP_PLAYER_STALE_SECONDS 未更新即在快照 players 中缺席。

前向兼容铁律：未知 sub_type / 未知字段一律忽略不报错；解析异常绝不向事件
循环抛出（handle_event 内部 try/except + logger.warning，带
「[{PLUGIN_NAME}][{server_name}]」前缀）。

**无数据语义**：未收到过真实流（调色板 + 至少存过 1 片瓦片）的服务器
不生成任何演示数据——status/snapshot 返回 source=none，reason 说明原因
（未收到事件 MAP_NO_DATA_REASON / 开关关闭 MAP_DISABLED_REASON），快照
bbox=null、tiles 空串、players 空；有真实流但该维度零瓦片时给「未探索」
全黑小窗（以该维度最新鲜玩家为中心 ±112）。

**磁盘持久化**（解决「palette 每连接只发一次、AstrBot 重启后收不到重发」）：
<data_dir>/map/<safe_server>/meta.json（调色板 id、colors 原 hex 串、上报
维度、各维度更新时间）+ 每维度 <dim>.json（瓦片存 zlib 压缩字节 b64）；
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
    MAP_DIMENSIONS,
    MAP_DISABLED_REASON,
    MAP_NO_DATA_REASON,
    MAP_PLAYER_STALE_SECONDS,
    MAP_REGISTRY_DIM_SHORT,
    MAP_SOURCE_NONE,
    MAP_SOURCE_REAL,
    MAP_SUB_DIMENSIONS,
    MAP_SUB_PALETTE,
    MAP_SUB_PLAYER_MOVE,
    MAP_SUB_TILE,
    MAP_TILE_FORMAT,
    PLUGIN_NAME,
)

# 本模块消费的 4 类 sub_type（禁止 import MAP_EVENT_SUB_TYPES——main 层专用）
_HANDLED_SUB_TYPES = (
    MAP_SUB_DIMENSIONS,
    MAP_SUB_PALETTE,
    MAP_SUB_TILE,
    MAP_SUB_PLAYER_MOVE,
)

# ---- 真实链路阈值（map_service 专用模块级常量，依据与取舍见各条注释）----
# 每 (server, dim) 瓦片上限：依据上游实测 8×8 瓦片（128×128 pal8）zlib 后
# 0.5–2 KB、摊销约 20 B/区块；20000 片 = 128 万区块、压缩态约 10–40 MB，
# 已覆盖超大地图；超出按插入序淘汰最旧（一次性告警）
MAP_TILES_MAX_PER_DIM = 20000
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
        # 每维度 revision：瓦片实际变化才 bump，供快照合成缓存失效
        self.revision: dict[str, int] = {}
        # 每维度最近一次瓦片变化时间（落盘 meta，排障用）
        self.dim_updated_at: dict[str, float] = {}
        # map_dimensions 上报的 registry key 集合（清单重发即整体替换）
        self.reported_dimensions: set[str] = set()
        # 玩家：key=uuid（缺失退 nickname）→ {name,x,y,z,dimension,updated_at}
        self.players: dict[str, dict[str, Any]] = {}
        self.last_event_at: float = 0.0
        # 曾存过至少 1 片瓦片的历史标记（status/snapshot 真实流判定门槛之一）
        self.ever_tile: bool = False
        # 落盘状态
        self.dirty: bool = False
        self.last_flush_at: float = 0.0
        # 去重告警：未映射维度 / 超限淘汰各只告警一次
        self.warned_dims: set[str] = set()
        self.warned_evict: set[str] = set()
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
        """消费 4 类地图 notice 事件；未知 sub_type 返回 False，否则恒 True。

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

    # ---- 四类事件的解析落地 ----

    def _apply_dimensions(self, server_name: str, payload: dict[str, Any]) -> None:
        """map_dimensions：整体替换上报维度清单，无法映射的只告警一次。"""
        dims = payload.get("dimensions")
        if not isinstance(dims, list):
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] map_dimensions 载荷缺"
                " dimensions 或非列表，忽略"
            )
            return
        reported: set[str] = {d for d in dims if isinstance(d, str) and d}
        st = self._state(server_name)
        st.reported_dimensions = reported
        for d in reported:
            if d not in MAP_REGISTRY_DIM_SHORT and d not in st.warned_dims:
                st.warned_dims.add(d)
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] 上报维度 {d!r} 无法映射到"
                    "面板维度（overworld/nether/the_end），该维度数据将被忽略"
                )
        self._touch(st)
        self._maybe_flush(server_name, st)

    def _apply_palette(self, server_name: str, payload: dict[str, Any]) -> None:
        """map_palette：解析并（在 id 变化时）替换调色板。

        - 非法色项 ⇒ 整盘拒绝并告警（宁缺勿错色）；
        - 超过 256 项 ⇒ 截断保留前 256 并告警；
        - 新 id ⇒ 替换调色板并清空该服务器全部已存瓦片（旧瓦片按旧调色板
          量化，混用会错色），并 bump 各维度 revision 使快照缓存失效。
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
        if pid != st.palette_id:
            stale = len(st.tiles)
            if stale:
                # 旧瓦片按旧调色板量化，必须整服清空
                for dim in {k[0] for k in st.tiles}:
                    st.revision[dim] = st.revision.get(dim, 0) + 1
                st.tiles.clear()
                st.dim_tile_count.clear()
            st.snapshot_cache.clear()
            logger.info(
                f"[{PLUGIN_NAME}][{server_name}] 收到调色板"
                f"（{st.palette_id or '无'} → {pid or '?'}），清空旧瓦片 {stale} 片"
            )
        st.palette_id = pid
        st.palette_colors = parsed
        st.palette_hex = [str(c) for c in colors]
        self._touch(st)
        self._maybe_flush(server_name, st)

    def _apply_tile(self, server_name: str, payload: dict[str, Any]) -> None:
        """map_tile：解码校验 zlib-pal8 瓦片，sha1 去重后按位置键覆盖写。

        - 维度无法映射 ⇒ 忽略整批，每 (server, dimension) 只告警一次；
        - 调色板未到 ⇒ 丢弃整批并告警（上游保证 palette 先于 tile；AstrBot
          侧重启场景由磁盘持久化兜底）；
        - format 非 MAP_TILE_FORMAT ⇒ 跳过该瓦片并告警（新编码会用新 format 串）；
        - sha1 是 zlib 压缩字节（b64 之前）的摘要：自算作去重键，与声称不符
          ⇒ 告警但按自算值继续；同位置同摘要 ⇒ 跳过（不 bump revision）；
        - 解码长度必须等于 side*side（side=size*16），否则丢弃并告警；
        - 按位置键覆盖写幂等（重连可能重放同一批瓦片）。
        """
        dim_raw = payload.get("dimension")
        dim = (
            MAP_REGISTRY_DIM_SHORT.get(dim_raw) if isinstance(dim_raw, str) else None
        )
        if dim is None:
            shown = dim_raw if isinstance(dim_raw, str) else repr(dim_raw)
            st = self._state(server_name)
            if shown not in st.warned_dims:
                st.warned_dims.add(shown)
                logger.warning(
                    f"[{PLUGIN_NAME}][{server_name}] 无法映射的维度 {shown!r}"
                    " 的瓦片已忽略（每维度只告警一次）"
                )
            self._touch(st, dirty=False)
            return
        tiles = payload.get("tiles")
        if not isinstance(tiles, list):
            logger.warning(
                f"[{PLUGIN_NAME}][{server_name}] map_tile 载荷缺 tiles"
                " 或非列表，忽略"
            )
            return
        st = self._state(server_name)
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
            dim = (
                MAP_REGISTRY_DIM_SHORT.get(dim_raw)
                if isinstance(dim_raw, str)
                else None
            ) or (dim_raw if isinstance(dim_raw, str) else "")
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
        收到过调色板且至少存过 1 片瓦片 ⇒ 真实流（source=MAP_SOURCE_REAL）；
        其余（未收到真实事件）⇒ source=none + MAP_NO_DATA_REASON。
        """
        if server_name in self._disabled:
            return {
                "server_name": server_name,
                "enabled": False,
                "source": MAP_SOURCE_NONE,
                "reason": MAP_DISABLED_REASON,
                "dimensions": list(MAP_DIMENSIONS),
                "updated_at": time.time(),
            }
        st = self._lookup(server_name)
        if st is not None and st.palette_colors and st.ever_tile:
            return {
                "server_name": server_name,
                "enabled": True,
                "source": MAP_SOURCE_REAL,
                "reason": "",
                "dimensions": self._mapped_dimensions(st),
                "updated_at": st.last_event_at,
            }
        return {
            "server_name": server_name,
            "enabled": True,
            "source": MAP_SOURCE_NONE,
            "reason": MAP_NO_DATA_REASON,
            "dimensions": list(MAP_DIMENSIONS),
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
        if dimension not in MAP_DIMENSIONS:
            return None
        now = time.time() if at is None else float(at)
        if server_name in self._disabled:
            return self._none_snapshot(
                server_name, dimension, now, MAP_DISABLED_REASON
            )
        st = self._lookup(server_name)
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
            "cell": {"size": 1, "unit": "block"},
            "bbox": None,
            "tiles": "",
            "players": [],
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

    def _mapped_dimensions(self, st: _ServerMapState) -> list[str]:
        """已上报维度映射为短名并按 MAP_DIMENSIONS 顺序去重；未上报给全量。"""
        if not st.reported_dimensions:
            return list(MAP_DIMENSIONS)
        shorts = {
            MAP_REGISTRY_DIM_SHORT[d]
            for d in st.reported_dimensions
            if d in MAP_REGISTRY_DIM_SHORT
        }
        return [d for d in MAP_DIMENSIONS if d in shorts]

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

    def _real_snapshot(
        self,
        server_name: str,
        st: _ServerMapState,
        dimension: str,
        now: float,
    ) -> dict[str, Any]:
        """真实流快照：有瓦片按并集 bbox 合成；零瓦片维度给全黑未探索小窗。"""
        fresh = self._fresh_player_entries(st, dimension, now)
        tiles = self._dim_tiles(st, dimension)
        if tiles:
            bbox, tiles_b64 = self._compose_tiles(
                server_name, st, dimension, tiles, fresh
            )
        else:
            bbox, tiles_b64 = self._blank_window(fresh)
        return {
            "server_name": server_name,
            "dimension": dimension,
            "updated_at": st.last_event_at,
            "source": MAP_SOURCE_REAL,
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
    ) -> tuple[tuple[int, int, int, int], str]:
        """按 bbox 合成 W*H*3 行优先 RGB 并 base64。

        bbox = 该维度全部瓦片块级矩形并集（x∈[cx*16,(cx+size)*16)）；并集
        w 或 h 超上限时，以新鲜玩家均值（无则瓦片质心）为中心取
        ≤MAP_SNAPSHOT_BBOX_MAX 窗口并夹在并集内。命中瓦片且下标 >0 →
        调色板色；下标 0 / 未覆盖 / 下标越界（调色板被截断）→ (0,0,0)。
        结果按 (dimension, revision, bbox) 缓存，每维度单条。
        """
        minx = min(cx * 16 for cx, _, _, _ in tiles)
        maxx = max((cx + size) * 16 for cx, _, size, _ in tiles)
        minz = min(cz * 16 for _, cz, _, _ in tiles)
        maxz = max((cz + size) * 16 for _, cz, size, _ in tiles)
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

        瓦片不校验 sha1/解压（启动性能），只校验 b64 可解码；内容级损坏由
        快照合成时的解压/长度校验兜住。
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
                dus = meta.get("dim_updated_at")
                if isinstance(dus, dict):
                    for k, v in dus.items():
                        f = _as_float(v)
                        if isinstance(k, str) and k in MAP_DIMENSIONS and f is not None:
                            st.dim_updated_at[k] = f
        for f in sorted(entry.glob("*.json")):
            if f.name == "meta.json":
                continue
            dim = f.stem
            if dim not in MAP_DIMENSIONS:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}][{entry.name}] 瓦片文件 {f.name} 损坏，跳过:"
                    f" {exc!r}"
                )
                continue
            if not isinstance(data, dict):
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
        if st.tiles:
            st.ever_tile = True
        if not (st.tiles or st.palette_colors or st.reported_dimensions):
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
                },
            )
            for dim in MAP_DIMENSIONS:
                dim_tiles: dict[str, dict[str, str]] = {}
                for (dd, cx, cz, size), (sha1v, comp) in st.tiles.items():
                    if dd != dim:
                        continue
                    dim_tiles[f"{cx},{cz},{size}"] = {
                        "s": sha1v,
                        "b": base64.b64encode(comp).decode("ascii"),
                    }
                if not dim_tiles and not (d / f"{dim}.json").is_file():
                    # 无瓦片且无旧文件：不产生空文件
                    continue
                self._atomic_write_json(d / f"{dim}.json", dim_tiles)
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
