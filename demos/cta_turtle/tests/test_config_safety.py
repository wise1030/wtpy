# -*- coding: utf-8 -*-
"""配置不得通过字符串/浮点静默改变运行语义。"""
import pytest

from core.ConfigLoader import ConfigLoader


_BASE = """\
base:
  unit_var: 200000
instruments:
  - code: SHFE.rb.HOT
    units: 1
"""


def _load(tmp_path, text):
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    return ConfigLoader.load(str(p), warn=False)


def test_quoted_intrabar_false_is_rejected_not_becomes_true(tmp_path):
    with pytest.raises(ValueError, match="intrabar_stop"):
        _load(tmp_path, _BASE + "    intrabar_stop: 'false'\n")


def test_fractional_speed_window_is_rejected(tmp_path):
    text = _BASE + """    speeds:
      - {name: fast, system: S1, entry: 20.9, exit: 10, share: 1.0}
"""
    with pytest.raises(ValueError, match="禁止浮点"):
        _load(tmp_path, text)


def test_notify_enabled_requires_urls_and_bool(tmp_path):
    with pytest.raises(ValueError, match="urls 不能为空"):
        _load(tmp_path, _BASE + "notify:\n  enabled: true\n  urls: []\n")
    with pytest.raises(ValueError, match="enabled 需为 bool"):
        _load(tmp_path, _BASE + "notify:\n  enabled: 'false'\n")
