#!/usr/bin/env python3
"""执行层订单事件旁路侧车 (2026-10-21 订单号归因实验)

订阅交易引擎消息总线上的 TRD_ORDER / TRD_TRADE 主题 (C++ EventNotifier
广播, 全量订单状态+逐笔成交, 含 localid 归因键), 落盘 JSONL 旁路日志,
交易进程零侵入 (与 BarJournal/LiveTruth 同设计哲学)。

用途:
  - 订单状态机留痕: 在途/部成/全成/已撤 全生命周期可回放;
  - 订单级成交归因: localid 级滑点/部成率/拒单率统计 (ExecAttributor);
  - 补齐 CTA 层结构性盲区: BaseCtaStrategy 无订单/成交回调, 现仅靠
    trades.csv 净持仓增量+时序分配归因 (无订单粒度)。

用法 (生产/模拟同构, 与监控同总线):
  python3 scripts/exec_journal.py                          # 前台, Ctrl+C 出统计
  python3 scripts/exec_journal.py --mq ipc:///tmp/wt_turtle.ipc \
      --out ../generated/exec_journal

产出: <out>/exec_YYYYMMDD.jsonl  (每行 {"type": order|trade, ...事件})
依赖: 真实 wtpy 运行时 (WtMsgQue); 纯逻辑见 core/ExecJournal.py (已单测)。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.ExecJournal import OrderTracker     # noqa: E402


def _journal_path(out_dir: str, ts: float) -> str:
    return os.path.join(out_dir,
                        time.strftime("exec_%Y%m%d.jsonl",
                                      time.localtime(ts)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mq", default="ipc:///tmp/wt_turtle.ipc",
                    help="须与策略 config.yaml notifier.url 完全一致")
    ap.add_argument("--out", default=os.path.abspath(os.path.join(
        ROOT, "..", "generated", "exec_journal")))
    ap.add_argument("--quiet-every", type=int, default=100,
                    help="每 N 条事件打印一次进度 (0=静默)")
    args = ap.parse_args()

    # 真实 wtpy 运行时依赖, 放 main 内保证模块可导入 (CI 纯逻辑模式)
    from wtpy.monitor.EventReceiver import (EventReceiver, EventSink,
                                            TOPIC_RT_ORDER, TOPIC_RT_TRADE)

    os.makedirs(args.out, exist_ok=True)
    tracker = OrderTracker()
    state = {"n": 0}

    class Sink(EventSink):
        def on_order(self, chnl, ord_info):
            self._dump("order", chnl, ord_info)

        def on_trade(self, chnl, trd_info):
            self._dump("trade", chnl, trd_info)

        def _dump(self, typ, chnl, info):
            ts = time.time()
            info = dict(info)
            info.pop("trader", None)
            # 订单/成交状态机在写日志前先消费 (去重等语义保持一致)
            if typ == "order":
                tracker.on_order({"trader": chnl, **info})
            else:
                tracker.on_trade({"trader": chnl, **info})
            line = {"type": typ, "chnl": chnl, "ts": ts, **info}
            with open(_journal_path(args.out, ts), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            state["n"] += 1
            if args.quiet_every and state["n"] % args.quiet_every == 0:
                st = tracker.stats()
                print(f"[execj] {state['n']} events, orders={st['orders']} "
                      f"{st['by_state']}")

    print(f"[execj] subscribing {args.mq} "
          f"topics=[{TOPIC_RT_ORDER},{TOPIC_RT_TRADE}] -> {args.out}")
    receiver = EventReceiver(url=args.mq, sink=Sink(),
                             topics=[TOPIC_RT_ORDER, TOPIC_RT_TRADE])
    try:
        receiver.run()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        receiver.release()
        print(f"[execj] total {state['n']} events; "
              f"final {json.dumps(tracker.stats(), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
