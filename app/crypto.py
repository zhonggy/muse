"""卡片敏感字段的对称加密（Fernet）。

密钥来源优先级：
1. 环境变量 MUSE_SECRET_KEY（推荐，容器化部署用）
2. data/secret.key（首次启动自动生成，权限 0600）
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA_DIR


class CardCipher:
    def __init__(self, key_path: Path | None = None) -> None:
        self.key_path = key_path or (DATA_DIR / "secret.key")
        self._fernet = Fernet(self._load_key())

    def _load_key(self) -> bytes:
        env = os.getenv("MUSE_SECRET_KEY", "").strip()
        if env:
            # 允许用户直接给任意字符串，派生为合法 Fernet key
            try:
                Fernet(env.encode())
                return env.encode()
            except Exception:
                return base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest())

        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return key

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise RuntimeError(
                "卡片解密失败：MUSE_SECRET_KEY 或 data/secret.key 与写入时不一致"
            ) from None


_cipher: CardCipher | None = None


def cipher() -> CardCipher:
    global _cipher
    if _cipher is None:
        _cipher = CardCipher()
    return _cipher


def mask_pan(pan: str) -> str:
    digits = "".join(ch for ch in pan if ch.isdigit())
    if len(digits) < 4:
        return "****"
    return "*" * (len(digits) - 4) + digits[-4:]
