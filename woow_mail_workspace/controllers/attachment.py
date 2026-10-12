from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, ValidationError
from werkzeug.exceptions import NotFound


class WorkspaceAttachment(http.Controller):
    @http.route("/woow_mail_workspace/attachment/<int:message_id>/<int:attachment_id>",
                type="http", auth="user", methods=["GET"])
    def download(self, message_id, attachment_id, scope="personal", **kwargs):
        try:
            message = request.env["woow.mail.workspace"]._checked_messages([message_id], scope)
            attachment = message.attachment_ids.filtered(lambda a: a.id == attachment_id)
            if not attachment or attachment.type != "binary":
                raise NotFound()
            # Elevation is limited to an attachment belonging to an already
            # authorized message (including native Odoo recipient-only access).
            stream = request.env["ir.binary"]._get_stream_from(attachment.sudo(), "raw")
            return stream.get_response(as_attachment=True)
        except (AccessError, ValidationError):
            raise NotFound() from None
