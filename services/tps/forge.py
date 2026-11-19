"""Forge 的 forge tps 输出。

Forge 1.21 仍沿用“Overall: Mean tick time: 4.424 ms. Mean TPS: 18.750”
（维度行为“Dim ...: Mean tick time: ... Mean TPS: ...”）；与 NeoForge 1.21
的“Overall: 20.000 TPS (6.167 ms/tick)”不是同一格式，故不共用解析。
"""

from __future__ import annotations

import re

from .base import TpsProvider, TpsValues, normalize_command, tps_float

# 主世界维度 ID 直接作为参数：forge tps minecraft:overworld。
# dim 只是 Brigadier 的参数名，不是要输入的字面量。
_OVERWORLD = "minecraft:overworld"
COMMANDS = (f"forge tps {_OVERWORLD}", "forge tps")

_OVERALL_RE = re.compile(
    r"Overall\s*:[^\r\n]*?Mean\s+TPS\s*:\s*([\d.]+)", re.IGNORECASE
)
_DIMENSION_RE = re.compile(r"Mean\s+TPS\s*:\s*([\d.]+)", re.IGNORECASE)


def parse(text: str) -> TpsValues | None:
    # 优先 Overall 汇总行；输出被截断到 Overall 之前时取首条维度行
    matched = _OVERALL_RE.search(text) or _DIMENSION_RE.search(text)
    if matched is None:
        return None
    value = tps_float(matched.group(1))
    return (value, None, None) if value is not None else None


def _brand_from_command(command: str) -> str | None:
    return "forge" if normalize_command(command) in COMMANDS else None


PROVIDER = TpsProvider(
    name="forge",
    brands=("forge",),
    commands=COMMANDS,
    parse=parse,
    long_commands=("forge tps",),
    brand_from_command=_brand_from_command,
)
