# 丝印工坊出片服务（ScreenShop）

红黑两色丝网印刷的 FastAPI 出片服务：整稿提交红黑矩形 → 自动栅格化两色色版 →
按可印格变化换号 → 编号校验签样 → 两色签齐后打包下载黑白 PNG 色版。
数据保存在 SQLite，交付为可直接启动的 Docker Compose。

## 业务规则

1. **固定画布**：每单创建时确定宽高，每边 1–512 格，之后改稿不改画布。
2. **整稿提交**：每次提交都是一份完整图样，矩形按数组顺序**从底到顶**排列；
   红黑矩形共享同一条图层序列，重叠格归上层矩形所属色版；没有矩形覆盖的格为空白，任何色版都不印。
3. **全局遮挡**：红块压在黑块上时，被压格只属于红版，黑版少格；
   **红块移开后黑版可印格恢复，黑版同样需要换号重签**。
4. **编号规则**：每个色版各自维护单调递增编号，首稿为 1；
   改稿时只比较该色**可印格集合**（位图字节），有变才 `+1` 并清除该色签样，无变则保留编号与签样；
   **改回旧图样时编号继续递增、不复用旧号**。
5. **签样**：登记签样必须显式提交所认可的编号，只与该色当前编号一致才受理（409 拒收迟到/旧号签样）。
6. **打包**：红黑两色都对**当前编号**签齐后，才能下载同一稿件的两张色版 PNG（ZIP）。
7. **失败回滚**：改稿在单个 SQLite 事务内完成，任何校验失败整体回滚，原校样与签样照常可用。

## 目录结构

```
app/
  config.py     # 常量与数据库路径（SCREENSHOP_DB 环境变量）
  database.py   # SQLite 连接、建表、事务
  imaging.py    # 栅格化（全局图层遮挡）、位图、黑白 PNG 渲染
  schemas.py    # Pydantic 请求/响应模型
  main.py       # FastAPI 路由与业务逻辑
tests/          # pytest 自测（10 个用例）
scripts/
  smoke_e2e.py  # 对运行中服务的端到端冒烟脚本
Dockerfile
docker-compose.yml
requirements.txt
```

## 启动（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000
# SQLite 数据持久化在命名卷 screenshop-data -> 容器内 /data/screenshop.db
```

浏览器打开 <http://localhost:8000/docs> 即可看到 Swagger UI，直接演示全部接口
（访问 `/` 会自动跳转到 `/docs`）。

## 本地直接运行（无 Docker 时）

```bash
python3 -m venv --without-pip .venv   # 若系统缺少 ensurepip，先引导 pip
pip install -r requirements.txt
SCREENSHOP_DB=./data/screenshop.db uvicorn app.main:app --reload
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/manuscripts` | 建稿（name、width、height，边长 ≤ 512） |
| GET | `/manuscripts` | 稿件列表 |
| GET | `/manuscripts/{id}` | 稿件详情：两色版编号、可印格数、签样状态 |
| POST | `/manuscripts/{id}/revisions` | 提交整稿/改稿，返回每色是否换号、是否清签 |
| GET | `/manuscripts/{id}/plates/{red\|black}/proof?scale=1..16` | 黑白 PNG 校样（黑=可印格，白=空白），响应头带当前编号 |
| POST | `/manuscripts/{id}/plates/{color}/sign` | 签样，body 必须含当前 `version` 与 `inspector` |
| GET | `/manuscripts/{id}/package` | 两色签齐后下载 ZIP（两张 PNG + manifest.txt） |

### /docs 演示流程

1. `POST /manuscripts` 建一个 8×8 稿件，记下返回的 `id`。
2. `POST /manuscripts/{id}/revisions`，body 示例（黑底 4×4，红块压中央 2×2）：
   ```json
   {"rectangles": [
     {"color": "black", "x": 0, "y": 0, "w": 4, "h": 4},
     {"color": "red",   "x": 1, "y": 1, "w": 2, "h": 2}
   ]}
   ```
   响应中黑版 `printable_cells=12`（16 格被红压住 4 格），两色编号均为 1。
3. `GET .../plates/red/proof` 与 `.../black/proof` 查看/下载黑白校样。
4. 两色分别 `sign`（`version: 1`），再 `GET .../package` 下载 ZIP。
5. 再次 `revisions`，把红块挪到 `(6,6)`：红黑两版都 `changed=true`、编号变 2、签样被清；
   此时拿 `version: 1` 去签会得到 409（迟到签样），打包同样 409。
6. 用 `version: 2` 重新签齐后可再次打包。

## 数据模型（SQLite）

- `manuscripts`：稿件与固定画布尺寸
- `revisions` / `rectangles`：每次整稿及其从底到顶的矩形留痕
- `plates`：每稿件每色当前编号与可印格位图（1 bit/格，行优先 MSB-first，直接字节比较）
- `signatures`：签样记录及所签编号；色版换号时对应行被删除

## 自测

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -v
# 端到端冒烟（先启动服务）：
python scripts/smoke_e2e.py http://127.0.0.1:8000
```

覆盖：首稿出片与 PNG 像素、遮挡变化（红块移开黑版换号清签）、无关色版保留编号与签样、
改回旧图样发新号、迟到签样 409、签齐才打包、失败回滚、512 边界与参数校验。
