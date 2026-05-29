# KS 数据接入分析（已并入 README）

> 本文件原为 `ks2wtpy` 早期 Phase 0~Phase 3 数据接入分析草稿。所有结论已正式收纳到 `tools/ks2wtpy/README.md` 与 `docs/stdcode_and_cpp_pitfalls.md`。

请按以下索引查阅：

| 原章节 | 现位置 |
|---|---|
| Phase 0/1 数据来源、转换闭环 | `README.md` §1 主要功能、§3 目录与模块、§7 三类访问场景 |
| Phase 2 basefiles 生产化 | `README.md` §3.13 references/、§11 acceptance |
| Phase 3 实盘历史预检 | `README.md` §6.9、§7.2、§11.5 live_gate |
| Phase 3.5 盘中宕机恢复 | **明确不在 `ks2wtpy` 范围内**，由独立模块承担 |
| 投研派生视图 | `README.md` §6.10、§7.3、§11.6 ResearchView |
| stdCode 三段规范 + C++ loader 陷阱 | `docs/stdcode_and_cpp_pitfalls.md` |
| KS S3 数据布局 / HOT 算法 | `wt-research` skill 的 `references/ks2wtpy-data-conversion.md` |

如需重建独立详档，请基于 `wt-research` skill 的 references 再生。
