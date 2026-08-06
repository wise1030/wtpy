"""make_deploy.py 同步脚本测试"""
import os
import sys

from conftest import *  # noqa: F401,F403

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import make_deploy  # noqa: E402


def _run_deploy(tmp, **kw):
    target = os.path.join(tmp, "deploy")
    common = os.path.join(tmp, "common")
    storage = os.path.join(tmp, "storage")
    os.makedirs(common, exist_ok=True)
    os.makedirs(storage, exist_ok=True)
    grp = make_deploy.make_deploy(target, common, storage, **kw)
    return grp, common, storage


def test_deploy_structure_complete():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        grp, _, _ = _run_deploy(tmp)
        assert os.path.isfile(os.path.join(grp, "run.py"))
        assert os.path.isfile(os.path.join(grp, "StraTurtle.py"))
        assert os.path.isfile(os.path.join(grp, "schemas.py"))
        assert os.path.isfile(os.path.join(grp, "helpers", "TurtleEngine.py"))
        assert os.path.isfile(os.path.join(grp, "helpers", "ConfigLoader.py"))
        assert os.path.isfile(os.path.join(grp, "config", "turtle_config.yaml"))
        for y in ("config.yaml", "executers.yaml", "actpolicy.yaml",
                  "filters.yaml", "logcfg.yaml",
                  "tdparsers.yaml", "tdtraders.yaml"):
            assert os.path.isfile(os.path.join(grp, y)), y
        # __pycache__ 不应被同步
        assert not os.path.exists(os.path.join(grp, "helpers", "__pycache__"))


def test_config_paths_rewritten_absolute():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        grp, common, storage = _run_deploy(tmp)
        with open(os.path.join(grp, "config.yaml"), encoding='utf-8') as f:
            content = f.read()
        assert "../common/" not in content
        assert "../storage/" not in content
        assert common + "/" in content
        assert storage + "/" in content
        # notifier url 保持 (与监控 mqurl 一致性来源)
        assert "ipc:///tmp/wt_turtle.ipc" in content


def test_deploy_idempotent():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        _run_deploy(tmp)
        grp, _, _ = _run_deploy(tmp)   # 二次执行不报错
        assert os.path.isfile(os.path.join(grp, "run.py"))


def test_keep_secrets_preserves_existing():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        grp, _, _ = _run_deploy(tmp)
        secret = os.path.join(grp, "tdtraders.yaml")
        with open(secret, 'w', encoding='utf-8') as f:
            f.write("# real credentials filled on server\n")
        _run_deploy(tmp)   # 默认 keep_secrets=True
        with open(secret, encoding='utf-8') as f:
            assert "real credentials" in f.read()


def test_no_keep_secrets_overwrites():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        grp, _, _ = _run_deploy(tmp)
        secret = os.path.join(grp, "tdtraders.yaml")
        with open(secret, 'w', encoding='utf-8') as f:
            f.write("# real credentials\n")
        _run_deploy(tmp, keep_secrets=False)
        with open(secret, encoding='utf-8') as f:
            assert "real credentials" not in f.read()
