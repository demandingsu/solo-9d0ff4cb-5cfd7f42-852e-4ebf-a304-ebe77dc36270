"""核心业务自测。

覆盖：
1. 整稿出片、编号从 1 开始、黑白 PNG 可下载且像素正确
2. 红块遮挡黑版 -> 移走红块后黑版可印格变化并换号清签（遮挡变化）
3. 只改不影响某色可印格的部分 -> 该色不换号、签样保留
4. 改回旧图样 -> 位图相同仍发新号
5. 签样只认当前编号，迟到签样被 409 拒收
6. 两色签齐才能打包，缺签/签旧号都被 409
7. 改稿失败（越界矩形）整体回滚，原校样与签样照常可用
8. 画布上限 512、越界与非法色校验
"""
from __future__ import annotations

import io
import zipfile

from PIL import Image


def _create(client, name="测试稿", w=10, h=10):
    r = client.post("/manuscripts", json={"name": name, "width": w, "height": h})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rev(client, mid, rects, note=None):
    payload = {"rectangles": rects}
    if note is not None:
        payload["note"] = note
    r = client.post(f"/manuscripts/{mid}/revisions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _plate(result, color):
    return next(p for p in result["plates"] if p["color"] == color)


def _proof_pixels(client, mid, color):
    r = client.get(f"/manuscripts/{mid}/plates/{color}/proof")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.mode == "L"
    return list(img.getdata()), r.headers


def _sign(client, mid, color, version, inspector="张三"):
    return client.post(
        f"/manuscripts/{mid}/plates/{color}/sign",
        json={"version": version, "inspector": inspector},
    )


# ---------------------------------------------------------------- 1

def test_first_revision_starts_at_version_one_and_proof_png(client):
    mid = _create(client)
    res = _rev(client, mid, [
        {"color": "red", "x": 0, "y": 0, "w": 2, "h": 2},
        {"color": "black", "x": 8, "y": 8, "w": 2, "h": 2},
    ])
    assert res["seq"] == 1
    red, black = _plate(res, "red"), _plate(res, "black")
    assert red["new_version"] == 1 and red["printable_cells"] == 4
    assert black["new_version"] == 1 and black["printable_cells"] == 4

    pixels, headers = _proof_pixels(client, mid, "red")
    assert headers["x-plate-version"] == "1"
    # 可印格为黑(0)，空白为白(255)
    black_idx = {i for i, v in enumerate(pixels) if v == 0}
    assert black_idx == {0, 1, 10, 11}

    detail = client.get(f"/manuscripts/{mid}").json()
    assert detail["current_revision_seq"] == 1
    assert {p["color"]: p["version"] for p in detail["plates"]} == {
        "red": 1, "black": 1
    }
    assert all(p["signed"] is False for p in detail["plates"])


# ---------------------------------------------------------------- 2

def test_occlusion_change_red_removed_black_renumbered_and_cleared(client):
    mid = _create(client)
    # 黑版整块 4x4，红块压在黑版中央 2x2
    _rev(client, mid, [
        {"color": "black", "x": 0, "y": 0, "w": 4, "h": 4},
        {"color": "red", "x": 1, "y": 1, "w": 2, "h": 2},
    ])
    detail = client.get(f"/manuscripts/{mid}").json()
    plates = {p["color"]: p for p in detail["plates"]}
    assert plates["red"]["printable_cells"] == 4
    assert plates["black"]["printable_cells"] == 12  # 16 - 被红压住的 4 格

    # 先把两色都签齐
    assert _sign(client, mid, "red", 1).status_code == 201
    assert _sign(client, mid, "black", 1).status_code == 201
    pkg = client.get(f"/manuscripts/{mid}/package")
    assert pkg.status_code == 200

    # 改稿：红块移到角落（仍 2x2），不再压黑版中央
    res = _rev(client, mid, [
        {"color": "black", "x": 0, "y": 0, "w": 4, "h": 4},
        {"color": "red", "x": 6, "y": 6, "w": 2, "h": 2},
    ])
    red, black = _plate(res, "red"), _plate(res, "black")
    # 两色可印格集合都变了：红块位置变；黑版从 12 格恢复为 16 格
    assert red["changed"] and red["old_version"] == 1 and red["new_version"] == 2
    assert red["signed_cleared"]
    assert black["changed"] and black["printable_cells"] == 16
    assert black["new_version"] == 2 and black["signed_cleared"]

    # 旧签样已清空
    detail = client.get(f"/manuscripts/{mid}").json()
    assert all(p["signed"] is False for p in detail["plates"])
    # 未重新签齐不能打包
    assert client.get(f"/manuscripts/{mid}/package").status_code == 409

    # 新校样编号头已更新
    _, headers = _proof_pixels(client, mid, "black")
    assert headers["x-plate-version"] == "2"


# ---------------------------------------------------------------- 3

def test_unaffected_plate_keeps_version_and_signature(client):
    mid = _create(client)
    _rev(client, mid, [
        {"color": "red", "x": 0, "y": 0, "w": 2, "h": 2},
        {"color": "black", "x": 8, "y": 8, "w": 2, "h": 2},
    ])
    assert _sign(client, mid, "black", 1).status_code == 201

    # 只挪动红块，黑版可印格集合不变
    res = _rev(client, mid, [
        {"color": "red", "x": 3, "y": 3, "w": 2, "h": 2},
        {"color": "black", "x": 8, "y": 8, "w": 2, "h": 2},
    ])
    red, black = _plate(res, "red"), _plate(res, "black")
    assert red["changed"] and red["new_version"] == 2
    assert black["changed"] is False and black["new_version"] == 1
    assert black["signed_cleared"] is False

    detail = client.get(f"/manuscripts/{mid}").json()
    plates = {p["color"]: p for p in detail["plates"]}
    assert plates["black"]["signed"] is True
    assert plates["black"]["signed_version"] == 1
    assert plates["red"]["signed"] is False


# ---------------------------------------------------------------- 4

def test_reverting_to_old_pattern_still_issues_new_number(client):
    mid = _create(client)
    rects_v1 = [{"color": "red", "x": 0, "y": 0, "w": 3, "h": 3}]
    rects_v2 = [{"color": "red", "x": 5, "y": 5, "w": 3, "h": 3}]
    r1 = _rev(client, mid, rects_v1)
    assert _plate(r1, "red")["new_version"] == 1
    r2 = _rev(client, mid, rects_v2)
    assert _plate(r2, "red")["new_version"] == 2
    # 改回与第 1 版完全相同的图样：位图相同但编号继续递增
    r3 = _rev(client, mid, rects_v1)
    red = _plate(r3, "red")
    assert red["changed"] is True          # 相对上一稿确实变了
    assert red["new_version"] == 3         # 发新号，不复用 1
    # 黑版三稿都空白，始终未换号
    assert _plate(r3, "black")["new_version"] == 1


# ---------------------------------------------------------------- 5

def test_late_signature_rejected_with_409(client):
    mid = _create(client)
    _rev(client, mid, [{"color": "red", "x": 0, "y": 0, "w": 2, "h": 2}])
    # 编号 1 的校样在流转，此时先不签
    _rev(client, mid, [{"color": "red", "x": 4, "y": 4, "w": 2, "h": 2}])
    # 迟到的旧编号签样
    r = _sign(client, mid, "red", 1)
    assert r.status_code == 409
    assert "编号不匹配" in r.json()["detail"]
    # 数据库里没有留下任何签样
    detail = client.get(f"/manuscripts/{mid}").json()
    red = next(p for p in detail["plates"] if p["color"] == "red")
    assert red["signed"] is False
    # 用当前编号签则成功
    assert _sign(client, mid, "red", 2).status_code == 201


# ---------------------------------------------------------------- 6

def test_package_requires_both_current_signatures(client):
    mid = _create(client)
    _rev(client, mid, [
        {"color": "red", "x": 0, "y": 0, "w": 2, "h": 2},
        {"color": "black", "x": 8, "y": 8, "w": 2, "h": 2},
    ])
    # 一签都没有
    assert client.get(f"/manuscripts/{mid}/package").status_code == 409
    assert _sign(client, mid, "red", 1).status_code == 201
    # 只签一色仍不行
    assert client.get(f"/manuscripts/{mid}/package").status_code == 409
    assert _sign(client, mid, "black", 1).status_code == 201

    r = client.get(f"/manuscripts/{mid}/package")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert any(n.endswith("red_v1.png") for n in names)
    assert any(n.endswith("black_v1.png") for n in names)
    assert any(n.endswith("manifest.txt") for n in names)
    # 包内两张都是合法 PNG
    for n in names:
        if n.endswith(".png"):
            Image.open(io.BytesIO(zf.read(n))).verify()


def test_sign_old_then_revise_then_package_blocked(client):
    mid = _create(client)
    # 红块压住黑块左上角，黑版可印 3 格
    _rev(client, mid, [{"color": "black", "x": 1, "y": 1, "w": 2, "h": 2},
                       {"color": "red", "x": 1, "y": 1, "w": 1, "h": 1}])
    assert _sign(client, mid, "red", 1).status_code == 201
    assert _sign(client, mid, "black", 1).status_code == 201
    # 红块挪走：红版换号清签；黑版从 3 格恢复为 4 格，同样换号清签
    _rev(client, mid, [{"color": "black", "x": 1, "y": 1, "w": 2, "h": 2},
                       {"color": "red", "x": 7, "y": 7, "w": 1, "h": 1}])
    detail = client.get(f"/manuscripts/{mid}").json()
    assert all(p["signed"] is False for p in detail["plates"])
    assert client.get(f"/manuscripts/{mid}/package").status_code == 409


# ---------------------------------------------------------------- 7

def test_failed_revision_rolls_back_keeping_old_proof_and_signature(client):
    mid = _create(client)
    _rev(client, mid, [{"color": "red", "x": 0, "y": 0, "w": 2, "h": 2}])
    assert _sign(client, mid, "red", 1).status_code == 201

    # 越界矩形 -> 422，整稿失败
    r = client.post(f"/manuscripts/{mid}/revisions", json={"rectangles": [
        {"color": "red", "x": 9, "y": 9, "w": 5, "h": 5},  # 超出 10x10
    ]})
    assert r.status_code == 422

    # 原稿编号、校样、签样全部照常
    detail = client.get(f"/manuscripts/{mid}").json()
    assert detail["current_revision_seq"] == 1
    red = next(p for p in detail["plates"] if p["color"] == "red")
    assert red["version"] == 1 and red["signed"] is True
    pixels, headers = _proof_pixels(client, mid, "red")
    assert headers["x-plate-version"] == "1"
    assert sum(1 for v in pixels if v == 0) == 4
    # 原签样仍可直接打包（补签黑版后）
    assert _sign(client, mid, "black", 1).status_code == 201
    assert client.get(f"/manuscripts/{mid}/package").status_code == 200


# ---------------------------------------------------------------- 8

def test_canvas_limits_and_validation(client):
    # 512 合法
    r = client.post("/manuscripts", json={"name": "大稿", "width": 512, "height": 512})
    assert r.status_code == 201
    # 513 被拒
    r = client.post("/manuscripts", json={"name": "超大", "width": 513, "height": 10})
    assert r.status_code == 422
    # 非法颜色
    mid = _create(client)
    r = client.post(f"/manuscripts/{mid}/revisions", json={"rectangles": [
        {"color": "blue", "x": 0, "y": 0, "w": 1, "h": 1},
    ]})
    assert r.status_code == 422
    # 不存在的稿件
    assert client.get("/manuscripts/9999").status_code == 404
    assert client.get("/manuscripts/9999/package").status_code == 404
    # 非法 color 路径参数
    assert client.get(f"/manuscripts/{mid}/plates/blue/proof").status_code == 422


def test_actions_before_first_revision(client):
    mid = _create(client, w=4, h=4)
    # 稿件详情可看，色版占位为 0 号、未签
    detail = client.get(f"/manuscripts/{mid}").json()
    assert detail["current_revision_seq"] == 0
    assert all(p["version"] == 0 and p["signed"] is False
               for p in detail["plates"])
    # 校样、签样、打包都明确报“尚未出片”，而不是 500
    assert client.get(f"/manuscripts/{mid}/plates/red/proof").status_code == 409
    assert _sign(client, mid, "red", 1).status_code == 409
    assert client.get(f"/manuscripts/{mid}/package").status_code == 409


def test_empty_revision_and_scale(client):
    mid = _create(client, w=4, h=4)
    # 空白整稿：两色都出 1 号但可印格为 0，校样全白
    res = _rev(client, mid, [])
    assert all(p["new_version"] == 1 and p["printable_cells"] == 0
               for p in res["plates"])
    r = client.get(f"/manuscripts/{mid}/plates/red/proof?scale=4")
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (16, 16)
    assert all(v == 255 for v in img.getdata())
