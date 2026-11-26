from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged, new_test_user


@tagged('post_install', '-at_install')
class TestMailManagement(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = cls.env(context={**cls.env.context, 'no_reset_password': True})
        cls.reader = new_test_user(ctx, login='mail-management-reader', groups='base.group_user', email='reader@example.invalid')
        cls.admin = new_test_user(ctx, login='mail-management-admin', groups='base.group_system', email='admin@example.invalid')
        cls.server = cls.env['fetchmail.server'].create({'name': 'Isolated management fixture', 'server_type': 'imap',
            'server': 'imap.example.invalid', 'user': 'ingest@example.invalid', 'password': 'test-only', 'active': False})
        cls.failure, _ = cls.env['mail.fetch.failure']._begin_processing(cls.server, 'imap',
            b'Message-ID: <management@example.invalid>\r\nSubject: Fixture\r\n\r\nOffline', remote_uid='management', uid_validity='1')
        cls.failure._mark_failed(RuntimeError('Synthetic error'))

    def test_admin_views_reference_original_ledger(self):
        views = self.env['woow.mail.workspace'].with_user(self.admin).management_views()
        self.assertEqual(set(views), {'list', 'form', 'search'})
        for vid in views.values():
            self.assertEqual(self.env['ir.ui.view'].browse(vid).model, 'mail.fetch.failure')

    def test_reader_cannot_request_management_or_ledger(self):
        with self.assertRaises(AccessError):
            self.env['woow.mail.workspace'].with_user(self.reader).management_views()
        with self.assertRaises(AccessError):
            self.env['mail.fetch.failure'].with_user(self.reader).search_read([], ['status'])
        for method in ['action_retry_failed', 'action_mark_skipped']:
            with self.assertRaises(AccessError):
                getattr(self.failure.with_user(self.reader), method)()

    def test_original_failure_app_hidden_without_deleting_records(self):
        self.assertFalse(self.env.ref('mail_fetchmail_durable.menu_mail_fetch_failure').active)
        self.assertTrue(self.env.ref('woow_mail_workspace.workspace_menu').active)
        self.assertTrue(self.failure.exists())
        self.assertTrue(self.failure.raw_attachment_id.datas)

    def test_uncertain_still_cannot_retry(self):
        self.failure.status = 'uncertain'
        with self.assertRaises(UserError):
            self.failure.with_user(self.admin).action_retry_failed()
        self.assertEqual(self.failure.status, 'uncertain')

    def test_settings_and_forms_keep_operator_confirmation(self):
        form = self.env.ref('woow_mail_workspace.failure_form')._get_combined_arch()
        self.assertTrue(form.xpath('//button[@name="action_retry_failed"]/@confirm'))
        self.assertTrue(form.xpath('//button[@name="action_mark_skipped"]/@confirm'))
        settings = self.env.ref('woow_mail_workspace.mail_settings_view')._get_combined_arch()
        app = settings.xpath('//app[@name="woow_mail_workspace"]')[0]
        self.assertEqual(app.get('groups'), 'base.group_system')
        self.assertFalse(app.xpath('.//field[contains(@name,"password")]'))
        self.assertIn('587', ''.join(app.itertext()))
        self.assertIn('993', ''.join(app.itertext()))
