"""Pytest 配置 — 自动检测 async 测试。"""

import pytest

pytest_plugins = ["pytest_asyncio"]


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
