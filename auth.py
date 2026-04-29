"""Password manager for poker-ai. Run `python auth.py` for interactive CLI."""
from __future__ import annotations

import json
import sys
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

PASSWORDS_FILE = Path(__file__).parent / "passwords.json"
_DEFAULT_PRICING = {"input": 2.5, "cached": 0.25, "output": 15.0}


class PasswordStore:
    def __init__(self, path: Path = PASSWORDS_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.path)

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _reset_if_new_day(entry: dict) -> dict:
        today = PasswordStore._today_utc()
        if entry.get("usage_date") != today:
            entry["usage_date"] = today
            entry["input_tokens"] = 0
            entry["cached_tokens"] = 0
            entry["output_tokens"] = 0
        return entry

    @staticmethod
    def _calc_cost(entry: dict, pricing: dict) -> float:
        p = {**_DEFAULT_PRICING, **pricing}
        return (
            entry.get("input_tokens", 0) * p["input"]
            + entry.get("cached_tokens", 0) * p["cached"]
            + entry.get("output_tokens", 0) * p["output"]
        ) / 1_000_000

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def has_passwords(self) -> bool:
        return bool(self._load())

    def authenticate(self, password: str) -> bool:
        return password in self._load()

    def get_all(self, pricing: dict | None = None) -> list[dict]:
        pricing = pricing or {}
        data = self._load()
        result = []
        for pwd, entry in data.items():
            entry = self._reset_if_new_day(dict(entry))
            result.append({
                "password": pwd,
                "input_tokens": entry.get("input_tokens", 0),
                "cached_tokens": entry.get("cached_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
                "cost": self._calc_cost(entry, pricing),
                "daily_limit": entry.get("daily_limit", 0.0),
            })
        return result

    def add(self, password: str) -> bool:
        with self._lock:
            data = self._load()
            if password in data:
                return False
            data[password] = {
                "daily_limit": 0.0,
                "usage_date": "",
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
            }
            self._save(data)
            return True

    def remove(self, password: str) -> bool:
        with self._lock:
            data = self._load()
            if password not in data:
                return False
            del data[password]
            self._save(data)
            return True

    def set_limit(self, password: str, limit_usd: float) -> bool:
        with self._lock:
            data = self._load()
            if password not in data:
                return False
            data[password]["daily_limit"] = limit_usd
            self._save(data)
            return True

    def reset_usage(self, password: str) -> bool:
        with self._lock:
            data = self._load()
            if password not in data:
                return False
            data[password].update({
                "usage_date": self._today_utc(),
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
            })
            self._save(data)
            return True

    def record_usage(self, password: str, input_t: int, cached_t: int, output_t: int) -> None:
        with self._lock:
            data = self._load()
            if password not in data:
                return
            entry = self._reset_if_new_day(data[password])
            entry["input_tokens"] = entry.get("input_tokens", 0) + input_t
            entry["cached_tokens"] = entry.get("cached_tokens", 0) + cached_t
            entry["output_tokens"] = entry.get("output_tokens", 0) + output_t
            data[password] = entry
            self._save(data)

    def is_over_budget(self, password: str, pricing: dict | None = None) -> bool:
        pricing = pricing or {}
        with self._lock:
            data = self._load()
            if password not in data:
                return False
            entry = self._reset_if_new_day(dict(data[password]))
            limit = entry.get("daily_limit", 0.0)
            if not limit:
                return False
            return self._calc_cost(entry, pricing) >= limit


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _load_pricing() -> dict:
    for path in (Path("game_config.yaml"), Path("config/game_config.yaml")):
        if path.exists():
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            pricing = cfg.get("ai", {}).get("pricing", {})
            if pricing:
                return pricing
    return {}


def _dw(s: str) -> int:
    """Terminal display width of a string (CJK wide chars count as 2)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """Left-justify *s* to *width* display columns."""
    return s + " " * max(0, width - _dw(s))


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("（暂无密码）")
        return
    headers = ["密码", "输入T", "缓存T", "输出T", "今日消费", "日限额"]
    col_data = []
    for r in rows:
        limit_str = f"${r['daily_limit']:.4f}" if r["daily_limit"] else "不限制"
        col_data.append([
            r["password"],
            f"{r['input_tokens']:,}",
            f"{r['cached_tokens']:,}",
            f"{r['output_tokens']:,}",
            f"${r['cost']:.6f}",
            limit_str,
        ])
    widths = [max(_dw(h), max((_dw(row[i]) for row in col_data), default=0))
              for i, h in enumerate(headers)]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    print(sep)
    print("| " + " | ".join(_pad(h, w) for h, w in zip(headers, widths)) + " |")
    print(sep)
    for row in col_data:
        print("| " + " | ".join(_pad(cell, w) for cell, w in zip(row, widths)) + " |")
    print(sep)


def _run_cli() -> None:
    store = PasswordStore()
    pricing = _load_pricing()
    print("Poker-AI 密码管理器  (输入 help 查看命令)")

    while True:
        try:
            line = input("auth> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ("exit", "quit"):
            break

        elif cmd == "help":
            print("  list                  — 列出所有密码及使用情况")
            print("  add <密码>             — 新增密码")
            print("  remove/rm <密码>       — 删除密码")
            print("  limit <密码> <金额>    — 设置日限额（0=不限制）")
            print("  reset <密码>           — 清零今日用量")
            print("  exit / quit           — 退出")

        elif cmd == "list":
            _print_table(store.get_all(pricing))

        elif cmd == "add":
            if len(parts) < 2:
                print("用法: add <密码>")
                continue
            pwd = parts[1]
            if store.add(pwd):
                print(f"已添加密码: {pwd}")
            else:
                print(f"密码已存在: {pwd}")

        elif cmd in ("remove", "rm"):
            if len(parts) < 2:
                print("用法: remove <密码>")
                continue
            pwd = parts[1]
            if store.remove(pwd):
                print(f"已删除密码: {pwd}")
            else:
                print(f"密码不存在: {pwd}")

        elif cmd == "limit":
            if len(parts) < 3:
                print("用法: limit <密码> <金额>")
                continue
            pwd = parts[1]
            try:
                amount = float(parts[2])
            except ValueError:
                print("金额必须是数字")
                continue
            if store.set_limit(pwd, amount):
                print(f"已设置 {pwd} 日限额: ${amount:.4f}" if amount else f"已取消 {pwd} 的日限额")
            else:
                print(f"密码不存在: {pwd}")

        elif cmd == "reset":
            if len(parts) < 2:
                print("用法: reset <密码>")
                continue
            pwd = parts[1]
            if store.reset_usage(pwd):
                print(f"已重置 {pwd} 今日用量")
            else:
                print(f"密码不存在: {pwd}")

        else:
            print(f"未知命令: {cmd}  (输入 help 查看命令)")


if __name__ == "__main__":
    _run_cli()
