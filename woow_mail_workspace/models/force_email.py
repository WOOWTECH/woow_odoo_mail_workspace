"""Make the workspace's Send button actually send email.

THE BUG THIS FIXES (observed on production woow_prod, 2026-10-15)

A workspace draft is delivered through Odoo's native composer, so delivery goes
through ``mail.thread._notify_thread()``, which splits recipients by the
``notif`` value computed in ``mail.followers._get_recipient_data()``. That value
comes from ``res_users.notification_type``: a recipient who happens to own an
Odoo user account whose preference is "Handle in Odoo" is classified as
``inbox`` and gets an in-app notification only -- **no email is ever created**.

Measured consequence: a message sent to az11081204@gmail.com produced one
``mail.notification`` row with ``notification_type = 'inbox'`` and **zero**
``mail.mail`` rows. The sender's UI reported success, the recipient never got
anything, and nothing anywhere recorded a failure. 8 of the 45 active users on
that database have the ``inbox`` preference, so the same silent loss applied to
every one of them.

WHY OVERRIDING THIS IS CORRECT RATHER THAN A WORKAROUND

``notification_type`` answers "how should Odoo notify this person about activity
on a business document they follow?". A mail client's Send button is a different
question: the user typed an address and asked for mail to leave the building. A
recipient's in-app preference must not silently convert that into no mail at
all. The in-app copy is not lost either -- the message is still posted on the
thread, so it remains visible in Odoo exactly as before.

WHY IT IS SCOPED TO A CONTEXT KEY

The override only acts when ``woow_mail_workspace_force_email`` is in the
context, which only ``MailDraft.action_queue`` sets. Every other notification
path on this database -- ERP document chatter, Discuss, activity reminders --
keeps honouring the user's preference untouched. A blanket override would
instead start emailing internal staff about every document they follow, which
is a different product decision and not this module's to make.

The hook is ``_notify_get_recipients``, the documented extension point (it is
what the sms and web-push modules extend to add their own ``notif`` values).
"""
from odoo import models

FORCE_EMAIL_KEY = "woow_mail_workspace_force_email"


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    def _notify_get_recipients(self, message, msg_vals, **kwargs):
        """Promote 'inbox' recipients to 'email' for workspace sends only."""
        recipients = super()._notify_get_recipients(message, msg_vals, **kwargs)
        if not self.env.context.get(FORCE_EMAIL_KEY):
            return recipients
        for recipient in recipients:
            # Only 'inbox' is promoted. 'sms' and any other channel added by
            # another module is left alone: this module has no business
            # rerouting a delivery channel it does not own.
            if recipient.get("notif") == "inbox":
                recipient["notif"] = "email"
        return recipients
