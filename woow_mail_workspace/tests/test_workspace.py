from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged, new_test_user
from odoo.tools import mute_logger


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

    @contextmanager
    def _smtp(self, refuse=False):
        """Fake ir.mail_server.send_email.

        Delivery is synchronous since 2026-10-15, so a test that forbids SMTP
        outright no longer describes the product -- it describes the queue-based
        design that silently delayed mail for up to an hour. These tests now let
        SMTP succeed and assert on what was handed to it.
        """
        sent = []

        def fake(self2, message, *args, **kwargs):
            if refuse:
                raise AssertionError("mailbox unavailable (simulated)")
            sent.append(message["To"])
            return "<fake-%d@probe.invalid>" % len(sent)

        with patch.object(type(self.env["ir.mail_server"]), "send_email", fake):
            yield sent

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

    def test_send_delivers_once_and_refuses_a_second_send(self):
        draft = self._draft()
        with self._smtp() as sent:
            draft.action_queue()
            self.assertEqual(draft.state, "queued")
            message = self.env["mail.message"].search([("model", "=", "woow.mail.conversation"), ("res_id", "=", draft.conversation_id.id)])
            self.assertEqual(len(message), 1)
            self.assertEqual(message.author_id, self.alice.partner_id)
            notification = self.env["mail.notification"].search([("mail_message_id", "=", message.id)])
            self.assertEqual(notification.mapped("notification_status"), ["sent"])
            self.assertEqual(len(sent), 1, "exactly one message handed to SMTP")
            with self.assertRaises(ValidationError):
                draft.action_queue()
            self.assertEqual(len(sent), 1, "a refused second send must not reach SMTP again")

    def test_send_to_inbox_preference_user_still_creates_email(self):
        """A recipient who handles notifications in Odoo must still receive mail.

        Regression for the production defect found on 2026-10-15: the recipient
        owned an Odoo user whose notification_type was 'inbox', so
        _notify_thread() classified them as an in-app recipient and created ZERO
        mail.mail rows. The sender saw success and nothing recorded a failure.
        """
        self.bob.notification_type = "inbox"
        draft = self._draft(partner_ids=[Command.set(self.bob.partner_id.ids)])
        with self._smtp() as sent:
            draft.action_queue()
        message = self.env["mail.message"].search(
            [("model", "=", "woow.mail.conversation"), ("res_id", "=", draft.conversation_id.id)])
        self.assertEqual(len(message), 1)
        notifications = self.env["mail.notification"].search([("mail_message_id", "=", message.id)])
        self.assertEqual(notifications.mapped("notification_type"), ["email"],
                         "an inbox-preference recipient must still get an email")
        self.assertEqual(notifications.mapped("notification_status"), ["sent"])
        self.assertEqual(len(sent), 1)
        self.assertIn("bob@example.invalid", sent[0])

    def test_send_hands_the_message_to_smtp_immediately(self):
        """Send must deliver now, not wait for the hourly queue cron.

        Regression for the production defect of 2026-10-15: mail.mail 5064 was
        created at 21:43 and the queue cron's nextcall was 22:24, so the message
        sat 41 minutes while the UI already said sent. Nothing in this test runs
        a cron -- if the send were still queued, state would be 'outgoing' and
        send_email would never have been called.
        """
        with self._smtp() as sent:
            draft = self._draft()
            draft.action_queue()
        self.assertEqual(len(sent), 1, "SMTP must be called during the request, not by a cron")
        message = self.env["mail.message"].search(
            [("model", "=", "woow.mail.conversation"), ("res_id", "=", draft.conversation_id.id)])
        notification = self.env["mail.notification"].search([("mail_message_id", "=", message.id)])
        self.assertEqual(notification.mapped("notification_status"), ["sent"])
        self.assertFalse(
            self.env["mail.mail"].search([("mail_message_id", "=", message.id),
                                          ("state", "=", "outgoing")]),
            "nothing may be left sitting in the outgoing queue")

    def test_send_reports_a_total_smtp_refusal_instead_of_claiming_success(self):
        """If the mail server refuses every recipient, say so and keep the draft."""
        draft = self._draft()
        with self._smtp(refuse=True):
            with mute_logger("odoo.addons.mail.models.mail_mail"):
                with self.assertRaises(ValidationError) as caught:
                    draft.action_queue()
        self.assertIn("sender@example.invalid", str(caught.exception))
        draft.invalidate_recordset(["state"])
        self.assertEqual(draft.state, "draft", "a refused send must leave the draft editable")

    def test_send_refuses_silently_losing_a_recipient(self):
        """If anything routes a recipient away from email, the send must fail loudly.

        Simulates a future regression (any cause: a preference, another module's
        channel, an override) by neutralising the force-email promotion. The
        sender must get an error naming the address, and nothing may be marked
        queued -- the 2026-10-15 defect was expensive precisely because it was
        silent.
        """
        self.bob.notification_type = "inbox"
        draft = self._draft(partner_ids=[Command.set(self.bob.partner_id.ids)])
        thread = type(self.env["mail.thread"])
        original = thread._notify_get_recipients

        def no_promotion(self2, message, msg_vals, **kwargs):
            recipients = original(self2, message, msg_vals, **kwargs)
            for recipient in recipients:
                recipient["notif"] = "inbox"
            return recipients

        with patch.object(thread, "_notify_get_recipients", no_promotion):
            with self.assertRaises(ValidationError) as caught:
                draft.action_queue()
        self.assertIn("bob@example.invalid", str(caught.exception))
        draft.invalidate_recordset(["state"])
        self.assertEqual(draft.state, "draft", "a failed send must leave the draft editable")

    def test_inbox_preference_untouched_outside_the_workspace(self):
        """The override is scoped: ERP chatter keeps honouring the preference.

        Without this, promoting 'inbox' to 'email' globally would start emailing
        internal staff about every document they follow -- a different product
        decision, and not this module's to make.
        """
        self.bob.notification_type = "inbox"
        self.contact.message_subscribe(partner_ids=self.bob.partner_id.ids)
        message = self.contact.with_user(self.alice).message_post(
            body="<p>Business chatter, not a workspace send</p>",
            partner_ids=self.bob.partner_id.ids,
            subtype_xmlid="mail.mt_comment",
            message_type="comment",
        )
        notifications = self.env["mail.notification"].search([("mail_message_id", "=", message.id)])
        self.assertEqual(notifications.mapped("notification_type"), ["inbox"])
        self.assertFalse(self.env["mail.mail"].search([("mail_message_id", "=", message.id)]))

    def test_reply_keeps_business_thread(self):
        action = self.service.compose(self.business.id, "reply", "accessible")
        draft = self.env["woow.mail.draft"].with_user(self.alice).browse(action["res_id"])
        with self._smtp():
            draft.action_queue()
        reply = self.env["mail.message"].search([("parent_id", "=", self.business.id)])
        self.assertEqual(len(reply), 1)
        self.assertEqual((reply.model, reply.res_id), ("res.partner", self.contact.id))
        self.assertFalse(draft.conversation_id)

    def test_new_conversation_recipient_can_reply_but_unrelated_admin_cannot(self):
        draft = self._draft(partner_ids=[Command.set(self.bob.partner_id.ids)])
        with self._smtp():
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
        with self._smtp():
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
