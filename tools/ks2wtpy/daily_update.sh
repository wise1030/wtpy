#!/bin/bash
#
# KS数据每日增量更新脚本
#
# 用法:
#   ./daily_update.sh                    # 使用默认配置
#   ./daily_update.sh --products SHFE.rb  # 只更新指定品种
#   ./daily_update.sh --full              # 强制全量转换
#
# 部署方式:
#   1. 将此脚本放在远程服务器上
#   2. 添加crontab定时任务:
#      # 每天下午16:00执行增量更新（假设KS数据15:30更新完成）
#      0 16 * * 1-5 /home/wondertrader/scripts/daily_update.sh >> /home/wondertrader/logs/ks2wtpy.log 2>&1
#
#   3. 或者从本地通过SSH触发:
#      ssh USER@KS_HOST "/home/wondertrader/scripts/daily_update.sh"
#

set -euo pipefail

# ============================================================
# 配置
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KS2WTPY_DIR="${SCRIPT_DIR}"

# Python解释器
PYTHON="${PYTHON:-python3}"

# 远程KS数据路径
KS_DATA_PATH="${KS_DATA_PATH:-/home/wondertrader/dist/storage/0518/ks-data}"

# 输出目录
OUTPUT_DIR="${OUTPUT_DIR:-/home/wondertrader/dist/storage/ks_converted}"

# 日志
LOG_DIR="${LOG_DIR:-/home/wondertrader/logs}"
LOG_FILE="${LOG_DIR}/ks2wtpy_$(date +%Y%m%d).log"

# ============================================================
# 函数
# ============================================================
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

check_prerequisites() {
    # 检查Python
    if ! command -v ${PYTHON} &> /dev/null; then
        log "ERROR: Python3 not found"
        exit 1
    fi

    # 检查lmdb库
    if ! ${PYTHON} -c "import lmdb" 2>/dev/null; then
        log "WARNING: lmdb库未安装，LMDB转换将被跳过"
        log "  安装: pip install lmdb"
    fi

    # 检查KS数据目录
    if [ ! -d "${KS_DATA_PATH}" ]; then
        log "ERROR: KS数据目录不存在: ${KS_DATA_PATH}"
        exit 1
    fi

    # 创建输出目录
    mkdir -p "${OUTPUT_DIR}"
    mkdir -p "${LOG_DIR}"
}

run_incremental() {
    log "开始增量更新..."

    ${PYTHON} "${KS2WTPY_DIR}/main.py" \
        --incremental \
        --local "${KS_DATA_PATH}" \
        --output "${OUTPUT_DIR}" \
        --periods m5,d \
        "$@"

    log "增量更新完成"
}

run_full() {
    log "开始全量转换..."

    ${PYTHON} "${KS2WTPY_DIR}/main.py" \
        --full \
        --local "${KS_DATA_PATH}" \
        --output "${OUTPUT_DIR}" \
        --periods m5,d \
        "$@"

    log "全量转换完成"
}

run_verify() {
    log "验证转换结果..."

    ${PYTHON} "${KS2WTPY_DIR}/main.py" \
        --verify \
        --local "${KS_DATA_PATH}" \
        --output "${OUTPUT_DIR}"

    log "验证完成"
}

# ============================================================
# 主逻辑
# ============================================================
main() {
    log "=========================================="
    log "KS → wtpy 数据转换"
    log "KS数据: ${KS_DATA_PATH}"
    log "输出目录: ${OUTPUT_DIR}"
    log "=========================================="

    check_prerequisites

    # 解析参数
    local mode="incremental"
    local extra_args=()

    while [[ $# -gt 0 ]]; do
        case $1 in
            --full)
                mode="full"
                shift
                ;;
            --verify)
                mode="verify"
                shift
                ;;
            --products|--exchanges|--periods|--output)
                extra_args+=("$1" "$2")
                shift 2
                ;;
            *)
                extra_args+=("$1")
                shift
                ;;
        esac
    done

    case ${mode} in
        full)
            run_full "${extra_args[@]}"
            ;;
        incremental)
            run_incremental "${extra_args[@]}"
            ;;
        verify)
            run_verify
            ;;
    esac

    log "=========================================="
    log "完成!"
    log "=========================================="
}

# 运行
main "$@" 2>&1 | tee -a "${LOG_FILE}"
