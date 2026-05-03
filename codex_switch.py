#!/usr/bin/env python3
"""Small local CODEX_HOME shortcut switcher."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional
from urllib import error, request


ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_USER_AGENT = "codex-cli/1.0.0"
DEFAULT_STORE_DIR = ".codex-shortcut-switcher"


class ConfigError(Exception):
    """Raised for user-fixable configuration errors."""


@dataclass(frozen=True)
class AccountAlias:
    alias: str
    codex_home: Path
    created_at: str

    @classmethod
    def from_dict(cls, data: Mapping[str, str]) -> "AccountAlias":
        return cls(
            alias=str(data["alias"]),
            codex_home=Path(str(data["codex_home"])).expanduser(),
            created_at=str(data["created_at"]),
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "alias": self.alias,
            "codex_home": str(self.codex_home),
            "created_at": self.created_at,
        }

    def to_public_dict(self) -> Dict[str, str]:
        return self.to_dict()


@dataclass(frozen=True)
class UsageAuth:
    access_token: str
    account_id: Optional[str]

    def __repr__(self) -> str:
        return f"UsageAuth(access_token=<hidden>, account_id={self.account_id!r})"


@dataclass(frozen=True)
class UsageWindow:
    remaining_percent: float
    used_percent: float
    reset_at: Optional[int]


@dataclass(frozen=True)
class UsageSummary:
    five_hour: UsageWindow
    weekly: Optional[UsageWindow]


class CodexAliasStore:
    def __init__(self, root: Optional[Path] = None):
        if root is None:
            configured = os.environ.get("CODEX_SWITCH_HOME")
            root = Path(configured).expanduser() if configured else default_store_root()
        self.root = root
        self.homes_dir = self.root / "homes"
        self.config_path = self.root / "aliases.json"

    def add_from_current(self, alias: str, replace: bool = False) -> AccountAlias:
        return self.add_from_auth(alias, current_codex_home() / "auth.json", replace=replace)

    def add_from_auth(self, alias: str, auth_path: Path, replace: bool = False) -> AccountAlias:
        alias = validate_alias(alias)
        auth_path = auth_path.expanduser()
        if not auth_path.is_file():
            raise ConfigError(f"auth.json not found: {auth_path}")

        accounts = {account.alias: account for account in self.list_accounts()}
        if alias in accounts and not replace:
            raise ConfigError(f"alias already exists: {alias} (use --replace to overwrite)")

        codex_home = self.homes_dir / alias
        codex_home.mkdir(parents=True, exist_ok=True)
        set_private_dir(codex_home)

        target = codex_home / "auth.json"
        shutil.copyfile(auth_path, target)
        os.chmod(target, 0o600)

        account = AccountAlias(
            alias=alias,
            codex_home=codex_home,
            created_at=accounts.get(alias, new_account(alias, codex_home)).created_at,
        )
        accounts[alias] = account
        self._save_accounts(accounts.values())
        return account

    def remove(self, alias: str, delete_home: bool = False) -> AccountAlias:
        alias = validate_alias(alias)
        accounts = {account.alias: account for account in self.list_accounts()}
        account = accounts.pop(alias, None)
        if account is None:
            raise ConfigError(f"unknown alias: {alias}")
        self._save_accounts(accounts.values())
        if delete_home:
            shutil.rmtree(account.codex_home)
        return account

    def get(self, alias: str) -> AccountAlias:
        alias = validate_alias(alias)
        for account in self.list_accounts():
            if account.alias == alias:
                return account
        raise ConfigError(f"unknown alias: {alias}")

    def list_accounts(self) -> List[AccountAlias]:
        if not self.config_path.exists():
            return []
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid config JSON: {self.config_path}: {exc}") from exc
        return sorted(
            [AccountAlias.from_dict(item) for item in data.get("accounts", [])],
            key=lambda account: account.alias,
        )

    def alias_names(self) -> List[str]:
        return [account.alias for account in self.list_accounts()]

    def command_env(
        self,
        alias: str,
        base_env: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        account = self.get(alias)
        auth_path = account.codex_home / "auth.json"
        if not auth_path.is_file():
            raise ConfigError(f"auth.json missing for alias: {alias}")
        env = dict(os.environ if base_env is None else base_env)
        env["CODEX_HOME"] = str(account.codex_home)
        return env

    def switch_alias(self, alias: str, target_codex_home: Optional[Path] = None) -> AccountAlias:
        account = self.get(alias)
        source = account.codex_home / "auth.json"
        if not source.is_file():
            raise ConfigError(f"auth.json missing for alias: {alias}")

        target_home = (target_codex_home or default_codex_home()).expanduser()
        target_home.mkdir(parents=True, exist_ok=True)
        set_private_dir(target_home)

        target = target_home / "auth.json"
        backup = target_home / "auth.json.codex-switch-backup"
        if target.exists():
            shutil.copyfile(target, backup)
            os.chmod(backup, 0o600)

        temp_target = target_home / "auth.json.codex-switch-tmp"
        shutil.copyfile(source, temp_target)
        os.chmod(temp_target, 0o600)
        temp_target.replace(target)
        os.chmod(target, 0o600)
        return account

    def _save_accounts(self, accounts: Iterable[AccountAlias]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        set_private_dir(self.root)
        data = {
            "version": 1,
            "accounts": [
                account.to_dict()
                for account in sorted(accounts, key=lambda item: item.alias)
            ],
        }
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.config_path)


def validate_alias(alias: str) -> str:
    if not ALIAS_RE.fullmatch(alias):
        raise ConfigError(
            "alias must be 1-64 chars and use only letters, digits, '.', '_', or '-' "
            "with a letter or digit first"
        )
    return alias


def current_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_codex_home() -> Path:
    return Path.home() / ".codex"


def default_store_root() -> Path:
    return Path.home() / DEFAULT_STORE_DIR


def set_private_dir(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, stat.S_IRWXU)


def new_account(alias: str, codex_home: Path) -> AccountAlias:
    return AccountAlias(
        alias=alias,
        codex_home=codex_home,
        created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-switch",
        description="Use separate local CODEX_HOME directories by account alias.",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="state directory (default: $CODEX_SWITCH_HOME or ~/.codex-shortcut-switcher)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add or replace an alias from an auth.json")
    add.add_argument("alias")
    source = add.add_mutually_exclusive_group()
    source.add_argument("--from-auth", type=Path, help="path to an auth.json file")
    source.add_argument("--from-current", action="store_true", help="copy auth.json from current CODEX_HOME")
    add.add_argument("--replace", action="store_true", help="replace an existing alias")

    list_parser = sub.add_parser("list", help="list configured aliases")
    list_parser.add_argument(
        "--usage",
        action="store_true",
        help="also show remaining 5-hour and weekly usage for OAuth aliases",
    )
    list_parser.add_argument(
        "--path",
        action="store_true",
        help="also show the stored CODEX_HOME path for each alias",
    )

    sub.add_parser("aliases", help="print aliases only, one per line, for macOS Shortcuts")

    path = sub.add_parser("path", help="print the CODEX_HOME path for an alias")
    path.add_argument("alias", nargs="?")

    env = sub.add_parser("env", help="print a shell export for an alias")
    env.add_argument("alias", nargs="?")

    switch = sub.add_parser("switch", help="copy an alias auth.json into ~/.codex/auth.json")
    switch.add_argument("alias", nargs="?")
    switch.add_argument(
        "--target-codex-home",
        type=Path,
        default=None,
        help="target Codex home to overwrite (default: ~/.codex)",
    )

    run = sub.add_parser("run", help="run a command with CODEX_HOME set to an alias")
    run.add_argument("alias", nargs="?")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="command to run after -- (default: codex)")

    remove = sub.add_parser("remove", help="remove an alias")
    remove.add_argument("alias", nargs="?")
    remove.add_argument("--delete-home", action="store_true", help="also delete the alias CODEX_HOME directory")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = CodexAliasStore(args.store)

    try:
        if args.command == "add":
            auth_path = args.from_auth
            account = (
                store.add_from_current(args.alias, replace=args.replace)
                if auth_path is None
                else store.add_from_auth(args.alias, auth_path, replace=args.replace)
            )
            print(f"added {account.alias}: {account.codex_home}")
            return 0

        if args.command == "list":
            accounts = store.list_accounts()
            if not accounts:
                print("no aliases configured")
                return 0
            usage_by_alias = None
            if args.usage:
                usage_by_alias = fetch_usage_labels(accounts)
            for row in format_account_rows(
                accounts,
                usage_by_alias=usage_by_alias,
                include_path=args.path,
            ):
                print(row)
            return 0

        if args.command == "aliases":
            for alias in store.alias_names():
                print(alias)
            return 0

        if args.command == "path":
            alias = resolve_alias_arg(args.alias)
            print(store.get(alias).codex_home)
            return 0

        if args.command == "env":
            alias = resolve_alias_arg(args.alias)
            account = store.get(alias)
            print(f"export CODEX_HOME={shlex.quote(str(account.codex_home))}")
            return 0

        if args.command == "switch":
            alias = resolve_alias_arg(args.alias)
            account = store.switch_alias(alias, args.target_codex_home)
            print(f"switched to {account.alias}")
            return 0

        if args.command == "run":
            alias = resolve_alias_arg(args.alias)
            command = normalize_run_argv(args.argv)
            env = store.command_env(alias)
            return subprocess.run(command, env=env, check=False).returncode

        if args.command == "remove":
            alias = resolve_alias_arg(args.alias)
            account = store.remove(alias, delete_home=args.delete_home)
            suffix = " and deleted home" if args.delete_home else ""
            print(f"removed {account.alias}{suffix}")
            return 0

        parser.error(f"unsupported command: {args.command}")
        return 2
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def normalize_run_argv(argv: List[str]) -> List[str]:
    if not argv:
        return ["codex"]
    if argv[0] == "--":
        argv = argv[1:]
    return argv or ["codex"]


def resolve_alias_arg(alias: Optional[str], stdin_text: Optional[str] = None) -> str:
    if alias:
        return normalize_alias_input(alias)
    text = sys.stdin.read() if stdin_text is None else stdin_text
    for line in text.splitlines():
        candidate = line.strip()
        if candidate:
            return normalize_alias_input(candidate)
    raise ConfigError("missing alias")


def normalize_alias_input(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return candidate
    if ALIAS_RE.fullmatch(candidate):
        return candidate
    return candidate.split()[0]


def format_account_rows(
    accounts: List[AccountAlias],
    usage_by_alias: Optional[Mapping[str, str]] = None,
    include_path: bool = False,
) -> List[str]:
    if not accounts:
        return []
    width = max(len(account.alias) for account in accounts)
    status_by_alias = {
        account.alias: "ok" if (account.codex_home / "auth.json").is_file() else "missing-auth"
        for account in accounts
    }
    status_width = max(len(status) for status in status_by_alias.values())
    formatted_usage = format_usage_labels_for_rows(usage_by_alias) if usage_by_alias is not None else None
    rows = []
    for account in accounts:
        status = status_by_alias[account.alias]
        if formatted_usage is None:
            row = f"{account.alias:<{width}}  {status:<{status_width}}"
        else:
            usage = formatted_usage.get(account.alias, "usage n/a")
            row = f"{account.alias:<{width}}  {status:<{status_width}}  {usage:<8}"
        if include_path:
            row = f"{row}  {account.codex_home}"
        rows.append(row.rstrip())
    return rows


def format_usage_labels_for_rows(usage_by_alias: Mapping[str, str]) -> Dict[str, str]:
    parts_by_alias = {}
    widths = [0, 0, 0, 0]

    for alias, label in usage_by_alias.items():
        parts = split_usage_label(label)
        parts_by_alias[alias] = parts
        for index, part in enumerate(parts):
            widths[index] = max(widths[index], len(part))

    formatted = {}
    for alias, parts in parts_by_alias.items():
        if len(parts) != 4:
            formatted[alias] = " ".join(parts)
            continue
        first_percent, first_reset, weekly_percent, weekly_reset = parts
        formatted[alias] = (
            f"{first_percent:>{widths[0]}} "
            f"{first_reset:<{widths[1]}} | "
            f"{weekly_percent:>{widths[2]}} "
            f"{weekly_reset:<{widths[3]}}"
        ).rstrip()
    return formatted


def split_usage_label(label: str) -> List[str]:
    sections = [section.strip() for section in label.split("|", 1)]
    if len(sections) != 2:
        return label.split()

    first = sections[0].split()
    weekly = sections[1].split()
    if len(first) != 2 or len(weekly) != 2:
        return label.split()
    return [first[0], first[1], weekly[0], weekly[1]]


def fetch_usage_labels(accounts: List[AccountAlias]) -> Dict[str, str]:
    labels = {}
    for account in accounts:
        try:
            labels[account.alias] = fetch_usage_label(account)
        except ConfigError as exc:
            labels[account.alias] = f"error: {exc}"
    return labels


def fetch_usage_label(account: AccountAlias) -> str:
    auth_path = account.codex_home / "auth.json"
    usage_auth = extract_usage_auth(auth_path)
    try:
        payload = fetch_usage_payload(usage_auth)
    except ConfigError as exc:
        if "HTTP 401" not in str(exc):
            raise
        raise ConfigError("access token expired; switch or sign in again to refresh safely") from exc
    usage = parse_usage_summary(payload)
    return format_usage_label(usage)


def extract_usage_auth(auth_path: Path) -> UsageAuth:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError("missing auth.json") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("auth.json is not valid JSON") from exc

    if data.get("OPENAI_API_KEY"):
        raise ConfigError("api-key auth has no ChatGPT usage")

    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise ConfigError("auth.json has no OAuth tokens")

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConfigError("auth.json has no access token")

    account_id = tokens.get("account_id")
    if account_id is not None and not isinstance(account_id, str):
        account_id = None

    return UsageAuth(access_token=access_token, account_id=account_id)


def fetch_usage_payload(usage_auth: UsageAuth) -> Mapping[str, object]:
    headers = {
        "Authorization": f"Bearer {usage_auth.access_token}",
        "User-Agent": CODEX_USER_AGENT,
    }
    if usage_auth.account_id:
        headers["chatgpt-account-id"] = usage_auth.account_id

    req = request.Request(CHATGPT_USAGE_URL, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raise ConfigError(f"usage API HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise ConfigError(f"usage API unavailable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ConfigError("usage API timed out") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ConfigError("usage API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigError("usage API returned unexpected payload")
    return payload


def parse_usage_summary(payload: Mapping[str, object]) -> UsageSummary:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise ConfigError("usage response has no rate_limit")

    windows = [
        rate_limit.get("primary_window"),
        rate_limit.get("secondary_window"),
    ]
    five_hour = None
    weekly = None
    for window in windows:
        if not isinstance(window, dict):
            continue
        parsed = parse_usage_window(window)
        if window.get("limit_window_seconds") == 18_000:
            five_hour = parsed
        elif window.get("limit_window_seconds") == 604_800:
            weekly = parsed
        elif five_hour is None:
            five_hour = parsed

    if five_hour is None:
        raise ConfigError("usage response has no 5-hour window")

    return UsageSummary(five_hour=five_hour, weekly=weekly)


def parse_usage_window(window: Mapping[str, object]) -> UsageWindow:
    used = window.get("used_percent")
    if not isinstance(used, (int, float)):
        raise ConfigError("usage response has no used_percent")
    used_float = float(used)
    remaining = max(0.0, min(100.0, 100.0 - used_float))

    reset_at = window.get("reset_at")
    if not isinstance(reset_at, int):
        reset_at = None

    return UsageWindow(
        remaining_percent=remaining,
        used_percent=used_float,
        reset_at=reset_at,
    )


def format_usage_label(usage: UsageSummary, now: Optional[int] = None) -> str:
    parts = [format_window_label("5h", usage.five_hour, now=now)]
    if usage.weekly is not None:
        parts.append(format_window_label("wk", usage.weekly, now=now))
    return " | ".join(parts)


def format_window_label(name: str, window: UsageWindow, now: Optional[int] = None) -> str:
    remaining = format_percent(window.remaining_percent)
    label = remaining
    if window.reset_at is not None:
        label = f"{label} {format_reset_countdown(window.reset_at, now=now)}"
    return label


def format_reset_countdown(reset_at: int, now: Optional[int] = None) -> str:
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    seconds = max(0, int(reset_at) - int(now))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = (remainder + 59) // 60
    if minutes == 60:
        hours += 1
        minutes = 0
    if hours == 24:
        days += 1
        hours = 0

    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def format_percent(value: float) -> str:
    value_float = float(value)
    if value_float.is_integer():
        return f"{int(value_float)}%"
    return f"{value_float:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
