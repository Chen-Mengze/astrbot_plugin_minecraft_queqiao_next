"""TPS 数据源注册表：采集器与 Web API 只依赖这里的公共入口。

加载器专属的指令与输出解析在 bukkit / spark / forge / neoforge 子模块里；
新增一个加载器时新建模块并在 PROVIDERS 注册即可。
"""

from __future__ import annotations

from ...core.constants import strip_format_codes
from ..slp_ping import brand_from_text
from . import bukkit, forge, neoforge, spark
from .base import TpsProvider, TpsValues, normalize_command

# 解析兜底顺序：已知品牌优先，其余按此顺序尝试
PROVIDERS: tuple[TpsProvider, ...] = (
    bukkit.PROVIDER,
    spark.PROVIDER,
    forge.PROVIDER,
    neoforge.PROVIDER,
)

# 品牌识别失败 / 品牌指令失效时的通用兜底（去重后追加在品牌候选之后）
_AUTO_FALLBACK_COMMANDS = ("tps", "forge tps", "spark tps")

_BRAND_TO_PROVIDER: dict[str, TpsProvider] = {
    brand: provider for provider in PROVIDERS for brand in provider.brands
}


def provider_for_brand(brand: str | None) -> TpsProvider | None:
    """品牌关键字 → 对应加载器模块；未知品牌返回 None。"""
    return _BRAND_TO_PROVIDER.get(brand) if brand else None


def parse_tps(
    output: str | None, server_type: str | None = None
) -> TpsValues | None:
    """解析 tps 指令输出为 (1m, 5m, 15m)；无法解析返回 None。

    server_type 为识别到的服务端品牌文本（如“Forge 1.21.4”）；已知品牌时
    先试对应加载器的解析器，再按注册顺序兜底。Forge / NeoForge 的输出格式
    不同，各自模块只认自己的输出。
    """
    if not output:
        return None
    # 颜色码可能嵌在数字/关键字中间（RCON 回传带 § 或 ANSI），先统一剥离
    text = strip_format_codes(output).strip()
    brand = brand_from_text(server_type) if server_type else None
    preferred = provider_for_brand(brand)
    ordered = [provider for provider in PROVIDERS if provider is not preferred]
    if preferred is not None:
        ordered.insert(0, preferred)
    for provider in ordered:
        values = provider.parse(text)
        if values is not None:
            return values
    return None


def tps_command_candidates(server_type: str | None, preference: str) -> list[str]:
    """auto 模式下按服务端品牌给出候选 TPS 指令（按尝试顺序）。

    - NeoForge：neoforge tps <主世界> → forge tps <主世界> → 完整指令
    - Forge：forge tps <主世界> → forge tps
    - Fabric/Quilt：spark tps
    - Bukkit 系：tps
    - 未知品牌：从通用兜底 tps 起步

    Forge/NeoForge 优先把主世界维度 ID 作为参数（forge tps minecraft:overworld，
    注意 dim 只是命令树的参数名，不是要输入的字面量）：完整 tps 输出会列出
    全部维度，大型整合包可达数 KB，超过单个 RCON 响应包上限后会被拆包，部分
    客户端只读首个回包，余包会串到下一条指令。单维度回包只有一行，不触发这个
    问题；不支持维度参数时再回退完整指令。

    末尾统一追加通用兜底指令（去重）：采集侧执行到能解析出数值的指令为止，
    品牌识别错误时仍能采到 TPS。显式填写具体指令时只返回该指令，不做自动
    回退，保证行为可预期。
    """
    pref = (preference or "").strip()
    if pref and pref.lower() != "auto":
        return [pref]
    brand = brand_from_text(server_type) if server_type else None
    provider = provider_for_brand(brand)
    first = list(provider.commands) if provider is not None else []
    return first + [
        command for command in _AUTO_FALLBACK_COMMANDS if command not in first
    ]


def resolve_tps_command(server_type: str | None, preference: str) -> str:
    """解析 auto 模式下优先执行的 TPS 指令（候选列表首个）。"""
    return tps_command_candidates(server_type, preference)[0]


def is_long_tps_command(command: str) -> bool:
    """是否为会产生长输出的完整 tps 指令（缓存时不再优先复用）。"""
    normalized = normalize_command(command)
    return any(normalized in provider.long_commands for provider in PROVIDERS)


def brand_from_tps_command(
    command: str, server_type: str | None = None
) -> str | None:
    """从实测成功的 TPS 指令反推服务端品牌；无法唯一确定时返回 None。

    server_type 已知时优先按它返回品牌：NeoForge 会接受兼容的 forge tps，
    成功后不应把品牌降级成 Forge。
    """
    normalized = normalize_command(command)
    known = brand_from_text(server_type) if server_type else None
    preferred = provider_for_brand(known)
    if known and preferred is not None and normalized in preferred.commands:
        return known
    for provider in PROVIDERS:
        if provider.brand_from_command is None:
            continue
        brand = provider.brand_from_command(command)
        if brand:
            return brand
    return None


__all__ = [
    "TpsProvider",
    "TpsValues",
    "brand_from_tps_command",
    "is_long_tps_command",
    "parse_tps",
    "provider_for_brand",
    "resolve_tps_command",
    "tps_command_candidates",
]
