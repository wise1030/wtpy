"""execution 层共享路径常量 (A1 迁移期)。

_GROUP_DIR = 策略组合根目录 (deploy 后 = 组合目录, 与 StraTurtle 模块
同名全局同值)。测试可 monkeypatch 本模块或各使用方的别名。
"""
import os

GROUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
