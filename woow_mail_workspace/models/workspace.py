"""A projection of existing Odoo messages, never a second IMAP consumer.

Only the explicit database scope elevates message READS, after checking
base.group_system. It excludes chats, internal notes, and system notifications.
Writes affect private UI state only; this module never writes/unlinks an existing
mail.message, changes notifications, or changes IMAP flags. Public RPC arguments
are untrusted, including scope, message IDs and folder IDs.
"""
from html import escape

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression
from odoo.tools import html_sanitize, html2plaintext


class MailFolder(models.Model):
    _name = "woow.mail.folder"
    _description = "Personal mail folder"
    _order = "name, id"

    name = fields.Char(required=True, size=80)
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user,
                              index=True, ondelete="cascade", readonly=True)
    _sql_constraints = [("owner_name_unique", "unique(user_id, name)", "This folder already exists.")]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("user_id", self.env.uid) != self.env.uid:
                raise AccessError(_("You may only create your own folders."))
            vals["user_id"] = self.env.uid
        return super().create(vals_list)

    def write(self, vals):
        if "user_id" in vals:
            raise AccessError(_("Folder ownership cannot be changed."))
        return super().write(vals)

    @api.constrains("name")
    def _check_name(self):
        if any(not (r.name or "").strip() for r in self):
            raise ValidationError(_("Please enter a folder name."))


class MailState(models.Model):
    _name = "woow.mail.state"
    _description = "Private mail workspace state"
    # No direct user ACL. All reads/writes pass through the guarded service.
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    message_id = fields.Many2one("mail.message", required=True, index=True, ondelete="cascade")
    is_read = fields.Boolean(default=False)
    starred = fields.Boolean(default=False)
    archived = fields.Boolean(default=False)
    trashed = fields.Boolean(default=False)
    folder_id = fields.Many2one("woow.mail.folder", ondelete="set null", index=True)
    _sql_constraints = [("user_message_unique", "unique(user_id, message_id)", "One state per user and message.")]


class MailWorkspace(models.AbstractModel):
    _name = "woow.mail.workspace"
    _description = "Mail workspace service"

    @api.model
    def _guard(self, scope="personal"):
        if not self.env.user.has_group("base.group_user"):
            raise AccessError(_("Internal users only."))
        if scope not in ("personal", "accessible", "database"):
            raise ValidationError(_("Unknown mailbox scope."))
        if scope == "database" and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Database mail requires Odoo administrator access."))

    @api.model
    def _source(self, scope):
        self._guard(scope)
        source = self.env["mail.message"]
        return source.sudo() if scope == "database" else source

    @api.model
    def _domain(self, scope):
        self._guard(scope)
        domain = [
            ("message_type", "in", ["email", "email_outgoing", "comment"]),
            ("is_internal", "=", False),
            # Native outgoing emails often have no subtype. The relational
            # condition alone excludes those rows instead of treating them as
            # non-internal, so explicitly include the empty relation.
            "|", ("subtype_id", "=", False), ("subtype_id.internal", "=", False),
            ("model", "not in", ["discuss.channel", "mail.channel"]),
        ]
        if scope == "personal":
            pid = self.env.user.partner_id.id
            domain = expression.AND([domain, expression.OR([
                [("author_id", "=", pid)], [("partner_ids", "in", [pid])],
                [("notified_partner_ids", "in", [pid])],
            ])])
        return domain

    @api.model
    def _checked_messages(self, ids, scope="personal"):
        self._guard(scope)
        if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or i <= 0 for i in ids):
            raise ValidationError(_("Select up to 100 messages."))
        messages = self._source(scope).search(expression.AND([self._domain(scope), [("id", "in", ids)]]))
        if set(messages.ids) != set(ids):
            raise AccessError(_("A message is unavailable or you no longer have access."))
        return messages

    @api.model
    def _states(self, extra=None):
        return self.env["woow.mail.state"].sudo().search([("user_id", "=", self.env.uid)] + (extra or []))

    @api.model
    def bootstrap(self):
        self._guard()
        return {
            "admin": self.env.user.has_group("base.group_system"),
            "name": self.env.user.name,
            "folders": self.env["woow.mail.folder"].search_read([], ["name"], order="name, id"),
            "draft_count": self.env["woow.mail.draft"].search_count([("state", "=", "draft")]),
        }

    @api.model
    def list_messages(self, scope="personal", box="inbox", search="", offset=0, unread=False, folder_id=False):
        domain = self._domain(scope)
        if box not in ("inbox", "sent", "starred", "archive", "trash", "all", "folder"):
            raise ValidationError(_("Unknown folder."))
        offset = max(0, int(offset))
        states = self._states()
        if box == "trash":
            domain += [("id", "in", states.filtered("trashed").message_id.ids)]
        else:
            domain += [("id", "not in", states.filtered("trashed").message_id.ids)]
        if box == "inbox":
            domain += [("author_id", "!=", self.env.user.partner_id.id),
                       ("id", "not in", states.filtered(lambda s: s.archived or s.folder_id).message_id.ids)]
        elif box == "sent":
            domain += [("author_id", "=", self.env.user.partner_id.id)]
        elif box == "starred":
            domain += [("id", "in", states.filtered("starred").message_id.ids)]
        elif box == "archive":
            domain += [("id", "in", states.filtered("archived").message_id.ids)]
        elif box == "folder":
            folder = self.env["woow.mail.folder"].search([("id", "=", int(folder_id or 0))])
            if not folder:
                raise AccessError(_("Folder unavailable."))
            domain += [("id", "in", states.filtered(lambda s: s.folder_id == folder).message_id.ids)]
        if unread:
            domain = expression.AND([domain,
                [("id", "not in", states.filtered("is_read").message_id.ids)],
                expression.OR([
                    [("author_id", "!=", self.env.user.partner_id.id)],
                    [("id", "in", states.filtered(lambda s: not s.is_read).message_id.ids)],
                ]),
            ])
        search = str(search or "").strip()[:200]
        if search:
            domain = expression.AND([domain, expression.OR([
                [("subject", "ilike", search)], [("email_from", "ilike", search)],
                [("body", "ilike", search)], [("partner_ids.email", "ilike", search)],
            ])])
        # Native mail.message filters document access after its SQL limit.
        # Resolve authorized IDs first, then paginate, so hidden rows cannot
        # consume a page. Bodies/attachments are only loaded for this page.
        authorized = self._source(scope).search(domain, order="date desc, id desc")
        messages = authorized[offset:offset + 41]
        state_map = {s.message_id.id: s for s in states}
        return {"messages": [self._summary(m, state_map.get(m.id)) for m in messages[:40]],
                "has_more": len(messages) > 40, "offset": offset}

    @api.model
    def _summary(self, message, state=None):
        return {
            "id": message.id, "subject": message.subject or _("(No subject)"),
            "sender": message.email_from or message.author_id.display_name or "",
            "preview": message.preview or "", "date": fields.Datetime.to_string(message.date),
            "is_read": state.is_read if state else message.author_id == self.env.user.partner_id,
            "starred": bool(state and state.starred), "archived": bool(state and state.archived),
            "trashed": bool(state and state.trashed), "folder_id": state.folder_id.id if state else False,
            "has_attachments": bool(message.attachment_ids),
            "record": message.record_name or "", "has_error": message.has_error,
        }

    @api.model
    def get_message(self, message_id, scope="personal"):
        message = self._checked_messages([message_id], scope)
        state = self._states([("message_id", "=", message_id)])
        result = self._summary(message, state)
        result.update({
            "body": str(html_sanitize(message.body or "", sanitize_style=True)),
            "recipients": [p.email_formatted for p in message.partner_ids],
            "attachments": [{"id": a.id, "name": a.name, "size": a.file_size,
                             "url": f"/woow_mail_workspace/attachment/{message.id}/{a.id}?scope={scope}"}
                            for a in message.attachment_ids],
            "can_open_record": self._record_access(message, "read"),
            "can_reply": self._record_access(message, "write"),
        })
        return result

    @api.model
    def _record_access(self, message, operation):
        if not message.model or not message.res_id or message.model not in self.env:
            return False
        record = self.env[message.model].browse(message.res_id).exists()
        return bool(record and record.has_access(operation))

    @api.model
    def open_record(self, message_id, scope="personal"):
        message = self._checked_messages([message_id], scope)
        if not self._record_access(message, "read"):
            raise AccessError(_("You cannot open this business record."))
        return {"type": "ir.actions.act_window", "res_model": message.model,
                "res_id": message.res_id, "views": [[False, "form"]], "target": "current"}

    @api.model
    def set_state(self, ids, values, scope="personal"):
        messages = self._checked_messages(ids, scope)
        allowed = {"is_read", "starred", "archived", "trashed", "folder_id"}
        if not isinstance(values, dict) or not values or set(values) - allowed:
            raise ValidationError(_("Invalid message action."))
        for key, value in values.items():
            if key != "folder_id" and type(value) is not bool:
                raise ValidationError(_("Invalid message state."))
        if "folder_id" in values and values["folder_id"]:
            if type(values["folder_id"]) is not int or not self.env["woow.mail.folder"].search_count([
                ("id", "=", values["folder_id"]), ("user_id", "=", self.env.uid)
            ]):
                raise AccessError(_("Folder unavailable."))
        # Serialize each user's state updates to avoid first-open uniqueness races.
        self.env.cr.execute("SELECT id FROM res_users WHERE id = %s FOR UPDATE", [self.env.uid])
        for message in messages:
            state = self._states([("message_id", "=", message.id)])
            if state:
                state.write(values)
            else:
                self.env["woow.mail.state"].sudo().create({
                    "user_id": self.env.uid, "message_id": message.id,
                    "is_read": message.author_id == self.env.user.partner_id, **values,
                })
        return True

    @api.model
    def compose(self, message_id=False, mode="new", scope="personal"):
        self._guard(scope)
        if mode not in ("new", "reply", "forward"):
            raise ValidationError(_("Unknown compose action."))
        vals = {}
        if mode != "new":
            message = self._checked_messages([message_id], scope)
            # Admin read privilege must never become a send-as privilege.
            if mode == "reply" and not self._record_access(message, "write"):
                raise AccessError(_("Reply requires write access to the original record."))
            vals.update({"source_message_id": message.id, "source_scope": scope, "mode": mode,
                         "subject": ("Re: " if mode == "reply" else "Fwd: ") + (message.subject or "")})
            if mode == "reply" and message.author_id != self.env.user.partner_id:
                vals["partner_ids"] = [(6, 0, message.author_id.ids)]
            vals["body"] = "<p><br></p><blockquote><p>%s — %s</p>%s</blockquote>" % (
                escape(message.email_from or ""), escape(fields.Datetime.to_string(message.date) or ""),
                # The native editor is not sandboxed: quote text only so opening
                # reply/forward cannot load the original sender's tracking pixel.
                escape(html2plaintext(message.body or "")).replace("\n", "<br/>"))
        draft = self.env["woow.mail.draft"].create(vals)
        if mode == "forward":
            # Copy into the owner's draft; never reassign the original attachments.
            for attachment in message.attachment_ids:
                copy = attachment.copy({"res_model": draft._name, "res_id": draft.id,
                                        "public": False, "access_token": False})
                draft.attachment_ids = [(4, copy.id)]
        return draft._form_action()
