# -*- coding: utf-8 -*-

import base64
import hashlib
import logging
import re
import traceback

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# Every quantifier that can be attacker-influenced (error text may carry raw
# sender headers) is bounded so that scanning is linear in the input size.
_CREDENTIAL_PATTERNS = (
    # key=value / key: value (password=..., token: ..., api_key=...), including
    # "Authorization: Bearer|Basic <credential>" (scheme kept, credential removed)
    (re.compile(r"(?i)(password|passwd|pwd|authorization|token|secret|api[_-]?key)(\s{0,8}[=:]\s{0,8})((?:(?:bearer|basic)\s{1,8})?)([^\s,;]+)"), r"\1\2\3[REDACTED]"),
    # A standalone long "Bearer <token>" (case-sensitive; plain prose such as
    # "Basic configuration" is deliberately left alone)
    (re.compile(r"\b(Bearer)(\s{1,8})[A-Za-z0-9._~+/=-]{20,}"), r"\1\2[REDACTED]"),
    # user:password@host in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}://[^\s:/@]{1,256}):[^\s/@]{1,256}@"), r"\1:[REDACTED]@"),
    # IMAP AUTHENTICATE <mech> <base64 initial response>
    (re.compile(r"(?i)\b(AUTHENTICATE\s{1,8}\S{1,32}\s{1,8})[A-Za-z0-9+/=]{8,}"), r"\1[REDACTED]"),
)
ERROR_TEXT_LIMIT = 16 * 1024
TRACEBACK_TEXT_LIMIT = 64 * 1024


def redact_secret_text(text, secrets=(), min_secret_len=1, max_chars=ERROR_TEXT_LIMIT, keep="head"):
    """Remove known secrets and obvious credential patterns from free text.

    ``secrets`` are literal values (server password / login) replaced wherever
    they occur; values shorter than ``min_secret_len`` are skipped so that very
    short values cannot mangle a whole traceback.  The text is capped to
    ``max_chars`` (first chars for ``keep="head"``, last for ``"tail"``) before
    the regexes run.
    """
    value = str(text)
    for secret in sorted({str(s) for s in secrets if s}, key=len, reverse=True):
        if len(secret) >= min_secret_len:
            value = value.replace(secret, "[REDACTED]")
    if max_chars and len(value) > max_chars:
        value = (value[:max_chars] + "...[truncated]") if keep == "head" else ("[truncated]..." + value[-max_chars:])
    for pattern, repl in _CREDENTIAL_PATTERNS:
        value = pattern.sub(repl, value)
    return value


def format_exception_text(error):
    """Traceback text for ``error`` (falls back to the active exception)."""
    if isinstance(error, BaseException):
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return traceback.format_exc()


def message_fingerprint(payload):
    return hashlib.sha256(payload or b"").hexdigest()


class MailFetchFailure(models.Model):
    """Tenant/database-scoped incoming-mail processing ledger."""

    _name = "mail.fetch.failure"
    _description = "Incoming Mail Fetch Failure Ledger"
    _order = "last_attempt desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    server_id = fields.Many2one("fetchmail.server", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", readonly=True, help="Reserved for deployments that extend fetchmail.server with company ownership; records remain isolated by database and server_id.")
    protocol = fields.Selection([("imap", "IMAP"), ("pop", "POP3")], required=True, index=True)
    mailbox = fields.Char(default="INBOX", required=True)
    uid_validity = fields.Char(index=True, default="", help="IMAP UIDVALIDITY. Empty for POP3/no UIDVALIDITY.")
    remote_uid = fields.Char(index=True, help="IMAP UID or POP3 UIDL when available")
    message_id_header = fields.Char(index=True)
    fingerprint = fields.Char(required=True, index=True)
    status = fields.Selection([
        ("processing", "Processing"),
        ("failed", "Failed"),
        ("uncertain", "Uncertain business commit"),
        ("succeeded", "Succeeded"),
        ("skipped", "Skipped"),
    ], default="processing", required=True, index=True)
    attempts = fields.Integer(default=0)
    last_attempt = fields.Datetime(index=True)
    first_failure = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    last_traceback = fields.Text(readonly=True)
    target_model_recorded = fields.Boolean(readonly=True, help="True when target_model was captured at fetch time (an empty target_model then means alias routing); False on legacy rows.")
    target_model = fields.Char(readonly=True, help="Model the server routed this message to when it was first fetched. Retries use it instead of the server's current target; empty on rows created before this field existed.")
    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    raw_attachment_id = fields.Many2one("ir.attachment", readonly=True, ondelete="set null")
    raw_size = fields.Integer(readonly=True, help="Original RFC822 payload size in bytes, recorded before Odoo business processing.")
    raw_retention_policy = fields.Selection([
        ("keep", "Keep until manual export/review"),
        ("purge_allowed", "May purge after reviewed success/skip"),
    ], default="keep", required=True, readonly=True, help="Failed and uncertain rows are never eligible for automatic raw-message deletion; no live purge cron is installed by this addon.")
    note = fields.Text()

    _sql_constraints = [
        ("server_uid_unique", "UNIQUE(server_id, protocol, mailbox, uid_validity, remote_uid)", "Remote message UID/UIDVALIDITY is already tracked for this incoming server."),
    ]

    @api.depends("server_id", "uid_validity", "remote_uid", "message_id_header", "status")
    def _compute_display_name(self):
        for rec in self:
            uid = f"{rec.uid_validity}/{rec.remote_uid}" if rec.uid_validity and rec.remote_uid else (rec.remote_uid or rec.message_id_header or rec.fingerprint[:12])
            rec.display_name = f"{rec.server_id.display_name or rec.server_id.id}: {uid} [{rec.status}]"

    @api.model
    def _message_headers(self, payload):
        import email
        msg = email.message_from_bytes(payload or b"")
        return msg.get("Message-Id") or msg.get("Message-ID") or False

    @api.model
    def _create_raw_attachment(self, payload, name):
        return self.env["ir.attachment"].sudo().create({
            "name": name,
            "type": "binary",
            "datas": base64.b64encode(payload or b""),
            "res_model": self._name,
            "public": False,
        })

    def _lock_rows(self):
        if self.ids:
            self.env.cr.execute("SELECT id FROM mail_fetch_failure WHERE id = ANY(%s) FOR UPDATE", [self.ids])

    def _redact_error(self, error):
        secrets = set(self.mapped("server_id.password") + self.mapped("server_id.user"))
        return redact_secret_text(error, secrets)

    def _redact_traceback(self, error):
        """Redacted traceback text.  Only the password is replaced literally
        (and only when >= 4 chars) so a short login cannot mangle file paths."""
        secrets = set(self.mapped("server_id.password"))
        return redact_secret_text(format_exception_text(error), secrets, min_secret_len=4, max_chars=TRACEBACK_TEXT_LIMIT, keep="tail")

    @api.model
    def _raw_purge_domain(self):
        """Documented safe boundary only; no cron calls this automatically.

        Operators may use this domain after exporting/reviewing rows.  Failed,
        uncertain and processing rows are intentionally excluded so raw evidence
        needed for recovery is never auto-deleted by this addon.
        """
        return [("status", "in", ["succeeded", "skipped"]), ("raw_retention_policy", "=", "purge_allowed")]

    @api.model
    def _begin_processing(self, server, protocol, payload, remote_uid=False, mailbox="INBOX", uid_validity="", max_bytes=None):
        payload = payload or b""
        raw_size = len(payload)
        if max_bytes and raw_size > max_bytes:
            raise UserError(_("Incoming message is %(size)s bytes, exceeding the durable fetch limit of %(limit)s bytes; it was left unread for operator handling.", size=raw_size, limit=max_bytes))
        fingerprint = message_fingerprint(payload)
        uid_validity = str(uid_validity or "")
        domain = [("server_id", "=", server.id), ("protocol", "=", protocol), ("mailbox", "=", mailbox)]
        if remote_uid:
            domain.extend([("uid_validity", "=", uid_validity), ("remote_uid", "=", str(remote_uid))])
        else:
            domain.append(("fingerprint", "=", fingerprint))
        existing = self.sudo().search(domain, limit=1)
        now = fields.Datetime.now()
        if existing:
            existing._lock_rows()
            if existing.fingerprint != fingerprint:
                existing.write({
                    "status": "uncertain",
                    "last_attempt": now,
                    "last_error": _("Fail-closed: remote UID matched an existing ledger row but payload fingerprint changed; no automatic processing performed."),
                })
                return existing, False
            if existing.status == "processing":
                existing.write({
                    "status": "uncertain",
                    "last_attempt": now,
                    "last_error": _("Previous worker stopped before recording success/failure; no automatic retry performed."),
                })
            return existing, False
        attachment = self._create_raw_attachment(payload, f"fetchmail-{server.id}-{uid_validity or 'no-uidvalidity'}-{remote_uid or fingerprint[:12]}.eml")
        rec = self.sudo().create({
            "server_id": server.id,
            "protocol": protocol,
            "mailbox": mailbox,
            "uid_validity": uid_validity,
            "remote_uid": str(remote_uid) if remote_uid else False,
            "message_id_header": self._message_headers(payload),
            "fingerprint": fingerprint,
            "target_model": server.object_id.model or False,
            "target_model_recorded": True,
            "status": "processing",
            "attempts": 1,
            "last_attempt": now,
            "raw_attachment_id": attachment.id,
            "raw_size": raw_size,
        })
        attachment.write({"res_id": rec.id})
        return rec, True

    def _mark_failed(self, error):
        self.sudo()._lock_rows()
        self.sudo().write({
            "status": "failed",
            "first_failure": self.first_failure or fields.Datetime.now(),
            "last_attempt": fields.Datetime.now(),
            "last_error": self._redact_error(error),
            "last_traceback": self._redact_traceback(error),
        })

    def _mark_succeeded(self, res_id=False, res_model=False):
        self.sudo()._lock_rows()
        self.sudo().write({
            "status": "succeeded",
            "last_attempt": fields.Datetime.now(),
            "last_error": False,
            "last_traceback": False,
            "res_id": res_id or False,
            "res_model": res_model or False,
        })

    def action_mark_skipped(self):
        self.check_access("write")
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only Settings users may change incoming failure ledger rows."))
        for rec in self:
            if rec.status not in ("failed", "uncertain"):
                raise UserError(_("Only failed or uncertain rows can be manually skipped."))
        for rec in self:
            rec.write({"status": "skipped", "note": (rec.note or "") + "\nManually skipped."})

    @api.model
    def _fetch_cron_uid(self):
        """User id the fetchmail cron runs (and so processes mail) as.

        Core runs ``fetchmail.server._fetch_mails`` from the
        ``mail.ir_cron_mail_gateway_action`` cron under that cron's ``user_id``
        (OdooBot / superuser by default).  Fall back to the superuser if the
        cron record was removed.
        """
        cron = self.env.ref("mail.ir_cron_mail_gateway_action", raise_if_not_found=False)
        return (cron and cron.sudo().user_id.id) or SUPERUSER_ID

    def action_retry_failed(self):
        self.check_access("write")
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only Settings users may retry incoming failures."))
        # Same identity and a clean context, exactly as the cron env is built, so
        # create_uid / record rules match an automatic fetch.  Access to trigger
        # this action was already checked above (write access + group_system).
        MailThread = api.Environment(self.env.cr, self._fetch_cron_uid(), {})["mail.thread"]
        for rec in self:
            with rec.server_id._durable_server_session_lock():
                rec.invalidate_recordset()
                rec = rec.browse(rec.id).exists()
                if not rec or rec.status != "failed":
                    raise UserError(_("Only failed rows can be retried; uncertain rows require manual business-record review first."))
                rec._lock_rows()
                if not rec.raw_attachment_id or not rec.raw_attachment_id.datas:
                    raise UserError(_("No raw message is available for retry of %s", rec.display_name))
                payload = base64.b64decode(rec.raw_attachment_id.datas)
                current_model = rec.server_id.object_id.model
                if rec.target_model_recorded:
                    target_model = rec.target_model or False  # False = alias routing
                    if (target_model or False) != (current_model or False):
                        _logger.warning(
                            "Retrying %s with its recorded target model %s although server %s now targets %s.",
                            rec.display_name, target_model, rec.server_id.id, current_model,
                        )
                else:
                    target_model = current_model or False  # legacy row
                if target_model and (target_model not in self.env or not isinstance(self.env[target_model], self.pool["mail.thread"])):
                    raise UserError(_("Cannot retry %(row)s: target model %(model)s is not available or does not support incoming mail.", row=rec.display_name, model=target_model))
                self.env.cr.execute(
                    """
                    UPDATE mail_fetch_failure
                       SET status = 'processing', attempts = attempts + 1, last_attempt = %s
                     WHERE id = %s AND status = 'failed'
                 RETURNING id
                    """,
                    [fields.Datetime.now(), rec.id],
                )
                if not self.env.cr.fetchone():
                    self.env.cr.rollback()
                    raise UserError(_("Retry was not claimed because the row state changed; reload and review before retrying."))
                rec.invalidate_recordset()
                self.env.cr.commit()  # durable retry fence before business work; session lock remains held on a separate cursor
                try:
                    res_id = MailThread.with_context(
                        fetchmail_cron_running=True,
                        default_fetchmail_server_id=rec.server_id.id,
                        durable_fetchmail_manual_retry=True,
                    ).message_process(
                        target_model,
                        payload,
                        save_original=rec.server_id.original,
                        strip_attachments=(not rec.server_id.attach),
                    )
                except Exception as exc:
                    self.env.cr.rollback()
                    rec._mark_failed(exc)
                    self.env.cr.commit()
                    if isinstance(exc, UserError):
                        raise
                    raise UserError(_("Retry failed: %s", rec._redact_error(exc))) from None
                else:
                    self.env.cr.commit()
                    rec._mark_succeeded(res_id=res_id, res_model=target_model)
                    self.env.cr.commit()
        return True
