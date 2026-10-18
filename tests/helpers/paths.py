"""仓库内绝对路径。

测试文件下沉到子目录后 `Path(__file__).parent.parent` 不再指向仓库根，
统一从这里取，避免每处各自推算层级。
"""

from __future__ import annotations

from pathlib import Path

# tests/helpers/paths.py -> parents[0]=helpers, [1]=tests, [2]=仓库根
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
# 真实上游样本（SSE / JSON），契约测试用
PROVIDER_FIXTURES = SRC / "provider" / "fixtures"

__all__ = [
    "PROVIDER_FIXTURES",
    "ROOT",
    "SCRIPTS",
    "SRC",
    "load_script",
    "provider_fixture",
    "script_path",
    "tmpdir",
]


def tmpdir() -> Path:
    """新建一个临时目录并返回（`tempfile.mkdtemp`，调用方自行清理）。"""
    import tempfile

    return Path(tempfile.mkdtemp())


def script_path(name: str) -> Path:
    """`scripts/` 下脚本的绝对路径，`name` 带或不带 `.py` 均可。"""
    filename = name if name.endswith(".py") else f"{name}.py"
    return SCRIPTS / filename


def provider_fixture(provider: str, name: str) -> str:
    """读取某渠道的真实样本文件内容。"""
    return (PROVIDER_FIXTURES / provider / name).read_text(encoding="utf-8")


def load_script(name: str):
    """按文件路径加载 `scripts/` 下的脚本模块（脚本不是包，不能 import）。"""
    import importlib.util

    path = script_path(name)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module
