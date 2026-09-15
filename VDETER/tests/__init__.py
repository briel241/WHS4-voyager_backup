"""VDE-TER 시험 package.

`src` layout의 `vdeter` package를 editable install 없이도 시험할 수 있도록 이 package를
import할 때 `src` 경로를 `sys.path`에 넣는다.
"""

from __future__ import annotations

import os
import sys

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.join(VDETER_ROOT, "src")
PROJECT_ROOT = os.path.dirname(os.path.dirname(VDETER_ROOT))
DOCS_CORE = os.path.join(PROJECT_ROOT, "docs", "core")

if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)
