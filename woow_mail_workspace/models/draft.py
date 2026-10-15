"""Persistent private drafts; delivery is delegated to Odoo's native composer.

No SMTP credentials and no direct SMTP calls: the native composer and
ir.mail_server do the sending. Delivery is SYNCHRONOUS -- the Send button hands
the message to SMTP inside the request. It used to be left to Odoo's queue, but
mail.ir_cron_mail_scheduler_action is hourly by default, so "sent" in the UI
meant "will leave within the hour" (see action_queue for the measurements).
Reply stays on the original business thread; forward/new uses a private
conversation so it cannot notify original followers.
"""
import logging

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)


class MailConversation(models.Model):
    _name = "woow.mail.conversation"
    _inherit = ["mail.thread"]
    _description = "Workspace email conversation"

    name = fields.Char(required=True)
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user,
                              readonly=True, ondelete="restrict", index=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True)
    participant_ids = fields.Many2many("res.partner", string="Initial recipients", readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("user_id", self.env.uid) != self.env.uid:
                raise AccessError(_("You may only create your own conversations."))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) & {"user_id", "participant_ids", "company_id"}:
            raise AccessError(_("Conversation ownership, participants and company cannot be changed."))
        return super().write(vals)


class MailDraft(models.Model):
    _name = "woow.mail.draft"
    _description = "Private email draft"
    _rec_name = "subject"
    _order = "write_date desc, id desc"

    subject = fields.Char(default="")
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user,
                              readonly=True, ondelete="cascade", index=True)
    partner_ids = fields.Many2many("res.partner", string="To")
    body = fields.Html(sanitize_style=True, default="<p><br></p>")
    attachment_ids = fields.Many2many("ir.attachment", string="Attachments")
    source_message_id = fields.Many2one("mail.message", readonly=True, ondelete="set null")
    source_scope = fields.Selection([(s, s) for s in ("personal", "accessible", "database")], default="personal", required=True, readonly=True)
    mode = fields.Selection([("new", "New"), ("reply", "Reply"), ("forward", "Forward")], default="new", required=True, readonly=True)
    state = fields.Selection([("draft", "Draft"), ("queued", "Queued")], default="draft", readonly=True, required=True)
    conversation_id = fields.Many2one("woow.mail.conversation", readonly=True, ondelete="restrict")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("user_id", self.env.uid) != self.env.uid or vals.get("state", "draft") != "draft" or vals.get("conversation_id"):
                raise AccessError(_("Invalid draft ownership or state."))
            vals["user_id"] = self.env.uid
        records = super().create(vals_list)
        records._validate_source()
        records._validate_attachments()
        return records

    def write(self, vals):
        if set(vals) & {"user_id", "state", "conversation_id", "source_message_id", "source_scope", "mode"}:
            raise AccessError(_("Draft ownership, source and delivery state are protected."))
        if any(r.state != "draft" for r in self):
            raise ValidationError(_("Queued messages cannot be edited."))
        result = super().write(vals)
        if "attachment_ids" in vals:
            self._validate_attachments()
        return result

    def unlink(self):
        if any(r.state != "draft" for r in self):
            raise ValidationError(_("Queued messages cannot be deleted here."))
        return super().unlink()

    def _validate_source(self):
        for draft in self:
            if draft.mode != "new":
                if not draft.source_message_id:
                    raise ValidationError(_("The original message is required."))
                message = self.env["woow.mail.workspace"]._checked_messages([draft.source_message_id.id], draft.source_scope)
                if draft.mode == "reply" and not self.env["woow.mail.workspace"]._record_access(message, "write"):
                    raise AccessError(_("Reply requires write access to the original record."))

    def _validate_attachments(self):
        for draft in self:
            draft.attachment_ids.check_access("read")
            # Only draft-owned uploads/copies may be passed to the native composer.
            if any(a.res_model != self._name or a.res_id != draft.id or a.create_uid != self.env.user
                   for a in draft.attachment_ids):
                raise AccessError(_("Attach files uploaded to this draft only."))

    def _form_action(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("Compose email"),
                "res_model": self._name, "res_id": self.id, "views": [[False, "form"]], "target": "new"}

    def action_save(self):
        self.check_access("write")
        return {"type": "ir.actions.act_window_close"}

    def action_discard(self):
        self.ensure_one()
        self.unlink()
        return {"type": "ir.actions.act_window_close"}

    def action_queue(self):
        self.ensure_one()
        self.check_access("write")
        if self.user_id != self.env.user:
            raise AccessError(_("You may only send your own drafts."))
        self.env.cr.execute("SELECT id FROM woow_mail_draft WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset(["state"])
        if self.state != "draft":
            raise ValidationError(_("This draft has already been queued. It will not be sent twice."))
        self._validate_source()
        self._validate_attachments()
        if not (self.subject or "").strip() or not self.partner_ids:
            raise ValidationError(_("Enter a subject and at least one recipient."))
        if any(not p.email for p in self.partner_ids):
            raise ValidationError(_("Every recipient must have an email address."))
        if self.mode == "reply":
            message = self.source_message_id
            model, res_id = message.model, message.res_id
        else:
            conversation = self.env["woow.mail.conversation"].with_context(mail_create_nolog=True, mail_create_nosubscribe=True).create({
                "name": self.subject, "participant_ids": [(6, 0, self.partner_ids.ids)],
            })
            model, res_id = conversation._name, conversation.id
            super(MailDraft, self).write({"conversation_id": conversation.id})
        # Two context keys, two different defects they prevent:
        #
        # force_send=True -- hand the message to SMTP inside this request rather
        #   than leaving it for mail.ir_cron_mail_scheduler_action. That cron is
        #   HOURLY by default, so a queued send sat up to an hour before leaving
        #   the building while the UI already said "sent". Measured on production
        #   2026-10-15: mail.mail 5064 created 21:43, cron nextcall 22:24 -- a
        #   41-minute wait on a mail client's Send button. The same defect hit the
        #   inzense tenant on 2026-10-12 and was patched there by shortening that
        #   tenant's cron; doing it per database leaves every other database
        #   broken, so the fix belongs here. A queue is also the wrong shape for
        #   this button for a second reason: process_email_queue() only ever
        #   re-picks state='outgoing', so an SMTP failure discovered an hour later
        #   lands in 'exception' and is never retried, with nobody watching.
        #
        # woow_mail_workspace_force_email -- a recipient whose
        #   res.users.notification_type is "inbox" would otherwise be notified
        #   in-app and NO email would be created at all (see models/force_email.py).
        composer = self.env["mail.compose.message"].with_context(
            mail_notify_force_send=True,
            woow_mail_workspace_force_email=True,
        ).create({
            "composition_mode": "comment", "model": model, "res_ids": repr([res_id]),
            "subject": self.subject, "body": self.body,
            "partner_ids": [(6, 0, self.partner_ids.ids)],
            "attachment_ids": [(6, 0, self.attachment_ids.ids)],
            "parent_id": self.source_message_id.id if self.mode == "reply" else False,
            "subtype_id": self.env.ref("mail.mt_comment").id,
            "force_send": True,
        })
        composer.with_context(woow_mail_workspace_force_email=True).action_send_mail()
        self._assert_every_recipient_got_an_email(model, res_id)
        super(MailDraft, self).write({"state": "queued"})
        return {"type": "ir.actions.act_window_close"}

    def _assert_every_recipient_got_an_email(self, model, res_id):
        """Refuse to report success unless every recipient actually has mail queued.

        The defect this guards against was silent, which is what made it
        expensive: on 2026-10-15 a production send produced an in-app
        notification and zero mail.mail rows, the UI said "sent", and nothing
        recorded a failure. The recipient found out by not receiving anything.

        Odoo's notification layer legitimately routes a recipient away from
        email for several reasons (preference, mail.thread overrides, another
        module adding a channel). Any of those is fine as a product decision but
        NOT fine silently from a mail client: raising here rolls the whole send
        back, leaves the draft editable, and tells the sender which address did
        not get queued -- a visible failure instead of a lost message.
        """
        self.ensure_one()
        message = self.env["mail.message"].sudo().search(
            [("model", "=", model), ("res_id", "=", res_id)], order="id desc", limit=1)
        emailed = self.env["mail.notification"].sudo().search(
            [("mail_message_id", "=", message.id), ("notification_type", "=", "email")]
        ).res_partner_id if message else self.env["res.partner"]
        missing = self.partner_ids - emailed
        if missing:
            raise ValidationError(_(
                "These recipients were not queued for email delivery, so the message "
                "was not sent: %s. Nothing was sent to anyone; the draft is unchanged. "
                "Report this rather than retrying -- retrying will fail the same way.",
                ", ".join(missing.mapped("email") or missing.mapped("display_name"))))

        # Now that the send is synchronous, SMTP has already been attempted and
        # its verdict is on the notification rows. Report it instead of letting
        # the UI claim success over an 'exception' row nobody will look at.
        notifications = self.env["mail.notification"].sudo().search(
            [("mail_message_id", "=", message.id), ("notification_type", "=", "email")])
        failed = notifications.filtered(lambda n: n.notification_status in ("exception", "bounce"))
        if not failed:
            return
        reason = next((n.failure_reason for n in failed if n.failure_reason), "")
        addresses = ", ".join(failed.res_partner_id.mapped("email")
                              or failed.res_partner_id.mapped("display_name"))
        if len(failed) == len(notifications):
            # Every recipient failed, so nothing reached SMTP: rolling back is
            # accurate and keeps the draft editable for a retry.
            raise ValidationError(_(
                "The mail server refused this message, so it was not sent to anyone "
                "(%(addresses)s). The draft is unchanged. Server said: %(reason)s",
                addresses=addresses, reason=reason or _("no reason reported")))
        # Partial failure: the recipients that did go out cannot be unsent, so
        # rolling back here would hide a message that is already in flight.
        # Keep the send and make the failure loud in the log instead.
        _logger.error(
            "woow_mail_workspace: draft %s delivered to %s of %s recipients; "
            "failed for %s (%s)", self.id, len(notifications) - len(failed),
            len(notifications), addresses, reason or "no reason reported")
