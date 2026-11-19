"""TPS 数据源抽象：每个加载器一个模块，各自持有指令与输出解析。

为什么拆开：各服务端核心的 tps 输出由各自维护、会随版本演进，而且彼此
并不一致——Forge 1.21 仍是“Overall: Mean tick time: ... Mean TPS: ...”，
NeoForge 1.21 则改成“Overall: 20.000 TPS (6.167 ms/tick)”。把指令与解析
放进加载器各自模块，适配某个加载器时只改对应文件，采集器与其它加载器都
不受影响。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: (1m, 5m, 15m)；只有单值均值的加载器后两档为 None
TpsValues = tuple[float | None, float | None, float | None]
TpsParser = Callable[[str], TpsValues | None]
BrandResolver = Callable[[str], str | None]


def normalize_command(command: str) -> str:
    """指令归一化（小写 + 折叠空白），用于候选去重与实测指令反推品牌。"""
    return " ".join((command or "").lower().split())


def tps_float(raw: str) -> float | None:
    """把正则捕获的数字转为 float；捕获内容非法时返回 None（不抛异常）。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def tps_values(*raw_values: str) -> TpsValues | None:
    """把捕获到的三档数值转成 TpsValues；任一档非法返回 None。"""
    first, second, third = (tps_float(raw) for raw in raw_values)
    if first is None or second is None or third is None:
        return None
    return (first, second, third)


@dataclass(frozen=True)
class TpsProvider:
    """一个 TPS 数据源（服务端核心 / 加载器）的指令与解析定义。

    brands 是 brand_from_text() 可能返回的品牌关键字；命中后该模块成为
    auto 模式的首选。commands 为品牌专属候选（按尝试顺序，长输出放后面）；
    long_commands 中的指令在采集侧缓存时不再优先复用。
    """

    name: str
    brands: tuple[str, ...]
    commands: tuple[str, ...]
    parse: TpsParser
    long_commands: tuple[str, ...] = ()
    #: 从实测成功的指令反推品牌；None 表示该模块的指令无法唯一确定品牌
    brand_from_command: BrandResolver | None = None
