from wtpy import BaseDataReporter
import json
import os


class ArbDataReporter(BaseDataReporter):
    """
    套利数据报告器 - 对齐框架BaseDataReporter

    实现三个核心方法:
    1. rpt_portfolio_rt_data_impl - 组合级实时数据报告
    2. rpt_strategy_rt_data_impl - 策略级实时数据报告
    3. rpt_init_data_impl - 初始化数据报告

    数据来源: 框架自动从 ./generated/ 目录读取并传入
    """

    def __init__(self, id: str, sub_strategy_names: list = None,
                 output_dir: str = "generated/arb_report"):
        BaseDataReporter.__init__(self, id)
        # 修复: BaseDataReporter.__tasks__/__thrd_task__/__stopped__只在init()中创建，
        # 但框架可能在init()之前调用report_init_data/report_rt_data，导致AttributeError
        # 注意: __dunder__形式(双下划线开头+结尾)不做name mangling，直接用原始名称
        if not hasattr(self, '__tasks__'):
            self.__tasks__ = list()
        if not hasattr(self, '__thrd_task__'):
            self.__thrd_task__ = None
        if not hasattr(self, '__stopped__'):
            self.__stopped__ = False
        # 修复: stra_names在BaseDataReporter.init()中从marker.json读取，
        # 但init()可能未被调用，report线程直接访问self.stra_names会AttributeError
        if not hasattr(self, 'stra_names'):
            self.stra_names = sub_strategy_names or []
        self._sub_names = sub_strategy_names or []
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def rpt_portfolio_rt_data_impl(self, rtData):
        """
        组合级实时数据报告

        rtData由框架传入，包含:
        - pid: 组合ID
        - dynamic_balance: 动态权益
        - total_pnl: 总盈亏
        - positions: 持仓信息
        等字段
        """
        if rtData is None:
            return
        filepath = os.path.join(self._output_dir, "portfolio_rt.json")
        try:
            with open(filepath, "w") as f:
                json.dump(rtData, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"ArbDataReporter: failed to write portfolio rt data: {e}")

    def rpt_strategy_rt_data_impl(self, rtData):
        """
        策略级实时数据报告

        rtData由框架传入，包含:
        - pid: 组合ID
        - sid: 策略名
        - positions: 策略持仓
        - orders: 委托信息
        - trades: 成交信息
        等字段
        """
        if rtData is None:
            return
        sid = rtData.get("sid", "unknown")
        filepath = os.path.join(self._output_dir, f"strategy_{sid}_rt.json")
        try:
            with open(filepath, "w") as f:
                json.dump(rtData, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"ArbDataReporter: failed to write strategy {sid} rt data: {e}")

    def rpt_init_data_impl(self, initData):
        """
        初始化数据报告

        initData由框架传入，包含:
        - pid: 组合ID
        - strategies: 策略名列表
        """
        if initData is None:
            return
        filepath = os.path.join(self._output_dir, "init_data.json")
        try:
            with open(filepath, "w") as f:
                json.dump(initData, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"ArbDataReporter: failed to write init data: {e}")
