"""会话标识（UMO）解析、匹配与去重。

AstrBot 的 UMO（``unified_msg_origin``）格式为 ``平台ID:消息类型:会话ID``，
插件配置里的 ``target_sessions`` 一般直接抄 ``/sid`` 的输出。但有两件事会让
「整串 UMO 精确匹配」变得脆弱：

1. **隔离对话（``platform_settings.unique_session``）**：AstrBot 会把群消息的
   会话 ID 改写成 ``发送者ID_群ID``（见 ``core/event_bus.py`` 的
   ``UNIQUE_SESSION_ID_BUILDERS``，如 ``aiocqhttp`` / ``slack`` / ``qq_official``
   / ``matrix``）。于是**同一个群里每个成员的 UMO 都不同**：
   ``平台:GroupMessage:1507157577_297223059`` 只匹配发送者 1507157577，
   其他成员的消息全部漏掉；为了「谁发都能转发」，用户只能把每个成员的 UMO
   都抄一遍，而 MC → 群方向又会因为多条 UMO 指向同一个群而**重复发送**。
2. **平台/编号写法差异**：手写 UMO 容易只写 ``平台:群号``，或者干脆只写群号。

本模块把配置项与事件都归一化成 :class:`SessionRef`：

- 匹配用「会话通道」而不是整串 UMO：事件侧优先取 AstrBot 给出的真实群号
  （``event.get_group_id()``，隔离对话下也始终是群号），拿不到时按
  ``发送者ID_群ID`` 约定退化为最后一段（仅纯数字复合形态）；
- 去重同理：多条配置项推导出同一个会话通道时只发送一次；
- 发送仍使用**用户填写的原始 UMO**（隔离对话的复合会话 ID 由平台适配器
  自己拆分），不擅自改写，保证既有配置行为不变。

任何函数都不依赖 astrbot 运行时，可直接离线单测。
"""

import re
from dataclasses import dataclass
from typing import Any

GROUP_MESSAGE = "GroupMessage"
FRIEND_MESSAGE = "FriendMessage"
OTHER_MESSAGE = "OtherMessage"

_KNOWN_TYPES = (GROUP_MESSAGE, FRIEND_MESSAGE, OTHER_MESSAGE)

# 隔离对话的复合会话 ID：``发送者ID_群ID``。只在「两段都是纯数字」时认定，
# 避免把本身带下划线的会话 ID（如 Lark 的 ``oc_xxx``）误拆分——那会把两个
# 不同的会话当成同一个。
_NUMERIC_COMPOSITE_RE = re.compile(r"^(?P<sender>[0-9]+)_(?P<group>[0-9]+)$")


def normalize_message_type(value: Any) -> str:
    """把 ``MessageType`` 枚举 / 字符串归一为 ``GroupMessage`` 这类短名。

    未知取值原样返回（只用于相等比较，不做语义推断）；``None`` / 空串返回空串。
    """
    if value is None:
        return ""
    name = getattr(value, "value", value)
    text = str(name or "").strip()
    if not text:
        return ""
    # 兼容 "MessageType.GROUP_MESSAGE" / "GROUP_MESSAGE" 这类字符串形态
    primary = text.rsplit(".", 1)[-1].replace("_", "")
    for known in _KNOWN_TYPES:
        if text.lower() == known.lower() or primary.lower() == known.lower():
            return known
    return text


def split_umo(raw: str) -> tuple[str, str, str]:
    """把 UMO 拆成 ``(平台ID, 消息类型, 会话ID)``，缺失段返回空串。

    只按前两个冒号切分：会话 ID 自身可能带冒号（如 Matrix 房间 ID
    ``!room:server``），必须完整保留在第三段。
    """
    text = str(raw or "").strip()
    if not text:
        return "", "", ""
    parts = text.split(":", 2)
    if len(parts) == 1:
        return "", "", text
    if len(parts) == 2:
        # ``平台:会话ID`` 简写（未写消息类型）
        return parts[0].strip(), "", parts[1].strip()
    return parts[0].strip(), normalize_message_type(parts[1]), parts[2].strip()


def composite_group_id(message_type: str, session_id: str) -> str:
    """复合会话 ID → 真实群号；不是已知复合形态返回空串。

    仅对「群消息 + 纯数字_纯数字」生效，即 AstrBot 隔离对话对 QQ 系平台的
    确定形态（``unique_session`` 的 ``{sender_id}_{group_id}``）。
    """
    if message_type != GROUP_MESSAGE or not session_id:
        return ""
    matched = _NUMERIC_COMPOSITE_RE.match(session_id)
    return matched.group("group") if matched else ""


@dataclass(frozen=True)
class SessionRef:
    """一个会话（UMO）的归一化描述。

    - ``raw``：用户填写 / 事件给出的原始字符串（发送时原样使用）
    - ``real_group_id``：事件侧 AstrBot 给出的真实群号，隔离对话下唯一可信
      的群标识；配置项一侧恒为空
    """

    raw: str = ""
    platform: str = ""
    message_type: str = ""
    session_id: str = ""
    real_group_id: str = ""

    @property
    def derived_group_id(self) -> str:
        """从复合会话 ID 推断的群号（仅纯数字形态）。"""
        return composite_group_id(self.message_type, self.session_id)

    @property
    def channel(self) -> str:
        """匹配 / 去重用的会话通道：真实群号 → 复合推断群号 → 会话 ID。"""
        return self.real_group_id or self.derived_group_id or self.session_id

    @property
    def is_full(self) -> bool:
        """是否为可直接发送的完整 UMO（平台 + 类型 + 会话 ID）。"""
        return bool(self.platform and self.message_type and self.session_id)

    @property
    def canonical(self) -> str:
        """规范 UMO：群消息收敛到真实群号，避免把发送者 ID 写进配置。

        事件侧有真实群号时用它（隔离对话下每个成员都会收敛到同一个群）；
        否则保留原始写法（不认识的平台不敢擅自改写）。
        """
        if self.platform and self.message_type:
            channel = self.channel
            if channel:
                return f"{self.platform}:{self.message_type}:{channel}"
        return self.raw


def parse_session_entry(raw: Any) -> SessionRef:
    """解析配置项（``target_sessions`` 的一条）为 :class:`SessionRef`。"""
    text = str(raw or "").strip()
    platform, message_type, session_id = split_umo(text)
    return SessionRef(
        raw=text,
        platform=platform,
        message_type=message_type,
        session_id=session_id,
    )


def _call_value(event: Any, name: str) -> Any:
    """防御式调用事件方法并返回原始值：缺方法 / 抛异常时返回 None。"""
    func = getattr(event, name, None)
    if not callable(func):
        return None
    try:
        return func()
    except Exception:  # noqa: BLE001 - 事件桩/异常实现都按缺失处理
        return None


def _call(event: Any, name: str) -> str:
    """防御式调用事件方法并转成去空白字符串。"""
    value = _call_value(event, name)
    return str(value or "").strip()


def event_session_ref(event: Any) -> SessionRef:
    """从 ``AstrMessageEvent`` 提取归一化会话信息。

    优先使用事件对象的方法（``get_platform_id`` / ``get_message_type`` /
    ``get_session_id`` / ``get_group_id``），缺失时回退解析 UMO 字符串，
    因此旧版 AstrBot 与离线测试桩都能直接使用。
    """
    raw = str(getattr(event, "unified_msg_origin", "") or "").strip()
    platform, message_type, session_id = split_umo(raw)
    platform = _call(event, "get_platform_id") or platform
    # 不能先转 str：真实事件返回 MessageType 枚举，str() 会变成
    # "MessageType.GROUP_MESSAGE"，必须把原始值交给 normalize_message_type
    message_type = (
        normalize_message_type(_call_value(event, "get_message_type")) or message_type
    )
    session_id = _call(event, "get_session_id") or session_id
    real_group_id = _call(event, "get_group_id")
    return SessionRef(
        raw=raw,
        platform=platform,
        message_type=message_type,
        session_id=session_id,
        real_group_id=real_group_id,
    )


def session_matches(target: SessionRef, event: SessionRef) -> bool:
    """判断配置项 ``target`` 是否指向事件 ``event`` 所在的会话。

    匹配顺序（从精确到宽松）：

    1. 整串 UMO 全等（最常见配置写法）；
    2. 平台 / 消息类型过滤（任一侧缺失视为通配）；
    3. 会话通道相等——覆盖隔离对话「同一群、不同发送者」与短写法；
    4. 会话 ID 全等 / 目标就是事件真实群号 / 目标以 ``_群号`` 结尾。
    """
    if not target.raw:
        return False
    if target.raw == event.raw:
        return True
    if (
        target.platform
        and event.platform
        and target.platform.lower() != event.platform.lower()
    ):
        return False
    if (
        target.message_type
        and event.message_type
        and target.message_type != event.message_type
    ):
        return False

    target_channel = target.channel
    event_channel = event.channel
    if target_channel and event_channel and target_channel == event_channel:
        return True
    if target.session_id and target.session_id == event.session_id:
        return True
    if event.real_group_id:
        if target.session_id == event.real_group_id:
            return True
        if (
            target.message_type == GROUP_MESSAGE
            and target.session_id.endswith("_" + event.real_group_id)
        ):
            return True
    return False


def send_target_key(ref: SessionRef) -> str:
    """发送 / 回声识别用的去重键：同一会话的多条 UMO 得到同一个键。"""
    return f"{(ref.platform or '').lower()}|{ref.message_type}|{ref.channel}"
