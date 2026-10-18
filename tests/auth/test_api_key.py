"""API Key 生成、摘要与预览。"""

from __future__ import annotations

from src.auth import api_key


def test_api_key_generate_digest_preview():
    key = api_key.generate_api_key()
    assert key.startswith("sk-")
    assert api_key.digest_api_key(key) == api_key.digest_api_key(key)
    assert api_key.preview_api_key(key).startswith("sk-…")
