"""ks2wtpy 投研服务化包：只读派生视图加载器。

公开入口：
    from research.adapter import open_research_view, ResearchView
或：
    from research import open_research_view, ResearchView
"""

from research.adapter import ResearchView, open_research_view

__all__ = ["ResearchView", "open_research_view"]
