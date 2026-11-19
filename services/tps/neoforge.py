"""NeoForge 的 neoforge tps 输出。

NeoForge 1.20.4 及更早沿用 Forge 的“Overall: Mean tick time: ... Mean TPS: ...”；
1.21 起改为“Overall: 20.000 TPS (6.167 ms/tick)”。两代格式都在本模块解析，
不依赖 Forge 模块——两个加载器的输出已经分叉，不能共用一套正则。
"""

from __future__ import annotations

import re

from .base import TpsProvider, TpsValues, normalize_command, tps_float

# NeoForge 1.21 的命令参数名是 dimension（1.20.4 为 dim），但都是把维度
# ID 直接作为参数；旧版 NeoForge 也接受兼容的 forge tps，完整指令仅作兜底。
_OVERWORLD = "minecraft:overworld"
COMMANDS = (
    f"neoforge tps {_OVERWORLD}",
    f"forge tps {_OVERWORLD}",
    "neoforge tps",
    "forge tps",
)

# 新版（1.21+）：Overall: 20.000 TPS (6.167 ms/tick)
_OVERALL_RE = re.compile(r"Overall\s*:[^\r\n]*?([\d.]+)\s*TPS\b", re.IGNORECASE)
# 维度行标签可能带命名空间冒号（gensokyolegacy:gensokyo:），
# 从最后一个合法分隔处取“数值 + TPS”
_DIMENSION_RE = re.compile(r"[^\r\n:]*:\s*([\d.]+)\s*TPS\b", re.IGNORECASE)
# 旧版（<=1.20.4，沿用 Forge 文本）
_LEGACY_OVERALL_RE = re.compile(
    r"Overall\s*:[^\r\n]*?Mean\s+TPS\s*:\s*([\d.]+)", re.IGNORECASE
)
_LEGACY_DIMENSION_RE = re.compile(r"Mean\s+TPS\s*:\s*([\d.]+)", re.IGNORECASE)


def parse(text: str) -> TpsValues | None:
    # Overall 汇总行优先：新版 → 旧版
    for pattern in (_OVERALL_RE, _LEGACY_OVERALL_RE):
        matched = pattern.search(text)
        if matched is None:
            continue
        value = tps_float(matched.group(1))
        if value is not None:
            return (value, None, None)
    # 输出被截断到 Overall 之前：取首条维度行
    for pattern in (_DIMENSION_RE, _LEGACY_DIMENSION_RE):
        matched = pattern.search(text)
        if matched is None:
            continue
        value = tps_float(matched.group(1))
        if value is not None:
            return (value, None, None)
    return None


def _brand_from_command(command: str) -> str | None:
    normalized = normalize_command(command)
    if normalized == "neoforge tps" or normalized.startswith("neoforge tps "):
        return "neoforge"
    return None


PROVIDER = TpsProvider(
    name="neoforge",
    brands=("neoforge",),
    commands=COMMANDS,
    parse=parse,
    long_commands=("neoforge tps", "forge tps"),
    brand_from_command=_brand_from_command,
)
