# -*- coding: utf-8 -*-

import hashlib
import logging
import re
from contextlib import contextmanager

from imaplib import IMAP4

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import ValidationError

from .mail_fetch_failure import TRACEBACK_TEXT_LIMIT, format_exception_text, redact_secret_text

_logger = logging.getLogger(__name__)
MAX_RAW_MESSAGE_BYTES_DEFAULT = 25 * 1024 * 1024


class FetchmailServer(models.Model):
    _inherit = "fetchmail.server"

    def _durable_lock_key(self):
        self.ensure_one()
        return int.from_bytes(hashlib.blake2b(f"mail_fetch:{self._cr.dbname}:{self.id}".encode(), digest_size=8).digest(), "big", signed=True)

    @contextmanager
    def _durable_server_session_lock(self):
        """Hold a per DB+server advisory lock across business commits/rollbacks.

        This deliberately uses a dedicated cursor and PostgreSQL session-level
        lock.  Transaction-level locks are released by the commits required by
        Odoo's fetchmail processing and therefore are not sufficient here.
        """
        self.ensure_one()
        lock_cr = self.pool.cursor()
        key = self._durable_lock_key()
        locked = False
        try:
            lock_cr.execute("SELECT pg_try_advisory_lock(%s)", [key])
            locked = bool(lock_cr.fetchone()[0])
            if not locked:
                raise ValidationError(_("This incoming server is already being processed; retry later."))
            yield lock_cr
        finally:
            if locked:
                try:
                    # Recover an aborted lock-cursor transaction before unlocking.
                    # Session locks survive rollback and MUST NOT leak into the pool.
                    lock_cr.rollback()
                    lock_cr.execute("SELECT pg_advisory_unlock(%s)", [key])
                    lock_cr.commit()
                except Exception:
                    # A pooled live connection must never retain this session lock.
                    lock_cr._cnx.close()
                    _logger.warning("Closed connection after durable fetchmail unlock failure for server %s", self.id, exc_info=True)
            lock_cr.close()

    def _durable_env(self):
        cr = self.pool.cursor()
        env = api.Environment(cr, SUPERUSER_ID, dict(self.env.context, durable_fetchmail_side_tx=True))
        return cr, env

    durable_mailbox = fields.Char(
        string="Durable IMAP mailbox",
        default="INBOX",
        help="IMAP mailbox/folder selected by the durable fetcher. Use an isolated folder for acceptance fixtures; POP3 is intentionally unsupported by this addon.",
    )

    def _durable_mailbox(self):
        self.ensure_one()
        return (self.durable_mailbox or "INBOX").strip() or "INBOX"

    def _durable_max_message_bytes(self):
        value = self.env["ir.config_parameter"].sudo().get_param("mail_fetchmail_durable.max_raw_message_bytes", str(MAX_RAW_MESSAGE_BYTES_DEFAULT))
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = MAX_RAW_MESSAGE_BYTES_DEFAULT
        return max(1, parsed)

    def _durable_begin(self, protocol, payload, remote_uid=False, mailbox="INBOX", uid_validity=""):
        cr, env = self._durable_env()
        try:
            isolated_server = env["fetchmail.server"].browse(self.id)
            max_bytes = isolated_server._durable_max_message_bytes()
            rec, should_process = env["mail.fetch.failure"]._begin_processing(
                isolated_server, protocol, payload, remote_uid=remote_uid, mailbox=mailbox, uid_validity=uid_validity, max_bytes=max_bytes
            )
            rec_id = rec.id
            cr.commit()
            return rec_id, should_process
        finally:
            cr.close()

    def _durable_mark_failed(self, rec_id, error):
        cr, env = self._durable_env()
        try:
            env["mail.fetch.failure"].browse(rec_id)._mark_failed(error)
            cr.commit()
        finally:
            cr.close()

    def _durable_mark_succeeded(self, rec_id, res_id=False):
        cr, env = self._durable_env()
        try:
            rec = env["mail.fetch.failure"].browse(rec_id)
            rec._mark_succeeded(res_id=res_id, res_model=rec.target_model or rec.server_id.object_id.model)
            cr.commit()
        finally:
            cr.close()

    @staticmethod
    def _extract_imap_uid(fetch_header, fallback):
        header = fetch_header.decode("ascii", "ignore") if isinstance(fetch_header, bytes) else str(fetch_header)
        match = re.search(r"\bUID\s+(\d+)\b", header)
        return match.group(1) if match else fallback.decode() if isinstance(fallback, bytes) else str(fallback)

    @staticmethod
    def _extract_imap_size(fetch_header):
        header = fetch_header.decode("ascii", "ignore") if isinstance(fetch_header, bytes) else str(fetch_header)
        match = re.search(r"\bRFC822\.SIZE\s+(\d+)\b", header, re.I)
        return int(match.group(1)) if match else False

    @staticmethod
    def _redact_secret_text(text, secrets=()):
        return redact_secret_text(text, secrets)

    def _durable_safe_traceback(self, exc, password=None):
        """Traceback of ``exc`` with the server password and credential patterns removed, for logging.

        Never raises: this runs inside except blocks, possibly on a broken cursor.
        Pass ``password`` (read before the fetch loop) to avoid touching the ORM here.
        """
        try:
            if password is None:
                password = self.password
            return redact_secret_text(format_exception_text(exc), [password], min_secret_len=4, max_chars=TRACEBACK_TEXT_LIMIT, keep="tail")
        except Exception:  # noqa: BLE001
            return "<traceback unavailable: %s>" % type(exc).__name__

    @staticmethod
    def _extract_uidvalidity(select_response, imap_server):
        candidates = []
        if select_response:
            candidates.append(str(select_response))
        try:
            typ, data = imap_server.response("UIDVALIDITY")
            typ = (typ or "").upper()
            if data:
                if typ == "UIDVALIDITY":
                    direct = re.search(r"\d+", str(data))
                    if direct:
                        return direct.group(0)
                if typ == "OK":
                    candidates.append(str(data))
        except Exception:
            pass
        text = " ".join(candidates)
        match = re.search(r"UIDVALIDITY\D+(\d+)", text, re.I)
        return match.group(1) if match else False

    def _imap_seen_uid(self, imap_server, uid):
        imap_server.uid("STORE", uid, "+FLAGS", "\\Seen")

    def fetch_mail(self, raise_exception=True):
        """Durable replacement for Odoo's cron fetch."""
        context = {"fetchmail_cron_running": True}
        MailThread = self.env["mail.thread"]
        for server in self:
            with server._durable_server_session_lock():
                _logger.info("start durable checking for new emails on %s server %s", server.server_type, server.name)
                secret_password, secret_user = server.password, server.user  # read once; used by except-path redaction
                context["default_fetchmail_server_id"] = server.id
                count, failed, skipped = 0, 0, 0
                connection_type = server._get_connection_type()
                if connection_type == "imap":
                    imap_server = None
                    try:
                        imap_server = server.connect()
                        mailbox = server._durable_mailbox()
                        select_response = imap_server.select(mailbox)
                        uid_validity = self._extract_uidvalidity(select_response, imap_server)
                        if not uid_validity:
                            raise ValidationError(_("IMAP server %s did not expose UIDVALIDITY; refusing durable fetch to avoid UID reuse ambiguity.", server.display_name))
                        result, data = imap_server.uid("SEARCH", None, "(UNSEEN)")
                        if result != "OK":
                            raise ValidationError(_("IMAP UID SEARCH failed for server %s", server.display_name))
                        max_bytes = server._durable_max_message_bytes()
                        for uid in data[0].split():
                            result, size_data = imap_server.uid("FETCH", uid, "(UID RFC822.SIZE)")
                            if result != "OK" or not size_data or not isinstance(size_data[0], (bytes, tuple)):
                                raise ValidationError(_("IMAP UID size check failed for message %s on %s", uid, server.display_name))
                            size_header = size_data[0][0] if isinstance(size_data[0], tuple) else size_data[0]
                            remote_uid = self._extract_imap_uid(size_header, uid)
                            remote_size = self._extract_imap_size(size_header)
                            if remote_size and remote_size > max_bytes:
                                failed += 1
                                _logger.warning(
                                    "Refusing oversized durable IMAP message on server %s mailbox %s uid %s: %s bytes exceeds %s; message left unread.",
                                    server.id, mailbox, remote_uid, remote_size, max_bytes,
                                )
                                continue
                            result, data = imap_server.uid("FETCH", uid, "(UID BODY.PEEK[])")
                            if result != "OK" or not data or not isinstance(data[0], tuple):
                                raise ValidationError(_("IMAP UID FETCH failed for message %s on %s", uid, server.display_name))
                            remote_uid = self._extract_imap_uid(data[0][0], uid)
                            payload = data[0][1]
                            rec_id, should_process = server._durable_begin("imap", payload, remote_uid=remote_uid, mailbox=mailbox, uid_validity=uid_validity)
                            if not should_process:
                                skipped += 1
                                server._imap_seen_uid(imap_server, uid)
                                self.env.cr.commit()
                                continue
                            res_id = False
                            try:
                                res_id = MailThread.with_context(**context).message_process(
                                    server.object_id.model,
                                    payload,
                                    save_original=server.original,
                                    strip_attachments=(not server.attach),
                                )
                            except Exception as exc:
                                self.env.cr.rollback()
                                server._durable_mark_failed(rec_id, exc)
                                server._imap_seen_uid(imap_server, uid)
                                self.env.cr.commit()
                                failed += 1
                                _logger.info("Failed to process durable IMAP mail from %s server %s.\n%s", server.server_type, server.name, server._durable_safe_traceback(exc, secret_password))
                            else:
                                self.env.cr.commit()
                                server._durable_mark_succeeded(rec_id, res_id=res_id)
                                server._imap_seen_uid(imap_server, uid)
                                self.env.cr.commit()
                            count += 1
                        _logger.info("Durable fetched %d IMAP email(s) on %s; %d failed, %d skipped.", count, server.name, failed, skipped)
                    except Exception as exc:
                        if raise_exception:
                            safe_error = self._redact_secret_text(exc, [secret_password, secret_user])
                            raise ValidationError(_("Couldn't get your emails. Check out the error message below for more info:\n%s", safe_error)) from exc
                        _logger.info("General durable IMAP failure for server %s.\n%s", server.name, server._durable_safe_traceback(exc, secret_password))
                    finally:
                        if imap_server:
                            try:
                                imap_server.close()
                                imap_server.logout()
                            except (OSError, IMAP4.abort) as exc:
                                _logger.warning("Failed to properly finish imap connection: %s.\n%s", server.name, server._durable_safe_traceback(exc, secret_password))
                elif connection_type == "pop":
                    message = _("mail_fetchmail_durable is IMAP-only: POP3 deletion and UID semantics are not durable enough for this platform. Configure an IMAP incoming server instead.")
                    if raise_exception:
                        raise ValidationError(message)
                    _logger.warning("Refusing POP3 fetch for durable server %s: %s", server.name, message)
                else:
                    return super(FetchmailServer, server).fetch_mail(raise_exception=raise_exception)
                server.write({"date": fields.Datetime.now()})
        return True
