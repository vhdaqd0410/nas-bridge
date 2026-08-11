"""sync 子包（预留重构入口）

目前 SyncEngine 仍全部定义在同目录的 sync_engine.py 中，
这里保留本文件方便未来拆分为 scanner.py / deliver.py / status.py。
"""
from sync_engine import SyncEngine

__all__ = ["SyncEngine"]
