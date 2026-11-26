"""端到端冒烟脚本：对运行中的服务走完整业务流程。

用法：python scripts/smoke_e2e.py [base_url]
"""
import io
import sys
import zipfile

import httpx
from PIL import Image

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def check(cond, msg):
    print(("  OK " if cond else " FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    c = httpx.Client(base_url=BASE, timeout=10)

    print("== 0. 文档与健康检查 ==")
    r = c.get("/docs")
    check(r.status_code == 200 and "Swagger UI" in r.text, "GET /docs 返回 Swagger UI")
    r = c.get("/health")
    check(r.status_code == 200 and r.json()["status"] == "ok", "GET /health")

    print("== 1. 建稿 8x8 ==")
    r = c.post("/manuscripts", json={"name": "冒烟稿", "width": 8, "height": 8})
    check(r.status_code == 201, f"建稿 201（实际 {r.status_code}）")
    mid = r.json()["id"]

    print("== 2. 首稿：黑底 4x4，红块压中央 2x2 ==")
    r = c.post(f"/manuscripts/{mid}/revisions", json={"rectangles": [
        {"color": "black", "x": 0, "y": 0, "w": 4, "h": 4},
        {"color": "red", "x": 1, "y": 1, "w": 2, "h": 2},
    ]})
    check(r.status_code == 201, "首稿提交成功")
    plates = {p["color"]: p for p in r.json()["plates"]}
    check(plates["red"]["new_version"] == 1, "红版编号=1")
    check(plates["black"]["new_version"] == 1 and plates["black"]["printable_cells"] == 12,
          "黑版编号=1 且被压后可印 12 格")

    print("== 3. 下载两张黑白校样并核对像素 ==")
    for color, expect_black in (("red", 4), ("black", 12)):
        r = c.get(f"/manuscripts/{mid}/plates/{color}/proof")
        img = Image.open(io.BytesIO(r.content))
        nblack = sum(1 for px in img.getdata() if px == 0)
        check(r.headers["x-plate-version"] == "1", f"{color} 校样版本头=1")
        check(nblack == expect_black, f"{color} 校样黑格 {nblack}=={expect_black}")

    print("== 4. 两色签 v1 并打包 ==")
    check(c.post(f"/manuscripts/{mid}/plates/red/sign",
                 json={"version": 1, "inspector": "甲"}).status_code == 201, "红版签 v1")
    check(c.post(f"/manuscripts/{mid}/plates/black/sign",
                 json={"version": 1, "inspector": "乙"}).status_code == 201, "黑版签 v1")
    r = c.get(f"/manuscripts/{mid}/package")
    check(r.status_code == 200, "两色签齐，打包成功")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    check(len([n for n in zf.namelist() if n.endswith(".png")]) == 2, "zip 内含两张色版 PNG")
    print("       包内文件：", zf.namelist())

    print("== 5. 改稿：红块移开 -> 遮挡变化 ==")
    r = c.post(f"/manuscripts/{mid}/revisions", json={"rectangles": [
        {"color": "black", "x": 0, "y": 0, "w": 4, "h": 4},
        {"color": "red", "x": 6, "y": 6, "w": 2, "h": 2},
    ]})
    plates = {p["color"]: p for p in r.json()["plates"]}
    check(plates["red"]["changed"] and plates["red"]["new_version"] == 2
          and plates["red"]["signed_cleared"], "红版换 v2 并清签")
    check(plates["black"]["changed"] and plates["black"]["new_version"] == 2
          and plates["black"]["printable_cells"] == 16,
          "红块移开后黑版恢复 16 格，换 v2 并清签")

    print("== 6. 迟到签样（仍拿 v1 来签）= 409 ==")
    r = c.post(f"/manuscripts/{mid}/plates/black/sign",
               json={"version": 1, "inspector": "乙"})
    check(r.status_code == 409, f"迟到 v1 签样被拒（实际 {r.status_code}）")
    print("       返回：", r.json()["detail"])
    r = c.get(f"/manuscripts/{mid}/package")
    check(r.status_code == 409, "未重新签齐，禁止打包")

    print("== 7. 按当前 v2 重新签齐并打包 ==")
    c.post(f"/manuscripts/{mid}/plates/red/sign", json={"version": 2, "inspector": "甲"})
    c.post(f"/manuscripts/{mid}/plates/black/sign", json={"version": 2, "inspector": "乙"})
    r = c.get(f"/manuscripts/{mid}/package")
    check(r.status_code == 200, "v2 两色签齐，打包成功")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    check(any("red_v2.png" in n for n in zf.namelist())
          and any("black_v2.png" in n for n in zf.namelist()), "包内文件名带 v2 编号")

    print("== 8. 改回首稿图样 -> 位图相同仍发新号 v3 ==")
    r = c.post(f"/manuscripts/{mid}/revisions", json={"rectangles": [
        {"color": "black", "x": 0, "y": 0, "w": 4, "h": 4},
        {"color": "red", "x": 1, "y": 1, "w": 2, "h": 2},
    ]})
    plates = {p["color"]: p for p in r.json()["plates"]}
    check(plates["red"]["new_version"] == 3 and plates["black"]["new_version"] == 3,
          "改回旧图样，两色均发新号 v3（不复用 v1）")
    check(all(p["signed_cleared"] for p in plates.values()), "旧 v2 签样全部清空")

    print("== 9. 改稿失败回滚：越界矩形 422，v3 校样/状态不变 ==")
    r = c.post(f"/manuscripts/{mid}/revisions", json={"rectangles": [
        {"color": "red", "x": 7, "y": 7, "w": 4, "h": 4},
    ]})
    check(r.status_code == 422, f"越界矩形被拒（实际 {r.status_code}）")
    detail = c.get(f"/manuscripts/{mid}").json()
    check(detail["current_revision_seq"] == 3, "修订序号仍停在 3，失败稿未入库")
    r = c.get(f"/manuscripts/{mid}/plates/red/proof")
    check(r.headers["x-plate-version"] == "3", "原 v3 校样照常可下载")

    print("== 10. 边界校验 ==")
    check(c.post("/manuscripts",
                 json={"name": "x", "width": 513, "height": 1}).status_code == 422,
          "边长 513 被拒")
    check(c.post("/manuscripts",
                 json={"name": "x", "width": 512, "height": 512}).status_code == 201,
          "边长 512 允许")

    print("\n全部冒烟检查通过 ✅")


if __name__ == "__main__":
    main()
