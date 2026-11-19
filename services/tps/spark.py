"""Spark 的 spark tps（Fabric / Quilt 没有内置 tps，须装 Spark）。

Spark 输出与 Bukkit 系同为“TPS from last ...”行，但窗口列表与 * 标记由
Spark 自己维护，这里独立解析，避免适配 Spark 时牵动 Bukkit 模块。
"""

from __future__ import annotations

import re

from .base import TpsProvider, TpsValues, tps_values

_SPARK_TPS_RE = re.compile(
    r"TPS\s+from\s+last\s+[^:\r\n]*:\s*([\d.]+)\*?\s*,\s*([\d.]+)\*?\s*,"
    r"\s*([\d.]+)\*?",
    re.IGNORECASE,
)


def parse(text: str) -> TpsValues | None:
    matched = _SPARK_TPS_RE.search(text)
    if matched is None:
        return None
    return tps_values(*matched.groups())


PROVIDER = TpsProvider(
    name="spark",
    brands=("fabric", "quilt"),
    commands=("spark tps",),
    parse=parse,
)
