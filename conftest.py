"""让 pytest 无论从哪个目录启动都能 import 到 src。

pytest 默认只把 rootdir 加进 sys.path 的情况很微妙：
`python -m pytest` 会加当前目录，光敲 `pytest` 未必。
这里显式补一次，省掉「明明有 src 却说 ModuleNotFoundError」的坑。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))