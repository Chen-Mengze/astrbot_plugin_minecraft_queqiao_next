"""Bukkit 系服务端（Paper / Spigot / Purpur / Folia 等）的 tps 输出。"""

from __future__ import annotations

import re

from .base import TpsProvider, TpsValues, tps_float, tps_values

# “TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0”；窗口列表可能更长
# （1m, 5m, 15m, 1h, 6h, 24h），值可能带 *（采样未满）
_TPS_RE = re.compile(
    r"TPS\s+from\s+last\s+[^:\r\n]*:\s*([\d.]+)\*?\s*,\s*([\d.]+)\*?\s*,"
    r"\s*([\d.]+)\*?",
    re.IGNORECASE,
)
# 无标准前缀的旧版裸三数：必须处于 TPS 语境且数值在合理区间，
# 避免把玩家名 / 其它数字行误判为 TPS
_TPS_BARE_RE = re.compile(r"([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)")
_TPS_VALUE_MIN = 0.0
_TPS_VALUE_MAX = 20.5  # TPS 上限 20，留一点四舍五入余量


def _parse_bare(matched: re.Match[str]) -> TpsValues | None:
    first, second, third = (tps_float(raw) for raw in matched.groups())
    if first is None or second is None or third is None:
        return None
    if not all(
        _TPS_VALUE_MIN <= value <= _TPS_VALUE_MAX for value in (first, second, third)
    ):
        return None
    return (first, second, third)


def parse(text: str) -> TpsValues | None:
    matched = _TPS_RE.search(text)
    if matched is not None:
        values = tps_values(*matched.groups())
        if values is not None:
            return values
    if "TPS" not in text.upper():
        return None
    bare = _TPS_BARE_RE.search(text)
    if bare is None:
        return None
    return _parse_bare(bare)


PROVIDER = TpsProvider(
    name="bukkit",
    brands=(
        "bukkit",
        "spigot",
        "paper",
        "purpur",
        "pufferfish",
        "folia",
        "leaves",
        "sponge",
    ),
    commands=("tps",),
    parse=parse,
)
