"""Finance history must support both legacy MyISAM and fresh InnoDB parents."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.apps import apps
from django.test import SimpleTestCase


class FinanceMigrationStorageContractTests(SimpleTestCase):
    modules = (
        import_module('finance.migrations.0019_finance_v2_ledger'),
        import_module('finance.migrations.0020_finance_v2_classification_audit'),
    )
    parents = (
        {'auth_user', 'finance_company', 'finance_project', 'finance_category',
         'finance_transaction', 'finance_account', 'finance_counterparty',
         'finance_recurrencerule', 'finance_counterpartycard', 'finance_obligationsettlement'},
        {'auth_user', 'finance_fundingsource', 'finance_ledgerclassification', 'finance_transaction'},
    )

    def editor(self, index, engine='InnoDB', *, changes=None, missing=(), vendor='mysql'):
        rows = {table: (engine, 'BASE TABLE') for table in self.parents[index]}
        rows['auth_user'] = ('InnoDB', 'BASE TABLE')
        rows.update(changes or {})
        for table in missing:
            rows.pop(table)
        editor = MagicMock()
        editor.connection.vendor = vendor
        editor.connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            (table, *values) for table, values in rows.items()
        ]
        return editor

    def apply(self, index, editor, *, backwards=False, final=False):
        operation = self.modules[index].Migration.operations[-1 if final else 0]
        state = SimpleNamespace(apps=apps)
        method = operation.database_backwards if backwards else operation.database_forwards
        method('finance', editor, state, state)

    def test_fresh_innodb_preserves_all_native_fk_parents(self):
        for index in range(2):
            with self.subTest(migration=index):
                editor = self.editor(index)
                self.apply(index, editor)
                editor.execute.assert_called_once_with('SET SESSION default_storage_engine=INNODB')
                cursor = editor.connection.cursor.return_value.__enter__.return_value
                self.assertEqual(set(cursor.execute.call_args.args[1]), self.parents[index])

    def test_legacy_myisam_preserves_existing_behavior_with_innodb_auth(self):
        for index in range(2):
            editor = self.editor(index, 'MyISAM')
            self.apply(index, editor)
            editor.execute.assert_called_once_with('SET SESSION default_storage_engine=MYISAM')

    def test_mixed_finance_engines_fail_before_any_ddl_or_session_change(self):
        for index in range(2):
            editor = self.editor(index, changes={'finance_transaction': ('MyISAM', 'BASE TABLE')})
            with self.assertRaisesRegex(RuntimeError, 'uniform finance FK parent engines'):
                self.apply(index, editor)
            editor.execute.assert_not_called()

    def test_fresh_finance_cannot_ignore_myisam_auth_fk(self):
        for index in range(2):
            editor = self.editor(index, changes={'auth_user': ('MyISAM', 'BASE TABLE')})
            with self.assertRaisesRegex(RuntimeError, 'InnoDB external FK parents'):
                self.apply(index, editor)
            editor.execute.assert_not_called()

    def test_missing_unsupported_and_view_parents_fail_before_ddl(self):
        for index in range(2):
            for kwargs in ({'missing': ['finance_transaction']},
                           {'changes': {'finance_transaction': ('Aria', 'BASE TABLE')}},
                           {'changes': {'finance_transaction': ('InnoDB', 'VIEW')}}):
                editor = self.editor(index, **kwargs)
                with self.assertRaisesRegex(RuntimeError, 'existing InnoDB/MyISAM FK parent tables'):
                    self.apply(index, editor)
                editor.execute.assert_not_called()

    def test_terminal_reset_does_not_inspect_or_alter_tables(self):
        for index in range(2):
            editor = self.editor(index)
            self.apply(index, editor, final=True)
            editor.execute.assert_called_once_with('SET SESSION default_storage_engine=INNODB')
            editor.connection.cursor.assert_not_called()

    def test_reverse_uses_the_same_parent_compatibility(self):
        for index in range(2):
            for engine in ('InnoDB', 'MyISAM'):
                editor = self.editor(index, engine)
                self.apply(index, editor, final=True, backwards=True)
                editor.execute.assert_called_once_with(f'SET SESSION default_storage_engine={engine.upper()}')

    def test_sqlite_does_no_engine_io(self):
        for index in range(2):
            editor = self.editor(index, vendor='sqlite')
            self.apply(index, editor)
            self.apply(index, editor, backwards=True, final=True)
            editor.execute.assert_not_called()
            editor.connection.cursor.assert_not_called()
