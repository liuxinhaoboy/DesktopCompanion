# -*- coding: utf-8 -*-
"""SecretsManager —— API 密钥的加密存储（Windows DPAPI）"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SECRET_PREFIX = "secret:"


class SecretsManager:
    def __init__(self, secrets_path: Path):
        self.path = Path(secrets_path)
        self._cache: dict[str, str] = {}
        self._dpapi_available = self._check_dpapi()

    @staticmethod
    def _check_dpapi() -> bool:
        try:
            import win32crypt
            return True
        except ImportError:
            return False

    @property
    def available(self) -> bool:
        return self._dpapi_available

    def _encrypt(self, plaintext: str) -> str:
        import win32crypt
        blob = win32crypt.CryptProtectData(
            plaintext.encode("utf-8"), "DesktopCompanion API Key",
            None, None, None, 0)
        return base64.b64encode(blob).decode("ascii")

    def _decrypt(self, b64_ciphertext: str) -> str:
        import win32crypt
        blob = base64.b64decode(b64_ciphertext)
        _, plaintext = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return plaintext.decode("utf-8")

    def _load_secrets(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_secrets(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def store(self, name: str, plaintext: str) -> str:
        if not self._dpapi_available or not plaintext:
            return plaintext
        try:
            secrets = self._load_secrets()
            secrets[name] = self._encrypt(plaintext)
            self._save_secrets(secrets)
            self._cache[name] = plaintext
            return f"{SECRET_PREFIX}{name}"
        except Exception as e:
            print(f"[Secrets] 加密失败，回退明文存储: {e}")
            return plaintext

    def resolve(self, key_value: str) -> str:
        if not key_value or not key_value.startswith(SECRET_PREFIX):
            return key_value
        name = key_value[len(SECRET_PREFIX):]
        if name in self._cache:
            return self._cache[name]
        if not self._dpapi_available:
            print(f"[Secrets] DPAPI 不可用，无法解析密钥引用 '{name}'")
            return ""
        secrets = self._load_secrets()
        if name not in secrets:
            print(f"[Secrets] 密钥引用 '{name}' 在 secrets.bin 中不存在")
            return ""
        try:
            plaintext = self._decrypt(secrets[name])
            self._cache[name] = plaintext
            return plaintext
        except Exception as e:
            print(f"[Secrets] 解密失败（可能换了Windows用户）: {e}")
            return ""

    def migrate_config_key(self, store) -> bool:
        cfg = store.cfg
        current_key = cfg.get("llm", {}).get("api_key", "")
        if not current_key:
            return False
        if current_key.startswith(SECRET_PREFIX):
            return True
        if not self._dpapi_available:
            print("[Secrets] pywin32 不可用，跳过密钥加密（明文继续可用）")
            return False
        ref = self.store("llm_api_key", current_key)
        if ref.startswith(SECRET_PREFIX):
            store.update_section("llm", {"api_key": ref})
            store.save()
            print("[Secrets] API key 已加密迁移到 data/secrets.bin，config.json 只留引用")
            return True
        return False


if __name__ == "__main__":
    import sys
    root = Path(__file__).resolve().parent.parent
    sm = SecretsManager(root / "data" / "secrets.bin")
    print(f"DPAPI 可用: {sm.available}")
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        test = "sk-test-1234567890"
        ref = sm.store("test_key", test)
        print(f"加密引用: {ref}")
        resolved = sm.resolve(ref)
        print(f"解密结果: {resolved}")
        print(f"往返一致: {resolved == test}")
