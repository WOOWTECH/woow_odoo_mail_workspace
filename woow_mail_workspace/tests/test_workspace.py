from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged, new_test_user


@tagged("post_install", "-at_install")
class TestWorkspace(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture_env = cls.env(context={**cls.env.context, "no_reset_password": True})
        cls.alice = new_test_user(fixture_env, login="workspace-alice", groups="base.group_user,base.group_partner_manager", email="alice@example.invalid")
        cls.bob = new_test_user(fixture_env, login="workspace-bob", groups="base.group_user", email="bob@example.invalid")
        cls.admin = new_test_user(fixture_env, login="workspace-admin", groups="base.group_system", email="admin@example.invalid")
        cls.portal = new_test_user(fixture_env, login="workspace-portal", groups="base.group_portal", email="portal@example.invalid")
        cls.external = cls.env["res.partner"].create({"name": "External sender", "email": "sender@example.invalid"})
        cls.contact = cls.env["res.partner"].create({"name": "Accessible business record"})
        cls.service = cls.env["woow.mail.workspace"].with_user(cls.alice)
        cls.private = cls.env["woow.mail.conversation"].with_user(cls.bob).with_context(mail_create_nolog=True).create({"name": "Bob private"})
        cls.incoming = cls._message("Alice incoming", partner_ids=[Command.set(cls.alice.partner_id.ids)])
        cls.sent = cls._message("Alice sent", author_id=cls.alice.partner_id.id)
        cls.business = cls._message("Accessible business", model="res.partner", res_id=cls.contact.id)
        cls.secret = cls._message("Bob private email", model=cls.private._name, res_id=cls.private.id)

    @classmethod
    def _message(cls, subject, **values):
        return cls.env["mail.message"].create({
            "subject": subject, "body": "<p>Fixture body only</p>",
            "message_type": "email", "subtype_id": cls.env.ref("mail.mt_comment").id,
            "author_id": cls.external.id, "email_from": cls.external.email_formatted,
            **values,
        })

    def _ids(self, service=None, **kwargs):
        return {r["id"] for r in (self.service if service is None else service).list_messages(box="all", **kwargs)["messages"]}

    def _draft(self, **values):
        return self.env["woow.mail.draft"].with_user(self.alice).create({
            "subject": "Offline fixture", "body": "<p>Never send externally</p>",
            "partner_ids": [Command.set(self.external.ids)], **values,
        })

    def test_personal_accessible_database_are_separate(self):
        self.assertEqual(self._ids(), {self.incoming.id, self.sent.id})
        self.assertIn(self.business.id, self._ids(scope="accessible"))
        self.assertNotIn(self.secret.id, self._ids(scope="accessible"))
        admin = self.service.with_user(self.admin)
        self.assertNotIn(self.secret.id, self._ids(admin))
        self.assertIn(self.secret.id, self._ids(admin, scope="database"))

    def test_notification_recipient_is_personal(self):
        message = self._message("Notification only")
        self.env["mail.notification"].create({"mail_message_id": message.id, "res_partner_id": self.alice.partner_id.id, "notification_type": "inbox"})
        self.assertIn(message.id, self._ids())

    def test_outgoing_without_subtype_is_visible(self):
        # Native mail.mail can retain email_outgoing rows without a subtype,
        # including standalone mail with no linked business model.
        for model, res_id in ((False, False), ("res.partner", self.contact.id)):
            message = self._message("Legacy outgoing without subtype",
                                    message_type="email_outgoing", subtype_id=False,
                                    author_id=self.alice.partner_id.id,
                                    model=model, res_id=res_id)
            self.assertIn(message.id, self._ids())
            self.assertIn(message.id, self._ids(self.service.with_user(self.admin), scope="database"))

    def test_database_guard_and_portal(self):
        with self.assertRaises(AccessError):
            self.service.list_messages(scope="database")
        with self.assertRaises(AccessError):
            self.service.with_user(self.portal).bootstrap()
        with self.assertRaises(ValidationError):
            self.service.list_messages(scope="sudo")

    def test_guessed_ids_cannot_read_write_or_forward(self):
        for call in (
            lambda: self.service.get_message(self.secret.id, "accessible"),
            lambda: self.service.set_state([self.secret.id], {"is_read": True}, "accessible"),
            lambda: self.service.compose(self.secret.id, "forward", "accessible"),
        ):
            with self.assertRaises(AccessError):
                call()

    def test_notes_chats_and_system_events_excluded(self):
        for values in ({"subtype_id": self.env.ref("mail.mt_note").id}, {"is_internal": True},
                       {"model": "discuss.channel", "res_id": 1}, {"message_type": "notification"}):
            message = self._message("Excluded", partner_ids=[Command.set(self.alice.partner_id.ids)], **values)
            self.assertNotIn(message.id, self._ids())

    def test_state_is_private_and_native_flags_untouched(self):
        notification = self.env["mail.notification"].create({"mail_message_id": self.incoming.id,
            "res_partner_id": self.alice.partner_id.id, "notification_type": "inbox", "is_read": False})
        before = self.incoming.write_date
        self.service.set_state([self.incoming.id], {"is_read": True, "starred": True, "trashed": True})
        self.assertTrue(self.service.get_message(self.incoming.id)["is_read"])
        self.assertFalse(notification.is_read)
        self.assertEqual(self.incoming.write_date, before)
        other = self.service.with_user(self.admin).get_message(self.incoming.id, "database")
        self.assertFalse(other["starred"])
        self.assertFalse(other["trashed"])
        self.service.set_state([self.incoming.id], {"trashed": False})
        self.assertIn(self.incoming.id, self._ids())
        self.assertTrue(self.incoming.exists())

    def test_folder_ownership_and_moving(self):
        folder = self.env["woow.mail.folder"].with_user(self.alice).create({"name": "Projects"})
        with self.assertRaises(AccessError):
            self.env["woow.mail.folder"].with_user(self.bob).create({"name": "Spoof", "user_id": self.alice.id})
        with self.assertRaises(AccessError):
            folder.write({"user_id": self.bob.id})
        with self.assertRaises(AccessError):
            self.service.with_user(self.bob).set_state([self.business.id], {"folder_id": folder.id}, "accessible")
        self.service.set_state([self.incoming.id], {"folder_id": folder.id})
        self.assertEqual([r["id"] for r in self.service.list_messages(box="folder", folder_id=folder.id)["messages"]], self.incoming.ids)
        self.assertNotIn(self.incoming.id, {r["id"] for r in self.service.list_messages()["messages"]})

    def test_direct_state_acl_denied(self):
        with self.assertRaises(AccessError):
            self.env["woow.mail.state"].with_user(self.alice).search([])

    def test_explicit_unread_overrides_sent_default(self):
        self.assertTrue(self.service.get_message(self.sent.id)["is_read"])
        self.assertEqual(self.service.list_messages(box="sent", unread=True)["messages"], [])
        self.service.set_state(self.sent.ids, {"is_read": False})
        self.assertEqual([m["id"] for m in self.service.list_messages(box="sent", unread=True)["messages"]], self.sent.ids)

    def test_discard_only_own_unsent_draft(self):
        draft = self._draft()
        with self.assertRaises(AccessError):
            draft.with_user(self.bob).action_discard()
        draft.action_discard()
        self.assertFalse(draft.exists())

    def test_invalid_state_payload(self):
        for ids, values in (([True], {"is_read": True}), ([self.incoming.id], {"user_id": self.bob.id}),
                            ([self.incoming.id], {"is_read": "false"})):
            with self.assertRaises(ValidationError):
                self.service.set_state(ids, values)

    def test_authorized_pagination_and_search(self):
        visible = [self._message("Pagination fixture", partner_ids=[Command.set(self.alice.partner_id.ids)]).id for _ in range(45)]
        for _ in range(43):
            self._message("Pagination fixture", model=self.private._name, res_id=self.private.id)
        page = self.service.list_messages(scope="accessible", box="all", search="Pagination fixture")
        self.assertEqual(len(page["messages"]), 40)
        self.assertTrue(page["has_more"])
        page2 = self.service.list_messages(scope="accessible", box="all", search="Pagination fixture", offset=40)
        self.assertEqual(len(page2["messages"]), 5)
        self.assertFalse(page2["has_more"])
        self.assertEqual({r["id"] for r in page["messages"] + page2["messages"]}, set(visible))
        # Same assertion input against the old limit-before-access behavior fails.
        native = self.env["mail.message"].with_user(self.alice).search(
            self.service._domain("accessible") + [("subject", "=", "Pagination fixture")],
            order="date desc, id desc", limit=41)
        self.assertLess(len(native), 40)

    def test_draft_ownership_and_protected_fields(self):
        draft = self._draft()
        with self.assertRaises(AccessError):
            draft.with_user(self.bob).read(["body"])
        with self.assertRaises(AccessError):
            draft.with_user(self.bob).action_queue()
        for vals in ({"state": "queued"}, {"user_id": self.bob.id}, {"mode": "reply"}):
            with self.assertRaises(AccessError):
                draft.write(vals)
        with self.assertRaises(AccessError):
            self._draft(user_id=self.bob.id)

    def test_send_queues_once_without_smtp(self):
        draft = self._draft()
        with patch.object(type(self.env["ir.mail_server"]), "send_email", side_effect=AssertionError("SMTP forbidden in isolated test")) as smtp:
            draft.action_queue()
            self.assertEqual(draft.state, "queued")
            message = self.env["mail.message"].search([("model", "=", "woow.mail.conversation"), ("res_id", "=", draft.conversation_id.id)])
            self.assertEqual(len(message), 1)
            self.assertEqual(message.author_id, self.alice.partner_id)
            outgoing = self.env["mail.mail"].search([("mail_message_id", "=", message.id)])
            self.assertEqual(len(outgoing), 1)
            self.assertEqual(outgoing.state, "outgoing")
            with self.assertRaises(ValidationError):
                draft.action_queue()
            smtp.assert_not_called()

    def test_reply_keeps_business_thread(self):
        action = self.service.compose(self.business.id, "reply", "accessible")
        draft = self.env["woow.mail.draft"].with_user(self.alice).browse(action["res_id"])
        with patch.object(type(self.env["ir.mail_server"]), "send_email", side_effect=AssertionError("SMTP forbidden")):
            draft.action_queue()
        reply = self.env["mail.message"].search([("parent_id", "=", self.business.id)])
        self.assertEqual(len(reply), 1)
        self.assertEqual((reply.model, reply.res_id), ("res.partner", self.contact.id))
        self.assertFalse(draft.conversation_id)

    def test_new_conversation_recipient_can_reply_but_unrelated_admin_cannot(self):
        draft = self._draft(partner_ids=[Command.set(self.bob.partner_id.ids)])
        with patch.object(type(self.env["ir.mail_server"]), "send_email", side_effect=AssertionError("SMTP forbidden")):
            draft.action_queue()
            original = self.env["mail.message"].search([("model", "=", "woow.mail.conversation"), ("res_id", "=", draft.conversation_id.id)])
            recipient = self.service.with_user(self.bob)
            self.assertTrue(recipient.get_message(original.id)["can_reply"])
            self.assertFalse(self.service.with_user(self.admin).get_message(original.id, "database")["can_reply"])
            action = recipient.compose(original.id, "reply")
            reply_draft = self.env["woow.mail.draft"].with_user(self.bob).browse(action["res_id"])
            reply_draft.action_queue()
            reply = self.env["mail.message"].search([("parent_id", "=", original.id)])
            self.assertEqual(len(reply), 1)
            self.assertEqual(reply.author_id, self.bob.partner_id)
            self.assertEqual(reply.res_id, draft.conversation_id.id)
        with self.assertRaises(AccessError):
            draft.conversation_id.with_user(self.bob).write({"participant_ids": [Command.link(self.admin.partner_id.id)]})

    def test_admin_read_does_not_grant_reply(self):
        admin = self.service.with_user(self.admin)
        self.assertFalse(admin.get_message(self.secret.id, "database")["can_reply"])
        with self.assertRaises(AccessError):
            admin.compose(self.secret.id, "reply", "database")

    def test_forward_copies_attachment_and_uses_new_thread(self):
        original = self.env["ir.attachment"].create({"name": "fixture.txt", "raw": b"fixture only", "res_model": self.contact._name, "res_id": self.contact.id, "public": True})
        self.business.attachment_ids = [Command.link(original.id)]
        action = self.service.compose(self.business.id, "forward", "accessible")
        draft = self.env["woow.mail.draft"].with_user(self.alice).browse(action["res_id"])
        self.assertEqual(len(draft.attachment_ids), 1)
        self.assertNotEqual(draft.attachment_ids.id, original.id)
        self.assertFalse(draft.attachment_ids.public)
        self.assertEqual(original.res_id, self.contact.id)
        draft.partner_ids = [Command.set(self.external.ids)]
        with patch.object(type(self.env["ir.mail_server"]), "send_email", side_effect=AssertionError("SMTP forbidden")):
            draft.action_queue()
        self.assertTrue(draft.conversation_id)
        self.assertEqual(original.res_model, "res.partner")
        self.assertEqual(original.res_id, self.contact.id)

    def test_foreign_attachment_cannot_be_reassigned(self):
        draft = self._draft()
        attachment = self.env["ir.attachment"].create({"name": "foreign.txt", "raw": b"foreign", "res_model": "res.partner", "res_id": self.contact.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            draft.attachment_ids = [Command.set(attachment.ids)]
        self.assertEqual(attachment.res_model, "res.partner")

    def test_reply_rechecks_revoked_record_access(self):
        own = self.env["woow.mail.conversation"].with_user(self.alice).with_context(mail_create_nolog=True).create({"name": "Owned"})
        original = self._message("Owned source", model=own._name, res_id=own.id, partner_ids=[Command.set(self.alice.partner_id.ids)])
        action = self.service.compose(original.id, "reply")
        draft = self.env["woow.mail.draft"].with_user(self.alice).browse(action["res_id"])
        self.cr.execute("UPDATE woow_mail_conversation SET user_id = %s WHERE id = %s", [self.bob.id, own.id])
        own.invalidate_recordset(["user_id"])
        with self.assertRaises(AccessError):
            draft.action_queue()

    def test_html_sanitization(self):
        self.incoming.body = '<p>Safe</p><script>alert(1)</script><img src="https://example.invalid/pixel" onerror="alert(1)"/>'
        body = self.service.get_message(self.incoming.id)["body"]
        self.assertIn("Safe", body)
        self.assertNotIn("<script", body)
        self.assertNotIn("onerror", body)

    def test_composer_quote_cannot_load_remote_pixels(self):
        self.business.body = '<p>Quote me</p><img src="https://example.invalid/pixel"/>'
        action = self.service.compose(self.business.id, "reply", "accessible")
        draft = self.env["woow.mail.draft"].with_user(self.alice).browse(action["res_id"])
        self.assertIn("Quote me", draft.body)
        self.assertNotIn("<img", draft.body)

    def test_admin_forward_uses_own_private_copy(self):
        original = self.env["ir.attachment"].create({"name": "admin-copy.txt", "raw": b"private fixture", "res_model": self.private._name, "res_id": self.private.id})
        self.secret.attachment_ids = [Command.link(original.id)]
        action = self.service.with_user(self.admin).compose(self.secret.id, "forward", "database")
        draft = self.env["woow.mail.draft"].with_user(self.admin).browse(action["res_id"])
        self.assertEqual(draft.attachment_ids.create_uid, self.admin)
        self.assertEqual(draft.attachment_ids.res_id, draft.id)
        self.assertFalse(draft.attachment_ids.public)
        self.assertEqual(original.res_id, self.private.id)
