"""海龟 datakit 行情进程 — 双进程拓扑数据侧 (P0)

职责: ParserCTP 直连行情 → WtDataWriter 合成/落盘 K线
      (rt mmap 当日块 + 会话轮转转存 his DSB) → UDP 广播
      (快照查询 bport=3997 / 广播 port=9001, 见 dtcfg.yaml)。

交易进程 run.py 经 ParserUDP 订阅本进程广播; 两进程共享同一 storage
目录 (datakit 写 rt/his, 交易引擎 WtDataReader 拼装读取)。

WatchDog 托管, 与 run.py 同为 guard 崩溃自动重启; 启动顺序无强依赖
(ParserUDP 在 datakit 未就绪时静默等待, 无 tick 即无 bar 事件)。
"""
import time

from wtpy import WtDtEngine

if __name__ == "__main__":
    engine = WtDtEngine()
    engine.initialize("dtcfg.yaml", "logcfgdt.yaml")
    engine.run(True)

    print("[turtle-dt] datakit running, press ctrl-c to exit")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
