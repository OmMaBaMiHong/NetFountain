"""账号库：接口调用方凭据 → 定向二级池（SQLite，标准库实现）。

- 表 ``accounts(username 主键, password_hash, assigned_site, created_at)``；
- 密码不存明文：每账号随机盐，存 ``salt$sha256(salt+password)``；
- 每次操作独立开连接（低频管理操作 + 租赁期校验，无需常驻句柄）；
- 库文件路径由 ``auth.db_path`` 配置（空 = ``<proxy>/data/accounts.db``），目录自动创建。
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "accounts.db"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    assigned_site TEXT NOT NULL,
    created_at    TEXT NOT NULL
)
"""


class DuplicateAccountError(Exception):
    """用户名已存在。"""


class AccountNotFoundError(Exception):
    """用户名不存在。"""


@dataclass(frozen=True)
class Account:
    """一条账号记录（不含密码材料）。"""

    username: str
    assigned_site: str
    created_at: str


def _digest(salt: str, password: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


class AccountStore:
    """账号增删查与凭据校验。"""

    def __init__(self, db_path: str | None = None) -> None:
        self._db = db_path or _DEFAULT_DB
        parent = os.path.dirname(os.path.abspath(self._db))
        os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute(_SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(self, username: str, password: str, assigned_site: str) -> Account:
        """注册账号；用户名已存在抛 ``DuplicateAccountError``。"""
        salt = secrets.token_hex(8)
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO accounts VALUES (?, ?, ?, ?)",
                    (username, f"{salt}${_digest(salt, password)}",
                     assigned_site, created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateAccountError(username) from exc
        return Account(username=username, assigned_site=assigned_site,
                       created_at=created_at)

    def delete(self, username: str) -> None:
        """删除账号；不存在抛 ``AccountNotFoundError``。"""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
            if cur.rowcount == 0:
                raise AccountNotFoundError(username)

    def list(self) -> list[Account]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT username, assigned_site, created_at "
                "FROM accounts ORDER BY created_at, username"
            ).fetchall()
        return [Account(r["username"], r["assigned_site"], r["created_at"])
                for r in rows]

    def verify(self, username: str, password: str) -> Account | None:
        """凭据正确返回账号；用户不存在或密码错误返回 ``None``。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT password_hash, assigned_site, created_at "
                "FROM accounts WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        salt, _, digest = row["password_hash"].partition("$")
        if secrets.compare_digest(digest, _digest(salt, password)):
            return Account(username=username, assigned_site=row["assigned_site"],
                           created_at=row["created_at"])
        return None
