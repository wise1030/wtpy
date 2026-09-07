"""scripts/update_contracts.py 纯函数测试"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "scripts"))
import update_contracts as uc  # noqa: E402


def test_is_fresh_today(tmp_path):
    p = tmp_path / "contracts.json"
    assert not uc.is_fresh_today(str(p), 20260902)      # 不存在
    p.write_text("{}")
    today = int(time.strftime("%Y%m%d"))
    assert uc.is_fresh_today(str(p), today)             # 刚写入=今天
    assert not uc.is_fresh_today(str(p), today + 1)     # 明天视角=不新鲜


def test_filter_expired():
    contracts = {
        "SHFE": {
            "rb2701": {"expiredate": 20270115, "name": "rb2701"},
            "rb2509": {"expiredate": 20250915, "name": "rb2509"},
            "rb9999": {"expiredate": 0, "name": "无到期日保留"},
        },
        "DCE": {
            "l2701": {"expiredate": 20270115},
            "l2401": {},                                # 缺字段保留
        },
    }
    out, removed = uc.filter_expired(contracts, 20260902)
    assert removed == 1
    assert set(out["SHFE"]) == {"rb2701", "rb9999"}
    assert set(out["DCE"]) == {"l2701", "l2401"}
    # 幂等: 再过滤一次剔除数为 0
    out2, removed2 = uc.filter_expired(out, 20260902)
    assert removed2 == 0 and out2 == out


def test_fill_missing_sessions():
    new = {"SHFE": {"cu": {"session": "", "name": "沪铜"},
                    "rb": {"session": "FN2300"}},
           "INE": {"sc": {"session": ""}}}
    old = {"SHFE": {"cu": {"session": "FN0100"}}}
    out, fixed = uc.fill_missing_sessions(new, old)
    assert fixed == 1
    assert out["SHFE"]["cu"]["session"] == "FN0100"   # 回填
    assert out["SHFE"]["rb"]["session"] == "FN2300"   # 已有不动
    assert out["INE"]["sc"]["session"] == ""          # 旧文件也没有→保持空


def test_backup_and_replace(tmp_path):
    dst = tmp_path / "contracts.json"
    dst.write_text(json.dumps({"old": 1}))
    src = tmp_path / "new.json"
    src.write_text(json.dumps({"new": 2}))
    uc.backup_and_replace(str(src), str(dst), 20260902)
    assert json.loads(dst.read_text()) == {"new": 2}
    bak = tmp_path / "contracts.json.bak.20260902"
    assert json.loads(bak.read_text()) == {"old": 1}
    assert not src.exists()                            # move 语义


def test_load_cred_yaml(tmp_path):
    p = tmp_path / "tdtraders.yaml"
    p.write_text(
        "traders:\n"
        "- front: tcp://182.254.243.31:30001\n"
        "  broker: '9999'\n"
        "  user: '048363'\n"
        "  pass: secret123\n")
    cred = uc.load_cred_yaml(str(p))
    assert cred == {"front": "tcp://182.254.243.31:30001",
                    "broker": "9999", "user": "048363",
                    "password": "secret123"}


def test_build_loader_config_explicit_module():
    cfg = uc.build_loader_config("tcp://1.2.3.4:30201", "9999", "u", "p",
                                 "app", "auth", "/tmp/save/",
                                 module="/x/thosttraderapi_se.so")
    assert cfg["ctp"]["front"] == "tcp://1.2.3.4:30201"
    assert cfg["ctp"]["module"] == "/x/thosttraderapi_se.so"
    assert cfg["config"]["mask"] == 1                  # 仅期货
    assert cfg["config"]["mapfiles"].endswith("map_future.ini")
    assert os.path.exists(cfg["config"]["mapfiles"])   # 映射文件随包
