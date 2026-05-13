import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_switch import (
    CodexAliasStore,
    ConfigError,
    UsageSummary,
    UsageAuth,
    UsageWindow,
    default_store_root,
    extract_usage_auth,
    fetch_usage_labels,
    format_account_rows,
    format_usage_label,
    format_usage_labels_for_rows,
    parse_usage_summary,
    resolve_alias_arg,
)


class CodexAliasStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = CodexAliasStore(self.root / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def write_auth(self, path: Path, content: str = '{"tokens":{"refresh_token":"secret"}}'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)

    def test_add_from_auth_copies_file_without_parsing(self):
        source = self.root / "current" / "auth.json"
        self.write_auth(source, "not json but copied byte-for-byte")

        account = self.store.add_from_auth("work", source)

        self.assertEqual(account.alias, "work")
        self.assertEqual(account.codex_home, self.root / "state" / "homes" / "work")
        self.assertEqual(
            (account.codex_home / "auth.json").read_text(encoding="utf-8"),
            "not json but copied byte-for-byte",
        )
        self.assertEqual(oct((account.codex_home / "auth.json").stat().st_mode & 0o777), "0o600")

    def test_aliases_are_validated(self):
        source = self.root / "auth.json"
        self.write_auth(source)

        for bad_alias in ["", "../x", "has space", ".hidden", "x/y"]:
            with self.subTest(bad_alias=bad_alias):
                with self.assertRaises(ConfigError):
                    self.store.add_from_auth(bad_alias, source)

    def test_duplicate_alias_is_rejected_unless_replace_is_set(self):
        first = self.root / "first" / "auth.json"
        second = self.root / "second" / "auth.json"
        self.write_auth(first, "first")
        self.write_auth(second, "second")

        self.store.add_from_auth("main", first)
        with self.assertRaises(ConfigError):
            self.store.add_from_auth("main", second)

        self.store.add_from_auth("main", second, replace=True)
        self.assertEqual(
            (self.root / "state" / "homes" / "main" / "auth.json").read_text(encoding="utf-8"),
            "second",
        )

    def test_default_store_root_uses_new_state_directory_only(self):
        self.assertEqual(default_store_root().name, ".codex-shortcut-switcher")

    def test_list_does_not_include_secret_contents(self):
        source = self.root / "auth.json"
        self.write_auth(source)
        self.store.add_from_auth("personal", source)

        payload = str([account.to_public_dict() for account in self.store.list_accounts()])

        self.assertIn("personal", payload)
        self.assertNotIn("refresh_token", payload)
        self.assertNotIn("secret", payload)

    def test_command_env_sets_codex_home_for_alias(self):
        source = self.root / "auth.json"
        self.write_auth(source)
        self.store.add_from_auth("personal", source)

        env = self.store.command_env("personal", {"PATH": "/bin", "CODEX_HOME": "/old"})

        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["CODEX_HOME"], str(self.root / "state" / "homes" / "personal"))

    def test_alias_names_are_sorted_for_shortcuts_choice_list(self):
        beta = self.root / "beta" / "auth.json"
        alpha = self.root / "alpha" / "auth.json"
        self.write_auth(beta, "beta")
        self.write_auth(alpha, "alpha")

        self.store.add_from_auth("beta", beta)
        self.store.add_from_auth("alpha", alpha)

        self.assertEqual(self.store.alias_names(), ["alpha", "beta"])

    def test_switch_alias_copies_auth_to_target_codex_home_with_backup(self):
        source = self.root / "source" / "auth.json"
        target = self.root / "target-codex"
        self.write_auth(source, "selected")
        self.write_auth(target / "auth.json", "previous")
        self.store.add_from_auth("work", source)

        switched = self.store.switch_alias("work", target)

        self.assertEqual(switched.alias, "work")
        self.assertEqual((target / "auth.json").read_text(encoding="utf-8"), "selected")
        self.assertEqual((target / "auth.json.codex-switch-backup").read_text(encoding="utf-8"), "previous")
        self.assertEqual(oct((target / "auth.json").stat().st_mode & 0o777), "0o600")

    def test_resolve_alias_arg_reads_first_non_empty_stdin_line(self):
        self.assertEqual(resolve_alias_arg(None, " \nwork\nignored\n"), "work")

    def test_resolve_alias_arg_prefers_positional_alias(self):
        self.assertEqual(resolve_alias_arg("work", "other\n"), "work")

    def test_resolve_alias_arg_accepts_selected_list_row(self):
        self.assertEqual(
            resolve_alias_arg("work  ok            58% left  /tmp/home"),
            "work",
        )

    def test_extract_usage_auth_reads_oauth_tokens_without_returning_refresh_token(self):
        auth_path = self.root / "auth.json"
        self.write_auth(
            auth_path,
            '{"tokens":{"access_token":"access","refresh_token":"refresh","account_id":"acc_123"}}',
        )

        usage_auth = extract_usage_auth(auth_path)

        self.assertEqual(usage_auth.access_token, "access")
        self.assertEqual(usage_auth.account_id, "acc_123")
        self.assertNotIn("refresh", repr(usage_auth))

    def test_parse_usage_summary_returns_5h_and_weekly_windows(self):
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 42.5,
                    "limit_window_seconds": 18000,
                    "reset_at": 1893456000,
                },
                "secondary_window": {
                    "used_percent": 4,
                    "limit_window_seconds": 604800,
                    "reset_at": 1893715200,
                },
            }
        }

        usage = parse_usage_summary(payload)

        self.assertEqual(usage.five_hour.remaining_percent, 57.5)
        self.assertEqual(usage.five_hour.used_percent, 42.5)
        self.assertEqual(usage.five_hour.reset_at, 1893456000)
        self.assertEqual(usage.weekly.remaining_percent, 96)
        self.assertEqual(usage.weekly.used_percent, 4)
        self.assertEqual(usage.weekly.reset_at, 1893715200)

    def test_format_usage_label_includes_cutoff_countdowns(self):
        usage = UsageSummary(
            five_hour=UsageWindow(75, 25, 1_893_456_000),
            weekly=UsageWindow(96, 4, 1_893_628_800),
        )

        label = format_usage_label(usage, now=1_893_448_800)

        self.assertEqual(label, "75% 2h | 96% 2d2h")

    def test_format_account_rows_includes_usage_when_available(self):
        source = self.root / "auth.json"
        self.write_auth(source)
        account = self.store.add_from_auth("personal", source)

        rows = format_account_rows(
            [account],
            usage_by_alias={"personal": "58% 2h | 96% 2d"},
        )

        self.assertEqual(rows, ["personal  ok  58% 2h | 96% 2d"])

    def test_usage_labels_are_columnized_across_accounts(self):
        usage_by_alias = {
            "Biz4Y-1-1": "64% 3h39m | 94% 6d22h",
            "Team-2-1": "100% 5h | 50% 2d15h",
            "Team-2-2": "58% 4h22m | 78% 6d17h",
            "Team-2-3": "0% 2h5m | 84% 6d21h",
        }

        aligned = format_usage_labels_for_rows(usage_by_alias)

        self.assertEqual(
            aligned,
            {
                "Biz4Y-1-1": " 64% 3h39m | 94% 6d22h",
                "Team-2-1": "100% 5h    | 50% 2d15h",
                "Team-2-2": " 58% 4h22m | 78% 6d17h",
                "Team-2-3": "  0% 2h5m  | 84% 6d21h",
            },
        )

    def test_usage_labels_keep_account_errors_unaligned(self):
        usage_by_alias = {
            "personal": "error: access token expired; switch or sign in again to refresh safely",
            "team": "100% 5h | 50% 2d15h",
        }

        aligned = format_usage_labels_for_rows(usage_by_alias)

        self.assertEqual(
            aligned["personal"],
            "error: access token expired; switch or sign in again to refresh safely",
        )
        self.assertEqual(aligned["team"], "100% 5h | 50% 2d15h")

    def test_format_account_rows_can_include_path_when_requested(self):
        source = self.root / "auth.json"
        self.write_auth(source)
        account = self.store.add_from_auth("personal", source)

        rows = format_account_rows([account], include_path=True)

        self.assertEqual(rows, [f"personal  ok  {account.codex_home}"])

    def test_usage_lookup_does_not_refresh_rotating_tokens_on_401(self):
        source = self.root / "auth.json"
        self.write_auth(
            source,
            '{"tokens":{"access_token":"old-access","refresh_token":"refresh","account_id":"acc_123"}}',
        )
        account = self.store.add_from_auth("personal", source)

        with mock.patch(
            "codex_switch.fetch_usage_payload",
            side_effect=ConfigError("usage API HTTP 401"),
        ), mock.patch(
            "codex_switch.extract_usage_auth",
            return_value=UsageAuth(access_token="old-access", account_id="acc_123"),
        ):
            labels = fetch_usage_labels([account])

        self.assertEqual(
            labels["personal"],
            "error: access token expired; switch or sign in again to refresh safely",
        )


if __name__ == "__main__":
    unittest.main()
