#!/usr/bin/env python3
"""
远程增量更新脚本

兼容旧入口参数，但实际复用 main.py 的 CLI 和配置构建逻辑，避免
remote_update.py 与 main.py 在 --dates/--include-option/DSB/S3 等能力上漂移。

用法:
  python remote_update.py --incremental --remote-host KS_HOST --remote-user USER --remote-path /path/to/ks
  python remote_update.py --full --remote-host KS_HOST --remote-user USER --remote-path /path/to/ks --products SHFE.rb,DCE.i
"""

import os
import sys
import logging

# 将当前目录加入path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import (  # noqa: E402
    _build_arg_parser,
    build_config_from_args,
    run_full_convert,
    run_incremental_update,
    run_config_only,
    verify_output,
    setup_logging,
)

logger = logging.getLogger('ks2wtpy')


def _normalize_legacy_remote_args(argv: list[str]) -> list[str]:
    """将旧 remote_update 参数映射到 main.py 参数。"""
    normalized: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--remote-host':
            normalized.append('--remote')
            if i + 1 < len(argv):
                normalized.append(argv[i + 1])
                i += 2
                continue
        elif arg in ('--remote-user', '--remote-path'):
            normalized.append(arg)
            if i + 1 < len(argv):
                normalized.append(argv[i + 1])
                i += 2
                continue
        else:
            normalized.append(arg)
        i += 1
    return normalized


def parse_args(argv: list[str] | None = None):
    parser = _build_arg_parser()
    raw = sys.argv[1:] if argv is None else argv
    return parser.parse_args(_normalize_legacy_remote_args(raw))


def main():
    args = parse_args()
    setup_logging(args.log_level)
    cfg, new_dates = build_config_from_args(args)

    # 执行
    if args.full:
        run_full_convert(cfg)
    elif args.incremental:
        run_incremental_update(cfg, new_dates=new_dates)
    elif args.config_only:
        run_config_only(cfg)
    elif args.verify:
        verify_output(cfg)


if __name__ == '__main__':
    main()
