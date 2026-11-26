"""请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Color = Literal["red", "black"]


class RectangleIn(BaseModel):
    color: Color
    # 左上角格坐标（0 起算），w/h 为格数，必须为正且不越界
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)


class RevisionIn(BaseModel):
    # 按从底到顶排列的红黑矩形；重叠格归数组中靠后的矩形
    rectangles: list[RectangleIn] = Field(default_factory=list)
    note: Optional[str] = None


class ManuscriptIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    width: int = Field(ge=1, le=512)
    height: int = Field(ge=1, le=512)


class SignIn(BaseModel):
    # 客户端必须声明它认可的色版编号；与当前编号不一致即拒绝
    version: int = Field(ge=1)
    inspector: str = Field(min_length=1, max_length=100)


class PlateOut(BaseModel):
    color: Color
    version: int
    printable_cells: int
    signed: bool
    signed_version: Optional[int] = None
    inspector: Optional[str] = None
    signed_at: Optional[str] = None


class RevisionResultPlate(BaseModel):
    color: Color
    old_version: int
    new_version: int
    changed: bool
    signed_cleared: bool
    printable_cells: int


class RevisionOut(BaseModel):
    manuscript_id: int
    revision_id: int
    seq: int
    plates: list[RevisionResultPlate]


class ManuscriptOut(BaseModel):
    id: int
    name: str
    width: int
    height: int
    created_at: str
    current_revision_seq: int
    plates: list[PlateOut]
