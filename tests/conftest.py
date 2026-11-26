"""pytest 夹具：每个测试使用独立临时 SQLite 库。"""
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "test.db"
    monkeypatch.setenv("SCREENSHOP_DB", str(db_path))
    # config 在导入时读取环境变量，需要重载
    import importlib

    from app import config as config_mod
    importlib.reload(config_mod)
    from app import database as db_mod
    importlib.reload(db_mod)
    from app import imaging as imaging_mod  # noqa: F401
    from app import schemas as schemas_mod  # noqa: F401
    from app import main as main_mod
    importlib.reload(main_mod)

    main_mod.init_db()
    with TestClient(main_mod.app) as c:
        yield c

    tmp.cleanup()
