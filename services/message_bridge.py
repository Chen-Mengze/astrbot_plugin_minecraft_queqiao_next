"""消息桥：MC 事件 ↔ 外部会话 的双向转发。

关键设计：
- `target_sessions`（UMO 列表）是绑定关系的锚点，同时决定两个方向的目标
- 外部 → MC 转发后，该内容在抑制窗口内从 MC 回传时不再转发，避免回声刷屏
- 富文本 `raw_message` 可能是 JSON 文本组件，转发前必须剥离为纯文本
- 外部会话的图片可转为 ChatImage 代码广播进游戏，由游戏端 ChatImage
  模组渲染（https://github.com/kitUIN/ChatImage）
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from astrbot.api import logger

from ..core.constants import (
    API_SET_MSG_EMOJI_LIKE,
    DEFAULT_CHATIMAGE_NAME,
    ECHO_SUPPRESS_WINDOW,
    EMOJI_OK_GESTURE,
    IMAGE_DOWNLOAD_TIMEOUT,
    MARK_TEXT_OK,
    MAX_IMAGE_DOWNLOAD_BYTES,
    OUTBOUND_ECHO_WINDOW,
    PLUGIN_NAME,
    prefix_matches,
    strip_prefix,
)
from ..core.models import QueQiaoEvent
from ..core.models_config import ServerConfig
from ..core.session_router import (
    GROUP_MESSAGE,
    SessionRef,
    event_session_ref,
    parse_session_entry,
    send_target_key,
    session_matches,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

# MC 传统格式化代码（§a 等）与文本组件都可能混在消息里
_FORMAT_CODE_RE = re.compile(r"§[0-9a-fk-orA-FK-OR]")


def strip_formatting(text: str) -> str:
    """剥离 MC 格式化代码。"""
    return _FORMAT_CODE_RE.sub("", text)


def component_to_text(raw: str) -> str:
    """把可能为 JSON 文本组件的字符串还原为纯文本。

    非原版服务端的 `raw_message` 可能是 {"text":"Hello","color":"light_purple"}
    这类形态，若直接转发到 QQ 会出现花括号，因此这里递归提取 text 字段。
    """
    if not raw:
        return ""

    stripped = raw.strip()
    if not stripped.startswith(("{", "[", '"')):
        return strip_formatting(stripped)

    try:
        data = json.loads(stripped)
    except ValueError:
        return strip_formatting(stripped)

    def _extract(node: object) -> str:
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(_extract(item) for item in node)
        if isinstance(node, dict):
            parts: list[str] = []
            if "text" in node:
                parts.append(str(node["text"]))
            if "extra" in node:
                parts.append(_extract(node["extra"]))
            if "with" in node:
                parts.append(_extract(node["with"]))
            return "".join(parts)
        return ""

    return strip_formatting(_extract(data)) or strip_formatting(stripped)


def build_chatimage_code(url: str, name: str = "") -> str:
    """把图片 URL 包装为 ChatImage 代码（`[[CICode,url=...,name=...]]`）。

    游戏端安装 ChatImage 模组后会在聊天栏直接渲染该图片；未安装时
    显示为普通文本，不影响其它玩家。name 留空用默认值「图片」。
    """
    display = (name or DEFAULT_CHATIMAGE_NAME).strip() or DEFAULT_CHATIMAGE_NAME
    return f"[[CICode,url={url},name={display}]]"


async def resolve_image_url(
    comp: Any,
    uploader: Any = None,
) -> tuple[str | None, str | None]:
    """从图片消息组件解析出「玩家客户端可访问」的图片 URL。

    返回 `(url, fail_reason)`：拿到 URL 时 fail_reason 为 None；否则给出
    失败原因（供排障日志展示）。

    优先级：
    1. 组件自带的公开 URL（如 QQ 图片 CDN 的 `url` 字段，无需中转）；
    2. 组件 `file` 字段本身就是 http(s) 链接（部分平台如此）；
    3. 已启用的图片转存服务（`ImageBedUploaderGroup`）：内置 HTTP 条目
       把字节登记到本服务生成 `{base_url}/img/<token>`，第三方图床条目把
       字节上传换取公网链接，两者按列表顺序逐个尝试；
    4. 注册到 AstrBot 文件服务生成链接（依赖 `callback_api_base` 可达）。
    """
    for attr in ("url", "file"):
        candidate = getattr(comp, attr, None)
        if candidate and str(candidate).startswith(("http://", "https://")):
            return str(candidate), None

    data: bytes | None = None
    reason = "无公开 URL"
    if uploader is not None and getattr(uploader, "enabled", False):
        data, reason = await acquire_image_bytes(comp)
        if data:
            uploaded, svc_error = await uploader.upload(data)
            if uploaded:
                return uploaded, None
            reason = f"图片转存失败: {svc_error}"

    # AstrBot 文件服务兜底（依赖 callback_api_base 且该地址对玩家可达）
    register = getattr(comp, "register_to_file_service", None)
    if callable(register):
        try:
            hosted = await register()
        except Exception as exc:
            reason = f"{reason}；文件服务兜底失败: {exc}"
        else:
            if hosted and str(hosted).startswith(("http://", "https://")):
                return str(hosted), None
            reason = f"{reason}；文件服务兜底返回非 URL: {hosted!r}"
    else:
        reason = f"{reason}；无文件服务兜底"
    return None, reason


async def acquire_image_bytes(comp: Any) -> tuple[bytes | None, str]:
    """把图片组件解析为本地字节，返回 `(bytes, fail_reason)`。

    借助 AstrBot 的 `Image.convert_to_file_path()` 统一处理 http(s) URL、
    `base64://`、`file://` URI 与本地路径，再读出字节；失败时 reason 说明原因。
    """
    convert = getattr(comp, "convert_to_file_path", None)
    if not callable(convert):
        return None, "组件无 convert_to_file_path"
    try:
        path = await convert()
    except Exception as exc:
        return None, f"convert_to_file_path 失败: {exc}"
    if not path:
        return None, "convert_to_file_path 返回空"
    try:
        return Path(path).read_bytes(), ""
    except OSError as exc:
        return None, f"读取本地文件失败: {path}: {exc}"


# ---- MC → 外部：游戏内图片识别与下载 ----

# 常见图片扩展名（用于判定「裸链接」是否按图片处理；忽略 query/fragment）
_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|jfif|avif)$", re.IGNORECASE)

# ChatImage 代码：[[CICode,url=...,name=...]]，group(1) 为 url（url 是第一个参数）
_CICODE_RE = re.compile(r"\[\[CICode,url=([^\],\s]+)(?:,[^\]]*)?\]\]", re.IGNORECASE)

# 裸链接：http(s) / file（file:// 一般只出现在 CICode 内，这里兼容裸写）
_URL_RE = re.compile(r"(?:https?|file)://[^\s]+", re.IGNORECASE)


def _is_image_url(url: str) -> bool:
    """判断裸链接是否按图片处理（按路径扩展名，忽略 query/fragment）。"""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return bool(_IMAGE_EXT_RE.search(path))


def extract_image_refs(text: str) -> list[str]:
    """从游戏内聊天文本中提取图片引用（CICode url 与裸图片链接），去重保持顺序。"""
    if not text:
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for match in _CICODE_RE.finditer(text):
        url = match.group(1).strip()
        if url and url not in seen:
            seen.add(url)
            refs.append(url)
    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        url = raw.rstrip(".,;!?)]}'\"")
        if url and url not in seen and _is_image_url(url):
            seen.add(url)
            refs.append(url)
    return refs


def strip_image_refs(text: str, keep: set[str] | None = None) -> str:
    """移除已成功转成图片的引用；`keep` 中的引用（下载失败）保留原文。

    只有被成功下载的引用才会被剥离成图片；失败的引用不能凭空消失，
    否则用户连「本来有张图」都看不到，因此通过 `keep` 保留下载失败的
    代码/链接原文。
    """
    if not text:
        return ""
    keep = keep or set()

    def _cic(match: re.Match) -> str:
        url = match.group(1).strip()
        return match.group(0) if url in keep else ""

    def _url(match: re.Match) -> str:
        raw = match.group(0)
        url = raw.rstrip(".,;!?)]}'\"")
        if url in keep:
            return raw
        return "" if _is_image_url(url) else raw

    text = _CICODE_RE.sub(_cic, text)
    text = _URL_RE.sub(_url, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def file_uri_to_local_path(uri: str) -> str | None:
    """把 file:// URI 解析为本地路径（Windows 盘符 / POSIX / localhost 均可）。"""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    netloc = parsed.netloc
    if netloc and netloc != "localhost":
        path = f"//{netloc}{path}"
    if re.match(r"^/[A-Za-z]:", path):  # /D:/x 或 /D:\x → D:/x、D:\x
        path = path[1:]
    return path or None


async def download_image_bytes(
    url: str,
    timeout: float = IMAGE_DOWNLOAD_TIMEOUT,
    max_bytes: int = MAX_IMAGE_DOWNLOAD_BYTES,
) -> bytes | None:
    """把图片引用（http(s)/file 链接）取成本地字节；失败返回 None。

    file:// 仅当文件存在于本机（AstrBot 与游戏同机、或文件可访问）时有效；
    http(s) 用 aiohttp 下载并限制大小。任何异常都不外抛，由调用方决定降级。
    """
    if url.startswith(("file://", "file:/")):
        path = file_uri_to_local_path(url)
        if not path:
            return None
        try:
            data = Path(path).read_bytes()
        except OSError:
            logger.debug(f"[{PLUGIN_NAME}] 读取本地图片失败: {path}")
            return None
        return data if 0 < len(data) <= max_bytes else None

    if not url.startswith(("http://", "https://")):
        return None

    try:
        import aiohttp  # AstrBot 运行时既有依赖，惰性导入
    except Exception:
        return None
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.content.read(max_bytes + 1)
    except Exception as exc:
        logger.debug(f"[{PLUGIN_NAME}] 下载图片失败 {url}: {exc}")
        return None
    if not data or len(data) > max_bytes:
        return None
    return data


@dataclass(frozen=True)
class _SessionTarget:
    """一条目标会话配置：服务器 + 归一化后的会话信息。"""

    server_name: str
    config: ServerConfig
    ref: SessionRef


class MessageBridge:
    """负责 MC 事件到外部会话的格式化与发送。"""

    def __init__(self, context, translator=None) -> None:
        self.context = context
        # 本地翻译器（插件侧把鹊桥英文/翻译键转中文），None 表示未启用
        self._translator = translator
        self._configs: dict[str, ServerConfig] = {}
        # 全部目标会话（每次注册 / 本地更新时重建），匹配走 core.session_router：
        # 兼容 AstrBot 隔离对话（unique_session）的「发送者ID_群ID」复合会话 ID
        self._targets: list[_SessionTarget] = []
        # 目标会话原始写法 -> 实际可发送的规范 UMO（事件里学到，供短写法回填）
        self._learned_sessions: dict[str, str] = {}
        # 已告警过的不可解析目标会话，避免每来一条消息刷一次日志
        self._unresolved_warned: set[str] = set()
        # 会话去重键 -> [(刚发往该会话的内容, 时间)]，用于识别平台回传的
        # Bot 自身消息（MC → 群 → MC 方向的回声）
        self._recent_outbound: dict[str, list[tuple[str, float]]] = {}
        # (server_name, content) -> 转发时间，用于回声抑制
        self._recently_forwarded: dict[tuple[str, str], float] = {}

    # ---- 服务器 / 目标会话注册 ----

    def register_server(self, config: ServerConfig) -> None:
        """注册服务器并重建目标会话索引（重复注册等于就地更新）。"""
        self._configs[config.server_name] = config
        self._rebuild_targets()

    def unregister_server(self, server_name: str) -> None:
        """取消注册某台服务器（配置删除 / 热重载前清理）。"""
        self._configs.pop(server_name, None)
        self._rebuild_targets()

    def reregister_server(self, config: ServerConfig) -> None:
        """配置就地更新后重建索引（等价 register_server，语义更明确）。"""
        self.register_server(config)

    def _rebuild_targets(self) -> None:
        self._targets = [
            _SessionTarget(
                server_name=config.server_name,
                config=config,
                ref=parse_session_entry(raw),
            )
            for config in self._configs.values()
            for raw in config.target_sessions
            if str(raw or "").strip()
        ]

    def clear(self) -> None:
        """清空已注册服务器索引，供配置热重载前重置使用。"""
        self._configs.clear()
        self._targets.clear()
        self._learned_sessions.clear()
        self._unresolved_warned.clear()
        self._recent_outbound.clear()
        self._recently_forwarded.clear()

    def servers_for_session(self, umo: str) -> list[tuple[str, ServerConfig]]:
        """查询某会话（UMO 字符串）绑定的服务器列表。

        无事件对象时的退化入口：只能按 UMO 自身信息匹配（隔离对话的复合
        会话 ID 仍能按「_群号」推导），日常事件处理请用
        `servers_for_event()`（能拿到真实群号，最准确）。
        """
        return self._lookup(parse_session_entry(umo), learn=False)

    def servers_for_event(self, event: Any) -> list[tuple[str, ServerConfig]]:
        """查询外部消息事件所在会话绑定的服务器列表。

        匹配时顺带记忆「目标会话原始写法 → 规范 UMO 映射」，让只写了
        `平台:群号` / `群号` 的短写目标也能在 MC → 外部方向回填发送。
        """
        return self._lookup(event_session_ref(event), learn=True)

    def _lookup(
        self, ref: SessionRef, *, learn: bool
    ) -> list[tuple[str, ServerConfig]]:
        found: list[tuple[str, ServerConfig]] = []
        seen: set[str] = set()
        for target in self._targets:
            if not session_matches(target.ref, ref):
                continue
            # 只为「短写法 / 不完整 UMO」学习回填映射：完整 UMO 一律按
            # 用户填写的原样发送，避免擅自改写平台自定义的会话 ID 格式
            if learn and not target.ref.is_full and ref.canonical:
                self._learned_sessions[target.ref.raw] = ref.canonical
            if target.server_name in seen:
                continue
            seen.add(target.server_name)
            found.append((target.server_name, target.config))
        return found

    # ---- 回声抑制 ----

    def mark_forwarded(self, server_name: str, content: str) -> None:
        """记录一条刚刚转发到 MC 的内容。"""
        self._recently_forwarded[(server_name, content)] = time.time()

    def _is_echo(self, server_name: str, content: str) -> bool:
        """判断该内容是否为刚刚转发出去的回声。"""
        key = (server_name, content)
        stamp = self._recently_forwarded.get(key)
        if stamp is None:
            return False
        del self._recently_forwarded[key]
        return (time.time() - stamp) < ECHO_SUPPRESS_WINDOW

    # ---- 目标会话解析 / 发送去重 ----

    def resolve_send_targets(self, config: ServerConfig) -> list[tuple[str, str]]:
        """把目标会话解析为 ``[(可直接发送的 UMO, 去重键)]``。

        两种「同一会话多条配置」的重复发送都在这里收敛：

        - 隔离对话下每个成员一条 UMO（``平台:GroupMessage:发送者_群号``），
          按会话通道去重后只发一次；
        - 完全重复的配置项（面板 / 手写重复粘贴）。

        短写法（``平台:群号``、群号）依赖事件处理时学到的规范 UMO 回填；
        仍解析不出来的会告警一次并跳过，不会静默丢消息。
        """
        plan: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in config.target_sessions:
            ref = parse_session_entry(raw)
            send_umo = ""
            key = ""
            learned = self._learned_sessions.get(ref.raw)
            if not ref.is_full and learned:
                learned_ref = parse_session_entry(learned)
                if learned_ref.is_full:
                    send_umo, key = learned_ref.raw, send_target_key(learned_ref)
            if not send_umo and ref.is_full:
                send_umo, key = ref.raw, send_target_key(ref)
            if not send_umo and ref.platform and ref.session_id:
                # `平台:会话ID` 简写：默认按群聊补全（私聊建议用 /mc target add）
                fallback = parse_session_entry(
                    f"{ref.platform}:{GROUP_MESSAGE}:{ref.session_id}"
                )
                send_umo, key = fallback.raw, send_target_key(fallback)
            if not send_umo:
                if ref.raw not in self._unresolved_warned:
                    self._unresolved_warned.add(ref.raw)
                    logger.warning(
                        f"[{PLUGIN_NAME}][{config.server_name}] 目标会话 "
                        f"{ref.raw!r} 无法解析为可发送的 UMO（缺平台信息，且尚未"
                        "收到过该会话的消息），已跳过。建议用 /mc target add 重新绑定"
                    )
                continue
            if key in seen:
                logger.debug(
                    f"[{PLUGIN_NAME}][{config.server_name}] 目标会话 {ref.raw!r} "
                    "与前面的配置指向同一会话，已合并（避免重复发送）"
                )
                continue
            seen.add(key)
            plan.append((send_umo, key))
        return plan

    def mark_outbound(self, key: str, content: str) -> None:
        """记录一条刚发往外部会话的内容，供平台回传时识别（MC → 群 → MC）。"""
        entries = self._recent_outbound.setdefault(key, [])
        entries.append((str(content or ""), time.time()))
        if len(entries) > 20:
            del entries[:-20]

    def is_outbound_echo(self, event: Any, text: str) -> bool:
        """判断外部消息是否为插件刚发出去的内容被平台回传。

        与 Robot 自身消息判定互补：部分平台（webhook / 部分协议端）不给
        ``self_id``，只能靠内容比对兜底。命中即消费记录，避免同一内容
        第二次被误吞。
        """
        content = str(text or "").strip()
        if not content:
            return False
        key = send_target_key(event_session_ref(event))
        entries = self._recent_outbound.get(key)
        if not entries:
            return False
        now = time.time()
        kept: list[tuple[str, float]] = []
        matched = False
        for item_content, stamp in entries:
            if now - stamp >= OUTBOUND_ECHO_WINDOW:
                continue
            if not matched and item_content == content:
                matched = True
                continue
            kept.append((item_content, stamp))
        if kept:
            self._recent_outbound[key] = kept
        else:
            self._recent_outbound.pop(key, None)
        return matched

    # ---- MC → 外部 ----

    def should_forward(self, config: ServerConfig, event: QueQiaoEvent) -> bool:
        """依据事件类型与配置判断是否需要转发。"""
        if event.is_chat:
            return config.forward_chat_to_astrbot
        if event.is_join or event.is_quit:
            return config.forward_join_leave_to_astrbot
        if event.is_death:
            return config.forward_death_to_astrbot
        if event.is_achievement:
            return config.forward_achievement_to_astrbot
        return False

    def format_event(self, config: ServerConfig, event: QueQiaoEvent) -> str:
        """把事件渲染为转发文本。"""
        player = event.player_name or "未知"

        if event.is_chat:
            message = component_to_text(event.raw_message) or component_to_text(
                event.message
            )
            return self._format_chat(config, player, message)

        # 多台服务器指向同一会话时必须能区分来源：
        # 进出/死亡/成就统一前置 `[{display_label}]`（display_name → 默认值 → server_name）
        if event.is_join:
            return f"[{config.display_label}] 🟢 {player} 加入了服务器"
        if event.is_quit:
            return f"[{config.display_label}] 🔴 {player} 离开了服务器"
        if event.is_death:
            if config.enable_local_translation and self._translator is not None:
                text = self._translator.translate_death(event.death) or "死亡"
            else:
                # text 可能是未格式化模板（%s占位），render_text 做参数填充
                text = event.death.render_text or "死亡"
            return f"[{config.display_label}] 💀 {text}"
        if event.is_achievement:
            # 本地翻译开启：直接翻成就名（display_name），固定拼接玩家名
            if config.enable_local_translation and self._translator is not None:
                name = self._translator.translate_achievement(event.achievement)
                if name:
                    return f"[{config.display_label}] 🏆 {player} 达成了成就 {name}"
                return f"[{config.display_label}] 🏆 {player} 达成了成就"
            # display_text 可能为空（未开翻译 + 服务端仅给 key），
            # 此时退到 display_name，最差也能给出成就 key，避免无信息量的「达成成就」
            text = event.achievement.display_text or event.achievement.display_name
            if not text:
                return f"[{config.display_label}] 🏆 {player} 达成了成就"

            # 补玩家名：开启翻译时整句已含玩家名（`X has made the advancement [Y]`），
            # 未开启时只降级到 display.title，那里**只有成就名**。
            # 故先判重，避免出现 `X X has made the advancement [Y]`。
            if event.player_name and event.player_name not in text:
                return f"[{config.display_label}] 🏆 {player} 达成了成就 {text}"
            return f"[{config.display_label}] 🏆 {text}"
        return ""

    @staticmethod
    def _format_chat(config: ServerConfig, player: str, message: str) -> str:
        """按聊天格式模板渲染。

        {display_name} 取显示名称：display_name 留空时用 display_name_default（默认 MC），
        两者都显式留空才输出空串（无前缀）；均不回退 server_name。
        """
        return config.forward_chat_format.format(
            player=player, message=message, display_name=config.server_label
        )

    async def _resolve_forward_images(
        self, config: ServerConfig, event: QueQiaoEvent, content: str
    ) -> tuple[str, list]:
        """开启 `forward_image_from_mc` 时，把聊天里的图片引用转为图片组件。

        返回 `(text, images)`：text 为剥离「已成功转图」引用后的格式化文本
        （下载失败的引用保留原文，避免信息静默丢失），images 为下载成功的
        Image 组件列表。
        """
        message = component_to_text(event.raw_message) or component_to_text(
            event.message
        )
        refs = extract_image_refs(message)
        if not refs:
            return content, []

        images: list = []
        converted: list[str] = []
        for ref in refs:
            data = await download_image_bytes(ref)
            if data is None:
                continue
            try:
                from astrbot.api.message_components import Image

                images.append(Image.fromBytes(data))
                converted.append(ref)
            except Exception as exc:
                logger.debug(f"[{PLUGIN_NAME}] 构建图片组件失败 {ref}: {exc}")

        if not images:
            return content, []

        keep = {ref for ref in refs if ref not in converted}
        clean = strip_image_refs(message, keep=keep)
        player = event.player_name or "未知"
        return self._format_chat(config, player, clean), images

    async def forward_event(
        self, server_name: str, config: ServerConfig, event: QueQiaoEvent
    ) -> bool:
        """把 MC 事件转发到该服务器配置的所有目标会话。"""
        if not self.should_forward(config, event):
            return False

        if not config.target_sessions:
            return False

        # 聊天消息需先经过回声抑制：外部发到 MC 的内容会原样回传为聊天事件
        if event.is_chat and self._is_echo(server_name, event.message.strip()):
            logger.debug(f"[{PLUGIN_NAME}][{server_name}] 抑制回声消息: {event.message}")
            return False

        content = self.format_event(config, event)
        images: list = []

        if event.is_chat and config.forward_image_from_mc:
            content, images = await self._resolve_forward_images(config, event, content)

        if not content and not images:
            return False

        # 目标会话去重（隔离对话下同一群会写成多条 UMO），每条会话只发一次
        targets = self.resolve_send_targets(config)
        if not targets:
            return False

        sent_count = 0
        for send_umo, key in targets:
            if await self._send(send_umo, content, images):
                self.mark_outbound(key, content)
                sent_count += 1
        if sent_count:
            logger.info(
                f"[{PLUGIN_NAME}][{server_name}] 游戏消息 → QQ: {content} "
                f"(已发送 {sent_count}/{len(targets)} 个会话)"
            )
        return sent_count > 0

    async def _send(self, umo: str, content: str, images: list | None = None) -> bool:
        """向指定会话发送消息（文本 + 可选图片组件）。"""
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            components = []
            if content:
                components.append(Plain(text=content))
            if images:
                components.extend(images)
            if not components:
                return False
            chain = MessageChain(chain=components)
            await self.context.send_message(umo, chain)
            return True
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 发送消息到会话 {umo} 失败: {exc}")
            return False

    # ---- 外部 → MC ----

    def should_relay(self, config: ServerConfig, text: str) -> bool:
        """判断外部消息是否应转发到 MC（按前缀过滤）。

        与 AI 前缀不同：转发前缀留空表示**全部转发**，
        因此这里不能用 `prefix_matches` 的「空即不匹配」语义。
        """
        prefix = config.auto_forward_prefix
        if not prefix:
            return True
        return prefix_matches(prefix, text)

    def strip_relay_prefix(self, config: ServerConfig, text: str) -> str:
        """去掉转发前缀。"""
        return strip_prefix(config.auto_forward_prefix, text)

    # ---- 转发回执 ----

    async def mark_relayed(self, event: Any, config: ServerConfig) -> None:
        """转发成功后按 `mark_option` 给出回执。

        - `emoji`：给原消息贴表情（走协议端私有接口，仅 aiocqhttp 支持）
        - `text`：回复一条 ✅ 文本
        - `none`：不提醒

        回执属于「锦上添花」，任何失败都只记 debug 日志，绝不影响转发结果。
        """
        option = config.mark_option
        if option == "none":
            return
        if option == "emoji":
            # 只贴表情，不额外发文本；平台不支持时不回退成文本，避免刷屏
            await self._react_with_emoji(event, emoji_id=config.mark_emoji_id)
            return
        await self._mark_text(event)

    async def _react_with_emoji(
        self, event: "AstrMessageEvent", emoji_id: int = EMOJI_OK_GESTURE
    ) -> bool:
        """给原消息贴表情（OneBot 扩展 `set_msg_emoji_like`，所有 aiocqhttp
        协议端均支持，如 NapCat / Lagrange / LLOneBot）。

        emoji_id: 要使用的表情符号 ID (默认: EMOJI_OK_GESTURE)
            - EMOJI_OK_GESTURE (124): 👌
            - EMOJI_THUMBS_UP (76): 👍
            - EMOJI_LOVE (66): ❤️
            - EMOJI_ROSE (63): 🌹

        非 aiocqhttp 平台、或拿不到 message_id 时静默跳过。

        注意：aiocqhttp 的 `CQHttp.__getattr__` 会为**任意**名字返回
        `call_action` 的偏函数，所以不能用 `getattr(bot, "set_msg_emoji_like")`
        判断接口是否存在——那样永远为真。这里直接走 `call_action`，
        由协议端在真正不支持时返回错误，再降级为静默跳过。
        """
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        if not message_id:
            return False

        bot = getattr(event, "bot", None)
        emoji_id = emoji_id or EMOJI_OK_GESTURE
        try:
            if hasattr(bot, "call_action"):
                await bot.call_action(
                    API_SET_MSG_EMOJI_LIKE,
                    message_id=message_id,
                    emoji_id=str(emoji_id),
                )
                return True
            if bot is not None:
                await bot.set_msg_emoji_like(
                    message_id=message_id, emoji_id=str(emoji_id)
                )
                return True
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 贴表情失败({message_id}): {exc}")
        return False

    async def _mark_text(self, event: Any) -> bool:
        """回复一条 ✅ 文本作为转发回执。"""
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            chain = MessageChain(chain=[Plain(text=MARK_TEXT_OK)])
            await event.send(chain)
            # 回执也是 Bot 自己发的消息：登记为「刚发出」，平台回传时不再转发进 MC
            self.mark_outbound(send_target_key(event_session_ref(event)), MARK_TEXT_OK)
            return True
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 转发回执发送失败: {exc}")
            return False
