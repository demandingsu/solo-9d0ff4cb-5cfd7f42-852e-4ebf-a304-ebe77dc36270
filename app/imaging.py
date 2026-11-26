"""栅格化、位图与黑白校样 PNG。

图层语义（关键）：
- 一次整稿提交的红黑矩形共享一条从底到顶的图层序列，layer 小者在底。
- 同一格被多个矩形覆盖时，只归最上层矩形的色版所有。
  因此红色图块压住黑版时，被压格不属于黑版；红块移开后黑版可印格会变化。
- 没有任何矩形覆盖的格为空白，任何色版都不印。
位图采用行优先、每格 1 bit、MSB-first，末尾补 0，便于直接逐字节比较。
"""
from __future__ import annotations

import io
from array import array

from PIL import Image

from .config import COLORS


def rasterize(width: int, height: int, rectangles: list[dict]) -> dict[str, bytes]:
    """把整稿矩形栅格化为 {color: bitmap}。

    rectangles: 已按从底到顶排序的矩形 dict，含 color/x/y/w/h。
    """
    n = width * height
    # 每格当前最上层的 layer 序号，-1 表示空白
    owner = array("i", [-1] * n)
    color_of = bytearray(n)  # 0=red, 1=black（仅 owner>=0 时有意义）
    color_index = {c: i for i, c in enumerate(COLORS)}

    for layer, rect in enumerate(rectangles):
        ci = color_index[rect["color"]]
        x0, y0 = rect["x"], rect["y"]
        x1, y1 = x0 + rect["w"], y0 + rect["h"]
        # 行级切片填充：后画的 layer 更大，直接覆盖同列格
        owner_span = array("i", [layer]) * rect["w"]
        color_span = bytearray([ci]) * rect["w"]
        for y in range(y0, y1):
            start = y * width + x0
            owner[start:start + rect["w"]] = owner_span
            color_of[start:start + rect["w"]] = color_span

    out: dict[str, bytes] = {}
    byte_len = (n + 7) // 8
    for ci, color in enumerate(COLORS):
        buf = bytearray(byte_len)
        for idx in range(n):
            if owner[idx] >= 0 and color_of[idx] == ci:
                buf[idx >> 3] |= 0x80 >> (idx & 7)
        out[color] = bytes(buf)
    return out


def render_png(bitmap: bytes, width: int, height: int, scale: int = 1) -> bytes:
    """把色部位图渲染为黑白 PNG：可印格黑、空白格白。"""
    n = width * height
    pixels = bytearray(b"\xff") * n  # 默认白
    for idx in range(n):
        if bitmap[idx >> 3] & (0x80 >> (idx & 7)):
            pixels[idx] = 0  # 可印 -> 黑
    img = Image.frombytes("L", (width, height), bytes(pixels))
    if scale > 1:
        img = img.resize((width * scale, height * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def printable_count(bitmap: bytes) -> int:
    """可印格数量，供接口展示与测试断言。"""
    return sum(bin(b).count("1") for b in bitmap)
