"""init_monitor_db.py 监控db预置测试"""
import os
import sqlite3
import sys

from conftest import *  # noqa: F401,F403

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from init_monitor_db import init_db  # noqa: E402


def _qry(db, sql):
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def test_init_db_registers_group_and_schedule():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "data.db")
        grp = os.path.join(tmp, "deploy", "cta_turtle")
        os.makedirs(grp)
        init_db(db, grp, mqurl="ipc:///tmp/test_turtle.ipc")

        rows = _qry(db, "SELECT groupid, gtype, env, mqurl FROM groups")
        assert len(rows) == 1
        assert rows[0][0] == "cta_turtle"
        assert rows[0][1] == "cta"
        assert rows[0][3] == "ipc:///tmp/test_turtle.ipc"

        rows = _qry(db,
                    "SELECT appid, param, folder, guard, mqurl FROM schedules"
                    " ORDER BY appid")
        assert len(rows) == 2                     # 双进程: datakit + 交易引擎
        assert rows[0][0] == "cta_turtle"
        assert rows[0][1] == "run.py"
        assert rows[0][2] == grp
        assert rows[0][3] == "true"
        assert rows[0][4] == "ipc:///tmp/test_turtle.ipc"
        assert rows[1][0] == "cta_turtle_dt"
        assert rows[1][1] == "runDt.py"
        assert rows[1][3] == "true"


def test_init_db_idempotent():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "data.db")
        grp = os.path.join(tmp, "deploy", "cta_turtle")
        os.makedirs(grp)
        init_db(db, grp)
        init_db(db, grp)   # 二次执行 = update, 不重复插
        assert len(_qry(db, "SELECT * FROM groups")) == 1
        assert len(_qry(db, "SELECT * FROM schedules")) == 2


def test_init_db_no_guard():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "data.db")
        grp = os.path.join(tmp, "g")
        os.makedirs(grp)
        init_db(db, grp, guard=False)
        rows = _qry(db, "SELECT guard FROM schedules")
        assert len(rows) == 2
        assert all(r[0] == "false" for r in rows)


def test_init_db_seeds_admin_with_hashed_pwd():
    """播种 admin 用户, 密码存 md5(loginid + md5(明文)) 双层口径 —
    前端 md5(明文) → 后端 md5(loginid+前端值) 比对 (2026-09-01 实测订正)"""
    import hashlib
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "data.db")
        grp = os.path.join(tmp, "g")
        os.makedirs(grp)
        init_db(db, grp, admin_pass="test@123")

        rows = _qry(db,
                    "SELECT loginid, role, passwd FROM users "
                    "WHERE loginid='admin'")
        assert len(rows) == 1
        assert rows[0][1] == "superman"
        inner = hashlib.md5("test@123".encode("utf-8")).hexdigest()
        expect = hashlib.md5(("admin" + inner).encode("utf-8")).hexdigest()
        assert rows[0][2] == expect


def test_init_db_admin_idempotent_no_pwd_reset():
    """二次执行不重置已有 admin 密码 (add_user 走 UPDATE 分支)"""
    import hashlib
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "data.db")
        grp = os.path.join(tmp, "g")
        os.makedirs(grp)
        init_db(db, grp, admin_pass="first@1")
        init_db(db, grp, admin_pass="second@2")
        rows = _qry(db, "SELECT passwd FROM users WHERE loginid='admin'")
        inner = hashlib.md5("first@1".encode("utf-8")).hexdigest()
        expect = hashlib.md5(("admin" + inner).encode("utf-8")).hexdigest()
        assert rows[0][0] == expect   # 仍是首次密码
