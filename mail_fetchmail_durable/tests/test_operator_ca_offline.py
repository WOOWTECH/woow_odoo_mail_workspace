"""Offline tests for the addon's operator-CA lookup.

Imports hooks.py directly (no Odoo runtime needed) because only the ssl-context
helper is under test.
"""
import importlib.util, os, pathlib, ssl, sys, tempfile, unittest

SRC = pathlib.Path(__file__).resolve().parent / 'hooks_under_test.py'


def load(dirpath):
    """Load a copy of hooks.py placed in dirpath, so _operator_ca_path resolves there."""
    target = pathlib.Path(dirpath) / 'hooks_under_test.py'
    target.write_text(pathlib.Path(os.environ['HOOKS_SRC']).read_text())
    spec = importlib.util.spec_from_file_location('hooks_under_test_%s' % abs(hash(dirpath)), target)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class OperatorCATests(unittest.TestCase):
    def test_no_ca_file_falls_back_to_platform_trust(self):
        with tempfile.TemporaryDirectory() as d:
            m = load(d)
            self.assertIsNone(m._operator_ca_path())
            ctx = m.strict_mail_ssl_context()
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertGreater(len(ctx.get_ca_certs()), 0, 'platform roots must still load')

    def test_ca_file_is_added_to_platform_trust_not_replacing_it(self):
        with tempfile.TemporaryDirectory() as d:
            m = load(d)
            baseline = len(m.strict_mail_ssl_context().get_ca_certs())
            (pathlib.Path(d) / 'mail-ca.crt').write_text(os.environ['TEST_CA_PEM'])
            self.assertIsNotNone(m._operator_ca_path())
            ctx = m.strict_mail_ssl_context()
            self.assertEqual(len(ctx.get_ca_certs()), baseline + 1,
                             'the operator CA must EXTEND the public roots, not replace them')
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_a_directory_named_like_the_ca_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            m = load(d)
            (pathlib.Path(d) / 'mail-ca.crt').mkdir()
            self.assertIsNone(m._operator_ca_path(), 'a directory must not be treated as a CA')
            self.assertTrue(m.strict_mail_ssl_context().check_hostname)

    def test_verification_is_never_relaxed(self):
        with tempfile.TemporaryDirectory() as d:
            m = load(d)
            (pathlib.Path(d) / 'mail-ca.crt').write_text(os.environ['TEST_CA_PEM'])
            ctx = m.strict_mail_ssl_context()
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_plaintext_warning_is_logged_once_per_server(self):
        with tempfile.TemporaryDirectory() as d:
            m = load(d)
            with self.assertLogs(m._logger, level='WARNING') as logs:
                self.assertTrue(m._warn_plaintext_once('IMAP', 'imap.example.test', 143))
                self.assertFalse(m._warn_plaintext_once('IMAP', 'imap.example.test', 143))
                self.assertTrue(m._warn_plaintext_once('IMAP', 'other.example.test', 143))
            self.assertEqual(len(logs.output), 2)
            self.assertNotIn('password', ' '.join(logs.output).lower())


if __name__ == '__main__':
    unittest.main()
