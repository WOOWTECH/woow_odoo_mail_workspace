# -*- coding: utf-8 -*-
"""Odoo registry tests for mail_fetchmail_durable."""

import time
import logging
from unittest.mock import patch

from odoo import SUPERUSER_ID
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "mail_fetchmail_durable")
class TestMailFetchFailureLedger(TransactionCase):

    def setUp(self):
        super().setUp()
        self.model = self.env["mail.fetch.failure"]
        self.server = self.env["fetchmail.server"].create({
            "name": "Unit incoming",
            "server_type": "imap",
            "server": "imap.example.test",
            "port": 993,
            "is_ssl": True,
            "user": "unit@example.test",
            "password": "not-a-real-secret",
            "object_id": self.env["ir.model"]._get("mail.thread").id,
            "state": "done",
        })
        self.payload = b"Message-ID: <unit@example.test>\r\nFrom: a@example.test\r\nTo: b@example.test\r\nSubject: Unit\r\n\r\nBody"
        self.payload_changed = b"Message-ID: <unit@example.test>\r\nFrom: a@example.test\r\nTo: b@example.test\r\nSubject: Unit\r\n\r\nChanged"

    def test_begin_processing_creates_unique_processing_row(self):
        rec, should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="42", uid_validity="100")
        self.assertTrue(should_process)
        self.assertEqual(rec.status, "processing")
        self.assertEqual(rec.raw_size, len(self.payload))
        again, should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="42", uid_validity="100")
        self.assertFalse(should_process)
        self.assertEqual(again.id, rec.id)
        self.assertEqual(again.status, "uncertain")

    def test_failure_and_manual_retry_state_machine_guards_uncertain(self):
        rec, _should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="43", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        self.assertEqual(rec.status, "failed")
        rec.write({"status": "uncertain"})
        with self.assertRaises(UserError):
            rec.action_retry_failed()
        self.assertEqual(rec.status, "uncertain", "uncertain rows are not retried by generic failed retry action")

    def test_same_uid_allowed_for_different_servers(self):
        other = self.server.copy({"name": "Other incoming", "user": "other@example.test"})
        first, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="same", uid_validity="100")
        second, _ = self.model._begin_processing(other, "imap", self.payload, remote_uid="same", uid_validity="100")
        self.assertNotEqual(first.id, second.id)

    def test_uidvalidity_reset_allows_reused_uid(self):
        first, should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="77", uid_validity="100")
        self.assertTrue(should_process)
        second, should_process = self.model._begin_processing(self.server, "imap", self.payload_changed, remote_uid="77", uid_validity="101")
        self.assertTrue(should_process)
        self.assertNotEqual(first.id, second.id)

    def test_same_uidvalidity_payload_mismatch_fails_closed(self):
        first, should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="88", uid_validity="100")
        self.assertTrue(should_process)
        second, should_process = self.model._begin_processing(self.server, "imap", self.payload_changed, remote_uid="88", uid_validity="100")
        self.assertFalse(should_process)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.status, "uncertain")
        self.assertIn("payload fingerprint changed", second.last_error)

    def test_oversize_message_fails_closed_before_ledger_or_seen(self):
        with self.assertRaises(UserError):
            self.model._begin_processing(self.server, "imap", self.payload, remote_uid="90", uid_validity="100", max_bytes=10)
        self.assertFalse(self.model.search([("remote_uid", "=", "90")]))

    def test_pop3_is_explicitly_unsupported_by_durable_fetcher(self):
        self.server.write({"server_type": "pop", "port": 995})
        with self.assertRaises(ValidationError):
            self.server.fetch_mail(raise_exception=True)

    def test_imap_size_guard_runs_before_full_body_fetch_or_seen(self):
        class OversizeIMAP:
            def __init__(self):
                self.commands = []
                self.seen = False

            def select(self, mailbox="INBOX"):
                self.commands.append(("SELECT", mailbox))
                return "OK", [b"1"]

            def response(self, key):
                return "UIDVALIDITY", [b"100"]

            def uid(self, command, *args):
                self.commands.append((command, args))
                if command == "SEARCH":
                    return "OK", [b"123"]
                if command == "FETCH" and args[-1] == "(UID RFC822.SIZE)":
                    return "OK", [(b"1 (UID 123 RFC822.SIZE 999999999)", b"")]
                if command == "FETCH" and "BODY.PEEK" in args[-1]:
                    raise AssertionError("BODY.PEEK[] must not run for oversized messages")
                if command == "STORE":
                    self.seen = True
                    return "OK", [b"flags"]
                raise AssertionError(command)

            def close(self):
                return "OK", []

            def logout(self):
                return "BYE", []

        imap = OversizeIMAP()
        self.env["ir.config_parameter"].sudo().set_param("mail_fetchmail_durable.max_raw_message_bytes", "10")
        with patch.object(type(self.env["fetchmail.server"]), "connect", lambda _s: imap):
            self.server.fetch_mail(raise_exception=True)
        self.assertIn(("FETCH", (b"123", "(UID RFC822.SIZE)")), imap.commands)
        self.assertFalse(any(cmd == "STORE" for cmd, _args in imap.commands if isinstance((cmd, _args), tuple)))
        self.assertFalse(self.model.search([("remote_uid", "=", "123")]))
        self.assertFalse(imap.seen)

    def test_redaction_removes_credentials_from_failure_ledger(self):
        secret = "super-secret-ledger-password"
        self.server.password = secret
        rec, _should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="91", uid_validity="100")
        rec._mark_failed(RuntimeError("password=%s token=abc123 visible" % secret))
        self.assertNotIn(secret, rec.last_error)
        self.assertNotIn("abc123", rec.last_error)
        self.assertIn("[REDACTED]", rec.last_error)

    def test_manual_retry_respects_concurrent_server_lock(self):
        rec, _should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="92", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        with self.server._durable_server_session_lock():
            with self.assertRaises(ValidationError):
                rec.action_retry_failed()
        self.assertEqual(rec.status, "failed")

    def test_public_actions_reject_ordinary_user(self):
        rec, _should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="99", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        user = self.env["res.users"].create({
            "name": "Ordinary Ledger User",
            "login": "ordinary-ledger-user@example.test",
            "email": "ordinary-ledger-user@example.test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(AccessError):
            rec.with_user(user).action_mark_skipped()
        with self.assertRaises(AccessError):
            rec.with_user(user).action_retry_failed()
        with self.assertRaises(AccessError):
            rec.with_user(user).read(['status'])
        with self.assertRaises(AccessError):
            rec.raw_attachment_id.with_user(user).read(['datas'])

    def test_traceback_is_redacted_like_last_error(self):
        secret = "tb-secret-ledger-password"
        self.server.password = secret
        rec, _should_process = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="93", uid_validity="100")
        try:
            raise RuntimeError("auth failed for %s; Authorization: Bearer abcdefgh12345678; imap://u:%s@host" % (secret, secret))
        except RuntimeError as exc:
            rec._mark_failed(exc)
        self.assertEqual(rec.status, "failed")
        self.assertIn("RuntimeError", rec.last_traceback, "traceback detail must be kept for debugging")
        self.assertNotIn(secret, rec.last_traceback)
        self.assertNotIn("abcdefgh12345678", rec.last_traceback)
        self.assertIn("[REDACTED]", rec.last_traceback)
        self.assertNotIn(secret, rec.last_error)

    def test_fetch_mail_log_does_not_leak_password_in_traceback(self):
        secret = "log-secret-ledger-password"
        self.server.password = secret
        try:
            raise RuntimeError("boom %s" % secret)
        except RuntimeError as exc:
            text = self.server._durable_safe_traceback(exc)
        self.assertNotIn(secret, text)
        self.assertIn("RuntimeError", text)

    def test_mark_skipped_keeps_each_records_own_note(self):
        first, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="94", uid_validity="100")
        second, _ = self.model._begin_processing(self.server, "imap", self.payload_changed, remote_uid="95", uid_validity="100")
        first._mark_failed(RuntimeError("boom"))
        second._mark_failed(RuntimeError("boom"))
        first.note = "note-one"
        second.note = "note-two"
        (first | second).action_mark_skipped()
        self.assertEqual(first.status, "skipped")
        self.assertEqual(second.status, "skipped")
        self.assertEqual(first.note, "note-one\nManually skipped.")
        self.assertEqual(second.note, "note-two\nManually skipped.")

    def _retry_capturing_message_process(self, rec):
        calls = []

        def fake_message_process(thread, model, message, **kwargs):
            calls.append({"model": model, "uid": thread.env.uid, "su": thread.env.su})
            return 4242

        # The test cursor forbids commit/rollback; the production code commits as a durable fence.
        with patch.object(type(self.env["mail.thread"]), "message_process", fake_message_process), \
                patch.object(self.env.cr, "commit", lambda: None), \
                patch.object(self.env.cr, "rollback", lambda: None):
            rec.action_retry_failed()
        return calls

    def test_begin_processing_records_target_model(self):
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="96", uid_validity="100")
        self.assertEqual(rec.target_model, "mail.thread")

    def test_retry_uses_recorded_target_model_after_server_model_change(self):
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="97", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        self.server.object_id = self.env["ir.model"]._get("res.partner")
        with self.assertLogs("odoo.addons.mail_fetchmail_durable.models.mail_fetch_failure", level="WARNING") as logs:
            calls = self._retry_capturing_message_process(rec)
        self.assertEqual([c["model"] for c in calls], ["mail.thread"])
        self.assertEqual(rec.status, "succeeded")
        self.assertEqual(rec.res_model, "mail.thread")
        self.assertTrue(any("res.partner" in line for line in logs.output))

    def test_retry_legacy_row_without_target_model_uses_server_model(self):
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="98", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        rec.write({"target_model": False, "target_model_recorded": False})
        self.server.object_id = self.env["ir.model"]._get("res.partner")
        calls = self._retry_capturing_message_process(rec)
        self.assertEqual([c["model"] for c in calls], ["res.partner"])

    def test_retry_runs_message_process_as_the_cron_user(self):
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="100", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        admin = self.env.ref("base.user_admin")
        self.assertNotEqual(admin.id, self.model._fetch_cron_uid(), "the triggering user must differ from the cron identity")
        calls = self._retry_capturing_message_process(rec.with_user(admin))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["uid"], self.model._fetch_cron_uid())
        self.assertNotEqual(calls[0]["uid"], admin.id)
        if calls[0]["uid"] == SUPERUSER_ID:
            self.assertTrue(calls[0]["su"], "cron identity is the superuser")
        cron = self.env.ref("mail.ir_cron_mail_gateway_action", raise_if_not_found=False)
        if cron:
            self.assertEqual(calls[0]["uid"], cron.sudo().user_id.id)

    def test_retry_still_requires_settings_group(self):
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="101", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        user = self.env["res.users"].create({
            "name": "Ordinary Retry User",
            "login": "ordinary-retry-user@example.test",
            "email": "ordinary-retry-user@example.test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(AccessError):
            rec.with_user(user).action_retry_failed()

    def test_retry_and_skip_reject_non_settings_user_past_acl(self):
        """Exercise the has_group('base.group_system') guard itself, with the ACL check bypassed."""
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="102", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        user = self.env["res.users"].create({
            "name": "Non Settings User",
            "login": "non-settings-user@example.test",
            "email": "non-settings-user@example.test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with patch.object(type(self.model), "check_access", lambda *a, **k: None):
            with self.assertRaises(UserError) as ctx:
                rec.with_user(user).action_retry_failed()
            self.assertIn("Settings users", str(ctx.exception))
            with self.assertRaises(UserError) as ctx:
                rec.with_user(user).action_mark_skipped()
            self.assertIn("Settings users", str(ctx.exception))
        self.assertEqual(rec.status, "failed")

    def test_retry_rejects_unavailable_or_non_mail_target_model(self):
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="103", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        for bad in ("does.not.exist", "res.country"):
            rec.write({"target_model": bad})
            with self.assertRaises(UserError):
                rec.action_retry_failed()
            self.assertEqual(rec.status, "failed", "row must stay failed when the target model is unusable")

    def test_retry_alias_routing_row_keeps_empty_model(self):
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="104", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))
        rec.write({"target_model": False, "target_model_recorded": True})
        self.server.object_id = self.env["ir.model"]._get("res.partner")
        calls = self._retry_capturing_message_process(rec)
        self.assertEqual([c["model"] for c in calls], [False], "recorded empty model = alias routing, not the legacy fallback")

    def test_retry_non_user_error_is_reraised_redacted(self):
        secret = "retry-secret-password"
        self.server.password = secret
        rec, _ = self.model._begin_processing(self.server, "imap", self.payload, remote_uid="105", uid_validity="100")
        rec._mark_failed(RuntimeError("boom"))

        def failing(thread, model, message, **kwargs):
            raise RuntimeError("bad auth %s" % secret)

        with patch.object(type(self.env["mail.thread"]), "message_process", failing), \
                patch.object(self.env.cr, "commit", lambda: None), \
                patch.object(self.env.cr, "rollback", lambda: None):
            with self.assertRaises(UserError) as ctx:
                rec.action_retry_failed()
        self.assertNotIn(secret, str(ctx.exception))
        self.assertEqual(rec.status, "failed")
        self.assertNotIn(secret, rec.last_traceback)

    def test_redaction_is_linear_and_does_not_overredact_prose(self):
        from odoo.addons.mail_fetchmail_durable.models.mail_fetch_failure import redact_secret_text
        started = time.monotonic()
        for text in ("a." * 50000, "password " * 20000, "http://" + "a" * 100000, "Bearer " * 20000, "AUTHENTICATE " * 20000):
            redact_secret_text(text, ["x" * 8])
            redact_secret_text(text, [], max_chars=64 * 1024, keep="tail")
        self.assertLess(time.monotonic() - started, 5.0)
        quick = time.monotonic()
        redact_secret_text("a." * 50000)
        self.assertLess(time.monotonic() - quick, 1.0)
        self.assertEqual(redact_secret_text("Basic configuration error"), "Basic configuration error")
        self.assertEqual(redact_secret_text("Authorization: Basic dXNlcjpwYXNz"), "Authorization: Basic [REDACTED]")
        self.assertEqual(redact_secret_text("Authorization: Bearer abcdefgh12345678"), "Authorization: Bearer [REDACTED]")
        self.assertLessEqual(len(redact_secret_text("z" * 100000)), 16 * 1024 + 32)

    def test_fetch_mail_own_log_output_is_redacted(self):
        secret = "fetchlog-secret-password"
        self.server.password = secret
        payload = self.payload

        class OneMessageIMAP:
            def select(self, mailbox="INBOX"):
                return "OK", [b"1"]

            def response(self, key):
                return "UIDVALIDITY", [b"100"]

            def uid(self, command, *args):
                if command == "SEARCH":
                    return "OK", [b"7"]
                if command == "FETCH" and args[-1] == "(UID RFC822.SIZE)":
                    return "OK", [(b"1 (UID 7 RFC822.SIZE 50)", b"")]
                if command == "FETCH":
                    return "OK", [(b"1 (UID 7)", payload)]
                if command == "STORE":
                    return "OK", [b"flags"]
                raise AssertionError(command)

            def close(self):
                return "OK", []

            def logout(self):
                return "BYE", []

        def failing(thread, model, message, **kwargs):
            raise RuntimeError("cannot route: password=%s and %s" % (secret, secret))

        # The production side-transaction cursors cannot see rows created inside
        # the test transaction, so run the ledger helpers on the test env instead.
        def begin(srv, protocol, payload, remote_uid=False, mailbox="INBOX", uid_validity=""):
            rec, should = srv.env["mail.fetch.failure"]._begin_processing(
                srv, protocol, payload, remote_uid=remote_uid, mailbox=mailbox, uid_validity=uid_validity)
            return rec.id, should

        def mark_failed(srv, rec_id, error):
            srv.env["mail.fetch.failure"].browse(rec_id)._mark_failed(error)

        server_cls = type(self.env["fetchmail.server"])
        with patch.object(server_cls, "connect", lambda _s: OneMessageIMAP()), \
                patch.object(server_cls, "_durable_begin", begin), \
                patch.object(server_cls, "_durable_mark_failed", mark_failed), \
                patch.object(type(self.env["mail.thread"]), "message_process", failing), \
                patch.object(self.env.cr, "commit", lambda: None), \
                patch.object(self.env.cr, "rollback", lambda: None), \
                self.assertLogs("odoo.addons.mail_fetchmail_durable.models.fetchmail_server", level=logging.INFO) as logs:
            self.server.fetch_mail(raise_exception=True)
        joined = "\n".join(logs.output)
        self.assertIn("Failed to process durable IMAP mail", joined)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn(secret, joined)
        ledger = self.model.search([("remote_uid", "=", "7")])
        self.assertEqual(ledger.status, "failed")
        self.assertNotIn(secret, ledger.last_traceback)

    def test_safe_traceback_helper_never_raises(self):
        class Boom:
            def __str__(self):
                raise ValueError("no str")

        text = self.server._durable_safe_traceback(RuntimeError(Boom()), "irrelevant-pass")
        self.assertIsInstance(text, str)
