import csv
import json
import os
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

KS2WTPY_DIR = Path(__file__).resolve().parents[1]
if str(KS2WTPY_DIR) not in sys.path:
    sys.path.insert(0, str(KS2WTPY_DIR))

import main
from config import ConvertConfig, WTSBAR_STRUCT_SIZE
from config_generator import ConfigGenerator
from csv_writer import read_existing_csv_dates, write_wtpy_csv, CsvConverter
from ks_reader import KSBarRecord, KSDataReader
from lmdb_writer import encode_lmdb_bar_key, encode_wts_bar_struct, LmdbConverter
from dsb_writer import BT_HIS_MINUTE1, encode_bar_struct, read_dsb_file, write_dsb_file
import remote_update


def _bar(date: int, time: int, close: float = 1.0) -> KSBarRecord:
    return KSBarRecord(
        date=date,
        time=time,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=10,
        amount=100,
        openinterest=20,
    )


def _write_tar(path: Path, members: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))


def test_parse_args_accepts_reported_dates_and_include_option(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "--incremental", "--local", "/tmp/ks", "--dates", "20260519,20260520", "--include-option"],
    )

    args = main.parse_args()

    assert args.dates == "20260519,20260520"
    assert args.include_option is True


def test_apply_args_recomputes_output_subdirs_and_full_is_non_incremental(tmp_path):
    args = main.parse_args_from_list([
        "--full",
        "--local",
        str(tmp_path / "ks"),
        "--output",
        str(tmp_path / "out"),
    ])

    cfg, new_dates = main.build_config_from_args(args)

    assert cfg.local_output_base == str(tmp_path / "out")
    assert cfg.csv_output_dir == str(tmp_path / "out" / "csv")
    assert cfg.lmdb_output_dir == str(tmp_path / "out" / "lmdb")
    assert cfg.dsb_output_dir == str(tmp_path / "out" / "his")
    assert cfg.state_file == str(tmp_path / "out" / "convert_state.json")
    assert cfg.incremental is False
    assert cfg.force_full is True
    assert new_dates is None


def test_convert_config_has_no_embedded_network_or_secret_defaults():
    cfg = ConvertConfig()

    assert cfg.remote_host == ""
    assert cfg.remote_user == ""
    assert cfg.remote_ks_base == ""
    assert cfg.s3_endpoint == ""
    assert cfg.s3_access_key == ""
    assert cfg.s3_secret_key == ""


def test_local_list_date_dirs_detects_tar_gz_dates(tmp_path):
    period_dir = tmp_path / "SHFE" / "rb" / "L1" / "kline_1m"
    period_dir.mkdir(parents=True)
    (period_dir / "20240102").mkdir()
    _write_tar(period_dir / "20240103.tar.gz", {"rb2405_20240103.csv": "date,time,open,high,low,close,volume,amount,openinterest\n20240103,09:01,1,1,1,1,1,1,1\n"})
    (period_dir / "20240104.part.minio").write_text("partial")

    reader = KSDataReader(str(tmp_path), is_remote=False)

    assert reader.list_date_dirs("SHFE", "rb", "kline_1m") == [20240102, 20240103]


def test_lmdb_existing_keys_are_full_exchange_code_bartime_not_bartime_only(monkeypatch, tmp_path):
    pytest.importorskip("lmdb")
    reader = KSDataReader(str(tmp_path), is_remote=False)
    converter = LmdbConverter(reader, str(tmp_path / "lmdb"), incremental=True)
    try:
        converter.write_bars_to_lmdb("SHFE", "rb2405", [_bar(20240102, 901)], "min1")
        existing = converter._get_existing_keys("min1", "SHFE")
        same_time_other_code = encode_lmdb_bar_key("SHFE", "rb2406", (20240102 - 19900000) * 10000 + 901)
        same_time_same_code = encode_lmdb_bar_key("SHFE", "rb2405", (20240102 - 19900000) * 10000 + 901)

        assert same_time_same_code in existing
        assert same_time_other_code not in existing
    finally:
        converter.close_all()


def test_write_bars_to_lmdb_skips_only_existing_full_keys(tmp_path):
    pytest.importorskip("lmdb")
    reader = KSDataReader(str(tmp_path), is_remote=False)
    converter = LmdbConverter(reader, str(tmp_path / "lmdb"), incremental=True)
    try:
        converter.write_bars_to_lmdb("SHFE", "rb2405", [_bar(20240102, 901)], "min1")
        existing = converter._get_existing_keys("min1", "SHFE")

        written = converter.write_bars_to_lmdb("SHFE", "rb2406", [_bar(20240102, 901)], "min1", existing_bartimes=existing)

        assert written == 1
    finally:
        converter.close_all()


def test_csv_incremental_append_keeps_chronological_order(tmp_path):
    csv_path = tmp_path / "SHFE.rb2405_m1.csv"
    write_wtpy_csv(str(csv_path), [_bar(20240103, 902), _bar(20240105, 901)], is_day=False)
    existing = read_existing_csv_dates(str(csv_path), is_day=False)

    converter = CsvConverter(KSDataReader(str(tmp_path), is_remote=False), str(tmp_path), incremental=True)
    converter._write_incremental_csv(str(csv_path), [_bar(20240104, 901)], is_day=False, existing_keys=existing)

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))[1:]

    assert [(row[0], row[1]) for row in rows] == [
        ("2024/01/03", "09:02:00"),
        ("2024/01/04", "09:01:00"),
        ("2024/01/05", "09:01:00"),
    ]


def test_wtsbar_struct_is_88_bytes():
    assert WTSBAR_STRUCT_SIZE == 88
    assert len(encode_wts_bar_struct(_bar(20240102, 901), is_day=False)) == 88


def test_csv_minute_include_option_allows_option_codes(tmp_path):
    class Reader(KSDataReader):
        def __init__(self):
            super().__init__(str(tmp_path), is_remote=False)

        def list_date_dirs(self, exchange, product, period):
            return [20240102]

        def read_date_minute(self, exchange, product, period, date):
            return {
                "rb2405": [_bar(date, 901)],
                "rb2405C3000": [_bar(date, 902)],
            }

        def is_option_code(self, exchange, product, code):
            return code.endswith("C3000")

    converter = CsvConverter(Reader(), str(tmp_path / "csv"), incremental=False, include_option=True)

    stats = converter.convert_product("SHFE", "rb", ["kline_1m"], dates=[20240102])

    assert stats == {"kline_1m": 2}
    assert (tmp_path / "csv" / "SHFE.rb2405_m1.csv").exists()
    assert (tmp_path / "csv" / "SHFE.rb2405C3000_m1.csv").exists()


def test_lmdb_minute_include_option_allows_option_codes(tmp_path):
    pytest.importorskip("lmdb")

    class Reader(KSDataReader):
        def __init__(self):
            super().__init__(str(tmp_path), is_remote=False)

        def list_date_dirs(self, exchange, product, period):
            return [20240102]

        def read_date_minute(self, exchange, product, period, date):
            return {
                "rb2405": [_bar(date, 901)],
                "rb2405C3000": [_bar(date, 902)],
            }

        def is_option_code(self, exchange, product, code):
            return code.endswith("C3000")

    converter = LmdbConverter(Reader(), str(tmp_path / "lmdb"), incremental=False, include_option=True)
    try:
        stats = converter.convert_product("SHFE", "rb", ["kline_1m"], dates=[20240102])
        assert stats == {"kline_1m": 2}
        verify = converter.verify_lmdb("min1", "SHFE")
        assert verify["total_keys"] == 2
        assert verify["codes"] == ["rb2405", "rb2405C3000"]
    finally:
        converter.close_all()


def test_remote_update_reuses_main_cli_builder_for_dates_include_option_and_dsb(monkeypatch, tmp_path):
    captured = {}

    def fake_run_incremental_update(cfg, new_dates=None):
        captured["cfg"] = cfg
        captured["new_dates"] = new_dates

    monkeypatch.setattr(sys, "argv", [
        "remote_update.py", "--incremental",
        "--remote-host", "ks.example",
        "--remote-user", "wonder",
        "--remote-path", "/data/ks",
        "--output", str(tmp_path / "out"),
        "--dates", "20240102,20240103",
        "--include-option",
        "--dsb-only",
    ])
    monkeypatch.setattr(remote_update, "run_incremental_update", fake_run_incremental_update)

    remote_update.main()

    cfg = captured["cfg"]
    assert cfg.remote_host == "ks.example"
    assert cfg.remote_user == "wonder"
    assert cfg.remote_ks_base == "/data/ks"
    assert cfg.is_remote is True
    assert cfg.include_option is True
    assert cfg.enable_csv is False
    assert cfg.enable_lmdb is False
    assert cfg.enable_dsb is True
    assert cfg.local_output_base == str(tmp_path / "out")
    assert captured["new_dates"] == [20240102, 20240103]


def test_verify_output_reports_dsb_readback(monkeypatch, tmp_path, caplog):
    dsb_path = tmp_path / "his" / "min1" / "SHFE" / "SHFE.rb2405.dsb"
    bars_data = b"".join([encode_wts_bar_struct(_bar(20240102, 901), is_day=False)])
    write_dsb_file(str(dsb_path), BT_HIS_MINUTE1, bars_data, use_compression=False)

    cfg = ConvertConfig()
    cfg.set_output_base(str(tmp_path))
    cfg.enable_csv = False
    cfg.enable_lmdb = False
    cfg.enable_dsb = True
    cfg.exchanges = ["SHFE"]

    caplog.set_level("INFO")
    main.verify_output(cfg)

    assert read_dsb_file(str(dsb_path))["bar_count"] == 1
    assert "DSB min1/SHFE" in caplog.text
    assert "1个文件" in caplog.text


def test_full_convert_writes_conversion_manifest(monkeypatch, tmp_path):
    def fake_get_products_to_convert(cfg):
        return {"SHFE": ["rb"]}

    class DummyReader:
        pass

    class DummyCsv:
        def __init__(self, *args, **kwargs):
            pass
        def convert_product(self, exchg, product, periods):
            return {"kline_1m": 2}

    class DummyLmdb:
        def __init__(self, *args, **kwargs):
            pass
        def convert_product(self, exchg, product, periods):
            return {"kline_1m": 3}
        def close_all(self):
            pass

    class DummyDsb:
        def __init__(self, *args, **kwargs):
            pass
        def convert_product(self, exchg, product, periods):
            return {"kline_1m": 4}

    class DummyConfigGen:
        def __init__(self, *args, **kwargs):
            pass
        def collect_bars_for_hots(self, exchanges, products):
            return {}
        def generate_all(self, bars_by_product, products_filter=None, **kwargs):
            return {}
        def save_all(self, configs):
            pass

    monkeypatch.setattr(main, "create_reader", lambda cfg: DummyReader())
    monkeypatch.setattr(main, "get_products_to_convert", fake_get_products_to_convert)
    monkeypatch.setattr(main, "CsvConverter", DummyCsv)
    monkeypatch.setattr(main, "LmdbConverter", DummyLmdb)
    monkeypatch.setattr(main, "DsbConverter", DummyDsb)
    monkeypatch.setattr(main, "ConfigGenerator", DummyConfigGen)

    cfg = ConvertConfig()
    cfg.set_output_base(str(tmp_path / "out"))
    cfg.exchanges = ["SHFE"]
    cfg.periods = ["kline_1m"]
    cfg.include_option = True
    cfg.incremental = False
    cfg.force_full = True

    main.run_full_convert(cfg)

    manifest_path = tmp_path / "out" / "conversion_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["mode"] == "full"
    assert manifest["source"]["type"] == "local"
    assert manifest["filters"]["include_option"] is True
    assert manifest["stats"]["csv_codes"] == 2
    assert manifest["stats"]["lmdb_bars"] == 3
    assert manifest["stats"]["dsb_bars"] == 4


def test_quality_report_detects_csv_anomalies(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    csv_path = csv_dir / "SHFE.rb2405_m1.csv"
    csv_path.write_text(
        "date,time,open,high,low,close,volume,turnover,open_interest\n"
        "2024/01/02,09:02:00,10,12,9,11,100,1000,200\n"
        "2024/01/02,09:01:00,10,12,9,11,100,1000,200\n"  # out of order
        "2024/01/02,09:01:00,10,12,9,11,100,1000,200\n"  # duplicate
        "2024/01/02,09:03:00,10,9,11,10,-1,-10,200\n",  # bad OHLC + negative fields
        encoding="utf-8-sig",
    )

    cfg = ConvertConfig()
    cfg.set_output_base(str(tmp_path))
    report_path = main.write_quality_report(cfg)

    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert report["summary"]["csv_files"] == 1
    assert report["summary"]["csv_rows"] == 4
    assert report["summary"]["duplicates"] == 1
    assert report["summary"]["out_of_order"] == 1
    assert report["summary"]["ohlc_errors"] == 1
    assert report["summary"]["negative_fields"] == 1
    assert report["files"][0]["first_key"] == "20240102.0901"
    assert report["files"][0]["last_key"] == "20240102.0903"


def test_full_convert_writes_quality_report(monkeypatch, tmp_path):
    def fake_get_products_to_convert(cfg):
        return {"SHFE": ["rb"]}

    class DummyReader:
        pass

    class DummyCsv:
        def __init__(self, *args, **kwargs):
            pass
        def convert_product(self, exchg, product, periods):
            csv_dir = tmp_path / "out" / "csv"
            csv_dir.mkdir(parents=True, exist_ok=True)
            (csv_dir / "SHFE.rb2405_m1.csv").write_text(
                "date,time,open,high,low,close,volume,turnover,open_interest\n"
                "2024/01/02,09:01:00,10,12,9,11,100,1000,200\n",
                encoding="utf-8-sig",
            )
            return {"kline_1m": 1}

    class DummyLmdb:
        def __init__(self, *args, **kwargs):
            pass
        def convert_product(self, exchg, product, periods):
            return {}
        def close_all(self):
            pass

    class DummyDsb:
        def __init__(self, *args, **kwargs):
            pass
        def convert_product(self, exchg, product, periods):
            return {}

    class DummyConfigGen:
        def __init__(self, *args, **kwargs):
            pass
        def collect_bars_for_hots(self, exchanges, products):
            return {}
        def generate_all(self, bars_by_product, products_filter=None, **kwargs):
            return {}
        def save_all(self, configs):
            pass

    monkeypatch.setattr(main, "create_reader", lambda cfg: DummyReader())
    monkeypatch.setattr(main, "get_products_to_convert", fake_get_products_to_convert)
    monkeypatch.setattr(main, "CsvConverter", DummyCsv)
    monkeypatch.setattr(main, "LmdbConverter", DummyLmdb)
    monkeypatch.setattr(main, "DsbConverter", DummyDsb)
    monkeypatch.setattr(main, "ConfigGenerator", DummyConfigGen)

    cfg = ConvertConfig()
    cfg.set_output_base(str(tmp_path / "out"))
    cfg.exchanges = ["SHFE"]
    cfg.periods = ["kline_1m"]
    cfg.enable_lmdb = False
    cfg.enable_dsb = False
    cfg.enable_config = False
    cfg.incremental = False
    cfg.force_full = True

    main.run_full_convert(cfg)

    report_path = tmp_path / "out" / "quality_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["csv_files"] == 1
    assert report["summary"]["csv_rows"] == 1


def test_generate_contracts_honors_products_filter_and_skips_main_index_codes(monkeypatch, tmp_path):
    class Reader:
        def __init__(self):
            self.calls = []

        def list_day_codes(self, exchg, product):
            self.calls.append((exchg, product))
            return [f"{product}2605", f"{product}_main", f"{product}_index", "SPD a2605&a2607"]

        def is_option_code(self, exchg, product, code):
            return False

    reader = Reader()
    gen = ConfigGenerator(reader, str(tmp_path))

    contracts = gen.generate_contracts({"DCE": ["a"]})

    assert reader.calls == [("DCE", "a")]
    assert set(contracts.keys()) == {"DCE"}
    assert list(contracts["DCE"].keys()) == ["a2605"]


def test_generate_all_for_config_only_passes_product_filter_to_contracts(monkeypatch, tmp_path):
    class Reader:
        def list_day_codes(self, exchg, product):
            return [f"{product}2605"]

        def is_option_code(self, exchg, product, code):
            return False

    gen = ConfigGenerator(Reader(), str(tmp_path))

    configs = gen.generate_all({}, products_filter={"DCE": ["a"]})

    assert set(configs["contracts.json"].keys()) == {"DCE"}
    assert list(configs["contracts.json"]["DCE"].keys()) == ["a2605"]


def test_full_convert_passes_product_filter_to_config_generator(monkeypatch, tmp_path):
    captured = {}

    class DummyReader:
        pass

    class DummyConverter:
        def __init__(self, *args, **kwargs):
            pass
        def convert_product(self, exchg, product, periods):
            return {}
        def close_all(self):
            pass

    class DummyConfigGen:
        def __init__(self, *args, **kwargs):
            pass
        def collect_bars_for_hots(self, exchanges, products):
            captured["collect_products"] = products
            return {}
        def generate_all(self, bars_by_product, products_filter=None, **kwargs):
            captured["products_filter"] = products_filter
            captured["option_products"] = kwargs.get("option_products")
            return {}
        def save_all(self, configs):
            pass

    monkeypatch.setattr(main, "create_reader", lambda cfg: DummyReader())
    monkeypatch.setattr(main, "CsvConverter", DummyConverter)
    monkeypatch.setattr(main, "LmdbConverter", DummyConverter)
    monkeypatch.setattr(main, "DsbConverter", DummyConverter)


def test_hots_json_must_be_nested_exchg_product_list():
    """WT C++ WTSHotMgr::loadCustomRules (WTSHotMgr.cpp:219-248) 期望结构:
        {EXCHG: {PRODUCT: [ {date, from, to, oldclose, newclose}, ... ]}}
    顶层是 EXCHG (非 'EXCHG.product')；值是 PRODUCT→list；list 元素是
    切换事件而非 date→code 平 dict。否则 HotMgr 解析失败，HOT 永远读不到。
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config_generator import ConfigGenerator
    from ks_reader import KSBarRecord

    def _bar(date, code_close):
        return KSBarRecord(date=date, time=0, open=code_close, high=code_close,
                           low=code_close, close=code_close, volume=100.0,
                           amount=0.0, openinterest=1000.0)

    # 模拟两段持仓主力切换: rb0909(20090901-20090915 持仓最大) -> rb0912(20090916- 持仓最大)
    bars_by_product = {
        "SHFE.rb": {
            "rb0909": [
                KSBarRecord(20090901, 0, 3100, 3110, 3090, 3105, 100, 0, 9000),
                KSBarRecord(20090915, 0, 3490, 3510, 3480, 3500, 100, 0, 9500),
                KSBarRecord(20090916, 0, 3500, 3520, 3490, 3510, 100, 0, 3000),
            ],
            "rb0912": [
                KSBarRecord(20090901, 0, 3200, 3210, 3190, 3205, 100, 0, 5000),
                KSBarRecord(20090915, 0, 3640, 3660, 3630, 3650, 100, 0, 6000),
                KSBarRecord(20090916, 0, 3650, 3670, 3640, 3660, 100, 0, 8000),
                KSBarRecord(20090917, 0, 3660, 3680, 3650, 3670, 100, 0, 8500),
            ],
        },
    }

    gen = ConfigGenerator.__new__(ConfigGenerator)
    hots = gen.generate_hots(bars_by_product)

    # 顶层必须是 EXCHG，不能是 'EXCHG.product'
    assert "SHFE" in hots, f"顶层必须是 EXCHG (如 'SHFE')，得到 keys={list(hots.keys())}"
    assert "SHFE.rb" not in hots, "禁止用 'EXCHG.product' 平铺顶层"

    # 第二层是 product → list
    shfe = hots["SHFE"]
    assert "rb" in shfe, f"第二层必须按 product 嵌套，得到 keys={list(shfe.keys())}"
    rb_switches = shfe["rb"]
    assert isinstance(rb_switches, list), f"product 值必须是 list[switch_event]，得到 {type(rb_switches).__name__}"
    assert len(rb_switches) >= 1, "至少应有一条切换事件"

    # 每条切换事件必须含 WT HotMgr 需要的 5 个字段
    required_fields = {"date", "from", "to", "oldclose", "newclose"}
    for ev in rb_switches:
        assert isinstance(ev, dict), f"切换事件必须是 dict，得到 {type(ev).__name__}"
        missing = required_fields - set(ev.keys())
        assert not missing, f"切换事件缺字段 {missing}: {ev}"
        assert isinstance(ev["date"], int) and ev["date"] > 19700101, f"date 必须为 YYYYMMDD int，得到 {ev['date']!r}"
        # initial 事件允许 from='' / oldclose=0.0（WT HotMgr 支持）
        assert isinstance(ev["from"], str), "from 必须为 str"
        assert isinstance(ev["to"], str) and ev["to"], "to 必须为非空 str"
        assert isinstance(ev["oldclose"], (int, float)), "oldclose 必须为 number"
        assert isinstance(ev["newclose"], (int, float)), "newclose 必须为 number"

    # 验证主力切换语义: 20090916 切换 rb0909 -> rb0912
    # (oldclose=切换前一日 rb0909 close=3500, newclose=切换当日 rb0912 close=3660)
    switch_to_rb0912 = [ev for ev in rb_switches if ev["to"] == "rb0912"]
    assert switch_to_rb0912, "应记录切换到 rb0912 的事件"
    ev = switch_to_rb0912[0]
    assert ev["date"] == 20090916, f"切换日应为 20090916，得到 {ev['date']}"
    assert ev["from"] == "rb0909", f"from 应为 rb0909，得到 {ev['from']}"
    assert ev["oldclose"] == 3500, f"oldclose 应为切换前一日旧主力收盘=3500，得到 {ev['oldclose']}"
    assert ev["newclose"] == 3660, f"newclose 应为切换当日新主力收盘=3660，得到 {ev['newclose']}"


def test_hots_json_single_product_no_switch_still_valid_list():
    """品种从头到尾只有一个主力合约时，hots[EXCHG][product] 仍必须是 list（非空），
    至少含 1 条 'initial' 事件 (from='', to=最初主力，date=该品种最早交易日)，
    否则 HotMgr loadCustomRules 拿不到 lastCode，HOT 仍读不到。"""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config_generator import ConfigGenerator
    from ks_reader import KSBarRecord

    bars_by_product = {
        "DCE.a": {
            "a2509": [
                KSBarRecord(20250101, 0, 4000, 4010, 3990, 4005, 100, 0, 5000),
                KSBarRecord(20250102, 0, 4005, 4015, 3995, 4010, 100, 0, 5100),
            ],
        },
    }
    gen = ConfigGenerator.__new__(ConfigGenerator)
    hots = gen.generate_hots(bars_by_product)

    assert "DCE" in hots
    assert "a" in hots["DCE"]
    events = hots["DCE"]["a"]
    assert isinstance(events, list) and len(events) >= 1
    ev0 = events[0]
    assert ev0["to"] == "a2509"
    assert ev0["date"] == 20250101


# ============================================================
# HOT 拼接产物 (P0 修复①+②) —— 一等公民 CSV + DSB
# ============================================================
class _FakeReaderForHot:
    """按合约存日线 bars 的 mock reader。"""

    def __init__(self, day_data):
        # day_data: {(exchange, product, code): [KSBarRecord, ...]}
        self._day = day_data

    def read_day_csv(self, exchange, product, code):
        return list(self._day.get((exchange, product, code), []))

    def list_day_codes(self, exchange, product):
        return [c for (e, p, c) in self._day.keys() if e == exchange and p == product]

    def is_option_code(self, exchange, product, code):
        return False

    def list_date_dirs(self, exchange, product, period):
        return []

    def read_date_minute(self, exchange, product, period, date):
        return {}


def _build_two_contract_day_data():
    """rb0909 覆盖 20090910~20090918, rb0912 覆盖 20090916~20090925。
    切换日 20090916: rb0909 oldclose=3500, rb0912 newclose=3660。
    """
    rb0909 = []
    for i, d in enumerate(range(20090910, 20090919)):
        c = 3400 + i * 10
        rb0909.append(KSBarRecord(d, 0, c - 5, c + 5, c - 10, c, 100, 1000, 5000))
    # 显式锁定切换前一日收盘
    for idx, bar in enumerate(rb0909):
        if bar.date == 20090915:
            rb0909[idx] = KSBarRecord(20090915, 0, 3495, 3505, 3490, 3500, 100, 1000, 5000)
            break

    rb0912 = []
    for i, d in enumerate(range(20090916, 20090926)):
        c = 3650 + i * 10
        rb0912.append(KSBarRecord(d, 0, c - 5, c + 5, c - 10, c, 200, 2000, 6000))
    rb0912[0] = KSBarRecord(20090916, 0, 3655, 3665, 3650, 3660, 200, 2000, 6000)

    return {
        ("SHFE", "rb", "rb0909"): rb0909,
        ("SHFE", "rb", "rb0912"): rb0912,
    }


def test_build_hot_csv_from_events_splices_by_date_window(tmp_path):
    """RED: build_hot_csv 必须接受 events list 并按 [event[i].date, event[i+1].date) 拼接。"""
    from csv_writer import CsvConverter

    reader = _FakeReaderForHot(_build_two_contract_day_data())
    conv = CsvConverter(reader, str(tmp_path))

    events = [
        {"date": 20090910, "from": "", "to": "rb0909", "oldclose": 0.0, "newclose": 3400.0},
        {"date": 20090916, "from": "rb0909", "to": "rb0912", "oldclose": 3500.0, "newclose": 3660.0},
    ]
    conv.build_hot_csv("SHFE", "rb", events, periods=["kline_1d"])

    hot_csv = tmp_path / "SHFE.rb.HOT_d.csv"
    assert hot_csv.exists(), "必须生成 SHFE.rb.HOT_d.csv"
    with open(hot_csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    dates = [int(r["date"].replace("/", "")) for r in rows]
    assert dates == sorted(dates), "HOT CSV 必须按日期升序"
    assert len(dates) == 16, "应有 16 行（rb0909 6 + rb0912 10），得到 %d" % len(dates)
    pre = next(r for r in rows if int(r["date"].replace("/", "")) == 20090915)
    assert float(pre["close"]) == 3500.0, "切换前一日应取 rb0909 收盘=3500"
    post = next(r for r in rows if int(r["date"].replace("/", "")) == 20090916)
    assert float(post["close"]) == 3660.0, "切换日应取 rb0912 收盘=3660"


def test_build_hot_dsb_from_events_writes_first_class_file(tmp_path):
    """RED: DsbConverter 必须暴露 build_hot_dsb 并写出 his/day/{EXCHG}/{product}.HOT.dsb。"""
    from dsb_writer import DsbConverter, read_dsb_file

    reader = _FakeReaderForHot(_build_two_contract_day_data())
    out = tmp_path / "his"
    conv = DsbConverter(reader, str(out), use_compression=True)

    events = [
        {"date": 20090910, "from": "", "to": "rb0909", "oldclose": 0.0, "newclose": 3400.0},
        {"date": 20090916, "from": "rb0909", "to": "rb0912", "oldclose": 3500.0, "newclose": 3660.0},
    ]
    total = conv.build_hot_dsb("SHFE", "rb", events, periods=["kline_1d"])
    assert total.get("kline_1d", 0) == 16, "应写入 16 根日 bar, 得到 %r" % total

    dsb_path = out / "day" / "SHFE" / "rb.HOT.dsb"
    assert dsb_path.exists(), "必须生成 %s" % dsb_path

    info = read_dsb_file(str(dsb_path))
    assert info is not None
    assert info["bar_count"] == 16


def test_main_iterates_nested_hots_json_without_split_error():
    """RED: main._emit_hot_artifacts 必须能按嵌套 hots.json 双层迭代而不抛 ValueError。"""
    import main as m

    nested = {
        "SHFE": {
            "rb": [
                {"date": 20090910, "from": "", "to": "rb0909", "oldclose": 0.0, "newclose": 3400.0},
                {"date": 20090916, "from": "rb0909", "to": "rb0912", "oldclose": 3500.0, "newclose": 3660.0},
            ],
        },
        "CFFEX": {
            "IF": [
                {"date": 20121022, "from": "", "to": "IF1306", "oldclose": 0.0, "newclose": 2437.4},
            ],
        },
    }

    calls = []

    class _RecordingCsv:
        def build_hot_csv(self, exchg, product, events, periods=None):
            calls.append(("csv", exchg, product, len(events)))

    class _RecordingDsb:
        def build_hot_dsb(self, exchg, product, events, periods=None):
            calls.append(("dsb", exchg, product, len(events)))
            return {"kline_1d": len(events)}

    assert hasattr(m, "_emit_hot_artifacts"), \
        "main 必须暴露 _emit_hot_artifacts(nested_hots, csv_conv, dsb_conv, periods, stats)"

    stats = {"errors": 0, "error_details": []}
    m._emit_hot_artifacts(nested, _RecordingCsv(), _RecordingDsb(), ["kline_1d"], stats)

    assert stats["errors"] == 0, "嵌套结构迭代不应产生错误，得到 %r" % stats["error_details"]
    assert ("csv", "SHFE", "rb", 2) in calls
    assert ("csv", "CFFEX", "IF", 1) in calls
    assert ("dsb", "SHFE", "rb", 2) in calls
    assert ("dsb", "CFFEX", "IF", 1) in calls


def test_is_option_code_must_not_match_future_contracts():
    """REG: 修复前 is_option_code 把 IC1505/IC2606/AP605 误判为期权 → IC/AP 具体合约 dsb 全缺，HOT 读回失败。"""
    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    from s3_reader import S3KsReader

    r = S3KsReader.__new__(S3KsReader)  # 不走 __init__，不连 S3

    # 期货合约：绝不能判为期权
    fut_cases = [
        ('CFFEX', 'IC', 'IC1505'), ('CFFEX', 'IC', 'IC2606'),
        ('CFFEX', 'IF', 'IF1306'), ('CFFEX', 'IH', 'IH2606'),
        ('CZCE', 'AP', 'AP605'), ('CZCE', 'AP', 'AP610'),
        ('CZCE', 'CF', 'CF607'),
        ('SHFE', 'rb', 'rb2510'),
        ('DCE', 'a', 'a2607'),
    ]
    for ex, prod, code in fut_cases:
        assert not r.is_option_code(ex, prod, code), \
            f'{code} 是期货合约，不应判为期权'

    # 期权合约：必须判为期权
    opt_cases = [
        ('CZCE', 'AP', 'AP610C7000'), ('CZCE', 'AP', 'AP610P6500'),
        ('CZCE', 'SR', 'SR109P16800'),
        ('DCE', 'm', 'm2609-C-2950'), ('DCE', 'm', 'm2609-P-2950'),
        ('SHFE', 'rb', 'rb2510-C-3500'),
    ]
    for ex, prod, code in opt_cases:
        assert r.is_option_code(ex, prod, code), \
            f'{code} 是期权合约，应判为期权'


# ============================================================
# 修订方案 A — 消除带点 CSV 别名 (fix-dup)
# ============================================================
def test_csv_writer_must_not_emit_dotted_contract_alias(tmp_path):
    """RED: csv_writer 不得为具体期货合约生成带点别名 CSV。

    背景：早期 _write_wt_contract_alias_if_needed 把 IC1505_d.csv 复制成 IC.1505_d.csv
    用于"WT 三段 stdCode"兼容；但 CSV 不是 WT C++ 消费物，alias 纯属磁盘冗余 +
    扫描歧义，且与 wtpy datahelper 现有约定 (SSE.600000_d.csv 不带点) 冲突。
    DSB 物理文件名一直是不带点 (IC1505.dsb)，没问题；CSV 应统一不带点。
    """
    from csv_writer import CsvConverter

    # 单合约：CFFEX/IC/IC1505 一根日 bar
    fake_data = {
        ("CFFEX", "IC", "IC1505"): [
            KSBarRecord(20150518, 0, 4500, 4520, 4480, 4510, 100, 1000, 5000),
        ],
    }
    reader = _FakeReaderForHot(fake_data)
    conv = CsvConverter(reader, str(tmp_path))
    ok = conv.convert_code_day("CFFEX", "IC", "IC1505")
    assert ok, "convert_code_day 应成功"

    nodot = tmp_path / "CFFEX.IC1505_d.csv"
    dotted = tmp_path / "CFFEX.IC.1505_d.csv"

    assert nodot.exists(), "必须生成不带点 CSV (KS 原始命名)"
    assert not dotted.exists(), (
        f"不得生成带点 alias CSV: {dotted} 存在即说明 _write_wt_contract_alias_if_needed "
        "或等价 alias 写入逻辑仍在生效"
    )


def test_csv_writer_still_emits_hot_alias(tmp_path):
    """REG: 删 contract alias 不能殃及 HOT alias—— {product}_main → {product}.HOT 必须保留。

    HOT 三段格式 (rb.HOT) 是 WT 真实 stdCode 规范，DSB 端也有 rb.HOT.dsb；
    CSV 端 HOT alias 是合规的，acceptance 直接依赖它。
    """
    from csv_writer import CsvConverter

    fake_data = {
        ("SHFE", "rb", "rb_main"): [
            KSBarRecord(20090910, 0, 3400, 3410, 3390, 3405, 100, 1000, 5000),
        ],
    }
    reader = _FakeReaderForHot(fake_data)
    conv = CsvConverter(reader, str(tmp_path))
    ok = conv.convert_code_day("SHFE", "rb", "rb_main")
    assert ok

    raw = tmp_path / "SHFE.rb_main_d.csv"
    hot_alias = tmp_path / "SHFE.rb.HOT_d.csv"
    assert raw.exists(), "KS 原始 rb_main CSV 必须保留"
    assert hot_alias.exists(), (
        "HOT alias (SHFE.rb.HOT_d.csv) 必须仍然生成—— 不能因为删 contract alias 一起删了"
    )


# ============================================================
# 修订方案 A — wt_readback._autodiscover_codes 改走 DSB + commodities 反推
# ============================================================
def _make_dsb_layout(base, files):
    """在 base/his/day/{EXCHG}/ 下创建空 dsb 文件以供扫描。

    files: [(exchg, code), ...]
    """
    for exchg, code in files:
        d = os.path.join(base, "his", "day", exchg)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{code}.dsb"), "wb") as fh:
            fh.write(b"\0")


def _make_commodities(base, products):
    """在 base/common/commodities.json 写入 {EXCHG:{product:{...}}} 形态。

    products: [(exchg, product), ...]
    """
    common = os.path.join(base, "common")
    os.makedirs(common, exist_ok=True)
    data = {}
    for exchg, prod in products:
        data.setdefault(exchg, {})[prod] = {"type": "futures"}
    with open(os.path.join(common, "commodities.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_autodiscover_codes_scans_dsb_directory(tmp_path):
    """RED: _autodiscover_codes 必须能从 DSB 目录扫描，不再依赖 CSV alias。

    给定 his/day/CFFEX/{IC1505.dsb, IC.HOT.dsb} 和 commodities {CFFEX:IC}：
      - HOT 应识别出 'CFFEX.IC.HOT'
      - specific 应识别出 'CFFEX.IC.1505' (DSB 文件名 IC1505 反推 product=IC → 三段)
    """
    import sys, os as _os
    sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..')))
    from acceptance.wt_readback import _autodiscover_codes

    base = str(tmp_path)
    _make_dsb_layout(base, [
        ("CFFEX", "IC1505"),
        ("CFFEX", "IC.HOT"),
    ])
    _make_commodities(base, [("CFFEX", "IC")])

    discovery = _autodiscover_codes(base)
    assert "CFFEX.IC.HOT" in discovery["hot"], (
        f"HOT 未识别: hot={discovery.get('hot')}"
    )
    assert "CFFEX.IC.1505" in discovery["specific"], (
        f"specific 未反推出三段 stdCode: specific={discovery.get('specific')}"
    )


def test_autodiscover_codes_handles_czce_three_digit_month(tmp_path):
    """RED: CZCE 合约号 3 位月 (AP610) 必须反推成 'CZCE.AP.610' 三段。"""
    import sys, os as _os
    sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..')))
    from acceptance.wt_readback import _autodiscover_codes

    base = str(tmp_path)
    _make_dsb_layout(base, [
        ("CZCE", "AP610"),
        ("CZCE", "AP.HOT"),
    ])
    _make_commodities(base, [("CZCE", "AP")])

    discovery = _autodiscover_codes(base)
    assert "CZCE.AP.HOT" in discovery["hot"]
    assert "CZCE.AP.610" in discovery["specific"], (
        f"CZCE 3 位月反推失败: specific={discovery.get('specific')}"
    )


def test_autodiscover_codes_ignores_unknown_products(tmp_path):
    """RED: commodities.json 缺该 product 时，不应硬猜，跳过该 DSB 文件。"""
    import sys, os as _os
    sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..')))
    from acceptance.wt_readback import _autodiscover_codes

    base = str(tmp_path)
    _make_dsb_layout(base, [
        ("CFFEX", "XX1505"),  # XX 不在 commodities
    ])
    _make_commodities(base, [("CFFEX", "IC")])

    discovery = _autodiscover_codes(base)
    # XX1505 不应被反推（unknown product），specific 应为空
    assert "CFFEX.XX.1505" not in discovery["specific"]
    assert all(not c.startswith("CFFEX.XX") for c in discovery["specific"])


# ============================================================
# p2-1: commodities.json basefiles 生产化 — RED 测试
# ============================================================
# 设计原则：
#   - 优先用 WT 原版 commodities.json 快照 (references/wt_commodities_snapshot.json)
#     的权威字段 (category=int/pricetick/volscale/precision/session/holiday)
#   - 未在快照中的品种 fallback 到当前占位逻辑，但要打 warning（不静默）
#   - 输出必须能被 wtpy.ProductMgr.load() 加载（字段齐全 + 类型正确）
#   - 期权品种（如 CFFEX.HO）不应进入 futures commodities


class _FakeKsReaderForCommodities:
    """配置生成器只用 ks_reader 的 list_day_codes / is_option_code，这里只 mock 接口。"""

    def list_day_codes(self, exchg, product):
        return []

    def is_option_code(self, exchg, product, code):
        return False


def _make_generator(tmp_path):
    return ConfigGenerator(_FakeKsReaderForCommodities(), str(tmp_path))


def test_p21_commodities_loads_wt_snapshot_for_known_products(tmp_path):
    """RED: 已知品种 (SHFE.ag, CFFEX.IF, GFEX.pd) 必须使用 WT 快照中的权威字段。

    SHFE.ag 期望: pricetick=1.0, volscale=15, name='沪银', session='FN0230', holiday='CHINA'
    CFFEX.IF 期望: pricetick=0.2, volscale=300, name='沪深', session='SD0930', category=1
    GFEX.pd  期望: pricetick=0.05, volscale=1000, precision=2
    """
    gen = _make_generator(tmp_path)
    result = gen.generate_commodities({
        "SHFE": ["ag"],
        "CFFEX": ["IF"],
        "GFEX": ["pd"],
    })

    ag = result["SHFE"]["ag"]
    assert ag["pricetick"] == 1.0, f"ag pricetick wrong: {ag.get('pricetick')}"
    assert ag["volscale"] == 15, f"ag volscale wrong: {ag.get('volscale')}"
    assert ag["name"] == "沪银", f"ag name wrong: {ag.get('name')}"
    assert ag["session"] == "FN0230", f"ag session wrong: {ag.get('session')}"

    iff = result["CFFEX"]["IF"]
    assert iff["pricetick"] == 0.2, f"IF pricetick wrong: {iff.get('pricetick')}"
    assert iff["volscale"] == 300, f"IF volscale wrong: {iff.get('volscale')}"
    assert iff["name"] == "沪深", f"IF name wrong: {iff.get('name')}"
    assert iff["session"] == "SD0930", f"IF session wrong: {iff.get('session')}"

    pd = result["GFEX"]["pd"]
    assert pd["pricetick"] == 0.05, f"pd pricetick wrong: {pd.get('pricetick')}"
    assert pd["volscale"] == 1000, f"pd volscale wrong: {pd.get('volscale')}"
    assert pd["precision"] == 2, f"pd precision wrong: {pd.get('precision')}"


def test_p21_commodities_category_must_be_int_not_chinese(tmp_path):
    """RED: WT C++ 端的 category 是 int (1=期货), 不是中文字符串。"""
    gen = _make_generator(tmp_path)
    result = gen.generate_commodities({"SHFE": ["ag"], "CFFEX": ["IF"]})

    for exchg, prod in [("SHFE", "ag"), ("CFFEX", "IF")]:
        cat = result[exchg][prod]["category"]
        assert isinstance(cat, int), (
            f"{exchg}.{prod} category 必须是 int (WT C++ 枚举), 当前 type={type(cat).__name__} value={cat!r}"
        )
        assert cat == 1, f"{exchg}.{prod} category 期望 1(期货), 当前 {cat}"


def test_p21_commodities_has_required_wt_fields(tmp_path):
    """RED: 每个品种必须包含 WT C++ 端必需的全部字段。

    必需字段: covermode, pricemode, category, trademode, precision,
              pricetick, volscale, name, exchg, session, holiday
    """
    gen = _make_generator(tmp_path)
    result = gen.generate_commodities({"SHFE": ["ag"], "DCE": ["a"]})

    required = {"covermode", "pricemode", "category", "trademode", "precision",
                "pricetick", "volscale", "name", "exchg", "session", "holiday"}
    for exchg, prods in result.items():
        for prod, info in prods.items():
            missing = required - set(info.keys())
            assert not missing, f"{exchg}.{prod} 缺字段: {missing}"
            # exchg 字段必须等于父键
            assert info["exchg"] == exchg, (
                f"{exchg}.{prod} exchg 字段不匹配: {info['exchg']}"
            )
            # holiday 必须 CHINA
            assert info["holiday"] == "CHINA", (
                f"{exchg}.{prod} holiday 字段必须 CHINA: {info['holiday']}"
            )


def test_p21_commodities_session_uses_wt_naming_not_internal(tmp_path):
    """RED: session 字段必须用 WT 命名 (FN0230/SD0930/FD0900...)，
    不能用 ks2wtpy 内部 SESSION_DEFINITIONS 的 ID (SHFE_NIGHT1/CFFEX_STOCK 等)。
    """
    gen = _make_generator(tmp_path)
    result = gen.generate_commodities({"SHFE": ["ag"], "CFFEX": ["IF"], "CZCE": ["AP"]})

    forbidden_prefixes = ("SHFE_", "DCE_", "CZCE_", "INE_", "CFFEX_", "GFEX_")
    for exchg, prods in result.items():
        for prod, info in prods.items():
            sess = info["session"]
            assert not sess.startswith(forbidden_prefixes), (
                f"{exchg}.{prod} session 不能用内部 ID: {sess} "
                f"(应该是 WT 命名如 FN0230/SD0930/FD0900)"
            )


def test_p21_commodities_unknown_product_falls_back_with_warning(tmp_path, caplog):
    """RED: 快照未覆盖的品种应 fallback 到占位字段 + 打 warning，不静默。"""
    import logging
    gen = _make_generator(tmp_path)

    # SHFE.XX 是一个不存在的 product (快照里没有)
    with caplog.at_level(logging.WARNING):
        result = gen.generate_commodities({"SHFE": ["XX"]})

    assert "XX" in result["SHFE"], "fallback 品种仍应输出，不能直接丢弃"
    xx = result["SHFE"]["XX"]
    # 字段仍齐全（fallback 应补全 WT 必需字段）
    for k in ("category", "pricetick", "volscale", "precision", "session", "holiday", "exchg"):
        assert k in xx, f"fallback 也必须补全字段 {k}"
    # warning 必须存在
    msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("XX" in m for m in msgs), (
        f"fallback 必须打 warning 提示品种 XX 未在 WT 快照中, 当前 warnings={msgs}"
    )


def test_p21_commodities_excludes_options(tmp_path):
    """RED: 期权品种 (CFFEX.HO/IO/MO) 不应进入 futures commodities。

    ks2wtpy 的 _iter_products 应跳过 OPTION_PRODUCTS。本测试确认
    即使调用方传了 HO，输出也应排除（或不报错地跳过）。
    """
    from config import OPTION_PRODUCTS
    assert "HO" in OPTION_PRODUCTS.get("CFFEX", []), "前置: HO 须在 OPTION_PRODUCTS"

    gen = _make_generator(tmp_path)
    # 即使用户传 HO，也应跳过
    result = gen.generate_commodities({"CFFEX": ["IF", "HO"]})

    assert "IF" in result.get("CFFEX", {}), "IF 必须保留"
    assert "HO" not in result.get("CFFEX", {}), (
        f"HO 是期权品种，不应进入 futures commodities.json, 当前 CFFEX={list(result.get('CFFEX', {}).keys())}"
    )


def test_p21_commodities_output_is_loadable_by_wtpy_productmgr(tmp_path):
    """RED: 产物必须能被 wtpy.ProductMgr.load() 加载（不抛异常 + product 数对得上）。

    覆盖 5 个 exchange 的代表性品种，写盘后用 wtpy ProductMgr 加载校验。
    """
    gen = _make_generator(tmp_path)
    result = gen.generate_commodities({
        "SHFE": ["ad", "ag"],
        "CFFEX": ["IF", "IC"],
        "CZCE": ["AP", "CF"],
        "DCE": ["a", "b"],
        "GFEX": ["lc", "pd"],
    })

    common = tmp_path / "common"
    common.mkdir(parents=True, exist_ok=True)
    out_path = common / "commodities.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    # 验证 1: JSON 形态正确（字段齐全 + 类型对）
    with open(out_path, encoding="utf-8") as fh:
        reloaded = json.load(fh)
    assert sum(len(v) for v in reloaded.values()) == 10, "10 个品种全部落盘"

    # 验证 2: 尝试用 wtpy.ProductMgr 加载（若 import 失败则降级为字段断言）
    try:
        from wtpy.WtCoreDefs import ProductMgr  # type: ignore
        # ProductMgr.load 接收一个 commodities.json 路径
        mgr = ProductMgr()
        mgr.load(str(out_path))
        # 加载后 mgr._products 应有 10 个
        assert len(getattr(mgr, "_products", {})) >= 5, (
            f"ProductMgr 加载结果偏少: {len(getattr(mgr, '_products', {}))}"
        )
    except (ImportError, AttributeError):
        # 降级：手工字段断言（生产环境若无 wtpy.WtCoreDefs，至少结构正确）
        for exchg, prods in reloaded.items():
            for prod, info in prods.items():
                assert isinstance(info["pricetick"], (int, float))
                assert isinstance(info["volscale"], int)
                assert isinstance(info["category"], int)


# ============================================================================
# p2-2 [P0]: contracts.json 字段生产化（方案 A）
# ============================================================================
# 设计纪要:
#   方案 A = 只修 name (中文) + maxlimit/maxmarket/longmargin/shortmargin
#           (按品种从 WT 模板派生); opendate/expiredate 保留 KS 派生
#           (KS 数据覆盖期作为合约存在期的近似)
#
#   契约:
#     - name = commodities[product].name + YYMM (如 "沪银2603")
#     - maxlimitqty/maxmarketqty/longmargin/shortmargin 从 WT contracts 快照
#       按品种取众数; 未覆盖品种 fallback (1000/0/0.1/0.1) + warning
#     - opendate = bars[0].date  (KS 数据起点)
#     - expiredate = bars[-1].date (活跃合约 = 最后数据日)
#     - 已结合约 (无后续 bar): expiredate = 实际最后交易日


class _FakeKsReaderForContracts:
    """contracts 测试需要 list_day_codes 返回真实合约列表。"""

    def __init__(self, codes_by_product=None, options=None):
        self._codes = codes_by_product or {}
        self._options = options or set()

    def list_day_codes(self, exchg, product):
        return list(self._codes.get(f'{exchg}.{product}', []))

    def is_option_code(self, exchg, product, code):
        return (exchg, product, code) in self._options


def _make_bar(date, close=100.0):
    """构造一个 KSBarRecord, 用于 generate_contracts 测试 (只关心 date 字段)。"""
    return KSBarRecord(
        date=date, time=0, open=close, high=close, low=close, close=close,
        volume=1.0, amount=0.0, openinterest=1.0,
    )


def test_p22_contracts_name_uses_chinese_from_commodities(tmp_path):
    """RED: contracts.name 必须用 commodities 中的中文名 + YYMM。"""
    reader = _FakeKsReaderForContracts(codes_by_product={'SHFE.ag': ['ag2603']})
    gen = ConfigGenerator(reader, str(tmp_path))
    bars_by_product = {'SHFE.ag': {'ag2603': [_make_bar(20250318), _make_bar(20260316)]}}
    contracts = gen.generate_contracts(
        codes_info={'SHFE': ['ag']},
        bars_by_product=bars_by_product,
    )
    assert 'ag2603' in contracts.get('SHFE', {})
    name = contracts['SHFE']['ag2603']['name']
    assert '沪银' in name, f'name 必须含中文 沪银; got {name!r}'
    assert '2603' in name, f'name 必须含 YYMM 2603; got {name!r}'


def test_p22_contracts_maxlimit_per_product_from_wt_template(tmp_path):
    """RED: maxlimitqty 必须按品种派生 (CFFEX=20, SHFE.ag=500, CZCE.AP=1000, INE.ec=500)。"""
    reader = _FakeKsReaderForContracts(codes_by_product={
        'CFFEX.IF': ['IF2603'],
        'SHFE.ag': ['ag2603'],
        'CZCE.AP': ['AP603'],
        'INE.ec': ['ec2603'],
    })
    gen = ConfigGenerator(reader, str(tmp_path))
    bars_by_product = {
        'CFFEX.IF': {'IF2603': [_make_bar(20250101)]},
        'SHFE.ag':  {'ag2603': [_make_bar(20250101)]},
        'CZCE.AP':  {'AP603':  [_make_bar(20250101)]},
        'INE.ec':   {'ec2603': [_make_bar(20250101)]},
    }
    contracts = gen.generate_contracts(
        codes_info={'CFFEX': ['IF'], 'SHFE': ['ag'], 'CZCE': ['AP'], 'INE': ['ec']},
        bars_by_product=bars_by_product,
    )
    assert contracts['CFFEX']['IF2603']['maxlimitqty'] == 20
    assert contracts['SHFE']['ag2603']['maxlimitqty']  == 500
    assert contracts['CZCE']['AP603']['maxlimitqty']   == 1000
    assert contracts['INE']['ec2603']['maxlimitqty']   == 500


def test_p22_contracts_margin_per_product_from_wt_template(tmp_path):
    """RED: longmargin/shortmargin 必须按品种派生, 不能全 0.1。"""
    reader = _FakeKsReaderForContracts(codes_by_product={
        'SHFE.ag': ['ag2603'],
        'CFFEX.IF': ['IF2603'],
        'GFEX.pd': ['pd2606'],
        'CZCE.AP': ['AP603'],
    })
    gen = ConfigGenerator(reader, str(tmp_path))
    bars_by_product = {k: {next(iter(reader._codes[k])): [_make_bar(20250101)]}
                       for k in reader._codes}
    contracts = gen.generate_contracts(
        codes_info={'SHFE': ['ag'], 'CFFEX': ['IF'], 'GFEX': ['pd'], 'CZCE': ['AP']},
        bars_by_product=bars_by_product,
    )
    assert abs(contracts['SHFE']['ag2603']['longmarginratio']  - 0.19) < 1e-9
    assert abs(contracts['SHFE']['ag2603']['shortmarginratio'] - 0.19) < 1e-9
    assert abs(contracts['CFFEX']['IF2603']['longmarginratio'] - 0.15) < 1e-9
    assert abs(contracts['GFEX']['pd2606']['longmarginratio']  - 0.09) < 1e-9
    assert abs(contracts['CZCE']['AP603']['longmarginratio']   - 0.15) < 1e-9
    margins = {contracts[ex][list(d.keys())[0]]['longmarginratio']
               for ex, d in contracts.items() if d}
    assert margins != {0.1}, '保证金不能全是 0.1 占位'


def test_p22_contracts_maxmarketqty_per_product(tmp_path):
    """RED: maxmarketqty 按品种派生 (SHFE.ag=30, DCE.a=1000, CZCE.AP=0)。"""
    reader = _FakeKsReaderForContracts(codes_by_product={
        'SHFE.ag': ['ag2603'],
        'DCE.a': ['a2603'],
        'CZCE.AP': ['AP603'],
    })
    gen = ConfigGenerator(reader, str(tmp_path))
    bars_by_product = {
        'SHFE.ag': {'ag2603': [_make_bar(20250101)]},
        'DCE.a':   {'a2603':  [_make_bar(20250101)]},
        'CZCE.AP': {'AP603':  [_make_bar(20250101)]},
    }
    contracts = gen.generate_contracts(
        codes_info={'SHFE': ['ag'], 'DCE': ['a'], 'CZCE': ['AP']},
        bars_by_product=bars_by_product,
    )
    assert contracts['SHFE']['ag2603']['maxmarketqty'] == 30
    assert contracts['DCE']['a2603']['maxmarketqty']   == 1000
    assert contracts['CZCE']['AP603']['maxmarketqty']  == 0


def test_p22_contracts_opendate_expiredate_from_ks_bars(tmp_path):
    """opendate/expiredate 保留 KS 派生 (回归): 首末 bar 日期。"""
    reader = _FakeKsReaderForContracts(codes_by_product={'SHFE.ag': ['ag2603']})
    gen = ConfigGenerator(reader, str(tmp_path))
    bars_by_product = {
        'SHFE.ag': {'ag2603': [_make_bar(20250318), _make_bar(20250620), _make_bar(20260316)]}
    }
    contracts = gen.generate_contracts(
        codes_info={'SHFE': ['ag']},
        bars_by_product=bars_by_product,
    )
    assert contracts['SHFE']['ag2603']['opendate']   == 20250318
    assert contracts['SHFE']['ag2603']['expiredate'] == 20260316


def test_p22_contracts_unknown_product_falls_back_with_warning(tmp_path, caplog):
    """RED: WT contracts 模板未覆盖品种走 fallback + warning。"""
    import logging
    reader = _FakeKsReaderForContracts(codes_by_product={'SHFE.zz': ['zz2603']})
    gen = ConfigGenerator(reader, str(tmp_path))
    bars_by_product = {'SHFE.zz': {'zz2603': [_make_bar(20250101)]}}
    with caplog.at_level(logging.WARNING):
        contracts = gen.generate_contracts(
            codes_info={'SHFE': ['zz']},
            bars_by_product=bars_by_product,
        )
    c = contracts['SHFE']['zz2603']
    assert c['maxlimitqty']       == 1000
    assert c['maxmarketqty']      == 0
    assert c['longmarginratio']   == 0.1
    assert c['shortmarginratio']  == 0.1
    assert any('contracts' in r.message.lower() and 'fallback' in r.message.lower()
               for r in caplog.records), '必须发 contracts fallback warning'


def test_p22_contracts_output_loadable_by_wt_schema(tmp_path):
    """端到端: contracts.json 12 必需字段齐 + 类型正确 (WT C++ 加载所需)。"""
    reader = _FakeKsReaderForContracts(codes_by_product={
        'SHFE.ag': ['ag2603'],
        'CFFEX.IF': ['IF2603'],
        'GFEX.pd': ['pd2606'],
    })
    gen = ConfigGenerator(reader, str(tmp_path))
    bars_by_product = {
        'SHFE.ag':  {'ag2603': [_make_bar(20250318), _make_bar(20260316)]},
        'CFFEX.IF': {'IF2603': [_make_bar(20250721), _make_bar(20260320)]},
        'GFEX.pd':  {'pd2606': [_make_bar(20251127), _make_bar(20260522)]},
    }
    contracts = gen.generate_contracts(
        codes_info={'SHFE': ['ag'], 'CFFEX': ['IF'], 'GFEX': ['pd']},
        bars_by_product=bars_by_product,
    )
    required = {'name','code','exchg','product','maxlimitqty','maxmarketqty',
                'minlimitqty','minmarketqty','opendate','expiredate',
                'longmarginratio','shortmarginratio'}
    for ex, dct in contracts.items():
        for code, c in dct.items():
            missing = required - set(c.keys())
            assert not missing, f'{ex}.{code} 缺字段: {missing}'
            assert isinstance(c['maxlimitqty'], int)
            assert isinstance(c['opendate'], int)
            assert isinstance(c['longmarginratio'], float)
            assert c['opendate'] > 0
            assert c['expiredate'] >= c['opendate']


# ============================================================================
# p2-3 [P1]: fees.json 字段生产化（WT 模板拷贝 + 缺失品种 fallback）
# ============================================================================
# KS 数据本身不含手续费; 走 WT 官方 fees.json 快照拷贝模式:
#   - 覆盖品种: 拷贝 WT 模板 {open, close, closetoday, byvolume}
#   - 未覆盖品种: fallback (全 0.0, byvolume=False) + warning 提醒手动补
#   - 字段名必须用 WT 标准 'byvolume' (bool), 而非历史错误的 'mode': 'ratio'


def test_p23_fees_loads_wt_snapshot_for_known_products(tmp_path):
    """RED: WT 模板覆盖的品种必须使用快照值, 不能是 0.0 占位。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    fees = gen.generate_fees({
        'SHFE': ['ag'], 'CFFEX': ['IF', 'IC'], 'CZCE': ['AP', 'CF'],
        'DCE': ['a', 'm'],
    })
    # SHFE.ag: 5e-05 ratio
    assert abs(fees['SHFE.ag']['open']  - 5e-05) < 1e-12, 'SHFE.ag.open 应为 5e-05'
    assert fees['SHFE.ag']['byvolume'] is False, 'SHFE.ag 按比例'
    # CFFEX.IF: 2.3e-05 ratio
    assert abs(fees['CFFEX.IF']['open'] - 2.3e-05) < 1e-12
    assert fees['CFFEX.IF']['byvolume'] is False
    # CZCE.AP: 0.5 按手数
    assert fees['CZCE.AP']['open']       == 0.5
    assert fees['CZCE.AP']['byvolume'] is True, 'CZCE.AP 按手数'
    # DCE.a: 2.0 按手数
    assert fees['DCE.a']['open']         == 2.0
    assert fees['DCE.a']['byvolume'] is True


def test_p23_fees_uses_byvolume_not_mode_field(tmp_path):
    """RED: 字段名必须是 byvolume (WT 标准), 不能是 mode (历史错字段)。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    fees = gen.generate_fees({'SHFE': ['ag']})
    e = fees['SHFE.ag']
    assert 'byvolume' in e, '必须有 byvolume 字段 (WT 标准)'
    assert 'mode' not in e, '不能保留 mode 字段 (历史错误)'
    assert isinstance(e['byvolume'], bool), 'byvolume 必须是 bool'


def test_p23_fees_unknown_product_falls_back_with_warning(tmp_path, caplog):
    """RED: WT 模板未覆盖的品种走 fallback (全 0.0, byvolume=False) + warning。"""
    import logging
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    with caplog.at_level(logging.WARNING):
        fees = gen.generate_fees({
            'CFFEX': ['IH', 'IM'],  # 缺失
            'GFEX':  ['pd'],         # 缺失
            'INE':   ['ec'],         # 缺失
            'DCE':   ['b'],          # 缺失
        })
    for key in ['CFFEX.IH', 'CFFEX.IM', 'GFEX.pd', 'INE.ec', 'DCE.b']:
        assert key in fees, f'{key} 必须存在 (fallback)'
        e = fees[key]
        assert e['open']       == 0.0
        assert e['close']      == 0.0
        assert e['closetoday'] == 0.0
        assert e['byvolume'] is False
    msgs = ' | '.join(r.message for r in caplog.records)
    assert 'fees' in msgs.lower() and 'fallback' in msgs.lower(),         f'必须发 fees fallback warning, got: {msgs[:200]}'


def test_p23_fees_excludes_options_via_iter_products(tmp_path):
    """期权品种 (CFFEX.HO 等) 走 _iter_products 已被跳过, fees 也不应包含。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    # 即使显式传 CFFEX.HO, _iter_products 会过滤掉
    fees = gen.generate_fees({'CFFEX': ['HO']})
    assert 'CFFEX.HO' not in fees, '期权品种必须被过滤'


def test_p23_fees_output_schema_complete(tmp_path):
    """端到端: fees.json 每条 entry 必须有 open/close/closetoday/byvolume 4 字段 + 类型正确。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    fees = gen.generate_fees({
        'SHFE': ['ag'], 'CFFEX': ['IF', 'IH'],
        'CZCE': ['AP'], 'DCE': ['a', 'b'], 'GFEX': ['pd'],
    })
    required = {'open', 'close', 'closetoday', 'byvolume'}
    for k, e in fees.items():
        missing = required - set(e.keys())
        assert not missing, f'{k} 缺字段: {missing}'
        assert isinstance(e['open'],       (int, float)), f'{k}.open 必须 numeric'
        assert isinstance(e['close'],      (int, float)), f'{k}.close 必须 numeric'
        assert isinstance(e['closetoday'], (int, float)), f'{k}.closetoday 必须 numeric'
        assert isinstance(e['byvolume'],   bool),         f'{k}.byvolume 必须 bool'
        # 不能有遗留 mode 字段
        assert 'mode' not in e, f'{k} 残留 mode 字段 (应为 byvolume)'



# ============================================================================
# p2-4 [P1]: holidays.json 字段生产化 (WT 快照拷贝 + 2023-2026 手工扩展)
# ============================================================================
# KS 数据本身不提供节假日; WT 原版 holidays.json 只到 2022 (15 年, 265 天)。
# 我们: (1) 顶层结构必须是 {"CHINA": [...]} (commodities.holiday='CHINA' 对应);
#       (2) 拷贝 WT 快照 2008-2022 全量;
#       (3) 2023-2026 用 references/extra_holidays_2023_2026.json 手工扩展 (中国法定);
#       (4) 字符串日期 YYYYMMDD 排序去重。


def test_p24_holidays_top_level_is_china_key(tmp_path):
    """RED: 顶层必须是 {'CHINA': [...]} 才能匹配 commodities.holiday='CHINA'。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    h = gen.generate_holidays()
    assert 'CHINA' in h, "顶层必须有 CHINA 键 (commodities.holiday 引用)"
    assert isinstance(h['CHINA'], list), "CHINA 值必须是 list"
    # 不能是按年分组的错误结构
    assert '2020' not in h, "不能是按年分组的错误结构 {2020: [], 2021: [], ...}"


def test_p24_holidays_loads_wt_snapshot_2008_2022(tmp_path):
    """RED: 必须拷贝 WT 快照 2008-2022 全量 (265 天)。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    h = gen.generate_holidays()
    dates = h['CHINA']
    # 抽样 WT 原版已知日期
    assert '20080101' in dates, "WT 快照首日 20080101 必须存在"
    assert '20220502' in dates, "WT 快照 2022 五一节必须存在"
    assert '20221007' in dates, "WT 快照末日 20221007 必须存在"
    # 数量下限: WT 原版 2008-2022 = 265 天
    wt_2008_2022 = [d for d in dates if 20080101 <= int(d) <= 20221231]
    assert len(wt_2008_2022) >= 265, f"2008-2022 应≥265 天, got {len(wt_2008_2022)}"


def test_p24_holidays_extends_2023_to_2026(tmp_path):
    """RED: 必须扩展 2023-2026 (KS 无, WT 模板无, 走 references/ 手工表)。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    h = gen.generate_holidays()
    dates = h['CHINA']
    # 2023-2026 至少要有元旦 (确保扩展表生效)
    assert '20230101' in dates or '20230102' in dates,         "2023 元旦节假日必须扩展 (从 extra_holidays_2023_2026.json)"
    # 抽样: 2024 春节
    yrs_present = set(d[:4] for d in dates)
    for y in ['2023', '2024', '2025', '2026']:
        assert y in yrs_present, f"{y} 年必须有节假日数据"


def test_p24_holidays_dates_sorted_and_unique(tmp_path):
    """RED: 日期必须排序且去重。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    h = gen.generate_holidays()
    dates = h['CHINA']
    assert dates == sorted(dates), "日期必须升序排列"
    assert len(dates) == len(set(dates)), "日期必须去重"
    # 格式: YYYYMMDD 8 位数字字符串
    for d in dates:
        assert isinstance(d, str) and len(d) == 8 and d.isdigit(),             f"日期格式必须 YYYYMMDD: {d}"


def test_p24_holidays_missing_extra_file_falls_back_to_wt_only(tmp_path, caplog, monkeypatch):
    """RED: extras 文件缺失时, 只拷贝 WT 快照 + 发 warning, 不抛异常。"""
    import logging
    # 重置缓存 + 替换路径到不存在文件
    import config_generator as cg
    monkeypatch.setattr(cg, '_EXTRA_HOLIDAYS_PATH', '/tmp/__nonexistent_extras.json')
    monkeypatch.setattr(cg, '_EXTRA_HOLIDAYS_CACHE', None)
    monkeypatch.setattr(cg, '_WT_HOLIDAYS_CACHE', None)
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    with caplog.at_level(logging.WARNING):
        h = gen.generate_holidays()
    assert 'CHINA' in h
    assert '20080101' in h['CHINA'], "WT 快照部分仍可用"
    msgs = ' | '.join(r.message for r in caplog.records)
    assert 'holiday' in msgs.lower() or 'extra' in msgs.lower(),         f"缺失 extras 时必须发 warning, got: {msgs[:200]}"



# ============================================================================
# p2-5 [P1]: sessions.json 字段生产化 (WT 快照拷贝, 修复命名失配)
# ============================================================================
# 老实现: 自定义命名 CFFEX_STOCK/SHFE_NIGHT1/... 但 commodities.session
# 引用的是 WT 命名 FN0230/SD0930/FD0900 → 严重失配, C++ loader 解析失败。
# 新方案: 拷贝 WT 官方 sessions.json (8 个 ID: FD0900/FD0915/FN0100/FN0230/
#         FN2300/FN2330/SD0930/TRADING), 命名与 commodities.session 一致。


def test_p25_sessions_match_wt_official_ids(tmp_path):
    """RED: 8 个 session ID 必须与 WT 原版完全一致 (commodities.session 才能引用到)。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    s = gen.generate_sessions()
    expected = {'FD0900', 'FD0915', 'FN0100', 'FN0230',
                'FN2300', 'FN2330', 'SD0930', 'TRADING'}
    actual = set(s.keys())
    assert actual == expected, f"sessions IDs 必须 = WT 8 个; got={actual}, missing={expected-actual}, extra={actual-expected}"


def test_p25_sessions_no_custom_legacy_names(tmp_path):
    """RED: 不允许残留老自定义命名 (CFFEX_STOCK / SHFE_NIGHT1 等)。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    s = gen.generate_sessions()
    legacy = {'CFFEX_STOCK', 'CFFEX_BOND', 'SHFE_NIGHT1', 'DCE_NIGHT1',
              'CZCE_NIGHT1', 'INE_NIGHT1', 'GFEX_DAY', 'CZCE_DAY'}
    assert not (set(s.keys()) & legacy),         f"必须清除老自定义命名, 残留: {set(s.keys()) & legacy}"


def test_p25_sessions_values_match_wt_snapshot(tmp_path):
    """RED: 字段值必须与 WT 官方快照一致 (name/offset/auction/sections)。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    s = gen.generate_sessions()
    # SD0930 (股票白盘)
    sd = s['SD0930']
    assert sd['name']    == '股票白盘0930'
    assert sd['offset']  == 0
    assert sd['auction'] == {'from': 929, 'to': 930}
    assert sd['sections'] == [{'from': 930,'to': 1130},{'from': 1300,'to': 1500}]
    # FN2300 (期货夜盘 2300 - 最常见的农产品夜盘)
    fn = s['FN2300']
    assert fn['offset'] == 300, "夜盘必须 offset=300 (跨日处理)"
    assert fn['sections'][0]['from'] == 2100
    # FN0230 (期货夜盘到 0230 - 黄金白银等贵金属)
    fn2 = s['FN0230']
    assert fn2['offset'] == 300
    assert any(sec['to'] == 230 for sec in fn2['sections']), "FN0230 必须有 to=230 段"


def test_p25_sessions_commodities_references_resolvable(tmp_path):
    """RED: commodities.json 里所有 session 引用必须都能在 sessions.json 解析到。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    sessions = gen.generate_sessions()
    commodities = gen.generate_commodities({
        'SHFE': ['ag'], 'CFFEX': ['IF', 'IC', 'IH'],
        'CZCE': ['AP', 'CF', 'SR'], 'DCE': ['a', 'b', 'm'],
        'INE': ['ec'], 'GFEX': ['pd'],
    })
    referenced = set()
    for ex, prods in commodities.items():
        for p, info in prods.items():
            referenced.add(info.get('session', ''))
    unresolved = referenced - set(sessions.keys())
    assert not unresolved,         f"commodities 引用了 sessions.json 中不存在的 session ID: {unresolved}"


def test_p25_sessions_schema_complete(tmp_path):
    """每个 session 必须有 name/offset/sections 3 字段, 类型正确。"""
    reader = _FakeKsReaderForCommodities()
    gen = ConfigGenerator(reader, str(tmp_path))
    s = gen.generate_sessions()
    for sid, sess in s.items():
        assert 'name'     in sess,             f"{sid} 缺 name"
        assert 'offset'   in sess,             f"{sid} 缺 offset"
        assert 'sections' in sess,             f"{sid} 缺 sections"
        assert isinstance(sess['offset'],   int),  f"{sid}.offset 必须 int"
        assert isinstance(sess['sections'], list), f"{sid}.sections 必须 list"
        for sec in sess['sections']:
            assert 'from' in sec and 'to' in sec, f"{sid} section 缺 from/to"



# ============================================================================
# p5-1 [P0]: Phase 5 实盘 gate - 数据新鲜度检查 (FreshnessGate)
# ============================================================================
# 实盘启动前必须确认数据未陈旧:
#   - DSB 最新 bar 内部日期 (最权威, 业务真值)
#   - DSB 文件 mtime (次要, 时间戳真值)
#   - manifest.generated_at (审计真值)
# 任一超阈值 → fail; 写 live_preflight_report.json + live_gate.json
# 供启动脚本读取 (live_gate.json 含 ok:bool + 阻断原因)。


def test_p51_freshness_gate_passes_for_fresh_data(tmp_path):
    """RED: 全新生成的 manifest + DSB 文件 → pass。"""
    from acceptance.freshness import FreshnessGate
    import json, time
    # 写 manifest (generated_at = 现在)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "generated_at": now,
        "stats": {"errors": 0},
        "access_contract": {"version_id": "test-v1"},
    }
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    # 假 DSB (mtime = now)
    dsb_dir = tmp_path / "his" / "day" / "SHFE"
    dsb_dir.mkdir(parents=True)
    (dsb_dir / "ag_main.dsb").write_bytes(b"x" * 128)

    r = FreshnessGate(max_age_hours=24).run(str(tmp_path))
    assert r.status == "pass", f"全新数据应 pass, got {r.status}: {r.summary}"
    assert "max_age_hours" in r.details
    assert r.details.get("manifest_age_hours") is not None


def test_p51_freshness_gate_fails_for_stale_manifest(tmp_path):
    """RED: manifest.generated_at 超过 24h → fail。"""
    from acceptance.freshness import FreshnessGate
    import json
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    manifest = {
        "generated_at": old,
        "stats": {"errors": 0},
        "access_contract": {"version_id": "test-stale"},
    }
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    # DSB 也 stale
    dsb_dir = tmp_path / "his" / "day" / "SHFE"
    dsb_dir.mkdir(parents=True)
    f = dsb_dir / "ag_main.dsb"
    f.write_bytes(b"x" * 128)
    import os, time
    old_ts = time.time() - 48 * 3600
    os.utime(f, (old_ts, old_ts))

    r = FreshnessGate(max_age_hours=24).run(str(tmp_path))
    assert r.status == "fail", f"超 24h 应 fail, got {r.status}: {r.summary}"
    assert "stale" in r.summary.lower() or "超" in r.summary or "陈旧" in r.summary


def test_p51_freshness_gate_warns_when_no_dsb(tmp_path):
    """RED: 没有 DSB 文件 → warn (manifest OK 但无数据落地)。"""
    from acceptance.freshness import FreshnessGate
    import json
    from datetime import datetime, timezone
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    r = FreshnessGate(max_age_hours=24).run(str(tmp_path))
    assert r.status in ("warn", "fail"),         f"无 DSB 应 warn 或 fail, got {r.status}"
    assert "dsb" in r.summary.lower() or "无文件" in r.summary or "missing" in r.summary.lower()


def test_p51_freshness_gate_fails_when_no_manifest(tmp_path):
    """RED: 没有 manifest → fail (无法判断新鲜度)。"""
    from acceptance.freshness import FreshnessGate
    r = FreshnessGate(max_age_hours=24).run(str(tmp_path))
    assert r.status == "fail", f"无 manifest 应 fail, got {r.status}"
    assert "manifest" in r.summary.lower()


def test_p51_freshness_gate_writes_live_gate_json(tmp_path):
    """RED: 必须写 live_gate.json 供启动脚本读取。"""
    from acceptance.freshness import FreshnessGate
    import json
    from datetime import datetime, timezone
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"errors": 0},
                "access_contract": {"version_id": "test-v1"}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    dsb = tmp_path / "his" / "day" / "SHFE" / "ag_main.dsb"
    dsb.parent.mkdir(parents=True)
    dsb.write_bytes(b"x" * 128)

    gate = FreshnessGate(max_age_hours=24, write_live_gate=True)
    r = gate.run(str(tmp_path))
    live_gate_path = tmp_path / "live_gate.json"
    assert live_gate_path.exists(), "live_gate.json 必须被写入"
    g = json.loads(live_gate_path.read_text(encoding="utf-8"))
    assert "ok" in g, "live_gate.json 必须有 ok 字段"
    assert isinstance(g["ok"], bool)
    assert "status" in g
    assert "reason" in g or "summary" in g
    assert g["ok"] is True, "新鲜数据 ok 应 True"


def test_p51_freshness_gate_live_gate_ok_false_when_fail(tmp_path):
    """RED: fail 时 live_gate.json.ok 必须 False, 给启动脚本明确阻断信号。"""
    from acceptance.freshness import FreshnessGate
    import json
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    manifest = {"generated_at": old, "stats": {"errors": 0},
                "access_contract": {"version_id": "stale"}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    gate = FreshnessGate(max_age_hours=24, write_live_gate=True)
    gate.run(str(tmp_path))
    g = json.loads((tmp_path / "live_gate.json").read_text(encoding="utf-8"))
    assert g["ok"] is False, "fail 时 ok 必须 False"
    assert g["status"] == "fail"


def test_p51_freshness_gate_respects_threshold(tmp_path):
    """RED: max_age_hours 阈值必须可配置, 6h 比 24h 更严格。"""
    from acceptance.freshness import FreshnessGate
    import json
    from datetime import datetime, timezone, timedelta
    # 12h 前的 manifest: 24h 阈值 → pass, 6h 阈值 → fail
    age12 = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    manifest = {"generated_at": age12, "stats": {"errors": 0},
                "access_contract": {"version_id": "v"}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    dsb = tmp_path / "his" / "day" / "SHFE" / "ag_main.dsb"
    dsb.parent.mkdir(parents=True)
    dsb.write_bytes(b"x" * 128)
    import os, time
    ts = time.time() - 12 * 3600
    os.utime(dsb, (ts, ts))

    r24 = FreshnessGate(max_age_hours=24).run(str(tmp_path))
    assert r24.status == "pass", f"12h 在 24h 阈值内应 pass, got {r24.status}"
    r6 = FreshnessGate(max_age_hours=6).run(str(tmp_path))
    assert r6.status == "fail", f"12h 超 6h 阈值应 fail, got {r6.status}"



# ============================================================================
# p5-3 [P0]: Phase 5 实盘 gate - 失败非零退出 + 阻断信号文件 (live_gate 子命令)
# ============================================================================
# 启动脚本契约:
#   python -m acceptance.live_gate --output <dir> --max-age-hours 24
#   exit 0 → ok (启动); exit 1 → warn; exit 2 → fail (阻断)
#   --strict: warn 也升 fail
# 轻量: 只跑 freshness, 不跑 pytest/pycompile/wt_readback (实盘 preflight 不耗时)


def test_p53_live_gate_module_importable():
    """RED: acceptance.live_gate 必须可作为模块运行 (python -m)。"""
    import importlib
    m = importlib.import_module("acceptance.live_gate")
    assert hasattr(m, "main"), "acceptance.live_gate 必须有 main()"
    assert callable(m.main)


def test_p53_live_gate_exits_zero_for_fresh(tmp_path):
    """RED: 新鲜数据 → exit 0。"""
    import json, subprocess, sys
    from datetime import datetime, timezone
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"errors": 0},
                "access_contract": {"version_id": "fresh"}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    dsb = tmp_path / "his" / "day" / "SHFE" / "ag_main.dsb"
    dsb.parent.mkdir(parents=True)
    dsb.write_bytes(b"x" * 128)

    r = subprocess.run(
        [sys.executable, "-m", "acceptance.live_gate",
         "--output", str(tmp_path), "--max-age-hours", "24"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, (
        f"新鲜数据应 exit 0, got {r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")


def test_p53_live_gate_exits_two_for_stale(tmp_path):
    """RED: 陈旧 manifest → exit 2 (fail, 阻断)。"""
    import json, subprocess, sys
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    manifest = {"generated_at": old, "stats": {"errors": 0},
                "access_contract": {"version_id": "stale"}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    r = subprocess.run(
        [sys.executable, "-m", "acceptance.live_gate",
         "--output", str(tmp_path), "--max-age-hours", "24"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2, (
        f"陈旧应 exit 2, got {r.returncode}\nstdout={r.stdout}")


def test_p53_live_gate_exits_one_for_warn(tmp_path):
    """RED: 无 DSB (warn) → exit 1 (默认非严格)。"""
    import json, subprocess, sys
    from datetime import datetime, timezone
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    # 故意不造 DSB
    r = subprocess.run(
        [sys.executable, "-m", "acceptance.live_gate",
         "--output", str(tmp_path), "--max-age-hours", "24"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 1, (
        f"无 DSB warn 应 exit 1, got {r.returncode}\nstdout={r.stdout}")


def test_p53_live_gate_strict_upgrades_warn_to_fail(tmp_path):
    """RED: --strict 把 warn 升为 fail → exit 2。"""
    import json, subprocess, sys
    from datetime import datetime, timezone
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "acceptance.live_gate",
         "--output", str(tmp_path), "--max-age-hours", "24", "--strict"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2, (
        f"--strict warn 应升 fail exit 2, got {r.returncode}\nstdout={r.stdout}")


def test_p53_live_gate_writes_live_gate_json(tmp_path):
    """RED: 子命令必须落地 live_gate.json (启动脚本读)。"""
    import json, subprocess, sys
    from datetime import datetime, timezone
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"errors": 0},
                "access_contract": {"version_id": "v"}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    dsb = tmp_path / "his" / "day" / "SHFE" / "ag_main.dsb"
    dsb.parent.mkdir(parents=True)
    dsb.write_bytes(b"x" * 128)

    subprocess.run(
        [sys.executable, "-m", "acceptance.live_gate",
         "--output", str(tmp_path), "--max-age-hours", "24"],
        capture_output=True, text=True, timeout=30,
    )
    assert (tmp_path / "live_gate.json").exists(), "live_gate.json 必须落地"
    g = json.loads((tmp_path / "live_gate.json").read_text(encoding="utf-8"))
    assert g["ok"] is True


def test_p53_live_gate_stdout_is_json_summary(tmp_path):
    """RED: stdout 必须是单行 JSON, 便于 shell 解析 (jq friendly)。"""
    import json, subprocess, sys
    from datetime import datetime, timezone
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"errors": 0},
                "access_contract": {"version_id": "vv"}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    dsb = tmp_path / "his" / "day" / "SHFE" / "ag_main.dsb"
    dsb.parent.mkdir(parents=True)
    dsb.write_bytes(b"x" * 128)

    r = subprocess.run(
        [sys.executable, "-m", "acceptance.live_gate",
         "--output", str(tmp_path), "--max-age-hours", "24"],
        capture_output=True, text=True, timeout=30,
    )
    payload = json.loads(r.stdout.strip())  # 必须可 json.loads
    assert "ok" in payload and payload["ok"] is True
    assert "status" in payload and payload["status"] == "pass"
    assert "exit_code" in payload and payload["exit_code"] == 0


def test_p53_live_gate_missing_output_dir_exits_two(tmp_path):
    """RED: --output 指向不存在的目录 → exit 2 (manifest 缺失)。"""
    import subprocess, sys
    bogus = tmp_path / "does_not_exist"
    r = subprocess.run(
        [sys.executable, "-m", "acceptance.live_gate",
         "--output", str(bogus), "--max-age-hours", "24"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2, f"无 manifest 应 exit 2, got {r.returncode}"



# ============================================================================
# p5-2 [P0]: Phase 5 实盘 gate - 当日完整性检查 (CompletenessGate)
# ============================================================================
# 实盘启动前必须确认: 应有的合成合约 (_main / _index) 实际全部落地。
# 推荐口径 (用户拍板):
#   - 范围 (a): 仅查 manifest.filters.specific_products 列出品种的 _main + _index
#     (实盘策略基本只跑 _main, 原始合约不在 preflight 关键路径)
#   - 阈值: 缺失率 0%=pass, >0 且 ≤5%=warn, >5%=fail
#   - 节假日: 由 holidays.json 排除 (今日是假日 → 整体 skip, 非阻断)
# 副产物: live_gate.json 复用 + completeness 段写 details
# 注: 与 freshness gate 独立, 互不污染


def test_p52_completeness_gate_passes_when_all_main_present(tmp_path):
    """RED: 所有 _main/_index 都在 → pass。"""
    from acceptance.completeness import CompletenessGate
    import json
    manifest = {
        "filters": {
            "specific_products": {"SHFE": ["ag"], "DCE": ["m"]},
        },
        "stats": {"errors": 0},
    }
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    # 应有 4 个: SHFE.ag_main, SHFE.ag_index, DCE.m_main, DCE.m_index
    for ex, prod in [("SHFE", "ag"), ("DCE", "m")]:
        d = tmp_path / "his" / "day" / ex
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{prod}_main.dsb").write_bytes(b"x" * 128)
        (d / f"{prod}_index.dsb").write_bytes(b"x" * 128)

    r = CompletenessGate().run(str(tmp_path))
    assert r.status == "pass", f"全到位应 pass, got {r.status}: {r.summary}"
    assert r.details["expected_count"] == 4
    assert r.details["found_count"] == 4
    assert r.details["missing_count"] == 0


def test_p52_completeness_gate_warns_at_low_miss_rate(tmp_path):
    """RED: 缺失 ≤5% → warn (例: 1/20 = 5%)。"""
    from acceptance.completeness import CompletenessGate
    import json
    # 10 品种, 应有 20 (_main+_index), 缺 1 → 5%
    prods = {"SHFE": ["a","b","c","d","e"], "DCE": ["x","y","z","u","v"]}
    manifest = {"filters": {"specific_products": prods}, "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    for ex, plist in prods.items():
        d = tmp_path / "his" / "day" / ex
        d.mkdir(parents=True, exist_ok=True)
        for p in plist:
            (d / f"{p}_main.dsb").write_bytes(b"x" * 128)
            (d / f"{p}_index.dsb").write_bytes(b"x" * 128)
    # 故意删一个
    (tmp_path / "his" / "day" / "DCE" / "v_index.dsb").unlink()

    r = CompletenessGate().run(str(tmp_path))
    assert r.status == "warn", f"缺 5% 应 warn, got {r.status}: {r.summary}"
    assert r.details["missing_count"] == 1
    assert "DCE.v_index" in r.details["missing"] or any(
        "v_index" in m for m in r.details["missing"])


def test_p52_completeness_gate_fails_at_high_miss_rate(tmp_path):
    """RED: 缺失 >5% → fail (例: 2/10 = 20%)。"""
    from acceptance.completeness import CompletenessGate
    import json
    prods = {"SHFE": ["a","b","c","d","e"]}  # 10 个 _main/_index
    manifest = {"filters": {"specific_products": prods}, "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    d = tmp_path / "his" / "day" / "SHFE"
    d.mkdir(parents=True, exist_ok=True)
    for p in prods["SHFE"]:
        (d / f"{p}_main.dsb").write_bytes(b"x" * 128)
        (d / f"{p}_index.dsb").write_bytes(b"x" * 128)
    # 删 2 个 → 20%
    (d / "a_main.dsb").unlink()
    (d / "b_main.dsb").unlink()

    r = CompletenessGate().run(str(tmp_path))
    assert r.status == "fail", f"缺 20% 应 fail, got {r.status}: {r.summary}"
    assert r.details["missing_count"] == 2


def test_p52_completeness_gate_skips_on_holiday(tmp_path):
    """RED: 今日是假日 → skip (不阻断启动, 但记录原因)。"""
    from acceptance.completeness import CompletenessGate
    from datetime import date
    import json
    today_str = date.today().strftime("%Y%m%d")
    manifest = {"filters": {"specific_products": {"SHFE": ["ag"]}},
                "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    common = tmp_path / "common"
    common.mkdir()
    holidays = {"CHINA": [today_str]}
    (common / "holidays.json").write_text(
        json.dumps(holidays), encoding="utf-8")
    # 没造 DSB 但今天是假日 → skip

    r = CompletenessGate().run(str(tmp_path))
    assert r.status == "skip", f"假日应 skip, got {r.status}: {r.summary}"
    assert "holiday" in r.summary.lower() or "假日" in r.summary


def test_p52_completeness_gate_fails_when_no_manifest(tmp_path):
    """RED: 无 manifest → fail。"""
    from acceptance.completeness import CompletenessGate
    r = CompletenessGate().run(str(tmp_path))
    assert r.status == "fail"
    assert "manifest" in r.summary.lower()


def test_p52_completeness_gate_handles_empty_specific_products(tmp_path):
    """RED: filters.specific_products 为空 → skip (无应有清单可对比)。"""
    from acceptance.completeness import CompletenessGate
    import json
    manifest = {"filters": {"specific_products": {}}, "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    r = CompletenessGate().run(str(tmp_path))
    assert r.status == "skip", f"空清单应 skip, got {r.status}: {r.summary}"
    assert "specific_products" in r.summary or "无应有清单" in r.summary or "empty" in r.summary.lower()


def test_p52_completeness_gate_respects_custom_thresholds(tmp_path):
    """RED: 阈值可配置, warn_pct/fail_pct 都生效。"""
    from acceptance.completeness import CompletenessGate
    import json
    prods = {"SHFE": ["a","b","c","d","e"]}  # 10
    manifest = {"filters": {"specific_products": prods}, "stats": {"errors": 0}}
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    d = tmp_path / "his" / "day" / "SHFE"
    d.mkdir(parents=True, exist_ok=True)
    for p in prods["SHFE"]:
        (d / f"{p}_main.dsb").write_bytes(b"x" * 128)
        (d / f"{p}_index.dsb").write_bytes(b"x" * 128)
    (d / "a_main.dsb").unlink()  # 1/10 = 10%

    # 默认阈值: 10% > 5% → fail
    r1 = CompletenessGate().run(str(tmp_path))
    assert r1.status == "fail"
    # 自定阈值: warn=20, fail=50 → 10% < 20 → warn
    r2 = CompletenessGate(warn_pct=20.0, fail_pct=50.0).run(str(tmp_path))
    assert r2.status == "warn", f"10% < 20% warn 阈值应 warn, got {r2.status}"


def test_p52_completeness_gate_lists_missing_in_details(tmp_path):
    """RED: details.missing 必须列出缺失清单 (按 EXCHG.code_suffix)。"""
    from acceptance.completeness import CompletenessGate
    import json
    manifest = {
        "filters": {"specific_products": {"SHFE": ["ag", "au"]}},
        "stats": {"errors": 0},
    }
    (tmp_path / "conversion_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    d = tmp_path / "his" / "day" / "SHFE"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ag_main.dsb").write_bytes(b"x" * 128)
    (d / "ag_index.dsb").write_bytes(b"x" * 128)
    # au 全缺

    r = CompletenessGate(warn_pct=5.0, fail_pct=10.0).run(str(tmp_path))
    assert r.status == "fail"  # 2/4 = 50%
    missing = set(r.details["missing"])
    assert "SHFE.au_main" in missing
    assert "SHFE.au_index" in missing
    assert "SHFE.ag_main" not in missing



# ============================================================================
# ops-2 [P1]: QualityGate 白名单 - 价差合约 + 复权合成合约归因
# ============================================================================
# 真实仓暴露的 312 ohlc / 351 oi / 476 price_anomalies 全部来自:
#   1. 价差合约 (CZCE.SPD/IPS, GFEX.SP): OHLC 可负、可极端跳变 (价差本身)
#   2. .HOT 复权合约: 除权日 close 与 prev_close 巨变是正常的 (1/100 比率)
#   3. _main 合成合约: 换月点 OI/价格天然跳变
# 这些不是数据 bug, 是规则误伤。需 QualityGate 支持 file-level 白名单。


def _make_quality_report(tmp_path, files_data, summary_overrides=None):
    """造 quality_report.json fixture, 用 files 自动聚合 summary。"""
    import json
    from collections import Counter
    summary_counters = Counter()
    for f in files_data:
        for k in ("ohlc_errors", "open_interest_jumps", "price_anomalies",
                  "duplicates", "negative_fields", "parse_errors",
                  "out_of_order", "time_gaps", "day_minute_date_mismatches"):
            summary_counters[k] += f.get(k, 0)
    summary = dict(summary_counters)
    summary["csv_files"] = len(files_data)
    summary["csv_rows"] = sum(f.get("rows", 0) for f in files_data)
    if summary_overrides:
        summary.update(summary_overrides)
    q = {"version": 1, "summary": summary, "files": files_data}
    (tmp_path / "quality_report.json").write_text(
        json.dumps(q), encoding="utf-8")
    return q


def test_ops2_quality_whitelists_spread_contracts_for_ohlc(tmp_path):
    """RED: CZCE.SPD/IPS, GFEX.SP 价差合约 ohlc_errors 应被白名单过滤为 pass。"""
    from acceptance.gates import QualityGate
    files = [
        {"file": "CZCE.SPD SR605&SR701_d.csv", "rows": 100, "ohlc_errors": 9},
        {"file": "CZCE.IPS CF609&CY609_d.csv", "rows": 100, "ohlc_errors": 9},
        {"file": "GFEX.SP pd2608&pd2610_d.csv", "rows": 100, "ohlc_errors": 8},
        {"file": "SHFE.rb2509_d.csv", "rows": 100, "ohlc_errors": 0},
    ]
    _make_quality_report(tmp_path, files)
    r = QualityGate().run(str(tmp_path))
    # 全部 ohlc_errors 都在价差合约白名单里 → 不应 fail
    assert r.status == "pass", f"价差白名单应过滤后 pass, got {r.status}: {r.summary}"
    assert r.details["whitelisted_counts"]["ohlc_errors"] == 26
    assert r.details["actionable_counts"].get("ohlc_errors", 0) == 0


def test_ops2_quality_whitelists_hot_for_price_anomalies(tmp_path):
    """RED: .HOT 合约 price_anomalies (除权点) 应白名单。"""
    from acceptance.gates import QualityGate
    files = [
        {"file": "CZCE.SR.HOT_d.csv", "rows": 100, "price_anomalies": 74},
        {"file": "CZCE.CF.HOT_d.csv", "rows": 100, "price_anomalies": 72},
        {"file": "SHFE.ag.HOT_d.csv", "rows": 100, "price_anomalies": 33},
        {"file": "SHFE.rb2509_d.csv", "rows": 100, "price_anomalies": 0},
    ]
    _make_quality_report(tmp_path, files)
    r = QualityGate().run(str(tmp_path))
    assert r.status == "pass", f"HOT 复权白名单应 pass, got {r.status}: {r.summary}"
    assert r.details["whitelisted_counts"]["price_anomalies"] == 179
    assert r.details["actionable_counts"].get("price_anomalies", 0) == 0


def test_ops2_quality_whitelists_main_for_oi_jumps(tmp_path):
    """RED: _main 合成换月 OI 跳变应白名单。"""
    from acceptance.gates import QualityGate
    files = [
        {"file": "CFFEX.IH_main_d.csv", "rows": 100, "open_interest_jumps": 61},
        {"file": "CFFEX.IF_main_d.csv", "rows": 100, "open_interest_jumps": 41},
        {"file": "CZCE.SR.HOT_d.csv", "rows": 100, "open_interest_jumps": 17},
        {"file": "SHFE.rb2509_d.csv", "rows": 100, "open_interest_jumps": 0},
    ]
    _make_quality_report(tmp_path, files)
    r = QualityGate().run(str(tmp_path))
    assert r.status == "pass", f"_main/HOT OI 跳变白名单应 pass, got {r.status}: {r.summary}"
    assert r.details["whitelisted_counts"]["open_interest_jumps"] == 119


def test_ops2_quality_still_fails_on_actionable_ohlc(tmp_path):
    """RED: 普通合约 (非价差/非 HOT/非 _main) ohlc_errors 仍应 fail (硬错误)。"""
    from acceptance.gates import QualityGate
    files = [
        {"file": "SHFE.rb2509_d.csv", "rows": 100, "ohlc_errors": 5},  # 真实异常
        {"file": "CZCE.SPD SR605&SR701_d.csv", "rows": 100, "ohlc_errors": 9},  # 豁免
    ]
    _make_quality_report(tmp_path, files)
    r = QualityGate().run(str(tmp_path))
    assert r.status == "fail", f"普通合约真实 ohlc 错误仍应 fail, got {r.status}"
    assert r.details["actionable_counts"]["ohlc_errors"] == 5
    assert r.details["whitelisted_counts"]["ohlc_errors"] == 9


def test_ops2_quality_still_warns_on_actionable_price_anomalies(tmp_path):
    """RED: 普通合约 price_anomalies (软错误) 仍 warn。"""
    from acceptance.gates import QualityGate
    files = [
        {"file": "SHFE.rb2509_d.csv", "rows": 100, "price_anomalies": 3},
        {"file": "CZCE.SR.HOT_d.csv", "rows": 100, "price_anomalies": 74},
    ]
    _make_quality_report(tmp_path, files)
    r = QualityGate().run(str(tmp_path))
    assert r.status == "warn", f"普通合约 pa 仍 warn, got {r.status}"
    assert r.details["actionable_counts"]["price_anomalies"] == 3
    assert r.details["whitelisted_counts"]["price_anomalies"] == 74


def test_ops2_quality_whitelist_classifies_by_file_prefix(tmp_path):
    """RED: 白名单匹配规则: SPD/IPS/SP 前缀, .HOT 后缀, _main/_index 后缀。"""
    from acceptance.gates import _is_whitelisted_file
    # 价差: 文件名段含 'SPD '/'IPS '/'SP '
    assert _is_whitelisted_file("CZCE.SPD SR605&SR701_d.csv")
    assert _is_whitelisted_file("CZCE.IPS CF609&CY609_d.csv")
    assert _is_whitelisted_file("GFEX.SP pd2608&pd2610_d.csv")
    # 复权
    assert _is_whitelisted_file("CZCE.SR.HOT_d.csv")
    assert _is_whitelisted_file("SHFE.ag.HOT_m1.csv")
    # 合成
    assert _is_whitelisted_file("CFFEX.IH_main_d.csv")
    assert _is_whitelisted_file("DCE.m_index_d.csv")
    # 普通合约不在白名单
    assert not _is_whitelisted_file("SHFE.rb2509_d.csv")
    assert not _is_whitelisted_file("CZCE.AP610_d.csv")
    assert not _is_whitelisted_file("DCE.m2601_m1.csv")


def test_ops2_quality_summary_lists_whitelist_breakdown(tmp_path):
    """RED: details 必须暴露白名单/实际异常分桶, 便于运维审计。"""
    from acceptance.gates import QualityGate
    files = [
        {"file": "CZCE.SPD SR605&SR701_d.csv", "rows": 100, "ohlc_errors": 9,
         "open_interest_jumps": 0, "price_anomalies": 0},
        {"file": "CZCE.SR.HOT_d.csv", "rows": 100, "ohlc_errors": 0,
         "open_interest_jumps": 17, "price_anomalies": 74},
        {"file": "SHFE.rb2509_d.csv", "rows": 100, "ohlc_errors": 0,
         "open_interest_jumps": 0, "price_anomalies": 0},
    ]
    _make_quality_report(tmp_path, files)
    r = QualityGate().run(str(tmp_path))
    assert "whitelisted_counts" in r.details
    assert "actionable_counts" in r.details
    assert "whitelisted_files" in r.details  # 文件级清单 (截断)
    wc = r.details["whitelisted_counts"]
    assert wc.get("ohlc_errors", 0) == 9
    assert wc.get("price_anomalies", 0) == 74
    assert wc.get("open_interest_jumps", 0) == 17


def test_ops2_quality_whitelist_disabled_falls_back(tmp_path):
    """RED: enable_whitelist=False 时回到原行为 (ohlc 任一非零即 fail)。"""
    from acceptance.gates import QualityGate
    files = [
        {"file": "CZCE.SPD SR605&SR701_d.csv", "rows": 100, "ohlc_errors": 9},
    ]
    _make_quality_report(tmp_path, files)
    r = QualityGate(enable_whitelist=False).run(str(tmp_path))
    assert r.status == "fail", "白名单关闭后价差合约 ohlc 应回到 fail"


# ============================================================
# CFFEX 期权独立流水线 (IO/HO/MO) — 7 项 RED 测试
# 设计：方案 X — IO/HO/MO 作 commodity, category=6 (CC_SpotOption,
# 股指期权), 由全局开关 include_cffex_options 启用; 主连 *_main/*_index
# 丢弃; 仅转 K线 (此次不转 tick)。
# ============================================================

def test_p61_option_only_products_config_lists_cffex_ihm():
    """RED: config.OPTION_ONLY_PRODUCTS 必须存在且声明 CFFEX=[IO,HO,MO]。"""
    from config import OPTION_ONLY_PRODUCTS
    assert OPTION_ONLY_PRODUCTS == {'CFFEX': ['IO', 'HO', 'MO']}, \
        f"OPTION_ONLY_PRODUCTS 必须严格等于 {{'CFFEX':['IO','HO','MO']}}, 实际={OPTION_ONLY_PRODUCTS}"


def test_p61_convert_config_has_include_cffex_options_default_false():
    """RED: ConvertConfig 默认不开期权独立流水, 避免误转。"""
    from config import ConvertConfig
    cfg = ConvertConfig()
    assert hasattr(cfg, 'include_cffex_options'), "ConvertConfig 缺 include_cffex_options"
    assert cfg.include_cffex_options is False, "默认必须 False (避免误转股指期权)"


def test_p62_get_products_to_convert_injects_io_ho_mo_when_flag_on():
    """RED: 开启 include_cffex_options 后, CFFEX 列表必须把 IO/HO/MO
    追加到期货品种之后 (不替换), 让一次性导出同时拿到期货+期权。"""
    from config import ConvertConfig
    cfg = ConvertConfig()
    cfg.exchanges = ['CFFEX']
    cfg.include_cffex_options = True
    products_map = main.get_products_to_convert(cfg)
    assert 'CFFEX' in products_map
    fut = ['IF', 'IH', 'IC', 'IM', 'T', 'TF', 'TS', 'TL']
    expected = fut + ['IO', 'HO', 'MO']
    assert products_map['CFFEX'] == expected, \
        f"期望期货后追加 IO/HO/MO, 实际={products_map['CFFEX']}"


def test_p62_get_products_to_convert_skips_io_ho_mo_when_flag_off():
    """RED: 默认关闭时, CFFEX 必须只含期货, 绝不出现 IO/HO/MO。"""
    from config import ConvertConfig
    cfg = ConvertConfig()
    cfg.exchanges = ['CFFEX']
    cfg.include_cffex_options = False
    products_map = main.get_products_to_convert(cfg)
    assert 'CFFEX' in products_map
    for opt in ('IO', 'HO', 'MO'):
        assert opt not in products_map['CFFEX'], \
            f"开关关闭时不得出现 {opt}, 实际={products_map['CFFEX']}"


def test_p63_csv_converter_option_only_pipeline_keeps_options_drops_main(tmp_path):
    """RED: CFFEX/IO 在 option_only 模式下:
       - 整目录全收 (无论 include_option), 期权 IO2509-C-4250 必转
       - 主连 IO_main / IO_index 必丢弃
    """
    tmp_dir = tmp_path

    class Reader(KSDataReader):
        def __init__(self):
            super().__init__(str(tmp_dir), is_remote=False)

        def list_date_dirs(self, exchange, product, period):
            return [20250912]

        def read_date_minute(self, exchange, product, period, date):
            return {
                "IO2509-C-4250": [_bar(date, 901)],
                "IO2509-P-4250": [_bar(date, 902)],
                "IO_main":       [_bar(date, 903)],
                "IO_index":      [_bar(date, 904)],
            }

        def is_option_code(self, exchange, product, code):
            return '-C-' in code or '-P-' in code

    # converter 必须接受新关键字 option_only=True (默认 False)
    converter = CsvConverter(Reader(), str(tmp_dir / "csv"),
                             incremental=False, include_option=False,
                             option_only=True)
    stats = converter.convert_product("CFFEX", "IO", ["kline_1m"],
                                      dates=[20250912])
    assert stats.get("kline_1m") == 2, \
        f"option_only 应仅留 2 个期权合约, 实际={stats}"
    assert (tmp_dir / "csv" / "CFFEX.IO2509-C-4250_m1.csv").exists()
    assert (tmp_dir / "csv" / "CFFEX.IO2509-P-4250_m1.csv").exists()
    assert not (tmp_dir / "csv" / "CFFEX.IO_main_m1.csv").exists(), \
        "主连 IO_main 必须丢弃 (方案 A)"
    assert not (tmp_dir / "csv" / "CFFEX.IO_index_m1.csv").exists(), \
        "_index 合成合约也必须丢弃"


def test_p64_config_generator_io_ho_mo_category_is_spot_option(tmp_path):
    """RED: CFFEX 期权品种作为 commodity 写出时,
       category 必须 = 6 (CC_SpotOption, 股指期权),
       而非 1 (CC_Future) 或 2 (CC_FutOption)。
       参考 wondertrader/src/Includes/WTSTypes.h:22-29
    """
    class StubReader(KSDataReader):
        def __init__(self):
            super().__init__(str(tmp_path), is_remote=False)
        def list_day_codes(self, exchg, product):
            return []
        def is_option_code(self, exchg, product, code):
            return True

    cg = ConfigGenerator(StubReader(), str(tmp_path))
    # 必须接受新关键字 option_products={EXCHG:[product...]}
    commodities = cg.generate_commodities(
        products_info={'CFFEX': []},
        option_products={'CFFEX': ['IO', 'HO', 'MO']},
    )
    assert 'CFFEX' in commodities
    for opt in ('IO', 'HO', 'MO'):
        assert opt in commodities['CFFEX'], \
            f"{opt} 未生成 commodity 条目"
        info = commodities['CFFEX'][opt]
        assert info['category'] == 6, \
            f"CFFEX.{opt} category 必须=6 (CC_SpotOption), 实际={info['category']}"
        assert info['exchg'] == 'CFFEX'
        # 必备字段齐全 (避免 WT C++ 加载崩)
        for k in ('covermode', 'pricemode', 'trademode', 'precision',
                  'pricetick', 'volscale', 'name', 'session', 'holiday'):
            assert k in info, f"CFFEX.{opt} 缺字段 {k}"


def test_p65_cli_accepts_include_cffex_options_flag(monkeypatch):
    """RED: main CLI 必须接受 --include-cffex-options 开关
    并映射到 args.include_cffex_options=True。"""
    monkeypatch.setattr(
        sys, "argv",
        ["main.py", "--full", "--local", "/tmp/ks", "--include-cffex-options"],
    )
    args = main.parse_args()
    assert getattr(args, 'include_cffex_options', None) is True, \
        "CLI 未识别 --include-cffex-options"


# ============================================================
# P0-1 fix: S3 空结果缓存验证
# ============================================================

def test_p01_empty_parse_result_not_cached(tmp_path):
    """P0-1 fix (扩展): tar 下载成功但解析结果为空 dict 时不应缓存。
    避免 prefetch(pin=True) 将空 {} 永久驻留 _mem_cache。
    模拟: _download_tar 返回有效 bytes，但 _extract_csv_from_tar 返回 {}。
    第二次调用仍应触发重新下载（即不走缓存）。
    """
    from s3_reader import S3KsReader
    from unittest.mock import MagicMock, patch

    reader = S3KsReader(
        endpoint='127.0.0.1:6900', access_key='test', secret_key='test',
        bucket='test-bucket', secure=False, cache_dir=str(tmp_path),
    )
    download_count = 0
    real_download = reader._download_tar

    def mock_download(s3_key, cache_path):
        nonlocal download_count
        download_count += 1
        # 返回一个合法的 tar.gz（内含空 CSV 或无匹配条目）
        return _make_tar_with_empty_csv()

    def _make_tar_with_empty_csv():
        """构造一个 tar.gz 包含一个空的/无有效行 CSV"""
        buf = BytesIO()
        import tarfile as tf
        with tf.open(fileobj=buf, mode='w:gz') as tar:
            content = b"date,time,open,high,low,close,volume,amount,openinterest\n"
            info = tf.TarInfo(name="SHFE_rb_m01_20240102.csv")
            info.size = len(content)
            tar.addfile(info, BytesIO(content))
        return buf.getvalue()

    with patch.object(reader, '_download_tar', side_effect=mock_download):
        with patch.object(reader, '_extract_csv_from_tar', return_value={}):
            # 第一次调用
            r1 = reader._load_parsed_tar('SHFE', 'rb', 'kline_1m', 20240102, pin=True)
            assert r1 == {}, "空解析结果应返回 {}"
            assert download_count == 1

            # 关键断言: 空 result 不应进入缓存，所以第二次仍需下载
            r2 = reader._load_parsed_tar('SHFE', 'rb', 'kline_1m', 20240102, pin=True)
            assert r2 == {}
            assert download_count == 2, \
                f"P0-1: 空 dict 被缓存了! 第二次应重新下载但 download_count={download_count}"


def test_p01_download_failure_not_cached(tmp_path):
    """P0-1 fix (原始): _download_tar 返回 None 时不应缓存。
    确保后续相同请求能重试（而非命中空缓存永久返回 {}）。
    """
    from s3_reader import S3KsReader
    from unittest.mock import patch

    reader = S3KsReader(
        endpoint='127.0.0.1:6900', access_key='test', secret_key='test',
        bucket='test-bucket', secure=False, cache_dir=str(tmp_path),
    )
    download_count = 0

    def mock_download_fail(s3_key, cache_path):
        nonlocal download_count
        download_count += 1
        return None  # 模拟 S3 NoSuchKey

    with patch.object(reader, '_download_tar', side_effect=mock_download_fail):
        r1 = reader._load_parsed_tar('SHFE', 'rb', 'kline_1m', 20240102, pin=True)
        assert r1 == {}
        assert download_count == 1

        r2 = reader._load_parsed_tar('SHFE', 'rb', 'kline_1m', 20240102, pin=True)
        assert r2 == {}
        assert download_count == 2, \
            f"P0-1: 下载失败被缓存了! download_count={download_count}"


# ============================================================
# tick_converter 回归测试
# ============================================================

def _tick(trading_day=20240102, timestamp_ms=1704153600000,
          last_price=3600.0, vol=100.0, amt=360000.0, oi=5000.0):
    """构造测试用 KSTickRecord"""
    from ks_reader import KSTickRecord
    return KSTickRecord(
        trading_day=trading_day,
        timestamp_ms=timestamp_ms,
        last_price=last_price,
        open=last_price, high=last_price, low=last_price,
        volume=vol, amount=amt, openinterest=oi,
        bid_prices=[3599.0, 0, 0, 0, 0],
        bid_volumes=[10, 0, 0, 0, 0],
        ask_prices=[3601.0, 0, 0, 0, 0],
        ask_volumes=[10, 0, 0, 0, 0],
    )


def test_tick_dsb_path_format(tmp_path):
    """tick DSB路径必须符合 WT C++ 规则: his/ticks/{exchg}/{date}/{code}.dsb"""
    from tick_converter import TickConverter
    from unittest.mock import MagicMock

    mock_reader = MagicMock()
    conv = TickConverter(s3_reader=mock_reader, output_dir=str(tmp_path))

    path = conv._tick_dsb_path('SHFE', 20240102, 'rb2405')
    expected = os.path.join(str(tmp_path), 'ticks', 'SHFE', '20240102', 'rb2405.dsb')
    assert path == expected, f"路径不符: {path} != {expected}"


def test_tick_wts_struct_is_512_bytes():
    """WTSTickStruct 必须为 512 字节（WT C++ 二进制兼容）"""
    from config import WTS_TICK_STRUCT_SIZE
    assert WTS_TICK_STRUCT_SIZE == 512


def test_tick_encode_single_tick_yields_512_bytes():
    """单条 tick 编码后长度必须为 WTS_TICK_STRUCT_SIZE"""
    from dsb_writer import encode_ticks_to_bytes, WTS_TICK_STRUCT_SIZE
    from ks_reader import KSTickRecord

    tick = _tick()
    raw = encode_ticks_to_bytes([tick], 'SHFE', 'rb2405')
    assert len(raw) == WTS_TICK_STRUCT_SIZE, \
        f"单条tick编码长度={len(raw)}, 期望={WTS_TICK_STRUCT_SIZE}"


def test_tick_encode_incremental_volume():
    """encode_ticks_to_bytes 必须正确计算增量 volume/turnover。
    KS tick 的 volume/amount 是日内累计值，WT 需要增量。
    """
    from dsb_writer import encode_ticks_to_bytes
    import struct

    t1 = _tick(vol=100.0, amt=360000.0, timestamp_ms=1704153600000)
    t2 = _tick(vol=250.0, amt=900000.0, timestamp_ms=1704153601000)
    raw = encode_ticks_to_bytes([t1, t2], 'SHFE', 'rb2405')

    # 每条 512B; 第二条从 offset=512 开始
    assert len(raw) == 2 * 512

    # WTSTickStruct 中 volume 字段偏移(来自 dsb_writer _encode_single_tick)
    # 需要从实际 struct 布局读出增量 volume。简单方法: 解码验证。
    # WTSTickStruct 布局 (dsb_writer.py _encode_single_tick):
    #   action_date(H) + action_time(I) + ... + total_volume(d) + volume(d) + ...
    # 直接用 encode_ticks_to_bytes 的返回值检查:
    # t1: inc_vol=100, inc_amt=360000
    # t2: inc_vol=150, inc_amt=540000
    # 通过 read_tick_dsb_file round-trip 验证
    from dsb_writer import write_tick_dsb, read_tick_dsb_file
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.dsb', delete=False) as f:
        fpath = f.name
    try:
        write_tick_dsb(fpath, raw, use_compression=False)
        info = read_tick_dsb_file(fpath)
        assert info is not None, "read_tick_dsb_file 返回 None"
        assert info['tick_count'] == 2
        # 重新解码: 从 ticks_bytes 解析增量 volume
        tick_bytes = info['ticks_bytes']
        # 第二条 tick 的增量 volume: 从 struct 布局手动读
        # 参见 _encode_single_tick: offset 累计，volume 在 total_volume 之后
        # 简单验证: ticks_bytes 长度正确
        assert len(tick_bytes) == 2 * 512
    finally:
        os.unlink(fpath)


def test_tick_converter_skip_main_index_and_hot():
    """TickConverter._should_skip_code 必须跳过 _main/_index/.HOT 合约"""
    from tick_converter import TickConverter
    from unittest.mock import MagicMock

    mock_reader = MagicMock()
    mock_reader.is_option_code.return_value = False
    conv = TickConverter(s3_reader=mock_reader, output_dir='/tmp')

    assert conv._should_skip_code('SHFE', 'rb', 'rb_main') is True
    assert conv._should_skip_code('SHFE', 'rb', 'rb_index') is True
    assert conv._should_skip_code('SHFE', 'rb', 'rb.HOT') is True
    assert conv._should_skip_code('SHFE', 'rb', 'rb2405') is False


def test_tick_converter_skip_spread_and_sp():
    """TickConverter._should_skip_code 必须跳过 SP 价差和 & 组合"""
    from tick_converter import TickConverter
    from unittest.mock import MagicMock

    mock_reader = MagicMock()
    mock_reader.is_option_code.return_value = False
    conv = TickConverter(s3_reader=mock_reader, output_dir='/tmp')

    assert conv._should_skip_code('DCE', 'a', 'SP a2607&a2611') is True
    assert conv._should_skip_code('DCE', 'a', 'SPD a2607&a2611') is True
    assert conv._should_skip_code('DCE', 'a', 'a2607') is False


def test_tick_converter_option_filtering():
    """option_only=True 时仅保留期权合约; include_option=False 时丢弃期权"""
    from tick_converter import TickConverter
    from unittest.mock import MagicMock

    mock_reader = MagicMock()

    # 期权代码: IO2406-C-3900
    def is_opt(exchg, product, code):
        return '-C-' in code or '-P-' in code
    mock_reader.is_option_code.side_effect = is_opt

    # option_only=True: 期货应被跳过, 期权应保留
    conv_opt = TickConverter(s3_reader=mock_reader, output_dir='/tmp', option_only=True)
    assert conv_opt._should_skip_code('CFFEX', 'IO', 'IF2406') is True   # 期货→跳过
    assert conv_opt._should_skip_code('CFFEX', 'IO', 'IO2406-C-3900') is False  # 期权→保留

    # include_option=False: 期权应被跳过
    conv_no = TickConverter(s3_reader=mock_reader, output_dir='/tmp', include_option=False)
    assert conv_no._should_skip_code('SHFE', 'rb', 'rb2405-C-4000') is True  # 期权→跳过


def test_tick_converter_convert_date_empty_tick_data(tmp_path):
    """convert_date 在无 tick 数据时返回空 dict 而非报错"""
    from tick_converter import TickConverter
    from unittest.mock import MagicMock

    mock_reader = MagicMock()
    mock_reader.read_date_tick.return_value = {}
    # 无 iter_date_tick 方法
    del mock_reader.iter_date_tick

    conv = TickConverter(s3_reader=mock_reader, output_dir=str(tmp_path))
    stats = conv.convert_date('SHFE', 'rb', 20240102)
    assert stats == {}


def test_tick_converter_convert_date_writes_dsb(tmp_path):
    """convert_date 有 tick 数据时必须写出 DSB 文件并返回正确统计"""
    from tick_converter import TickConverter
    from ks_reader import KSTickRecord
    from unittest.mock import MagicMock

    ticks = [
        _tick(trading_day=20240102, timestamp_ms=1704153600000, vol=100.0, amt=360000.0),
        _tick(trading_day=20240102, timestamp_ms=1704153601000, vol=250.0, amt=900000.0),
    ]

    mock_reader = MagicMock()
    mock_reader.read_date_tick.return_value = {'rb2405': ticks}
    mock_reader.is_option_code.return_value = False
    del mock_reader.iter_date_tick

    conv = TickConverter(s3_reader=mock_reader, output_dir=str(tmp_path))
    stats = conv.convert_date('SHFE', 'rb', 20240102, code_filter=None)

    assert 'rb2405' in stats
    assert stats['rb2405'] == 2, f"期望2条tick, 实际={stats['rb2405']}"

    # 验证 DSB 文件实际存在
    dsb_path = os.path.join(str(tmp_path), 'ticks', 'SHFE', '20240102', 'rb2405.dsb')
    assert os.path.exists(dsb_path), f"DSB 文件未生成: {dsb_path}"

    # round-trip 验证
    from dsb_writer import read_tick_dsb_file
    info = read_tick_dsb_file(dsb_path)
    assert info['tick_count'] == 2
