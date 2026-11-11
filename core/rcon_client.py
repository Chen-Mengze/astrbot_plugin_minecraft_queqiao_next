"""直连 RCON 客户端（兜底通道）。

Minecraft 服务端对超过单个 RCON 包上限（4096 字节）的输出会拆成多条响应包。
旧实现所用的第三方客户端只读取第一条响应包：长输出被截断，剩余包留在连接里
被下一条指令读走——例如多维度整合包的 `forge tps` 输出有数 KB，拆包后的
残片会被 `list` 当成玩家列表文本（把 TPS 数据解析成玩家名），而 `list`
自己的回包又留给下一条 TPS 指令（偶发解析不了 TPS）。

因此本模块自带最小 RCON 协议实现（仅标准库 asyncio），按包完整拼接拆包响应，
并校验请求 ID：发现读到上一条指令的响应时断开重连，避免把别人的输出当成自己的。

同时提供 RCON 输出解析的防御口径：玩家列表只认标准回包格式，任何其它输出
（TPS 文本、命令报错等）都不解析成玩家名。
"""

import asyncio
import contextlib
import random
import re
import struct

from astrbot.api import logger

from .constants import (
    PLUGIN_NAME,
    RCON_COMMAND_TIMEOUT,
    RCON_CONNECT_TIMEOUT,
    RCON_MAX_PACKET_PAYLOAD,
    strip_format_codes,
)
from .models_config import ServerConfig

# RCON 包类型（Minecraft 服务端协议）
_PACKET_TYPE_RESPONSE = 0
_PACKET_TYPE_COMMAND = 2
_PACKET_TYPE_LOGIN = 3
# 已读满一包后再等待后续拆包的时间：服务端拆包是连续发出的，正常在毫秒级
# 到达；短暂等待没有后续包即认为输出结束（兼容恰好 4096 字节整包的边界）
_SPLIT_DRAIN_TIMEOUT = 0.2
# 单包最大长度：载荷上限 + 请求 ID(4) + 类型(4) + 两个空终止符(2)
_MAX_PACKET_BYTES = RCON_MAX_PACKET_PAYLOAD + 10

# 标准 `list` 回包：`There are 2 of a max of 20 players online: Alice, Bob`。
# 只认这段前缀后再取冒号后的名单——RCON 串扰时可能返回 TPS 等其它输出，
# 不能把任意含冒号的文本（如 `TPS from last ...: 20.0, 20.0, 20.0`）当玩家名。
_PLAYER_LIST_RE = re.compile(
    r"There\s+are\s+\d+\s+of\s+a\s+max\s+of\s+\d+\s+players\s+online\s*:\s*([^\r\n]*)",
    re.IGNORECASE,
)
# TPS 输出特征：串扰自愈时用来判断「这条输出其实是 TPS 指令的回包」
_TPS_HINT_RES = (
    re.compile(r"Mean\s+TPS\s*:", re.IGNORECASE),
    re.compile(r"TPS\s+from\s+last", re.IGNORECASE),
)


def parse_player_list(output: str | None) -> list[str] | None:
    """从 `list` 指令输出解析玩家名；不是标准回包时返回 None。

    兼容形如：`There are 2 of a max of 20 players online: Alice, Bob`。
    返回 None 表示「这段输出不是 list 指令的回包」（例如 RCON 响应串扰时
    读到了 TPS 输出），调用方不应把其中的文本当玩家名，应降级到在线查询。
    0 人在线（冒号后为空）返回空列表。
    """
    if not output:
        return None
    matched = _PLAYER_LIST_RE.search(strip_format_codes(output))
    if matched is None:
        return None
    return [name.strip() for name in matched.group(1).split(",") if name.strip()]


def looks_like_player_list(output: str | None) -> bool:
    """输出是否为标准 `list` 回包（只看格式特征，不要求名单可解析）。"""
    if not output:
        return False
    return _PLAYER_LIST_RE.search(strip_format_codes(output)) is not None


def looks_like_tps_output(output: str | None) -> bool:
    """输出是否像 TPS 指令的回包（用于识别 RCON 响应串扰）。"""
    if not output:
        return False
    text = strip_format_codes(output)
    return any(hint.search(text) for hint in _TPS_HINT_RES)


def _encode_packet(request_id: int, packet_type: int, payload: bytes) -> bytes:
    """按 RCON 协议编码一个请求包（长度前缀 + 请求 ID + 类型 + 载荷 + 双空终止）。"""
    body = struct.pack("<ii", request_id, packet_type) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def _decode_packet(packet: bytes) -> tuple[int, int, bytes]:
    """解析一个完整 RCON 包（含长度前缀），返回 (request_id, type, payload)。"""
    if len(packet) < 14:
        raise ValueError("RCON 响应包长度不合法")
    length = struct.unpack("<i", packet[:4])[0]
    if length != len(packet) - 4:
        raise ValueError("RCON 响应包长度与声明的长度不一致")
    request_id, packet_type = struct.unpack("<ii", packet[4:12])
    payload = packet[12:]
    if payload.endswith(b"\x00\x00"):
        payload = payload[:-2]
    return request_id, packet_type, payload


async def _read_packet(
    reader: asyncio.StreamReader, timeout: float
) -> tuple[int, int, bytes]:
    """读取一个完整 RCON 响应包；超时/连接关闭向上抛出。"""
    length_data = await asyncio.wait_for(reader.readexactly(4), timeout)
    length = struct.unpack("<i", length_data)[0]
    if length < 10 or length > _MAX_PACKET_BYTES:
        raise ValueError(f"RCON 响应包长度异常: {length}")
    body = await asyncio.wait_for(reader.readexactly(length), timeout)
    return _decode_packet(length_data + body)


class RconClient:
    """单台服务器的 RCON 连接，带串行化的自动重连。

    同一时间只允许一条指令在途（串行锁）：RCON 是单连接请求-响应协议，
    并发下发会让响应与请求错位。
    """

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.server_name = config.server_name
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._lock = asyncio.Lock()

    # ---- 输出解析（静态入口，便于调用方只依赖 RconClient） ----

    @staticmethod
    def parse_player_list(output: str | None) -> list[str] | None:
        """从 `list` 指令输出解析玩家名；不是标准回包时返回 None（见模块函数）。"""
        return parse_player_list(output)

    @staticmethod
    def looks_like_player_list(output: str | None) -> bool:
        """输出是否为标准 `list` 回包（见模块函数）。"""
        return looks_like_player_list(output)

    @property
    def enabled(self) -> bool:
        """是否具备启用条件（配置开启 + 密码已填）。"""
        if not self.config.rcon_fallback_enabled:
            return False
        if not self.config.rcon_password:
            logger.warning(
                f"[{PLUGIN_NAME}][{self.server_name}] 已启用 RCON 兜底但未配置密码，跳过"
            )
            return False
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """建立 RCON 连接并完成鉴权（幂等）。"""
        if not self.enabled:
            return False
        if self._connected and self._writer is not None:
            return True

        async with self._lock:
            if self._connected and self._writer is not None:
                return True
            reader: asyncio.StreamReader | None = None
            writer: asyncio.StreamWriter | None = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self.config.rcon_host, self.config.rcon_port
                    ),
                    RCON_CONNECT_TIMEOUT,
                )
                writer.write(
                    _encode_packet(
                        random.randint(0, 0x7FFFFFFF),
                        _PACKET_TYPE_LOGIN,
                        self.config.rcon_password.encode("utf-8"),
                    )
                )
                await writer.drain()
                # 鉴权响应：标准协议回一条 AUTH_RESPONSE；个别服务端在它之前
                # 额外补一条空 RESPONSE 包，因此最多读两条，认到 AUTH 为止
                authenticated = False
                for _ in range(2):
                    request_id, packet_type, _payload = await _read_packet(
                        reader, RCON_CONNECT_TIMEOUT
                    )
                    if request_id == -1:
                        raise PermissionError("RCON 密码错误")
                    if packet_type == _PACKET_TYPE_RESPONSE:
                        authenticated = True
                        break
                if not authenticated:
                    raise PermissionError("RCON 登录未返回鉴权响应")
            except asyncio.CancelledError:
                if writer is not None:
                    writer.close()
                raise
            except Exception as exc:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                logger.error(
                    f"[{PLUGIN_NAME}][{self.server_name}] RCON 连接失败 "
                    f"({self.config.rcon_host}:{self.config.rcon_port}): {exc}"
                )
                self._reader = None
                self._writer = None
                self._connected = False
                return False

            self._reader = reader
            self._writer = writer
            self._connected = True
            logger.info(
                f"[{PLUGIN_NAME}][{self.server_name}] RCON 已连接 "
                f"{self.config.rcon_host}:{self.config.rcon_port}"
            )
            return True

    async def execute(self, command: str) -> str | None:
        """执行指令并返回完整输出；失败返回 None（失败后自动断开以便下次重连）。

        长输出可能被服务端拆成多条响应包，这里持续读取直到收到不满一包的
        尾包。读到不属于本条指令的响应（请求 ID 不匹配）说明连接内响应已
        错位，直接断开，避免把上一条指令的输出当成自己的。
        """
        if not self.enabled:
            return None
        if not await self.connect():
            return None

        try:
            async with self._lock:
                # 在锁内再取连接：等待期间连接可能已被其它协程因错误关闭，
                # 用进入前的引用会往已关闭的 socket 写数据
                reader = self._reader
                writer = self._writer
                if reader is None or writer is None:
                    return None
                request_id = random.randint(0, 0x7FFFFFFF)
                writer.write(
                    _encode_packet(
                        request_id,
                        _PACKET_TYPE_COMMAND,
                        command.encode("utf-8"),
                    )
                )
                await writer.drain()
                chunks: list[bytes] = []
                resp_id, _packet_type, payload = await _read_packet(
                    reader, RCON_COMMAND_TIMEOUT
                )
                if resp_id != request_id:
                    raise ConnectionError(
                        f"RCON 响应错位（期望 {request_id}，实际 {resp_id}）"
                    )
                chunks.append(payload)
                while len(payload) >= RCON_MAX_PACKET_PAYLOAD:
                    try:
                        resp_id, _packet_type, payload = await _read_packet(
                            reader, _SPLIT_DRAIN_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        break
                    if resp_id != request_id:
                        raise ConnectionError(
                            f"RCON 响应错位（期望 {request_id}，实际 {resp_id}）"
                        )
                    chunks.append(payload)
                return b"".join(chunks).decode("utf-8", errors="replace")
        except asyncio.CancelledError:
            # 取消时指令已发出、响应可能未读完：立即断开连接重新对齐，
            # 本次结果按「未知」放弃（调用方不得重发）。这里不能 await
            # 完整 close（取消路径上等 socket 关闭可能再次被打断/卡住）
            writer = self._writer
            self._reader = None
            self._writer = None
            self._connected = False
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
            raise
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}][{self.server_name}] RCON 执行失败: {exc}")
            await self.close()
            return None

    async def close(self) -> None:
        """关闭连接。"""
        writer = self._writer
        self._reader = None
        self._writer = None
        self._connected = False
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
