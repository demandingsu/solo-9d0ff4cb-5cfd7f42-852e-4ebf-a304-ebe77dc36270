"""丝印工坊 FastAPI 出片服务。

业务规则：
- 每单画布固定尺寸，每边最多 512 格。
- 整稿按从底到顶提交红黑矩形；重叠格归上层，空白不印；跨色遮挡按全局图层处理。
- 改稿只给“可印格集合”发生变化的色版换新编号并清签样；改回旧图样同样发新号。
- 签样只认该色当前编号；两色都签齐当前编号后才能打包下载两张色版。
- 改稿在单事务内完成，失败回滚，原校样和签样照常可用。
"""
from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager, closing

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse, Response

from . import config, imaging
from .database import connect, init_db, transaction
from .schemas import (
    ManuscriptIn,
    ManuscriptOut,
    PlateOut,
    RevisionIn,
    RevisionOut,
    RevisionResultPlate,
    SignIn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="丝印工坊出片服务",
    version="1.0.0",
    description=(
        "红黑两色丝网印刷出片：整稿提交、按可印格变化换号、签样校验、两色签齐打包。\n\n"
        "在本页（/docs）可直接演示全部接口。"
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 工具函数

def _get_manuscript(conn, manuscript_id: int) -> dict:
    row = conn.execute(
        "SELECT id, name, width, height, created_at FROM manuscripts WHERE id = ?",
        (manuscript_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="稿件不存在")
    return dict(row)


def _get_plate(conn, manuscript_id: int, color: str) -> dict:
    row = conn.execute(
        "SELECT manuscript_id, color, version, bitmap FROM plates "
        "WHERE manuscript_id = ? AND color = ?",
        (manuscript_id, color),
    ).fetchone()
    if row is None or row["bitmap"] is None or row["version"] == 0:
        raise HTTPException(status_code=409, detail="该色版尚未出片，请先提交整稿")
    return dict(row)


def _validate_color(color: str) -> str:
    if color not in config.COLORS:
        raise HTTPException(status_code=422, detail="color 只能是 red 或 black")
    return color


def _plate_view(conn, manuscript_id: int, color: str) -> PlateOut:
    row = conn.execute(
        "SELECT p.color, p.version, p.bitmap, s.version AS signed_version, "
        "s.inspector, s.signed_at "
        "FROM plates p LEFT JOIN signatures s "
        "ON s.manuscript_id = p.manuscript_id AND s.color = p.color "
        "WHERE p.manuscript_id = ? AND p.color = ?",
        (manuscript_id, color),
    ).fetchone()
    if row is None:
        # 尚未出片时给出占位视图
        return PlateOut(
            color=color, version=0, printable_cells=0,
            signed=False, signed_version=None, inspector=None, signed_at=None,
        )
    d = dict(row)
    signed = d["signed_version"] is not None and d["signed_version"] == d["version"]
    return PlateOut(
        color=d["color"],
        version=d["version"],
        printable_cells=imaging.printable_count(d["bitmap"]) if d["bitmap"] else 0,
        signed=signed,
        signed_version=d["signed_version"] if signed else None,
        inspector=d["inspector"] if signed else None,
        signed_at=d["signed_at"] if signed else None,
    )


# ---------------------------------------------------------------- 基础端点

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 稿件

@app.post("/manuscripts", status_code=201, tags=["稿件"])
def create_manuscript(body: ManuscriptIn):
    with closing(connect()) as conn, transaction(conn):
        cur = conn.execute(
            "INSERT INTO manuscripts (name, width, height) VALUES (?, ?, ?)",
            (body.name, body.width, body.height),
        )
        mid = cur.lastrowid
        for color in config.COLORS:
            conn.execute(
                "INSERT INTO plates (manuscript_id, color, version, bitmap) "
                "VALUES (?, ?, 0, NULL)",
                (mid, color),
            )
    return {"id": mid, "name": body.name, "width": body.width, "height": body.height}


@app.get("/manuscripts", tags=["稿件"])
def list_manuscripts():
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT m.id, m.name, m.width, m.height, m.created_at, "
            "(SELECT COALESCE(MAX(seq), 0) FROM revisions r WHERE r.manuscript_id = m.id) AS current_revision_seq "
            "FROM manuscripts m ORDER BY m.id"
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/manuscripts/{manuscript_id}", response_model=ManuscriptOut, tags=["稿件"])
def get_manuscript(manuscript_id: int):
    with closing(connect()) as conn:
        m = _get_manuscript(conn, manuscript_id)
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM revisions WHERE manuscript_id = ?",
            (manuscript_id,),
        ).fetchone()["s"]
        plates = [_plate_view(conn, manuscript_id, c) for c in config.COLORS]
        return ManuscriptOut(
            id=m["id"], name=m["name"], width=m["width"], height=m["height"],
            created_at=m["created_at"], current_revision_seq=seq, plates=plates,
        )


# ---------------------------------------------------------------- 整稿 / 改稿

@app.post("/manuscripts/{manuscript_id}/revisions", response_model=RevisionOut,
          status_code=201, tags=["整稿/改稿"])
def submit_revision(manuscript_id: int, body: RevisionIn):
    with closing(connect()) as conn, transaction(conn):
        m = _get_manuscript(conn, manuscript_id)
        width, height = m["width"], m["height"]

        # 输入校验：任何矩形越界整个提交失败并回滚，旧校样与签样不受影响
        rects = []
        for i, r in enumerate(body.rectangles):
            if r.x + r.w > width or r.y + r.h > height:
                raise HTTPException(
                    status_code=422,
                    detail=(f"第 {i} 个矩形越界：画布 {width}x{height}，"
                            f"矩形 ({r.x},{r.y}) {r.w}x{r.h}"),
                )
            rects.append(r.model_dump())

        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM revisions "
            "WHERE manuscript_id = ?",
            (manuscript_id,),
        ).fetchone()
        next_seq = seq_row["next_seq"]

        rev_cur = conn.execute(
            "INSERT INTO revisions (manuscript_id, seq, note) VALUES (?, ?, ?)",
            (manuscript_id, next_seq, body.note),
        )
        revision_id = rev_cur.lastrowid
        for layer, r in enumerate(rects):
            conn.execute(
                "INSERT INTO rectangles (revision_id, color, layer, x, y, w, h) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (revision_id, r["color"], layer, r["x"], r["y"], r["w"], r["h"]),
            )

        # 全局图层栅格化：红黑矩形按同一从底到顶序列处理遮挡
        bitmaps = imaging.rasterize(width, height, rects)

        results = []
        for color in config.COLORS:
            new_bitmap = bitmaps[color]
            old = conn.execute(
                "SELECT version, bitmap FROM plates WHERE manuscript_id = ? AND color = ?",
                (manuscript_id, color),
            ).fetchone()
            old_version, old_bitmap = old["version"], old["bitmap"]
            # 只按可印格集合比较；改回旧图样位图相同，但版本仍递增 -> 发新号
            changed = old_bitmap != new_bitmap
            new_version = old_version + 1 if changed else old_version
            conn.execute(
                "UPDATE plates SET version = ?, bitmap = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE manuscript_id = ? AND color = ?",
                (new_version, new_bitmap, manuscript_id, color),
            )
            signed_cleared = False
            if changed:
                cur = conn.execute(
                    "DELETE FROM signatures WHERE manuscript_id = ? AND color = ?",
                    (manuscript_id, color),
                )
                signed_cleared = cur.rowcount > 0
            results.append(RevisionResultPlate(
                color=color,
                old_version=old_version,
                new_version=new_version,
                changed=changed,
                signed_cleared=signed_cleared,
                printable_cells=imaging.printable_count(new_bitmap),
            ))

        return RevisionOut(
            manuscript_id=manuscript_id,
            revision_id=revision_id,
            seq=next_seq,
            plates=results,
        )


# ---------------------------------------------------------------- 校样

@app.get("/manuscripts/{manuscript_id}/plates/{color}/proof", tags=["校样"])
def get_proof(manuscript_id: int, color: str,
              scale: int = Query(1, ge=1, le=16, description="每格放大倍数")):
    _validate_color(color)
    with closing(connect()) as conn:
        _get_manuscript(conn, manuscript_id)
        plate = _get_plate(conn, manuscript_id, color)
        m = conn.execute(
            "SELECT width, height FROM manuscripts WHERE id = ?",
            (manuscript_id,),
        ).fetchone()
        png = imaging.render_png(plate["bitmap"], m["width"], m["height"], scale)
        return Response(
            content=png,
            media_type="image/png",
            headers={
                "Content-Disposition": (
                    f'inline; filename="m{manuscript_id}_{color}_v{plate["version"]}.png"'
                ),
                "X-Plate-Color": color,
                "X-Plate-Version": str(plate["version"]),
            },
        )


# ---------------------------------------------------------------- 签样

@app.post("/manuscripts/{manuscript_id}/plates/{color}/sign",
          status_code=201, tags=["签样"])
def sign_plate(manuscript_id: int, color: str, body: SignIn):
    _validate_color(color)
    with closing(connect()) as conn, transaction(conn):
        _get_manuscript(conn, manuscript_id)
        plate = _get_plate(conn, manuscript_id, color)
        current_version = plate["version"]
        if body.version != current_version:
            raise HTTPException(
                status_code=409,
                detail=(f"编号不匹配：当前 {color} 版编号为 {current_version}，"
                        f"提交的是 {body.version}；迟到签样拒收，请核对最新校样"),
            )
        # 同色重复签当前编号视为更新签样人
        conn.execute(
            "INSERT INTO signatures (manuscript_id, color, version, inspector) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(manuscript_id, color) DO UPDATE SET "
            "version = excluded.version, inspector = excluded.inspector, "
            "signed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (manuscript_id, color, current_version, body.inspector),
        )
        return {
            "manuscript_id": manuscript_id,
            "color": color,
            "version": current_version,
            "inspector": body.inspector,
            "status": "signed",
        }


# ---------------------------------------------------------------- 打包

@app.get("/manuscripts/{manuscript_id}/package", tags=["打包下载"])
def download_package(manuscript_id: int):
    with closing(connect()) as conn:
        m = _get_manuscript(conn, manuscript_id)
        missing = []
        plate_data = []
        for color in config.COLORS:
            plate = _get_plate(conn, manuscript_id, color)
            sig = conn.execute(
                "SELECT version FROM signatures WHERE manuscript_id = ? AND color = ?",
                (manuscript_id, color),
            ).fetchone()
            if sig is None or sig["version"] != plate["version"]:
                missing.append(color)
                continue
            plate_data.append((color, plate))
        if missing:
            raise HTTPException(
                status_code=409,
                detail=f"以下色版未签齐当前编号，禁止打包：{', '.join(missing)}",
            )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for color, plate in plate_data:
                png = imaging.render_png(
                    plate["bitmap"], m["width"], m["height"], scale=1
                )
                zf.writestr(
                    f"manuscript_{manuscript_id}/{color}_v{plate['version']}.png", png
                )
            manifest = (
                f"manuscript_id={manuscript_id}\nname={m['name']}\n"
                f"canvas={m['width']}x{m['height']}\n"
                + "\n".join(
                    f"{c}_version={p['version']}" for c, p in plate_data
                )
                + "\n"
            )
            zf.writestr(f"manuscript_{manuscript_id}/manifest.txt", manifest)
        payload = buf.getvalue()
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="manuscript_{manuscript_id}_package.zip"'
                )
            },
        )
