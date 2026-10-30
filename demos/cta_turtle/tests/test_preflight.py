# -*- coding: utf-8 -*-
"""手工/部署两个入口的 WT_PREFLIGHT 失败策略必须一致。"""
import importlib
import pytest

import runLive


deploy_run = importlib.import_module("deploy_tpl.run")


@pytest.mark.parametrize("mod", [runLive, deploy_run])
def test_invalid_preflight_mode_is_rejected(mod):
    with pytest.raises(ValueError, match="WT_PREFLIGHT"):
        mod._resolve_preflight_mode("strict-ish")
    assert mod._resolve_preflight_mode(None) == "warn"
    assert mod._resolve_preflight_mode("off") == "off"


@pytest.mark.parametrize("mod", [runLive, deploy_run])
def test_strict_any_exception_aborts_but_warn_degrades(mod):
    def boom():
        raise RuntimeError("checker broken")

    with pytest.raises(SystemExit, match="checker broken"):
        mod._run_preflight_step("strict", "unit-test", boom)
    # warn 是显式降级，只在本模式允许继续。
    assert mod._run_preflight_step("warn", "unit-test", boom) is None


@pytest.mark.parametrize("mod", [runLive, deploy_run])
def test_preflight_system_exit_is_never_swallowed(mod):
    def abort():
        raise SystemExit("fatal data")

    with pytest.raises(SystemExit, match="fatal data"):
        mod._run_preflight_step("strict", "unit-test", abort)
