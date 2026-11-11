# -*- coding: utf-8 -*-
"""python -m crypto → 启动加密货币系统 Web（:9091）。"""
import sys

from crypto.web.app import CFG  # noqa: F401  (import 即完成配置/依赖检查)


def main() -> int:
    import uvicorn
    from crypto.web.app import app
    uvicorn.run(app, host=CFG["web"]["host"], port=int(CFG["web"]["port"]),
                log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
