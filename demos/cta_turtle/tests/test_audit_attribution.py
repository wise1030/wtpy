"""audit_attribution 解析逻辑回归 (D5, 2026-11-12)。"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from audit_attribution import (audit_trades_csv, parse_strategy_log,  # noqa: E402
                               PRODUCTS)


def test_parse_log_evidence_and_alerts(tmp_path):
    p = tmp_path / "turtle_X_2026-11-12.log"
    p.write_text("\n".join([
        "[info] turtle_X fill re-anchor layer2 5946.00 -> 5937.85 "
        "(trades), stop/add levels shifted",
        "[info] turtle_X fill re-anchor last (orders) — re-anchored",
        "[warning] turtle_X exit unconfirmed 130s: close fills 3/10",
        "[info] drop stale pending DEC_ADD @ bar_eval 20261112",
        "[info] ordinary line without anything",
    ]), encoding="utf-8")
    rec = parse_strategy_log(str(p))
    assert rec["evidence"] == {"trades": 1, "orders": 1}
    assert rec["alerts"]["exit_unconfirmed"] == 1
    assert rec["alerts"]["stale_pending"] == 1
    assert rec["alerts"]["signal_window"] == 0


def test_audit_trades_csv_filters_shared_account(tmp_path):
    p = tmp_path / "trades.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["localid", "date", "time", "code", "action",
                    "volume", "price", "tradeid", "orderid"])
        # 本策略 rb (有归因) / 本策略 TA (localid=0) / futu_mm ao (过滤)
        w.writerow(["101", "20261112", "0", "SHFE.rb.2601", "OP", "3",
                    "3100", "1", "9"])
        w.writerow(["0", "20261112", "0", "CZCE.TA701", "CL", "2",
                    "5200", "2", "8"])
        w.writerow(["7", "20261112", "0", "SSE.ao.2612", "OP", "5",
                    "2667", "3", "7"])
    t = audit_trades_csv(str(p))
    assert t["rows"] == 3 and t["ours_rows"] == 2      # ao 被白名单过滤
    assert t["ours_volume"] == 5.0
    assert t["attributed_volume"] == 3.0               # TA 行 localid=0
    assert 0.4 < t["attribute_rate"] < 0.8
    assert t["close_volume"] == 2.0 and t["open_volume"] == 3.0


def test_audit_trades_csv_missing_file():
    t = audit_trades_csv("/nonexistent/trades.csv")
    assert "error" in t
