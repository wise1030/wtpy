"""部署同步脚本 — 把策略代码/配置同步为自包含组合目录

用法:
  python3 make_deploy.py                          # 同步到 ./deploy/cta_turtle
  python3 make_deploy.py --target /opt/wt_deploy  # 同步到服务器部署根目录
  python3 make_deploy.py --verify /opt/wt_deploy/cta_turtle
      # 校验远端目录与本地源一致 (manifest sha256 比对, 发现漂移)

行为:
- 复制: run.py / runDt.py / StraTurtle.py / schemas.py / helpers/ / config/
  + live yaml + datakit yaml (dtcfg/logcfgdt/mdparsers)
- 重写 config.yaml 与 dtcfg.yaml: ../common/ ../storage/ → 绝对路径
  (CWD 无关化, WtEngine.init 的 folder 参数不传文件名时被忽略,
  basefiles 按 CWD 解析, WatchDog cwd=组合目录, 不重写会指错)
- --keep-secrets (默认): tdtraders/mdparsers 目标已存在则跳过,
  避免覆盖服务器上已填的账户凭据
- 幂等: 重复执行 = 覆盖同步 (除 secrets)
- 每次同步生成 deploy.manifest.json (全部已部署文件的 sha256 + 时间戳)
  — 2026-09 部署靠手工 rsync+md5 逐个核对, 本 manifest 把该校验
  固化; --verify 可随后发现远端漂移/被篡改
"""
import argparse
import hashlib
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))

CODE_FILES = ["StraTurtle.py", "schemas.py"]
CODE_DIRS = ["helpers", "filters"]
CFG_FILES = ["config/turtle_config.yaml"]
LIVE_YAMLS = ["executers.yaml", "actpolicy.yaml", "filters.yaml",
              "logcfg.yaml", "tdparsers.yaml"]
SECRET_YAMLS = ["tdtraders.yaml"]
TPL_FILES = {"deploy_tpl/run.py": "run.py",
             "deploy_tpl/runDt.py": "runDt.py",
             "deploy_tpl/dtcfg.yaml": "dtcfg.yaml",
             "deploy_tpl/statemonitor.yaml": "statemonitor.yaml",
             "deploy_tpl/logcfgdt.yaml": "logcfgdt.yaml",
             "scripts/update_contracts.py": "scripts/update_contracts.py",
             "scripts/pick_hot_sina.py": "scripts/pick_hot_sina.py",
             "scripts/update_hots.py": "scripts/update_hots.py",
             "scripts/update_hot_daily.py":
                 "scripts/update_hot_daily.py",
              "scripts/convert_csv_to_dsb.py":
                  "scripts/convert_csv_to_dsb.py",
              "scripts/timer_notify.py": "scripts/timer_notify.py",
              "scripts/ctp_loader/map_future.ini":
                  "scripts/ctp_loader/map_future.ini"}
SECRET_TPL = {"deploy_tpl/mdparsers.yaml": "mdparsers.yaml"}


def sync_file(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


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
    grp = os.path.join(target_root, "cta_turtle")
    os.makedirs(grp, exist_ok=True)

    for rel, dst_rel in TPL_FILES.items():
        sync_file(os.path.join(HERE, rel), os.path.join(grp, dst_rel))
    for fname in CODE_FILES:
        sync_file(os.path.join(HERE, fname), os.path.join(grp, fname))
    for d in CODE_DIRS:
        src_dir = os.path.join(HERE, d)
        dst_dir = os.path.join(grp, d)
        if os.path.exists(dst_dir):
            shutil.rmtree(dst_dir)
        shutil.copytree(src_dir, dst_dir,
                        ignore=shutil.ignore_patterns('__pycache__'))
    for rel in CFG_FILES:
        sync_file(os.path.join(HERE, rel), os.path.join(grp, rel))
    for fname in LIVE_YAMLS:
        sync_file(os.path.join(HERE, fname), os.path.join(grp, fname))
    for fname in SECRET_YAMLS:
        dst = os.path.join(grp, fname)
        if keep_secrets and os.path.exists(dst):
            print(f"[make_deploy] keep existing secrets: {fname}")
        else:
            sync_file(os.path.join(HERE, fname), dst)
    for rel, dst_rel in SECRET_TPL.items():
        dst = os.path.join(grp, dst_rel)
        if keep_secrets and os.path.exists(dst):
            print(f"[make_deploy] keep existing secrets: {dst_rel}")
        else:
            sync_file(os.path.join(HERE, rel), dst)

    dst_cfg = os.path.join(grp, "config.yaml")
    sync_file(os.path.join(HERE, "config.yaml"), dst_cfg)
    rewrite_config(dst_cfg, common, storage)
    # datakit 配置同样含 ../common//../storage 相对路径, 一并绝对化
    rewrite_config(os.path.join(grp, "dtcfg.yaml"), common, storage)

    manifest = write_manifest(grp)
    print(f"[make_deploy] synced -> {grp} ({len(manifest['files'])} files)")
    return grp


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(grp: str, exclusions=("deploy.manifest.json",)) -> dict:
    """生成部署目录文件清单 (相对路径 → sha256 + size)。

    凭据类文件 (tdtraders.yaml/mdparsers.yaml) 只记名不记哈希: 服务器
    侧填了真实凭据, 与本地占位文件必然不同, 记哈希会让 --verify
    每次都报漂移。"""
    SECRET_NAMES = {"tdtraders.yaml", "mdparsers.yaml"}
    files = {}
    for root, _dirs, names in os.walk(grp):
        for name in sorted(names):
            if name in exclusions or name.endswith((".pyc", ".log")):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, grp)
            if name in SECRET_NAMES:
                files[rel] = {"sha256": None, "secret": True}
            else:
                files[rel] = {"sha256": _sha256(full),
                              "size": os.path.getsize(full)}
    import datetime as _dt
    manifest = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
                "files": files}
    with open(os.path.join(grp, "deploy.manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def verify_manifest(grp: str) -> list:
    """校验部署目录与 manifest 一致; 返回漂移项列表 (空=一致)。"""
    mpath = os.path.join(grp, "deploy.manifest.json")
    if not os.path.exists(mpath):
        return [f"manifest 缺失: {mpath}"]
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    drifts = []
    for rel, info in sorted(manifest.get("files", {}).items()):
        full = os.path.join(grp, rel)
        if not os.path.exists(full):
            drifts.append(f"缺失: {rel}")
            continue
        if info.get("secret"):
            continue
        if info.get("sha256") != _sha256(full):
            drifts.append(f"哈希漂移: {rel}")
    return drifts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=str, default=os.path.join(HERE, "deploy"),
                    help="部署根目录 (组合目录为其下 cta_turtle/)")
    ap.add_argument("--common", type=str,
                    default=os.path.abspath(os.path.join(HERE, "..", "common")),
                    help="basefiles 目录绝对路径")
    ap.add_argument("--storage", type=str,
                    default=os.path.abspath(os.path.join(HERE, "..", "storage")),
                    help="数据存储目录绝对路径")
    ap.add_argument("--no-keep-secrets", action="store_true",
                    help="覆盖目标已有的 tdparsers/tdtraders")
    ap.add_argument("--verify", type=str, default="",
                    help="校验模式: 传入部署组合目录路径, 与 manifest 比对")
    args = ap.parse_args()

    if args.verify:
        drifts = verify_manifest(os.path.abspath(args.verify))
        if drifts:
            print(f"[make_deploy] {len(drifts)} 处漂移:")
            for d in drifts:
                print(f"  {d}")
            raise SystemExit(1)
        print(f"[make_deploy] verify OK: {args.verify} 与 manifest 一致")
        return

    make_deploy(os.path.abspath(args.target),
                os.path.abspath(args.common),
                os.path.abspath(args.storage),
                keep_secrets=not args.no_keep_secrets)


if __name__ == "__main__":
    main()
