"""直连 RCON 客户端（兜底通道）。

当鹊桥未开启 RCON 时，本模块提供一条独立的 RCON 通道，用于执行远程指令与
查询在线玩家。依赖 `aio-mc-rcon`，采用惰性 import：未安装时插件其余功能不受影响。
"""

import asyncio
import contextlib
import re

from astrbot.api import logger

from .constants import PLUGIN_NAME, strip_format_codes
from .models_config import ServerConfig

try:  # 惰性降级：缺失依赖时仅关闭兜底通道
    import aiomcrcon

    _RCON_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    aiomcrcon = None  # type: ignore[assignment]
    _RCON_AVAILABLE = False

# 标准 `list` 回包：`There are 2 of a max of 20 players online: Alice, Bob`。
# 只认这段前缀后再取冒号后的名单——RCON 回包异常时可能读到 TPS 等其它输出，
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
    返回 None 表示「这段输出不是 list 指令的回包」，调用方不应把其中的文本
    当玩家名，应降级到在线查询。0 人在线（冒号后为空）返回空列表。
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


class RconClient:
    """单台服务器的 RCON 连接，带串行化的自动重连。"""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.server_name = config.server_name
        self._client: object | None = None
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """是否具备启用条件（配置开启 + 依赖可用 + 密码已填）。"""
        if not self.config.rcon_fallback_enabled:
            return False
        if not self.config.rcon_password:
            logger.warning(
                f"[{PLUGIN_NAME}][{self.server_name}] 已启用 RCON 兜底但未配置密码，跳过"
            )
            return False
        if not _RCON_AVAILABLE:
            logger.warning(
                f"[{PLUGIN_NAME}][{self.server_name}] 已启用 RCON 兜底但未安装 "
                f"aio-mc-rcon，请执行 pip install aio-mc-rcon"
            )
            return False
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """建立 RCON 连接（幂等）。"""
        if not self.enabled:
            return False
        if self._connected and self._client is not None:
            return True

        async with self._lock:
            if self._connected and self._client is not None:
                return True
            try:
                client = aiomcrcon.Client(
                    self.config.rcon_host,
                    self.config.rcon_port,
                    self.config.rcon_password,
                )
                await client.connect()
            except Exception as exc:
                logger.error(
                    f"[{PLUGIN_NAME}][{self.server_name}] RCON 连接失败 "
                    f"({self.config.rcon_host}:{self.config.rcon_port}): {exc}"
                )
                self._client = None
                self._connected = False
                return False

            self._client = client
            self._connected = True
            logger.info(
                f"[{PLUGIN_NAME}][{self.server_name}] RCON 已连接 "
                f"{self.config.rcon_host}:{self.config.rcon_port}"
            )
            return True

    async def execute(self, command: str) -> str | None:
        """执行指令并返回输出；失败返回 None（失败后自动断开以便下次重连）。"""
        if not self.enabled:
            return None

        if not await self.connect():
            return None

        client = self._client
        if client is None:
            return None

        try:
            async with self._lock:
                response = await client.send_cmd(command)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}][{self.server_name}] RCON 执行失败: {exc}")
            await self.close()
            return None

        # aiomcrcon 返回 (response, request_id)
        if isinstance(response, tuple) and response:
            return str(response[0])
        return str(response) if response is not None else ""

    async def close(self) -> None:
        """关闭连接。"""
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.close()

    # ---- 输出解析（静态入口，便于调用方只依赖 RconClient） ----

    @staticmethod
    def parse_player_list(output: str | None) -> list[str] | None:
        """从 `list` 指令输出解析玩家名；不是标准回包时返回 None（见模块函数）。"""
        return parse_player_list(output)

    @staticmethod
    def looks_like_player_list(output: str | None) -> bool:
        """输出是否为标准 `list` 回包（见模块函数）。"""
        return looks_like_player_list(output)
