import os
import sys
import unittest
from argparse import Namespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import migrate_environment as migration


class MigrationTests(unittest.TestCase):
    def test_same_source_and_target_is_rejected(self):
        args = Namespace(
            organization="acme",
            repository="app",
            from_environment="prd",
            to_environment="PRD",
            migration_type="vars",
            current_repository="acme/automation",
            github_output=None,
        )

        with self.assertRaises(migration.MigrationError):
            migration.command_validate(args)

    @patch.object(migration, "assert_environment_exists")
    @patch.object(migration, "required_token", return_value="token")
    def test_cross_repo_secret_migration_is_rejected(self, _token, _exists):
        args = Namespace(
            organization="acme",
            repository="app",
            from_environment="uat",
            to_environment="prd",
            migration_type="secrets",
            current_repository="acme/automation",
            github_output=None,
        )

        with self.assertRaises(migration.MigrationError):
            migration.command_validate(args)

    @patch.object(migration, "list_environment_variables")
    @patch.object(migration, "required_token", return_value="token")
    def test_copy_vars_skips_existing_without_overwrite(self, _token, list_vars):
        list_vars.side_effect = [
            [{"name": "API_URL", "value": "https://source.example"}],
            [{"name": "API_URL", "value": "https://target.example"}],
        ]
        args = Namespace(
            organization="acme",
            repository="app",
            from_environment="uat",
            to_environment="prd",
            overwrite=False,
            dry_run=False,
        )

        with patch.object(migration, "update_environment_variable") as update_var:
            migration.command_copy_vars(args)
            update_var.assert_not_called()

    @patch.object(migration, "list_environment_variables")
    @patch.object(migration, "required_token", return_value="token")
    def test_copy_vars_updates_existing_with_overwrite(self, _token, list_vars):
        list_vars.side_effect = [
            [{"name": "API_URL", "value": "https://source.example"}],
            [{"name": "API_URL", "value": "https://target.example"}],
        ]
        args = Namespace(
            organization="acme",
            repository="app",
            from_environment="uat",
            to_environment="prd",
            overwrite=True,
            dry_run=False,
        )

        with patch.object(migration, "update_environment_variable") as update_var:
            migration.command_copy_vars(args)
            update_var.assert_called_once()

    @patch.object(migration, "list_environment_variables")
    @patch.object(migration, "required_token", return_value="token")
    def test_dry_run_does_not_write(self, _token, list_vars):
        list_vars.side_effect = [
            [{"name": "NEW_VAR", "value": "value"}],
            [],
        ]
        args = Namespace(
            organization="acme",
            repository="app",
            from_environment="uat",
            to_environment="prd",
            overwrite=True,
            dry_run=True,
        )

        with patch.object(migration, "create_environment_variable") as create_var:
            migration.command_copy_vars(args)
            create_var.assert_not_called()


if __name__ == "__main__":
    unittest.main()
