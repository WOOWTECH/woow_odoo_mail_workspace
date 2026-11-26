# -*- coding: utf-8 -*-
"""Process-level TLS hardening hooks for Odoo mail transports.

The ORM models in this addon are tenant/database scoped.  This post_load hook is
process scoped because Python's stdlib POP3/IMAP/SMTP constructors are not Odoo
models.  It is deliberately narrow: only Odoo's mail/fetchmail wrapper classes
are replaced, and plaintext server configurations remain plaintext so existing
records must still opt in to SSL/TLS/STARTTLS (a one-time warning is logged per
server when one does not).  Platform trust is optionally extended by a
`mail-ca.crt` placed next to this module (see strict_mail_ssl_context()).
"""

import logging
import os
import ssl
import threading

_logger = logging.getLogger(__name__)

_PLAINTEXT_WARNED = set()
_PLAINTEXT_WARNED_LOCK = threading.Lock()


def _warn_plaintext_once(kind, host, port):
    """Log, once per process and server, that a mail connection has no SSL/TLS.

    Warning only: plaintext configurations keep working exactly as before.
    """
    key = (kind, str(host), str(port))
    with _PLAINTEXT_WARNED_LOCK:
        if key in _PLAINTEXT_WARNED:
            return False
        _PLAINTEXT_WARNED.add(key)
    _logger.warning(
        "%s connection to %s:%s is configured without SSL/TLS; credentials and mail travel in plaintext. "
        "Enable SSL/TLS (or STARTTLS for SMTP) on this server record.",
        kind, host, port,
    )
    return True


# An operator-supplied CA bundle, looked up next to this addon. See
# strict_mail_ssl_context() for why this exists and what it deliberately is not.
_CA_FILENAME = "mail-ca.crt"


def _operator_ca_path():
    """Path to an operator-installed CA bundle beside this addon, or None.

    Returns the path only when it is a readable regular file, so a stray
    directory or dangling symlink falls back to platform trust instead of
    raising inside a connection attempt.
    """
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), _CA_FILENAME)
    try:
        if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            return candidate
    except OSError:
        pass
    return None


def strict_mail_ssl_context():
    """Return a client context with hostname and chain verification.

    Trust is platform trust, OPTIONALLY EXTENDED by a CA bundle the operator
    places next to this addon as `mail-ca.crt`.

    Why the file lives here rather than in the usual places: a mail server on a
    private CA has to be trusted somehow, and on a Helm-managed deployment the
    two obvious routes are both unavailable. Setting SSL_CERT_FILE means editing
    the Deployment, which `helm upgrade` reverts -- silently, so mail would stop
    working at the next routine release with no visible cause. Writing into the
    container's system trust store needs a writable rootfs and root, and is lost
    on every pod restart anyway. The addons directory is a PVC: it survives
    restarts and upgrades, and it is already the thing that delivers this addon.

    What this is NOT: the file is read, never written. This repository ships no CA.
    Deployers may supply `mail-ca.crt` next to the module; it is then trusted IN
    ADDITION to the system roots for every IMAP/POP3/SMTP SSL connection made by
    this Odoo process (it is not limited to one host, and carries no name
    constraints), so whoever holds that CA's key can issue certificates these
    connections will accept.  Deployers should only ship a CA they control and
    delete the file when it is not needed.  It extends trust; it never relaxes
    verification. check_hostname
    and CERT_REQUIRED stay on in both branches, so a private CA buys recognition
    of one server, not permission to skip verification.
    """
    ca = _operator_ca_path()
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    if ca:
        # load_verify_locations ADDS to the default roots loaded above, so public
        # certificates keep verifying normally.
        context.load_verify_locations(cafile=ca)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = max(getattr(ssl.TLSVersion, "TLSv1_2"), context.minimum_version)
    return context


def _patch_fetchmail_tls():
    from odoo.addons.mail.models import fetchmail

    class StrictIMAP4Connection(fetchmail.IMAP4Connection):
        def __init__(self, server, port, is_ssl):
            if is_ssl:
                obj = fetchmail.IMAP4_SSL(
                    server,
                    port,
                    ssl_context=strict_mail_ssl_context(),
                )
                setattr(self, "__obj__", obj)
                self._IMAP4Connection__obj__ = obj
            else:
                _warn_plaintext_once("IMAP", server, port)
                super().__init__(server, port, is_ssl)

    class StrictPOP3Connection(fetchmail.POP3Connection):
        def __init__(self, server, port, is_ssl, timeout=fetchmail.MAIL_TIMEOUT):
            if is_ssl:
                obj = fetchmail.POP3_SSL(
                    server,
                    port,
                    timeout=timeout,
                    context=strict_mail_ssl_context(),
                )
                setattr(self, "__obj__", obj)
                self._POP3Connection__obj__ = obj
            else:
                _warn_plaintext_once("POP3", server, port)
                super().__init__(server, port, is_ssl, timeout=timeout)

    fetchmail.IMAP4Connection = StrictIMAP4Connection
    fetchmail.POP3Connection = StrictPOP3Connection


def _patch_smtp_tls():
    from odoo.addons.base.models import ir_mail_server

    class StrictSMTPConnection(ir_mail_server.SMTPConnection):
        def __init__(self, server, port, encryption, context=None):
            if encryption not in ("ssl", "starttls"):
                _warn_plaintext_once("SMTP", server, port)
            if encryption == "ssl" and context is None:
                context = strict_mail_ssl_context()
            self._strict_default_context = context or strict_mail_ssl_context()
            super().__init__(server, port, encryption, context=context)

        def starttls(self, *args, **kwargs):
            if kwargs.get("context") is None:
                kwargs["context"] = self._strict_default_context
            obj = getattr(self, "__obj__", None) or self._SMTPConnection__obj__
            return obj.starttls(*args, **kwargs)

    ir_mail_server.SMTPConnection = StrictSMTPConnection


def post_load():
    """Patch Odoo wrapper classes after addon load.

    This is reversible by uninstalling/removing the addon and restarting Odoo.
    """
    _patch_fetchmail_tls()
    _patch_smtp_tls()
