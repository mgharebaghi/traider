"""Read-only LBank diagnostic must never leak secrets or touch order endpoints."""
import json
import unittest
from unittest.mock import Mock

from lbank_access_check import (ACCOUNT_URL, PERMISSIONS_URL, TIME_URL,
                                check_access, sign_payload, summarize)


def response(status=200, data=None, content_type="application/json"):
    r = Mock(status_code=status, headers={"Content-Type": content_type})
    r.json.return_value = data or {}
    return r


class LBankAccessTests(unittest.TestCase):
    def test_authentication_does_not_mutate_input_or_send_secret(self):
        params = {"asset": "USDT", "productGroup": "SwapU"}
        payload, headers = sign_payload("sample-key", "sample-secret", params,
                                       timestamp="1665990154559", nonce="n" * 35)
        self.assertEqual(params, {"asset": "USDT", "productGroup": "SwapU"})
        self.assertEqual(len(payload["sign"]), 64)
        self.assertEqual(headers["signature_method"], "HmacSHA256")
        self.assertNotIn("sample-secret", json.dumps([payload, headers]))
        self.assertNotIn("timestamp", payload)

    def test_rsa_signature_can_be_verified_with_public_key(self):
        import base64
        import hashlib
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        secret = base64.b64encode(key.private_bytes(serialization.Encoding.DER,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())).decode()
        payload, headers = sign_payload("example", secret, {}, timestamp="123", nonce="abc")
        canonical = "api_key=example&echostr=abc&signature_method=RSA&timestamp=123"
        digest = hashlib.md5(canonical.encode()).hexdigest().upper().encode()
        key.public_key().verify(base64.b64decode(payload["sign"]), digest,
                                padding.PKCS1v15(), hashes.SHA256())

    def test_api_permission_success_does_not_certify_trading(self):
        session = Mock()
        session.post.return_value = response(data={"code": 0, "result": "true", "data": {
            "enableFuturesTrading": True, "enableReading": True,
            "enableWithdrawals": False, "api_key": "SECRET-KEY"}})
        session.get.side_effect = [response(data={"success": True, "error_code": 0}),
                                   response(403, content_type="text/html")]
        report = check_access("SECRET-KEY", "SECRET-VALUE", session=session)
        self.assertTrue(report["permissions"]["permissions"]["enableFuturesTrading"])
        self.assertFalse(report["live_trading_ready"])
        self.assertEqual(report["futures_account"]["http_status"], 403)
        self.assertNotIn("SECRET", json.dumps(report))
        calls = [(args.args[0], args.kwargs) for args in session.get.call_args_list + session.post.call_args_list]
        self.assertEqual({url for url, _ in calls}, {ACCOUNT_URL, TIME_URL, PERMISSIONS_URL})
        self.assertTrue(all(kwargs["allow_redirects"] is False for _, kwargs in calls))
        session.close.assert_not_called()

    def test_html_block_response_never_prints_response_body(self):
        r = response(403, content_type="text/html")
        r.json.side_effect = ValueError("api_key=secret")
        r.text = "request rejected for api_key=secret"
        report = summarize(r)
        self.assertFalse(report["api_response"])
        self.assertNotIn("secret", json.dumps(report))

    def test_network_exception_does_not_disclose_signed_url(self):
        session = Mock()
        session.post.side_effect = RuntimeError("https://example/?api_key=SECRET")
        session.get.side_effect = RuntimeError("sign=SECRET")
        report = check_access("example", "test", session=session, account_post=True)
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertEqual(report["futures_account"]["error_type"], "RuntimeError")
        self.assertEqual(session.post.call_args_list[-1].args[0], ACCOUNT_URL)
        self.assertIn("json", session.post.call_args_list[-1].kwargs)

    def test_summary_rejects_untrusted_status_strings_and_unknown_flags(self):
        r = response(data={"code": "api_key=secret", "result": "secret", "msg": "secret",
                           "data": {"enableReading": "secret", "api_key": "secret"}})
        self.assertNotIn("secret", json.dumps(summarize(r, permissions=True)))


if __name__ == "__main__":
    unittest.main()
