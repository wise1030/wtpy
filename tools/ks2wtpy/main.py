#!/usr/bin/env python3
"""
KS数据 → wtpy回测数据 转换系统 - 主入口

用法:
  # 全量转换（CSV + LMDB + 配置文件）
  python main.py --full --remote KS_HOST

  # 增量更新（只转换新增日期的数据）
  python main.py --incremental --remote KS_HOST

  # 只转换CSV
  python main.py --csv-only --remote KS_HOST

  # 只转换LMDB
  python main.py --lmdb-only --remote KS_HOST

  # 指定品种
  python main.py --products SHFE.rb,SHFE.rb,DCE.i --remote KS_HOST

  # 从本地目录转换
  python main.py --full --local /path/to/ks-data

  # 只生成配置文件
  python main.py --config-only --remote KS_HOST

增量更新机制:
  - 维护 convert_state.json 记录每个品种每个周期的最新转换日期
  - 每次运行时只读取和转换新增日期的数据
  - CSV模式: 合并已有文件并按时间重写，保持顺序
  - LMDB模式: 向已有数据库中put新key
"""

import os
import sys
import json
import csv
import time
import hashlib
import logging
import argparse
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import (
    ConvertConfig, EXCHANGES, FUTURES_PRODUCTS, OPTION_PRODUCTS,
    OPTION_ONLY_PRODUCTS,
    WTPY_SUPPORTED_PERIODS, PERIOD_MAP,
)
from ks_reader import KSDataReader
from csv_writer import CsvConverter
from lmdb_writer import LmdbConverter
from dsb_writer import DsbConverter
from config_generator import ConfigGenerator

logger = logging.getLogger('ks2wtpy')


def create_reader(cfg: ConvertConfig):
    """根据配置创建数据读取器(S3优先，其次SSH/本地)"""
    if cfg.use_s3:
        logger.info(f"数据源: S3 (bucket={cfg.s3_bucket})")
        try:
            from s3_reader import S3KsReader
        except ImportError as exc:
            raise ImportError("使用--s3需要安装minio库: pip install minio") from exc
        return S3KsReader(
            endpoint=cfg.s3_endpoint,
            access_key=cfg.s3_access_key,
            secret_key=cfg.s3_secret_key,
            bucket=cfg.s3_bucket,
            secure=cfg.s3_secure,
            cache_dir=cfg.s3_cache_dir or None,
        )
    else:
        logger.info(f"数据源: {'远程 ' + cfg.remote_host if cfg.is_remote else '本地 ' + cfg.remote_ks_base}")
        return KSDataReader(
            base_path=cfg.remote_ks_base,
            is_remote=cfg.is_remote,
            remote_host=cfg.remote_host,
            remote_user=cfg.remote_user,
        )


def setup_logging(level: str = 'INFO'):
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )


def atomic_write_text(path: str, content: str, encoding: str = 'utf-8') -> None:
    """原子写文本文件：先写同目录临时文件，再 os.replace 到目标路径。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, 'w', encoding=encoding) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def atomic_json_dump(path: str, data: Dict) -> None:
    """原子写 JSON 文件，避免中断留下半截审计文件。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def load_state(state_file: str) -> Dict:
    """加载转换状态文件"""
    if not os.path.exists(state_file):
        return {}

    try:
        with open(state_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"加载状态文件失败: {e}, 将重新开始")
        return {}


def save_state(state_file: str, state: Dict):
    """保存转换状态文件"""
    atomic_json_dump(state_file, state)


def _source_summary(cfg: ConvertConfig) -> Dict:
    """生成不含敏感凭据的数据源摘要。"""
    if cfg.use_s3:
        return {
            'type': 's3',
            'endpoint': cfg.s3_endpoint,
            'bucket': cfg.s3_bucket,
            'secure': cfg.s3_secure,
        }
    if cfg.is_remote:
        return {
            'type': 'remote',
            'host': cfg.remote_host,
            'user': cfg.remote_user,
            'path': cfg.remote_ks_base,
        }
    return {
        'type': 'local',
        'path': cfg.remote_ks_base,
    }


def _raw_artifacts_summary(cfg: ConvertConfig, sample_limit: int = 50) -> Optional[Dict]:
    """扫描本地 raw/cache 目录，生成不含凭据的原始对象摘要。"""
    cache_dir = cfg.s3_cache_dir if cfg.use_s3 else ''
    if not cache_dir or not os.path.isdir(cache_dir):
        return None

    files = []
    total_bytes = 0
    file_count = 0
    for root, _, names in os.walk(cache_dir):
        for name in sorted(names):
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            rel_path = os.path.relpath(path, cache_dir).replace(os.sep, '/')
            file_count += 1
            total_bytes += size
            if len(files) < sample_limit:
                try:
                    sha256 = hashlib.sha256()
                    with open(path, 'rb') as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b''):
                            sha256.update(chunk)
                    digest = sha256.hexdigest()
                except OSError:
                    digest = None
                item = {'path': rel_path, 'bytes': size}
                if digest:
                    item['sha256'] = digest
                files.append(item)

    return {
        'cache_dir': cache_dir,
        'file_count': file_count,
        'total_bytes': total_bytes,
        'sample_limit': sample_limit,
        'files': files,
    }


def write_conversion_manifest(cfg: ConvertConfig, mode: str, stats: Dict,
                              elapsed: float, extra: Optional[Dict] = None) -> str:
    """写入结构化转换 manifest，作为后续质量报告/审计追踪入口。"""
    raw_artifacts = _raw_artifacts_summary(cfg)
    manifest_path = os.path.join(cfg.local_output_base, 'conversion_manifest.json')
    quality_report_path = os.path.join(cfg.local_output_base, 'quality_report.json')
    stats_public = {k: v for k, v in stats.items() if k != 'start_time'}
    stats_fingerprint = hashlib.sha256(
        json.dumps(stats_public, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    ).hexdigest()
    version_id = f"ks2wtpy-{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{stats_fingerprint[:12]}"
    scenario_contract = {
        'backtest': {
            'primary_storage': 'dsb' if cfg.enable_dsb else ('lmdb' if cfg.enable_lmdb else 'csv'),
            'config_templates': {
                'csv': os.path.join(cfg.local_output_base, 'configbt_csv.yaml'),
                'dsb': os.path.join(cfg.local_output_base, 'configbt_dsb.yaml'),
                'lmdb': os.path.join(cfg.local_output_base, 'configbt_lmdb.yaml'),
            },
            'basefiles': cfg.config_output_dir,
        },
        'live_history': {
            'storage_base': cfg.local_output_base,
            'preflight_report': os.path.join(cfg.local_output_base, 'live_preflight_report.json'),
            'startup_gate': os.path.join(cfg.local_output_base, 'live_gate.json'),
            'alerts': os.path.join(cfg.local_output_base, 'live_alerts.jsonl'),
            'preflight_required': True,
            'writes_realtime_dmb': False,
            'purpose': 'read-only historical bar preload before live trading starts',
        },
        'research': {
            'derived_view_only': True,
            'output_dir': os.path.join(cfg.local_output_base, 'research'),
            'metadata': os.path.join(cfg.local_output_base, 'research', 'research_view_metadata.json'),
            'lineage_metadata': os.path.join(cfg.local_output_base, 'research', 'research_view_metadata.json'),
            'optional_formats': ['csv', 'parquet'],
            'authority_source': 'wt_storage_converted_outputs',
        },
    }
    manifest = {
        'version': 1,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'data_version': {
            'version_id': version_id,
            'manifest': manifest_path,
            'quality_report': quality_report_path,
            'stats_fingerprint': stats_fingerprint,
            'source_fingerprint_scope': 'source_summary+filters+enabled_outputs+stats',
        },
        'mode': mode,
        'source': _source_summary(cfg),
        'outputs': {
            'base': cfg.local_output_base,
            'csv': cfg.csv_output_dir,
            'lmdb': cfg.lmdb_output_dir,
            'dsb': cfg.dsb_output_dir,
            'config': cfg.config_output_dir,
            'state': cfg.state_file,
        },
        'access_scenarios': scenario_contract,
        'access_contract': {
            'authority': 'conversion_manifest.json',
            'version_id': version_id,
            'quality_gate': {
                'quality_report': quality_report_path,
                'block_on_stats_errors': True,
                'block_on_live_preflight_failure': True,
            },
            'scenarios': scenario_contract,
        },
        'enabled_outputs': {
            'csv': cfg.enable_csv,
            'lmdb': cfg.enable_lmdb,
            'dsb': cfg.enable_dsb,
            'config': cfg.enable_config,
        },
        'filters': {
            'exchanges': cfg.exchanges,
            'periods': cfg.periods,
            'include_option': cfg.include_option,
            'specific_products': cfg.specific_products,
        },
        'data_semantics': {
            'std_code_format': 'EXCHG.CODE',
            'ks_date_field': 'source date/trading date as provided by KS',
            'minute_time_field': 'bar timestamp as provided by KS, preserved without session shifting',
            'bar_time_key': 'YYYYMMDDHHMM for minute bars, YYYYMMDD0000 for day bars',
            'night_session_adjustment': 'not_applied',
        },
        'flags': {
            'incremental': cfg.incremental,
            'force_full': cfg.force_full,
        },
        'stats': {k: v for k, v in stats.items() if k != 'start_time'},
        'elapsed_seconds': round(elapsed, 3),
    }
    if raw_artifacts:
        manifest['raw_artifacts'] = raw_artifacts
    error_details = stats.get('error_details') or []
    if error_details:
        manifest['error_details'] = error_details
    if extra:
        manifest['extra'] = extra

    atomic_json_dump(manifest_path, manifest)
    logger.info(f"转换manifest: {manifest_path}")
    return manifest_path


def _csv_key_from_row(row: List[str], is_day: bool) -> Optional[int]:
    """把 wtpy CSV 行转成可比较时间键：YYYYMMDDHHMM，日线用 YYYYMMDD0000。"""
    if not row:
        return None
    date_str = row[0].replace('/', '').replace('-', '').strip()
    if not date_str.isdigit():
        return None
    if is_day:
        return int(date_str) * 10000
    if len(row) < 2:
        return None
    time_str = row[1].replace(':', '').strip()
    if not time_str.isdigit():
        return None
    return int(date_str) * 10000 + int(time_str[:4])


def _format_quality_key(key: Optional[int]) -> Optional[str]:
    if key is None:
        return None
    date_val = key // 10000
    time_val = key % 10000
    return f"{date_val}.{time_val:04d}"


def _quality_key_to_datetime(key: int) -> Optional[datetime]:
    try:
        date_val = key // 10000
        time_val = key % 10000
        return datetime.strptime(f"{date_val}{time_val:04d}", "%Y%m%d%H%M")
    except ValueError:
        return None


def _expected_gap_minutes(basename: str) -> Optional[int]:
    if basename.endswith('_m1.csv'):
        return 1
    if basename.endswith('_m5.csv'):
        return 5
    return None


def _is_expected_session_break(prev_dt: datetime, cur_dt: datetime, gap_minutes: int) -> bool:
    """识别常见期货交易时段合法休市间隔，避免质量报告误报。"""
    prev_hm = prev_dt.hour * 100 + prev_dt.minute
    cur_hm = cur_dt.hour * 100 + cur_dt.minute
    legal_breaks = {
        (1015, 1030), (1015, 1031),
        (1130, 1330), (1130, 1331),
        (1500, 2100), (1501, 2100),
        (2300, 901), (2301, 901), (2330, 901), (2331, 901),
        (100, 901), (101, 901), (230, 901), (231, 901),
    }
    if (prev_hm, cur_hm) in legal_breaks:
        return True
    if gap_minutes == 5 and (prev_hm, cur_hm) in {(1015, 1035), (1130, 1335), (1500, 2105)}:
        return True
    return False


def _scan_csv_quality(filepath: str) -> Dict:
    """扫描单个 wtpy CSV 文件，输出基础质量指标。"""
    basename = os.path.basename(filepath)
    is_day = basename.endswith('_d.csv')
    stats = {
        'file': basename,
        'rows': 0,
        'duplicates': 0,
        'out_of_order': 0,
        'ohlc_errors': 0,
        'negative_fields': 0,
        'parse_errors': 0,
        'time_gaps': 0,
        'open_interest_jumps': 0,
        'price_anomalies': 0,
        'price_anomaly_samples': [],
        'first_key': None,
        'last_key': None,
        'dates': [],
        'session_aware': False,
    }
    seen = set()
    keys = []
    prev_key = None
    prev_oi = None
    prev_close = None
    gap_minutes = _expected_gap_minutes(basename)
    stats['session_aware'] = bool(gap_minutes)

    try:
        with open(filepath, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if not row:
                    continue
                stats['rows'] += 1
                key = _csv_key_from_row(row, is_day)
                if key is None:
                    stats['parse_errors'] += 1
                    continue
                if key in seen:
                    stats['duplicates'] += 1
                seen.add(key)
                if prev_key is not None and key < prev_key:
                    stats['out_of_order'] += 1
                if gap_minutes and prev_key is not None and key > prev_key:
                    prev_dt = _quality_key_to_datetime(prev_key)
                    cur_dt = _quality_key_to_datetime(key)
                    if prev_dt and cur_dt:
                        delta_minutes = int((cur_dt - prev_dt).total_seconds() // 60)
                        if delta_minutes > gap_minutes and not _is_expected_session_break(prev_dt, cur_dt, gap_minutes):
                            stats['time_gaps'] += 1
                prev_key = key
                keys.append(key)

                offset = 1 if is_day else 2
                try:
                    open_px = float(row[offset])
                    high_px = float(row[offset + 1])
                    low_px = float(row[offset + 2])
                    close_px = float(row[offset + 3])
                    volume = float(row[offset + 4])
                    turnover = float(row[offset + 5])
                    open_interest = float(row[offset + 6]) if len(row) > offset + 6 else 0.0
                except (ValueError, IndexError):
                    stats['parse_errors'] += 1
                    continue

                if high_px < max(open_px, low_px, close_px) or low_px > min(open_px, high_px, close_px):
                    stats['ohlc_errors'] += 1
                if volume < 0 or turnover < 0 or open_interest < 0:
                    stats['negative_fields'] += 1
                if prev_close is not None and prev_close > 0 and close_px > 0:
                    ratio = max(close_px / prev_close, prev_close / close_px)
                    if ratio >= 20.0:
                        stats['price_anomalies'] += 1
                        if len(stats['price_anomaly_samples']) < 5:
                            stats['price_anomaly_samples'].append({
                                'date': str(key // 10000),
                                'time': f"{key % 10000:04d}",
                                'previous_close': prev_close,
                                'close': close_px,
                                'ratio': round(ratio, 6),
                            })
                if close_px > 0:
                    prev_close = close_px
                if prev_oi is not None:
                    if open_interest < 0:
                        stats['open_interest_jumps'] += 1
                    elif prev_oi >= 0:
                        baseline = max(abs(prev_oi), 1.0)
                        if abs(open_interest - prev_oi) / baseline > 10.0:
                            stats['open_interest_jumps'] += 1
                prev_oi = open_interest
    except Exception as exc:
        stats['parse_errors'] += 1
        stats['error'] = str(exc)

    if keys:
        stats['first_key'] = _format_quality_key(min(keys))
        stats['last_key'] = _format_quality_key(max(keys))
        stats['dates'] = [str(date_val) for date_val in sorted({key // 10000 for key in keys})]
    return stats


def _csv_quality_code_and_period(basename: str) -> Tuple[Optional[str], Optional[str]]:
    for suffix, period in [('_d.csv', 'day'), ('_m1.csv', 'min1'), ('_m5.csv', 'min5')]:
        if basename.endswith(suffix):
            return basename[:-len(suffix)], period
    return None, None


def _scan_date_consistency(files: List[Dict]) -> List[Dict]:
    """比较同一合约日线日期集合和分钟线日期集合。"""
    by_code: Dict[str, Dict[str, set]] = {}
    for stats in files:
        code, period = _csv_quality_code_and_period(stats.get('file', ''))
        if not code or period not in {'day', 'min1', 'min5'}:
            continue
        item = by_code.setdefault(code, {'day': set(), 'minute': set()})
        dates = set(stats.get('dates', []))
        if period == 'day':
            item['day'].update(dates)
        else:
            item['minute'].update(dates)

    mismatches = []
    for code, groups in sorted(by_code.items()):
        day_dates = groups['day']
        minute_dates = groups['minute']
        if not day_dates or not minute_dates:
            continue
        minute_not_in_day = sorted(minute_dates - day_dates)
        day_not_in_minute = sorted(day_dates - minute_dates)
        if minute_not_in_day or day_not_in_minute:
            mismatches.append({
                'code': code,
                'minute_dates_not_in_day': minute_not_in_day,
                'day_dates_not_in_minute': day_not_in_minute,
                'mismatch_count': len(minute_not_in_day) + len(day_not_in_minute),
            })
    return mismatches


def _load_wt_data_helper():
    """加载 wtpy 官方 WtDataHelper；失败时返回 (None, reason)，避免质量报告硬失败。"""
    try:
        from wtpy.wrapper.WtDtHelper import WtDataHelper
    except Exception as first_exc:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        try:
            from wtpy.wrapper.WtDtHelper import WtDataHelper
        except Exception as second_exc:
            return None, f"WtDataHelper unavailable: {second_exc or first_exc}"
    try:
        return WtDataHelper(), None
    except Exception as exc:
        return None, f"WtDataHelper init failed: {exc}"


def _bars_len(records) -> int:
    if records is None:
        return 0
    try:
        return len(records)
    except Exception:
        pass
    ndarray = getattr(records, 'ndarray', None)
    if ndarray is not None:
        try:
            return len(ndarray)
        except Exception:
            pass
    return 0


def _scan_official_dsb_readback(cfg: ConvertConfig, sample_limit: int = 20) -> List[Dict]:
    """使用 wtpy 官方 WtDataHelper 试读 DSB，验证 C/native 读取链路是否可用。"""
    if not cfg.enable_dsb or not os.path.isdir(cfg.dsb_output_dir):
        return []

    dsb_files = []
    for subdir in ['min1', 'min5', 'day']:
        base = os.path.join(cfg.dsb_output_dir, subdir)
        if not os.path.isdir(base):
            continue
        for root, _, names in os.walk(base):
            for name in sorted(names):
                if name.endswith('.dsb'):
                    dsb_files.append((subdir, os.path.join(root, name)))
    dsb_files.sort(key=lambda item: item[1])

    helper, reason = _load_wt_data_helper()
    if helper is None:
        return [{
            'available': False,
            'reason': reason,
            'sample_files': [path for _, path in dsb_files[:sample_limit]],
        }]

    result = []
    for subdir, path in dsb_files[:sample_limit]:
        item = {
            'available': True,
            'subdir': subdir,
            'file': path,
            'bars': 0,
        }
        try:
            records = helper.read_dsb_bars(path)
            item['bars'] = _bars_len(records)
            item['readable'] = records is not None
        except Exception as exc:
            item['available'] = False
            item['readable'] = False
            item['reason'] = str(exc)
        result.append(item)
    return result


def _scan_readback_quality(cfg: ConvertConfig) -> Dict[str, List[Dict]]:
    """扫描 LMDB/DSB 输出，返回 Python 读回统计，不连接原始数据源。"""
    result = {'lmdb': [], 'dsb': []}

    if cfg.enable_lmdb and os.path.isdir(cfg.lmdb_output_dir):
        try:
            lmdb_converter = LmdbConverter(None, cfg.lmdb_output_dir)
            if hasattr(lmdb_converter, 'verify_lmdb'):
                try:
                    for subdir in ['min1', 'min5', 'day']:
                        for exchg in cfg.exchanges:
                            stats = lmdb_converter.verify_lmdb(subdir, exchg)
                            if stats.get('exists'):
                                result['lmdb'].append({
                                    'subdir': subdir,
                                    'exchange': exchg,
                                    'total_keys': stats.get('total_keys', 0),
                                    'codes': stats.get('codes', []),
                                })
                finally:
                    if hasattr(lmdb_converter, 'close_all'):
                        lmdb_converter.close_all()
        except ImportError:
            result['lmdb_error'] = 'lmdb not installed'

    if cfg.enable_dsb and os.path.isdir(cfg.dsb_output_dir):
        try:
            dsb_converter = DsbConverter(None, cfg.dsb_output_dir)
            if hasattr(dsb_converter, 'verify_dsb'):
                for subdir in ['min1', 'min5', 'day']:
                    for exchg in cfg.exchanges:
                        stats = dsb_converter.verify_dsb(subdir, exchg)
                        if stats.get('exists'):
                            result['dsb'].append({
                                'subdir': subdir,
                                'exchange': exchg,
                                'file_count': stats.get('file_count', 0),
                                'total_bars': stats.get('total_bars', 0),
                                'details': stats.get('details', []),
                            })
        except ImportError:
            result['dsb_error'] = 'zstandard not installed'

    return result


def _scan_hots_quality(cfg: ConvertConfig) -> List[Dict]:
    """
    扫描 hots.json 主力映射质量：切换、回切、日期缺口。

    这里不判断“切换是否合理”（需要交易所权威主力规则/成交持仓阈值），只做结构性
    审计，帮助回测前发现主力序列异常抖动或缺日期。
    """
    path = os.path.join(cfg.config_output_dir, 'hots.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            hots = json.load(f)
    except Exception as exc:
        return [{
            'product': '<hots.json>',
            'records': 0,
            'switches': 0,
            'reverts': 0,
            'date_gaps': 0,
            'error': str(exc),
        }]

    result = []
    for product, mapping in sorted(hots.items()):
        if not isinstance(mapping, dict):
            continue
        items = []
        for date_text, code in mapping.items():
            try:
                date_val = int(date_text)
                dt = datetime.strptime(str(date_val), '%Y%m%d')
            except (TypeError, ValueError):
                continue
            items.append((date_val, dt, str(code)))
        items.sort(key=lambda x: x[0])
        switches = 0
        reverts = 0
        date_gaps = 0
        seen_codes = set()
        prev_code = None
        prev_dt = None
        switch_samples = []
        gap_samples = []
        for date_val, dt, code in items:
            if prev_dt is not None:
                gap_days = (dt - prev_dt).days
                if gap_days > 1:
                    date_gaps += 1
                    if len(gap_samples) < 5:
                        gap_samples.append({'from': prev_dt.strftime('%Y%m%d'), 'to': str(date_val), 'days': gap_days})
            if prev_code is not None and code != prev_code:
                switches += 1
                if code in seen_codes:
                    reverts += 1
                if len(switch_samples) < 5:
                    switch_samples.append({'date': str(date_val), 'from': prev_code, 'to': code})
            seen_codes.add(code)
            prev_code = code
            prev_dt = dt
        result.append({
            'product': product,
            'records': len(items),
            'first_date': str(items[0][0]) if items else None,
            'last_date': str(items[-1][0]) if items else None,
            'unique_contracts': len({item[2] for item in items}),
            'switches': switches,
            'reverts': reverts,
            'date_gaps': date_gaps,
            'switch_samples': switch_samples,
            'gap_samples': gap_samples,
        })
    return result


def _emit_hot_artifacts(nested_hots, csv_converter, dsb_converter, periods, stats):
    """按嵌套 hots.json {EXCHG: {product: [events]}} 双层迭代，
    分别调用 csv_converter.build_hot_csv 和 dsb_converter.build_hot_dsb。

    异常按 (exchg, product) 粒度计入 stats['errors'] / stats['error_details']，
    单品种失败不影响其它品种继续。
    """
    if not nested_hots:
        return
    for exchg, prod_map in nested_hots.items():
        if not isinstance(prod_map, dict):
            stats['errors'] += 1
            stats['error_details'].append({
                'stage': 'emit_hot_artifacts',
                'exchange': exchg,
                'product': '',
                'periods': list(periods),
                'error_type': 'StructureError',
                'message': f'hots.json[{exchg}] 必须是 dict<product, events>，得到 {type(prod_map).__name__}',
            })
            continue
        for product, events in prod_map.items():
            if csv_converter is not None:
                try:
                    csv_converter.build_hot_csv(exchg, product, events, periods=periods)
                except Exception as e:
                    logger.error(f"生成HOT CSV失败: {exchg}.{product}, 错误: {e}")
                    stats['errors'] += 1
                    stats['error_details'].append({
                        'stage': 'build_hot_csv',
                        'exchange': exchg,
                        'product': product,
                        'periods': list(periods),
                        'error_type': type(e).__name__,
                        'message': str(e),
                    })
            if dsb_converter is not None:
                try:
                    dsb_converter.build_hot_dsb(exchg, product, events, periods=periods)
                except Exception as e:
                    logger.error(f"生成HOT DSB失败: {exchg}.{product}, 错误: {e}")
                    stats['errors'] += 1
                    stats['error_details'].append({
                        'stage': 'build_hot_dsb',
                        'exchange': exchg,
                        'product': product,
                        'periods': list(periods),
                        'error_type': type(e).__name__,
                        'message': str(e),
                    })


def write_backtest_config_templates(cfg: ConvertConfig) -> Dict[str, str]:
    """
    按当前输出目录生成可直接引用的 wtpy 回测配置模板。

    旧的静态 configbt_*.yaml 使用相对 ../storage/ks_converted 路径；当用户通过
    --output 指向独立数据仓时，模板会漂移。这里使用 ConvertConfig 中已经重算的
    CSV/DSB/LMDB/common 路径生成随输出目录同步的模板。
    """
    os.makedirs(cfg.local_output_base, exist_ok=True)
    common = cfg.config_output_dir
    basefiles = {
        'commodity': os.path.join(common, 'commodities.json'),
        'contract': os.path.join(common, 'contracts.json'),
        'holiday': os.path.join(common, 'holidays.json'),
        'hot': os.path.join(common, 'hots.json'),
        'session': os.path.join(common, 'sessions.json'),
    }
    fees = os.path.join(common, 'fees.json')

    def render(mode: str, data_path: str, module: Optional[str] = None) -> str:
        lines = [
            '# Auto-generated by ks2wtpy; paths follow the selected --output directory.',
            'env:',
            '    mocker: cta',
            '',
            'replayer:',
            f'    mode: {mode}',
            f'    path: {data_path}',
        ]
        if module:
            lines.extend([
                '    store:',
                f'        path: {data_path}',
                f'        module: {module}',
            ])
        lines.extend([
            '    basefiles:',
            f"        commodity: {basefiles['commodity']}",
            f"        contract: {basefiles['contract']}",
            f"        holiday: {basefiles['holiday']}",
            f"        hot: {basefiles['hot']}",
            f"        session: {basefiles['session']}",
            f'    fees: {fees}',
            '    stime: 202101010900',
            '    etime: 202605011500',
            '',
        ])
        return '\n'.join(lines)

    outputs = {
        'csv': os.path.join(cfg.local_output_base, 'configbt_csv.yaml'),
        'dsb': os.path.join(cfg.local_output_base, 'configbt_dsb.yaml'),
        'lmdb': os.path.join(cfg.local_output_base, 'configbt_lmdb.yaml'),
    }
    atomic_write_text(outputs['csv'], render('csv', cfg.csv_output_dir))
    atomic_write_text(outputs['dsb'], render('wtp', cfg.dsb_output_dir, 'WtDataStorage'))
    atomic_write_text(outputs['lmdb'], render('wtp', cfg.lmdb_output_dir, 'WtDataStorageAD'))
    logger.info(f"回测配置模板: {outputs['csv']}, {outputs['dsb']}, {outputs['lmdb']}")
    return outputs


def _period_to_csv_suffix(period: str) -> Optional[str]:
    mapping = {
        'min1': '_m1.csv',
        'm1': '_m1.csv',
        'kline_1m': '_m1.csv',
        'min5': '_m5.csv',
        'm5': '_m5.csv',
        'kline_5m': '_m5.csv',
        'day': '_d.csv',
        'd': '_d.csv',
        'kline_1d': '_d.csv',
    }
    return mapping.get(period)


def _csv_path_for_code_period(cfg: ConvertConfig, std_code: str, period: str) -> str:
    suffix = _period_to_csv_suffix(period)
    if not suffix:
        raise ValueError(f'Unsupported period for CSV view: {period}')
    return os.path.join(cfg.csv_output_dir, f'{std_code}{suffix}')


def _scan_csv_dates_and_rows(path: str, is_day: bool) -> Tuple[int, set]:
    rows = 0
    dates = set()
    if not os.path.exists(path):
        return rows, dates
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            key = _csv_key_from_row(row, is_day=is_day)
            if key is None:
                continue
            rows += 1
            dates.add(str(key // 10000))
    return rows, dates


def _live_preflight_block_reason(check: Dict) -> str:
    reasons = []
    if check.get('available_bars', 0) < check.get('min_bars', 0):
        reasons.append(f"bars {check.get('available_bars', 0)} < {check.get('min_bars', 0)}")
    missing_dates = check.get('missing_dates') or []
    if missing_dates:
        reasons.append(f"missing_dates={','.join(map(str, missing_dates))}")
    if not reasons:
        reasons.append('preflight check failed')
    return f"{check.get('std_code')} {check.get('period')}: {'; '.join(reasons)}"


def write_live_gate_and_alerts(cfg: ConvertConfig, report: Dict, report_path: str) -> Tuple[str, str]:
    """Write machine-consumable startup gate and JSONL alert events for live preflight."""
    failed_checks = [check for check in report.get('checks', []) if check.get('status') != 'pass']
    block_reasons = [_live_preflight_block_reason(check) for check in failed_checks]
    gate = {
        'version': 1,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'scenario': 'live_history_startup_gate',
        'allow_start': report.get('decision') == 'pass' and not failed_checks,
        'decision': report.get('decision', 'block'),
        'report': report_path,
        'failed_checks': len(failed_checks),
        'block_reasons': block_reasons,
        'safety': report.get('safety', {}),
    }
    gate_path = os.path.join(cfg.local_output_base, 'live_gate.json')
    atomic_json_dump(gate_path, gate)

    alert_lines = []
    for check, reason in zip(failed_checks, block_reasons):
        alert_lines.append(json.dumps({
            'version': 1,
            'generated_at': gate['generated_at'],
            'severity': 'critical',
            'event': 'live_history_preflight_block',
            'std_code': check.get('std_code'),
            'period': check.get('period'),
            'reason': reason,
            'available_bars': check.get('available_bars'),
            'min_bars': check.get('min_bars'),
            'missing_dates': check.get('missing_dates', []),
            'report': report_path,
        }, ensure_ascii=False))
    alerts_path = os.path.join(cfg.local_output_base, 'live_alerts.jsonl')
    atomic_write_text(alerts_path, ('\n'.join(alert_lines) + '\n') if alert_lines else '', encoding='utf-8')
    return gate_path, alerts_path


def write_live_preflight_report(cfg: ConvertConfig, required: Optional[Dict] = None) -> str:
    """
    写入实盘历史数据加载前的只读预检报告。

    该函数只检查离线仓库，不写实时 dmb/rt 目录；发现必需历史缺失时给出 block 决策。
    required 结构: {"EXCHG.code": {"min1": {"min_bars": N, "dates": [YYYYMMDD, ...]}}}
    """
    required = required or {}
    checks = []
    failed = 0
    for std_code, period_reqs in sorted(required.items()):
        for period, rule in sorted(period_reqs.items()):
            min_bars = int(rule.get('min_bars', 0) or 0)
            expected_dates = {str(d) for d in rule.get('dates', [])}
            suffix = _period_to_csv_suffix(period)
            is_day = suffix == '_d.csv'
            csv_path = _csv_path_for_code_period(cfg, std_code, period)
            rows, available_dates = _scan_csv_dates_and_rows(csv_path, is_day=is_day)
            missing_dates = sorted(expected_dates - available_dates)
            status = 'pass' if rows >= min_bars and not missing_dates else 'fail'
            if status == 'fail':
                failed += 1
            checks.append({
                'std_code': std_code,
                'period': period,
                'status': status,
                'csv_path': csv_path,
                'available_bars': rows,
                'min_bars': min_bars,
                'available_dates': sorted(available_dates),
                'required_dates': sorted(expected_dates),
                'missing_dates': missing_dates,
            })

    realtime_dmb_files = []
    for root_name in ['rt', 'dmb']:
        root_path = os.path.join(cfg.local_output_base, root_name)
        if not os.path.isdir(root_path):
            continue
        for root, _, names in os.walk(root_path):
            for name in names:
                if name.endswith('.dmb'):
                    realtime_dmb_files.append(os.path.join(root, name))

    report = {
        'version': 1,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'scenario': 'live_history_preflight',
        'decision': 'pass' if failed == 0 else 'block',
        'summary': {
            'checks': len(checks),
            'failed_checks': failed,
            'realtime_dmb_files': len(realtime_dmb_files),
        },
        'safety': {
            'reads_offline_storage': True,
            'writes_realtime_dmb': False,
            'realtime_dmb_files_detected': realtime_dmb_files[:50],
        },
        'outputs': {
            'csv': cfg.csv_output_dir,
            'lmdb': cfg.lmdb_output_dir,
            'dsb': cfg.dsb_output_dir,
            'basefiles': cfg.config_output_dir,
        },
        'checks': checks,
    }
    path = os.path.join(cfg.local_output_base, 'live_preflight_report.json')
    report['gate'] = os.path.join(cfg.local_output_base, 'live_gate.json')
    report['alerts'] = os.path.join(cfg.local_output_base, 'live_alerts.jsonl')
    atomic_json_dump(path, report)
    write_live_gate_and_alerts(cfg, report, path)
    logger.info(f'实盘历史预检报告: {path}')
    return path


def _research_row_from_csv(std_code: str, period: str, row: List[str], is_day: bool) -> Optional[List[str]]:
    try:
        if is_day:
            date_text = row[0].replace('/', '-')
            values = row[1:8]
            dt_text = f'{date_text} 00:00:00'
        else:
            date_text = row[0].replace('/', '-')
            time_text = row[1]
            values = row[2:9]
            dt_text = f'{date_text} {time_text}'
        if len(values) < 7:
            return None
        return [dt_text, std_code, period] + values[:7]
    except Exception:
        return None


def _load_pyarrow():
    """Best-effort pyarrow loader for optional research parquet export."""
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
        return pa.Table, pq, None
    except Exception as exc:
        return None, None, str(exc)


def _research_row_dict(std_code: str, period: str, row: List[str], is_day: bool) -> Optional[Dict]:
    item = _research_row_from_csv(std_code, period, row, is_day=is_day)
    if not item:
        return None
    fields = ['datetime', 'std_code', 'period', 'open', 'high', 'low', 'close', 'volume', 'turnover', 'open_interest']
    result: Dict = dict(zip(fields, item))
    for key in ['open', 'high', 'low', 'close', 'volume', 'turnover', 'open_interest']:
        try:
            result[key] = float(result[key])
        except Exception:
            pass
    return result


def export_research_view(cfg: ConvertConfig, codes: Optional[List[str]] = None,
                         periods: Optional[List[str]] = None,
                         output_dir: Optional[str] = None) -> Dict:
    """
    从转换后的 WT CSV 仓库派生投研 CSV 视图，并写入 lineage 元数据。

    派生视图不是权威存储；权威源仍是 ks2wtpy 转换后的 csv/lmdb/dsb/common 与 manifest/quality_report。
    """
    periods = periods or ['min1', 'min5', 'day']
    output_dir = output_dir or os.path.join(cfg.local_output_base, 'research')
    os.makedirs(output_dir, exist_ok=True)

    if codes is None:
        codes = []
        if os.path.isdir(cfg.csv_output_dir):
            for name in sorted(os.listdir(cfg.csv_output_dir)):
                code, period = _csv_quality_code_and_period(name)
                if code and code not in codes:
                    codes.append(code)

    table_cls, parquet_module, parquet_error = _load_pyarrow()
    parquet_available = table_cls is not None and parquet_module is not None
    csv_files = []
    parquet_files = []
    datasets = []
    header = 'datetime,std_code,period,open,high,low,close,volume,turnover,open_interest\n'
    for std_code in codes:
        for period in periods:
            suffix = _period_to_csv_suffix(period)
            if not suffix:
                continue
            src = _csv_path_for_code_period(cfg, std_code, period)
            if not os.path.exists(src):
                continue
            is_day = suffix == '_d.csv'
            rows_out = []
            row_dicts = []
            with open(src, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    item = _research_row_from_csv(std_code, period, row, is_day=is_day)
                    if item:
                        rows_out.append(item)
                    row_dict = _research_row_dict(std_code, period, row, is_day=is_day)
                    if row_dict:
                        row_dicts.append(row_dict)
            out_name = f'{std_code}_{period}_research.csv'.replace('/', '_')
            out_path = os.path.join(output_dir, out_name)
            lines = [header]
            lines.extend(','.join(map(str, row)) + '\n' for row in rows_out)
            atomic_write_text(out_path, ''.join(lines), encoding='utf-8')
            csv_files.append(out_path)
            dataset = {
                'std_code': std_code,
                'period': period,
                'source_csv': src,
                'derived_csv': out_path,
                'rows': len(rows_out),
            }
            if parquet_available:
                assert table_cls is not None and parquet_module is not None
                parquet_name = f'{std_code}_{period}_research.parquet'.replace('/', '_')
                parquet_path = os.path.join(output_dir, parquet_name)
                parquet_module.write_table(table_cls.from_pylist(row_dicts), parquet_path)
                parquet_files.append(parquet_path)
                dataset['derived_parquet'] = parquet_path
                dataset['parquet_status'] = 'written'
            else:
                dataset['parquet_status'] = 'skipped'
            datasets.append(dataset)

    formats = ['csv'] + (['parquet'] if parquet_files else [])
    metadata = {
        'version': 1,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'scenario': 'research_analysis',
        'authority_source': 'wt_storage_converted_outputs',
        'derived_view_only': True,
        'formats': formats,
        'parquet': {
            'status': 'available' if parquet_files else 'unavailable',
            'files': parquet_files,
            'reason': '' if parquet_files else (parquet_error or 'no parquet files written'),
        },
        'inputs': {
            'conversion_manifest': os.path.join(cfg.local_output_base, 'conversion_manifest.json'),
            'quality_report': os.path.join(cfg.local_output_base, 'quality_report.json'),
            'csv_dir': cfg.csv_output_dir,
            'lmdb_dir': cfg.lmdb_output_dir,
            'dsb_dir': cfg.dsb_output_dir,
            'basefiles': cfg.config_output_dir,
        },
        'datasets': datasets,
    }
    metadata_path = os.path.join(output_dir, 'research_view_metadata.json')
    atomic_json_dump(metadata_path, metadata)
    logger.info(f'投研派生视图元数据: {metadata_path}')
    return {'metadata_path': metadata_path, 'csv_files': csv_files, 'parquet_files': parquet_files, 'datasets': datasets}


def write_quality_report(cfg: ConvertConfig) -> str:
    """写入转换后质量报告，覆盖 CSV 基础质量和 LMDB/DSB Python 读回统计。"""
    files = []
    summary = {
        'csv_files': 0,
        'csv_rows': 0,
        'duplicates': 0,
        'out_of_order': 0,
        'ohlc_errors': 0,
        'negative_fields': 0,
        'parse_errors': 0,
        'time_gaps': 0,
        'open_interest_jumps': 0,
        'price_anomalies': 0,
        'day_minute_date_mismatches': 0,
        'hots_products': 0,
        'hots_switches': 0,
        'hots_reverts': 0,
        'hots_date_gaps': 0,
        'lmdb_keys': 0,
        'dsb_files': 0,
        'dsb_bars': 0,
        'dsb_official_files': 0,
        'dsb_official_bars': 0,
    }

    if cfg.enable_csv and os.path.isdir(cfg.csv_output_dir):
        for name in sorted(os.listdir(cfg.csv_output_dir)):
            if not name.endswith('.csv'):
                continue
            # 跳过 HOT 复权产物 *_d_logret.csv / *_m1_logret.csv：
            # 它们多出 logret/code 两列且后缀不是标准 _d.csv/_m1.csv，
            # _scan_csv_quality 的 is_day 判定会落到分钟线分支并误把 close
            # 当 time_str 解析，导致 parse_errors 等于其全部行数。
            # logret 是衍生指标，质量已由源文件 *_d.csv / *_m1.csv 覆盖。
            if name.endswith('_logret.csv'):
                continue
            stats = _scan_csv_quality(os.path.join(cfg.csv_output_dir, name))
            files.append(stats)
            summary['csv_files'] += 1
            summary['csv_rows'] += stats['rows']
            for key in ['duplicates', 'out_of_order', 'ohlc_errors', 'negative_fields', 'parse_errors', 'time_gaps', 'open_interest_jumps', 'price_anomalies']:
                summary[key] += stats[key]

    date_consistency = _scan_date_consistency(files)
    summary['day_minute_date_mismatches'] = sum(item['mismatch_count'] for item in date_consistency)

    hots_quality = _scan_hots_quality(cfg)
    summary['hots_products'] = len([item for item in hots_quality if item.get('product') != '<hots.json>'])
    summary['hots_switches'] = sum(item.get('switches', 0) for item in hots_quality)
    summary['hots_reverts'] = sum(item.get('reverts', 0) for item in hots_quality)
    summary['hots_date_gaps'] = sum(item.get('date_gaps', 0) for item in hots_quality)

    readback = _scan_readback_quality(cfg)
    for item in readback['lmdb']:
        summary['lmdb_keys'] += item.get('total_keys', 0)
    for item in readback['dsb']:
        summary['dsb_files'] += item.get('file_count', 0)
        summary['dsb_bars'] += item.get('total_bars', 0)

    dsb_official = _scan_official_dsb_readback(cfg)
    for item in dsb_official:
        if item.get('available') and 'file' in item:
            summary['dsb_official_files'] += 1
            summary['dsb_official_bars'] += item.get('bars', 0)

    report = {
        'version': 1,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'source': _source_summary(cfg),
        'outputs': {
            'base': cfg.local_output_base,
            'csv': cfg.csv_output_dir,
            'lmdb': cfg.lmdb_output_dir,
            'dsb': cfg.dsb_output_dir,
        },
        'summary': summary,
        'files': files,
        'date_consistency': date_consistency,
        'hots': hots_quality,
        'lmdb': readback['lmdb'],
        'dsb': readback['dsb'],
        'dsb_official': dsb_official,
    }
    if 'lmdb_error' in readback:
        report['lmdb_error'] = readback['lmdb_error']
    if 'dsb_error' in readback:
        report['dsb_error'] = readback['dsb_error']
    path = os.path.join(cfg.local_output_base, 'quality_report.json')
    atomic_json_dump(path, report)
    logger.info(f"质量报告: {path}")
    return path


def get_products_to_convert(cfg: ConvertConfig) -> Dict[str, List[str]]:
    """获取需要转换的品种列表

    返回: {交易所: [品种1, 品种2, ...]}
    """
    if cfg.specific_products:
        return cfg.specific_products

    result = {}
    for exchg in cfg.exchanges:
        products = list(FUTURES_PRODUCTS.get(exchg, []))
        # 纯期权独立品种目录（CFFEX 的 IO/HO/MO）默认不混入期货品种列表：
        # 这些目录里没有期货合约，只有期权+合成主连。
        # 对于期货+期权共享品种目录（如 SHFE/cu、CZCE/SR），不在此排除——
        # 转换时由 is_option_code() 在合约层面过滤期权，期货合约正常保留。
        # 当 include_cffex_options=True 时，把 OPTION_ONLY_PRODUCTS 中
        # 该交易所声明的纯期权品种追加到末尾，让一次性导出同时拿到期货+期权；
        # converter 在遇到这些品种时走 option_only 分支。
        if cfg.include_cffex_options:
            for opt_prod in OPTION_ONLY_PRODUCTS.get(exchg, []):
                if opt_prod not in products:
                    products.append(opt_prod)
        if products:
            result[exchg] = products
    return result


def is_option_only_product(exchg: str, product: str) -> bool:
    """判断品种是否属于纯期权独立目录 (OPTION_ONLY_PRODUCTS)。

    用于 converter / config_generator 决定是否走 option_only 分支。
    """
    return product in OPTION_ONLY_PRODUCTS.get(exchg, [])


def run_full_convert(cfg: ConvertConfig):
    """执行全量转换"""
    logger.info("=" * 60)
    logger.info("KS → wtpy 全量转换开始")
    logger.info(f"输出目录: {cfg.local_output_base}")
    logger.info(f"转换模式: CSV={cfg.enable_csv}, LMDB={cfg.enable_lmdb}, 配置={cfg.enable_config}")
    logger.info("=" * 60)

    # 创建数据读取器
    ks_reader = create_reader(cfg)

    # 获取品种列表
    products_map = get_products_to_convert(cfg)
    periods = cfg.periods

    # 加载状态
    state = load_state(cfg.state_file) if cfg.incremental else {}

    # 创建转换器
    csv_converter = None
    lmdb_converter = None
    dsb_converter = None

    if cfg.enable_csv:
        os.makedirs(cfg.csv_output_dir, exist_ok=True)
        csv_converter = CsvConverter(ks_reader, cfg.csv_output_dir, cfg.incremental, include_option=cfg.include_option)

    if cfg.enable_lmdb:
        os.makedirs(cfg.lmdb_output_dir, exist_ok=True)
        try:
            lmdb_converter = LmdbConverter(ks_reader, cfg.lmdb_output_dir, cfg.incremental, include_option=cfg.include_option)
        except ImportError:
            logger.warning("lmdb库未安装，跳过LMDB转换。安装: pip install lmdb")
            lmdb_converter = None

    if cfg.enable_dsb:
        os.makedirs(cfg.dsb_output_dir, exist_ok=True)
        try:
            dsb_converter = DsbConverter(ks_reader, cfg.dsb_output_dir, use_compression=True, include_option=cfg.include_option)
        except ImportError:
            logger.warning("zstandard库未安装，跳过DSB转换。安装: pip install zstandard")
            dsb_converter = None

    # 逐交易所、品种转换
    total_stats = {
        'csv_codes': 0,
        'lmdb_bars': 0,
        'errors': 0,
        'error_details': [],
        'start_time': time.time(),
    }

    for exchg, products in products_map.items():
        logger.info(f"\n{'='*40}")
        logger.info(f"处理交易所: {exchg} ({len(products)}个品种)")
        logger.info(f"{'='*40}")

        for product in products:
            logger.info(f"\n--- {exchg}.{product} ---")

            try:
                # ---- 性能优化：S3 数据并行预热 ----
                # 在 csv/lmdb/dsb 三阶段开始前，先并行下载+解析所有需要的 tar.gz
                # 让后续阶段全部命中内存缓存，避免 4 次重复 gzip+tar+CSV 解析。
                # 仅当 reader 支持 prefetch（S3KsReader）时启用。
                if hasattr(ks_reader, 'prefetch_minute_dates'):
                    try:
                        # 分批预热 + 不 pin：prefetch_minute_dates 支持 batch_size
                        # 控制同时在线的 tar 数。pin=False 让 FIFO 淘汰旧日期，
                        # 峰值 ≈ batch_size + MEM_CACHE_ITEMS 条目，可预测可控。
                        # 三阶段 (CSV→LMDB→DSB) 按日期序遍历，最近的 batch
                        # 在缓存中命中，远的日期需重新解析磁盘 cache (快于 S3)。
                        _pin = False
                        for period in periods:
                            if period == 'kline_1d':
                                if hasattr(ks_reader, 'prefetch_day'):
                                    ks_reader.prefetch_day(exchg, product, pin=_pin)
                            else:
                                all_dates = ks_reader.list_date_dirs(exchg, product, period)
                                if all_dates:
                                    ks_reader.prefetch_minute_dates(
                                        exchg, product, period, all_dates, pin=_pin)
                    except Exception as exc:
                        logger.warning(f"预热失败（不影响转换继续）: {exchg}.{product} {exc}")

                # 纯期权品种走 option_only 分支
                _is_opt_only = is_option_only_product(exchg, product)

                # CSV转换
                if csv_converter:
                    csv_converter.option_only = _is_opt_only
                    stats = csv_converter.convert_product(exchg, product, periods)
                    for period, count in stats.items():
                        total_stats['csv_codes'] += count
                        logger.info(f"  CSV {period}: {count}个合约")

                # LMDB转换
                if lmdb_converter:
                    lmdb_converter.option_only = _is_opt_only
                    stats = lmdb_converter.convert_product(exchg, product, periods)
                    for period, count in stats.items():
                        total_stats['lmdb_bars'] += count
                        logger.info(f"  LMDB {period}: {count}条")

                # DSB转换
                if dsb_converter:
                    dsb_converter.option_only = _is_opt_only
                    stats = dsb_converter.convert_product(exchg, product, periods)
                    for period, count in stats.items():
                        total_stats.setdefault('dsb_bars', 0)
                        total_stats['dsb_bars'] += count
                        logger.info(f"  DSB {period}: {count}条")

                # 更新状态
                state_key = f"{exchg}.{product}"
                state[state_key] = {
                    'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'periods': periods,
                }

                # 单品种处理完毕：清掉该品种的 pinned 内存缓存，避免多品种累积爆内存
                if hasattr(ks_reader, 'clear_mem_cache'):
                    # 仅在多品种且非最后一个时清；这里简单全清，每个品种独立预热
                    ks_reader.clear_mem_cache()

            except Exception as e:
                logger.error(f"转换失败: {exchg}.{product}, 错误: {e}")
                total_stats['errors'] += 1
                total_stats['error_details'].append({
                    'stage': 'convert_product',
                    'exchange': exchg,
                    'product': product,
                    'periods': list(periods),
                    'error_type': type(e).__name__,
                    'message': str(e),
                })

    # 关闭LMDB
    if lmdb_converter:
        lmdb_converter.close_all()

    # 生成配置文件
    if cfg.enable_config:
        logger.info("\n" + "=" * 40)
        logger.info("生成基础配置文件")
        logger.info("=" * 40)

        config_gen = ConfigGenerator(ks_reader, cfg.config_output_dir)

        # 收集日线数据用于生成hots.json
        bars_by_product = config_gen.collect_bars_for_hots(
            exchanges=cfg.exchanges,
            products=products_map,
        )

        configs = config_gen.generate_all(
            bars_by_product,
            products_filter=products_map,
            option_products=(OPTION_ONLY_PRODUCTS if cfg.include_cffex_options else None),
        )
        config_gen.save_all(configs)
        if csv_converter or dsb_converter:
            nested_hots = configs.get('hots.json') or {}
            _emit_hot_artifacts(
                nested_hots,
                csv_converter,
                dsb_converter,
                periods,
                total_stats,
            )
        write_backtest_config_templates(cfg)

    # 保存状态
    save_state(cfg.state_file, state)

    # 汇总
    elapsed = time.time() - total_stats['start_time']
    logger.info("\n" + "=" * 60)
    logger.info("转换完成!")
    logger.info(f"  CSV合约数: {total_stats['csv_codes']}")
    logger.info(f"  LMDB写入条数: {total_stats['lmdb_bars']}")
    logger.info(f"  DSB写入条数: {total_stats.get('dsb_bars', 0)}")
    logger.info(f"  错误数: {total_stats['errors']}")
    logger.info(f"  耗时: {elapsed:.1f}秒")
    write_conversion_manifest(cfg, 'full', total_stats, elapsed)
    write_quality_report(cfg)
    logger.info("=" * 60)


def run_incremental_update(cfg: ConvertConfig, new_dates: List[int] = None):
    """执行增量更新

    @new_dates 指定新增日期列表，为None时自动检测
    """
    logger.info("=" * 60)
    logger.info("KS → wtpy 增量更新开始")
    logger.info("=" * 60)

    # 加载状态
    state = load_state(cfg.state_file)

    # 创建数据读取器
    ks_reader = create_reader(cfg)

    # 获取品种列表
    products_map = get_products_to_convert(cfg)
    periods = [p for p in cfg.periods if p != 'kline_1d']  # 日线增量比较特殊

    # 创建转换器
    csv_converter = None
    lmdb_converter = None
    dsb_converter = None

    if cfg.enable_csv:
        csv_converter = CsvConverter(ks_reader, cfg.csv_output_dir, incremental=True, include_option=cfg.include_option)

    if cfg.enable_lmdb:
        try:
            lmdb_converter = LmdbConverter(ks_reader, cfg.lmdb_output_dir, incremental=True, include_option=cfg.include_option)
        except ImportError:
            lmdb_converter = None

    if cfg.enable_dsb:
        try:
            dsb_converter = DsbConverter(ks_reader, cfg.dsb_output_dir, use_compression=True, include_option=cfg.include_option)
        except ImportError:
            dsb_converter = None

    total_new = 0
    total_stats = {
        'new_items': 0,
        'errors': 0,
        'error_details': [],
        'start_time': time.time(),
    }

    for exchg, products in products_map.items():
        for product in products:
            logger.info(f"增量更新: {exchg}.{product}")

            try:
                # 检查每个分钟线周期的新日期
                for period in periods:
                    if period not in WTPY_SUPPORTED_PERIODS:
                        continue

                    # 获取KS中的日期目录
                    all_dates = ks_reader.list_date_dirs(exchg, product, period)

                    # 从状态中获取上次转换的日期
                    state_key = f"{exchg}.{product}.{period}"
                    last_date = state.get(state_key, {}).get('last_date', 0)

                    # 筛选新日期
                    if new_dates:
                        new = [d for d in all_dates if d in set(new_dates)]
                    else:
                        new = [d for d in all_dates if d > last_date]

                    if not new:
                        continue

                    logger.info(f"  {period}: 发现{len(new)}个新日期 ({new[0]}-{new[-1]})")

                    # 纯期权品种走 option_only 分支
                    _is_opt_only = is_option_only_product(exchg, product)

                    # CSV增量
                    if csv_converter:
                        csv_converter.option_only = _is_opt_only
                        stats = csv_converter.convert_product(exchg, product, [period], dates=new)
                        added = sum(stats.values())
                        total_new += added
                        total_stats['new_items'] += added

                    # LMDB增量
                    if lmdb_converter:
                        lmdb_converter.option_only = _is_opt_only
                        stats = lmdb_converter.convert_product(exchg, product, [period], dates=new)
                        added = sum(stats.values())
                        total_new += added
                        total_stats['new_items'] += added

                    # DSB增量 (重新生成整个DSB文件)
                    if dsb_converter:
                        dsb_converter.option_only = _is_opt_only
                        stats = dsb_converter.convert_product(exchg, product, [period], dates=new)
                        added = sum(stats.values())
                        total_new += added
                        total_stats['new_items'] += added

                    # 更新状态
                    state[state_key] = {
                        'last_date': max(new),
                        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    }

                # 日线增量: 重新读取所有日线（日线文件是按合约合并的）
                if 'kline_1d' in cfg.periods:
                    state_key = f"{exchg}.{product}.kline_1d"
                    last_update = state.get(state_key, {}).get('last_update', '')

                    if csv_converter:
                        stats = csv_converter.convert_product(exchg, product, ['kline_1d'])
                        added = sum(stats.values())
                        total_new += added
                        total_stats['new_items'] += added

                    if lmdb_converter:
                        stats = lmdb_converter.convert_product(exchg, product, ['kline_1d'])
                        added = sum(stats.values())
                        total_new += added
                        total_stats['new_items'] += added

                    if dsb_converter:
                        stats = dsb_converter.convert_product(exchg, product, ['kline_1d'])
                        added = sum(stats.values())
                        total_new += added
                        total_stats['new_items'] += added

                    state[state_key] = {
                        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    }

            except Exception as e:
                logger.error(f"增量更新失败: {exchg}.{product}, 错误: {e}")
                total_stats['errors'] += 1

    # 关闭LMDB
    if lmdb_converter:
        lmdb_converter.close_all()

    # 保存状态
    save_state(cfg.state_file, state)

    elapsed = time.time() - total_stats['start_time']
    write_conversion_manifest(
        cfg,
        'incremental',
        total_stats,
        elapsed,
        extra={'requested_dates': new_dates},
    )
    write_quality_report(cfg)
    logger.info(f"\n增量更新完成! 新增数据: {total_new}")


def run_config_only(cfg: ConvertConfig):
    """只生成配置文件"""
    logger.info("生成基础配置文件...")

    ks_reader = create_reader(cfg)

    config_gen = ConfigGenerator(ks_reader, cfg.config_output_dir)

    # 收集日线数据用于hots
    products_map = get_products_to_convert(cfg)
    bars_by_product = config_gen.collect_bars_for_hots(
        exchanges=cfg.exchanges,
        products=products_map,
    )

    configs = config_gen.generate_all(
        bars_by_product,
        products_filter=products_map,
        option_products=(OPTION_ONLY_PRODUCTS if cfg.include_cffex_options else None),
    )
    config_gen.save_all(configs)
    write_backtest_config_templates(cfg)

    logger.info("配置文件生成完成!")


def verify_output(cfg: ConvertConfig):
    """验证转换结果"""
    logger.info("验证转换结果...")

    # 验证CSV
    if cfg.enable_csv and os.path.exists(cfg.csv_output_dir):
        csv_files = [f for f in os.listdir(cfg.csv_output_dir) if f.endswith('.csv')]
        logger.info(f"CSV文件数: {len(csv_files)}")
        for f in csv_files[:5]:
            filepath = os.path.join(cfg.csv_output_dir, f)
            with open(filepath, 'r') as fh:
                lines = fh.readlines()
                logger.info(f"  {f}: {len(lines)-1}条数据")

    # 验证LMDB
    if cfg.enable_lmdb and os.path.exists(cfg.lmdb_output_dir):
        try:
            lmdb_converter = LmdbConverter(None, cfg.lmdb_output_dir)
            try:
                for subdir in ['min5', 'min1', 'day']:
                    for exchg in cfg.exchanges:
                        stats = lmdb_converter.verify_lmdb(subdir, exchg)
                        if stats.get('exists'):
                            logger.info(f"  LMDB {subdir}/{exchg}: {stats['total_keys']}条, "
                                        f"{len(stats['codes'])}个合约")
            finally:
                lmdb_converter.close_all()
        except ImportError:
            logger.warning("lmdb库未安装，跳过LMDB验证")

    # 验证DSB: 读取文件头并解压 payload，检查 bar_count
    if cfg.enable_dsb and os.path.exists(cfg.dsb_output_dir):
        try:
            dsb_converter = DsbConverter(None, cfg.dsb_output_dir)
            for subdir in ['min5', 'min1', 'day']:
                for exchg in cfg.exchanges:
                    stats = dsb_converter.verify_dsb(subdir, exchg)
                    if stats.get('exists'):
                        logger.info(f"  DSB {subdir}/{exchg}: {stats['file_count']}个文件, "
                                    f"抽样读回{stats['total_bars']}条")
        except ImportError:
            logger.warning("zstandard库未安装，跳过DSB验证")


def _build_arg_parser():
    """构建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        description='KS数据 → wtpy回测数据 转换系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 全量转换（CSV + LMDB + 配置文件）
  python main.py --full --remote KS_HOST

  # 增量更新
  python main.py --incremental --remote KS_HOST

  # 只转换CSV
  python main.py --full --csv-only --remote KS_HOST

  # 指定品种和周期
  python main.py --full --products SHFE.rb,DCE.i --periods m5,d --remote KS_HOST

  # 从本地目录转换
  python main.py --full --local /path/to/ks-data

  # 只生成配置文件
  python main.py --config-only --remote KS_HOST

  # 验证转换结果
  python main.py --verify --remote KS_HOST
        """,
    )

    # 运行模式
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument('--full', action='store_true', help='全量转换')
    mode_group.add_argument('--incremental', action='store_true', help='增量更新')
    mode_group.add_argument('--config-only', action='store_true', help='只生成配置文件')
    mode_group.add_argument('--verify', action='store_true', help='验证转换结果')
    mode_group.add_argument('--live-preflight', action='store_true', help='只读实盘历史加载预检，不连接KS源、不写实时dmb')
    mode_group.add_argument('--research-export', action='store_true', help='从转换后的离线仓派生投研CSV视图和lineage元数据')
    mode_group.add_argument('--tick', action='store_true', help='Tick数据转换: S3→WTSTickStruct→DSB (按日+按合约)')

    # 数据源
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument('--remote', metavar='HOST', help='远程服务器地址')
    source_group.add_argument('--local', metavar='PATH', help='本地KS数据目录')
    source_group.add_argument('--s3', action='store_true', help='使用MinIO S3数据源(无需SSH)')

    # 转换选项
    parser.add_argument('--csv-only', action='store_true', help='只转换CSV')
    parser.add_argument('--lmdb-only', action='store_true', help='只转换LMDB')
    parser.add_argument('--dsb-only', action='store_true', help='只转换DSB')
    parser.add_argument('--no-csv', action='store_true', help='不转换CSV')
    parser.add_argument('--no-lmdb', action='store_true', help='不转换LMDB')
    parser.add_argument('--no-dsb', action='store_true', help='不转换DSB')
    parser.add_argument('--no-config', action='store_true', help='不生成配置文件')

    # 过滤
    parser.add_argument('--exchanges', metavar='EX1,EX2', help='指定交易所(逗号分隔)')
    parser.add_argument('--products', metavar='EXCHG.PROD1,EXCHG.PROD2',
                        help='指定品种(逗号分隔, 如 SHFE.rb,DCE.i)')
    parser.add_argument('--periods', metavar='m5,d,m1', default='m5,d',
                        help='指定周期(逗号分隔, 默认m5,d)')
    parser.add_argument('--dates', metavar='YYYYMMDD,...',
                        help='增量模式下指定需要转换的日期(逗号分隔，仅分钟线)')
    parser.add_argument('--include-option', action='store_true',
                        help='包含期权合约，默认过滤期权')
    parser.add_argument('--include-cffex-options', action='store_true',
                        help='启用 CFFEX 纯期权独立流水线 (IO/HO/MO)，'
                             '默认关闭以避免误转股指期权')

    # 输出
    parser.add_argument('--output', metavar='PATH', help='输出目录')
    parser.add_argument('--live-requirements', metavar='PATH', default='',
                        help='实盘历史预检需求JSON，结构为 {"EXCHG.code": {"min1": {"min_bars": N, "dates": [...]}}}')
    parser.add_argument('--research-codes', metavar='EXCHG.code,...', default='',
                        help='投研导出的stdCode列表，逗号分隔；为空时扫描CSV目录')
    parser.add_argument('--research-periods', metavar='min1,min5,day', default='min1,min5,day',
                        help='投研导出的周期列表，逗号分隔')
    parser.add_argument('--research-output', metavar='PATH', default='',
                        help='投研派生视图输出目录，默认 --output/research')
    parser.add_argument('--remote-user', default='', help='远程SSH用户名')
    parser.add_argument('--remote-path', default='', help='远程KS数据路径')

    # Tick专用选项
    parser.add_argument('--tick-dates', metavar='YYYYMMDD,...', default='',
                        help='Tick模式指定日期(逗号分隔), 空=全部可用日期')
    parser.add_argument('--tick-code', metavar='CODE', default='',
                        help='Tick模式只转换指定合约代码')

    # S3
    parser.add_argument('--s3-endpoint', default=os.environ.get('KS2WTPY_S3_ENDPOINT', ''),
                        help='S3/MinIO endpoint，也可用KS2WTPY_S3_ENDPOINT')
    parser.add_argument('--s3-access-key', default=os.environ.get('KS2WTPY_S3_ACCESS_KEY', ''),
                        help='S3 access key，也可用KS2WTPY_S3_ACCESS_KEY')
    parser.add_argument('--s3-secret-key', default=os.environ.get('KS2WTPY_S3_SECRET_KEY', ''),
                        help='S3 secret key，也可用KS2WTPY_S3_SECRET_KEY')
    parser.add_argument('--s3-bucket', default=os.environ.get('KS2WTPY_S3_BUCKET', 'ks-data'),
                        help='S3 bucket，默认ks-data')
    parser.add_argument('--s3-secure', action='store_true', help='S3使用HTTPS')
    parser.add_argument('--s3-cache-dir', default='', help='S3本地缓存目录')

    # 其他
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    parser.add_argument('--force', action='store_true', help='强制全量（忽略增量状态）')

    return parser


def parse_args_from_list(argv: Optional[List[str]] = None):
    """解析给定参数列表，便于测试复用。"""
    return _build_arg_parser().parse_args(argv)


def parse_args():
    """解析命令行参数"""
    return parse_args_from_list(None)


def parse_dates_arg(dates_arg: Optional[str]) -> Optional[List[int]]:
    if not dates_arg:
        return None
    dates = []
    for item in dates_arg.split(','):
        item = item.strip()
        if not item:
            continue
        if not (item.isdigit() and len(item) == 8):
            raise ValueError(f"非法日期格式: {item}, 应为YYYYMMDD")
        dates.append(int(item))
    return dates or None


def build_config_from_args(args) -> tuple[ConvertConfig, Optional[List[int]]]:
    """从已解析命令行参数构建转换配置。"""
    cfg = ConvertConfig()

    # 输出目录必须先设置，避免后续S3 cache依赖旧base
    if args.output:
        cfg.set_output_base(args.output)

    # 数据源
    if args.s3:
        cfg.use_s3 = True
        cfg.is_remote = False
        cfg.s3_endpoint = args.s3_endpoint
        cfg.s3_access_key = args.s3_access_key
        cfg.s3_secret_key = args.s3_secret_key
        cfg.s3_bucket = args.s3_bucket
        cfg.s3_secure = args.s3_secure
        cfg.s3_cache_dir = args.s3_cache_dir or os.path.join(cfg.local_output_base, 's3_cache')
    elif args.remote:
        cfg.remote_host = args.remote
        cfg.remote_user = args.remote_user
        cfg.remote_ks_base = args.remote_path
        cfg.is_remote = True
    elif args.local:
        cfg.remote_ks_base = args.local
        cfg.is_remote = False

    # 转换选项
    if args.csv_only:
        cfg.enable_csv = True
        cfg.enable_lmdb = False
        cfg.enable_dsb = False
        cfg.enable_config = False
    elif args.lmdb_only:
        cfg.enable_csv = False
        cfg.enable_lmdb = True
        cfg.enable_dsb = False
        cfg.enable_config = False
    elif args.dsb_only:
        cfg.enable_csv = False
        cfg.enable_lmdb = False
        cfg.enable_dsb = True
        cfg.enable_config = False
    else:
        cfg.enable_csv = not args.no_csv
        cfg.enable_lmdb = not args.no_lmdb
        cfg.enable_dsb = not args.no_dsb
        cfg.enable_config = not args.no_config

    # 过滤
    if args.exchanges:
        cfg.exchanges = [x.strip() for x in args.exchanges.split(',') if x.strip()]

    if args.products:
        products_map = {}
        for item in args.products.split(','):
            parts = item.strip().split('.')
            if len(parts) == 2:
                exchg, product = parts
                products_map.setdefault(exchg, []).append(product)
        cfg.specific_products = products_map

    period_map_reverse = {'m1': 'kline_1m', 'm5': 'kline_5m', 'd': 'kline_1d'}
    cfg.periods = [period_map_reverse.get(p.strip(), p.strip()) for p in args.periods.split(',') if p.strip()]
    cfg.include_option = args.include_option
    cfg.include_cffex_options = getattr(args, 'include_cffex_options', False)

    # --full 应重建输出，--force 作为兼容别名
    if args.full or args.force:
        cfg.force_full = True
        cfg.incremental = False

    return cfg, parse_dates_arg(args.dates)


def load_live_requirements(path: str) -> Dict:
    """读取实盘历史预检需求JSON；未提供时返回空需求。"""
    if not path:
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('live requirements JSON must be an object')
    return data


def parse_csv_list(value: str) -> List[str]:
    """解析逗号分隔CLI参数，过滤空项。"""
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def parse_int_list(value: str) -> List[int]:
    """解析逗号分隔的整数CLI参数(如日期列表)。"""
    if not value:
        return []
    result = []
    for item in value.split(','):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))
    return result


def run_tick_convert(cfg, tick_dates: List[int] = None,
                     code_filter: str = '') -> None:
    """执行tick数据转换: S3 → WTSTickStruct → DSB

    @cfg          ConvertConfig实例
    @tick_dates   指定日期列表, None=全部可用
    @code_filter  合约代码过滤, 空=全部
    """
    import time
    from tick_converter import TickConverter

    if not cfg.use_s3:
        logger.error("Tick模式需要S3数据源 (--s3)")
        return

    s3_reader = create_reader(cfg)
    if s3_reader is None:
        logger.error("无法创建S3读取器，请检查凭据配置")
        return

    output_dir = cfg.local_output_base
    if not output_dir:
        logger.error("请指定输出目录 (--output)")
        return

    # 确保输出根目录含 his/ 子目录
    his_dir = os.path.join(output_dir, 'his')
    os.makedirs(his_dir, exist_ok=True)

    converter = TickConverter(
        s3_reader=s3_reader,
        output_dir=his_dir,
        use_compression=True,
        include_option=cfg.include_option,
    )

    # 解析品种列表: specific_products = {exchg: [product, ...]}
    products_spec = cfg.specific_products
    if not products_spec:
        logger.error("Tick模式需要指定品种 (--products EXCHG.PROD,...)")
        return

    total_start = time.time()
    total_ticks = 0
    total_files = 0
    product_count = 0

    for exchange, product_list in products_spec.items():
        for product in product_list:
            product_count += 1
            logger.info("=" * 60)
            logger.info("开始Tick转换: %s.%s", exchange, product)

            product_start = time.time()
            stats = converter.convert_product_dates(
                exchange, product,
                dates=tick_dates if tick_dates else None,
                code_filter=code_filter or None,
            )
            elapsed = time.time() - product_start

            product_ticks = sum(stats.values())
            total_ticks += product_ticks
            total_files += len(stats)

            logger.info("完成 %s.%s: %d合约, %d ticks, %.1fs",
                         exchange, product, len(stats), product_ticks, elapsed)

            # 逐合约摘要
            for code, count in sorted(stats.items(), key=lambda x: -x[1])[:5]:
                logger.info("  %s: %d ticks", code, count)
            if len(stats) > 5:
                logger.info("  ... 及其他 %d 合约", len(stats) - 5)

    total_elapsed = time.time() - total_start
    logger.info("=" * 60)
    logger.info("Tick转换完成: %d品种, %d文件, %d ticks, %.1fs",
                product_count, total_files, total_ticks, total_elapsed)

    # 写入简单manifest
    manifest = {
        'mode': 'tick',
        'products': {k: v for k, v in products_spec.items()},
        'total_ticks': total_ticks,
        'total_files': total_files,
        'elapsed_seconds': round(total_elapsed, 1),
        'tick_dates': tick_dates or 'all',
        'code_filter': code_filter or 'all',
    }
    manifest_path = os.path.join(output_dir, 'tick_manifest.json')
    import json
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    logger.info("Tick manifest: %s", manifest_path)


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
    elif args.live_preflight:
        write_live_preflight_report(cfg, required=load_live_requirements(args.live_requirements))
    elif args.research_export:
        export_research_view(
            cfg,
            codes=parse_csv_list(args.research_codes),
            periods=parse_csv_list(args.research_periods),
            output_dir=args.research_output or None,
        )
    elif args.tick:
        run_tick_convert(
            cfg,
            tick_dates=parse_int_list(getattr(args, 'tick_dates', '')),
            code_filter=getattr(args, 'tick_code', ''),
        )


if __name__ == '__main__':
    main()
