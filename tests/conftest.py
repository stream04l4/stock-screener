# -*- coding: utf-8 -*-
"""pytest 配置：把 tests/ 目录加入 sys.path（便于导入 conftest_helpers）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
