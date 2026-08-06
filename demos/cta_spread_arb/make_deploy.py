"""部署同步脚本 - 把策略代码/配置同步为自包含组合目录

用法:
  python3 make_deploy.py                          # 同步到 ./deploy/cta_spread_arb
  python3 make_deploy.py --target ../deploy       # 同步到统一部署根 (与 cta_turtle 共管)
  python3 make_deploy.py --target /opt/wt_deploy  # 同步到服务器部署根目录

行为:
- 复制: deploy_tpl/run.py -> run.py / schemas.py / strategies/ / helpers/ /
        factors/ / config/(整棵树) + live yaml
- 重写 config.yaml: ../common/ ../storage/ -> 绝对路径 (CWD 无关化,
  WtEngine.init 的 folder 参数不传文件名时被忽略, basefiles 按 CWD 解析,
  WatchDog cwd=组合目录, 不重写会指错)
- --keep-secrets (默认): tdparsers/tdtraders 目标已存在则跳过,
  避免覆盖服务器上已填的账户凭据
- 幂等: 重复执行 = 覆盖同步 (除 secrets)

多组合共管: 与 cta_turtle/make_deploy.py 配合, 两组合同步到同一部署根,
各自子目录隔离 (cta_turtle/ vs cta_spread_arb/), 由 demos/runMonSvr.py
统一监控。详见 docs/DEPLOY.md。
"""
import argparse
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))

CODE_FILES = ["schemas.py"]
CODE_DIRS = ["strategies", "helpers", "factors"]
CFG_DIR = "config"
LIVE_YAMLS = ["executers.yaml", "actpolicy.yaml", "filters.yaml",
              "logcfg.yaml"]
SECRET_YAMLS = ["tdparsers.yaml", "tdtraders.yaml"]
TPL_FILES = {"deploy_tpl/run.py": "run.py"}


def sync_file(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def sync_dir(src: str, dst: str):
    """同步目录, 删除目标后整体拷贝 (忽略 __pycache__)"""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns('__pycache__'))


def rewrite_config(dst_cfg: str, common: str, storage: str):
    """config.yaml 路径重写为绝对路径 (保留注释, 字符串替换)"""
    with open(dst_cfg, encoding='utf-8') as f:
        content = f.read()
    content = content.replace("../common/", common.rstrip('/') + '/')
    content = content.replace("../storage/", storage.rstrip('/') + '/')
    with open(dst_cfg, 'w', encoding='utf-8') as f:
        f.write(content)


def make_deploy(target_root: str, common: str, storage: str,
                keep_secrets: bool = True) -> str:
    grp = os.path.join(target_root, "cta_spread_arb")
    os.makedirs(grp, exist_ok=True)

    # run.py from template
    for rel, dst_rel in TPL_FILES.items():
        sync_file(os.path.join(HERE, rel), os.path.join(grp, dst_rel))
    # code files
    for fname in CODE_FILES:
        sync_file(os.path.join(HERE, fname), os.path.join(grp, fname))
    # code dirs (strategies/helpers/factors)
    for d in CODE_DIRS:
        src_dir = os.path.join(HERE, d)
        if not os.path.isdir(src_dir):
            continue
        sync_dir(src_dir, os.path.join(grp, d))
    # config tree (experiments/base/profiles/contracts + yaml)
    src_cfg_dir = os.path.join(HERE, CFG_DIR)
    if os.path.isdir(src_cfg_dir):
        sync_dir(src_cfg_dir, os.path.join(grp, CFG_DIR))
    # live yamls
    for fname in LIVE_YAMLS:
        sync_file(os.path.join(HERE, fname), os.path.join(grp, fname))
    # secrets (keep existing by default)
    for fname in SECRET_YAMLS:
        dst = os.path.join(grp, fname)
        if keep_secrets and os.path.exists(dst):
            print(f"[make_deploy] keep existing secrets: {fname}")
        else:
            sync_file(os.path.join(HERE, fname), dst)

    # root config.yaml + path rewrite
    dst_cfg = os.path.join(grp, "config.yaml")
    sync_file(os.path.join(HERE, "config.yaml"), dst_cfg)
    rewrite_config(dst_cfg, common, storage)

    print(f"[make_deploy] synced -> {grp}")
    return grp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=str, default=os.path.join(HERE, "deploy"),
                    help="部署根目录 (组合目录为其下 cta_spread_arb/)")
    ap.add_argument("--common", type=str,
                    default=os.path.abspath(os.path.join(HERE, "..", "common")),
                    help="basefiles 目录绝对路径")
    ap.add_argument("--storage", type=str,
                    default=os.path.abspath(os.path.join(HERE, "..", "storage")),
                    help="数据存储目录绝对路径")
    ap.add_argument("--no-keep-secrets", action="store_true",
                    help="覆盖目标已有的 tdparsers/tdtraders")
    args = ap.parse_args()

    make_deploy(os.path.abspath(args.target),
                os.path.abspath(args.common),
                os.path.abspath(args.storage),
                keep_secrets=not args.no_keep_secrets)


if __name__ == "__main__":
    main()
