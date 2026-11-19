"""URL-safe base64 without padding, as passkeys, web push, native push and the mail sign-in use it (#387).

Written against the four copies as they stood, and unchanged by folding them into one: the same answers before and after."""
from __future__ import annotations

import base64
import binascii
import json
import unittest

from app import ms_mail, native_push, push, webauthn

SAMPLES = [b"", b"f", b"fo", b"foo", b"foob", b"\xfb\xff\xfe", bytes(range(256))]


class Base64UrlTests(unittest.TestCase):
    def test_every_encoder_gives_the_same_unpadded_url_safe_text(self):
        for data in SAMPLES:
            expected = base64.urlsafe_b64encode(data).decode().rstrip("=")
            for encode in (webauthn.b64url, push._b64, native_push._b64):
                text = encode(data)
                self.assertEqual(text, expected)
                self.assertIsInstance(text, str)
                self.assertNotIn("=", text)
                self.assertFalse(set(text) & set("+/"))

    def test_every_decoder_reads_text_with_or_without_padding(self):
        for data in SAMPLES:
            unpadded = base64.urlsafe_b64encode(data).decode().rstrip("=")
            for decode in (webauthn.unb64url, push._unb64):
                self.assertEqual(decode(unpadded), data)
        self.assertEqual(push._unb64("Zm9vYg=="), b"foob")
        self.assertEqual(webauthn.unb64url("Zm9vYg=="), b"foob")

    def test_a_passkey_value_that_is_not_text_or_not_base64_is_a_passkey_error(self):
        for bad in (None, 5, b"Zm9v", ["Zm9v"]):
            with self.assertRaises(webauthn.WebAuthnError):
                webauthn.unb64url(bad)
        with self.assertRaises(webauthn.WebAuthnError):
            webauthn.unb64url("a")  # One character can never be a whole byte

    def test_a_push_value_that_is_not_base64_raises_as_the_standard_decoder_does(self):
        with self.assertRaises(binascii.Error):
            push._unb64("a")

    def test_the_mail_account_is_read_from_an_unpadded_token(self):
        for name in ("a@example.test", "ab@example.test", "abc@example.test"):  # Three lengths: each padding case
            claims = base64.urlsafe_b64encode(json.dumps({"preferred_username": name}).encode()).decode().rstrip("=")
            self.assertEqual(ms_mail._account_of(f"header.{claims}.signature"), name)
        self.assertIsNone(ms_mail._account_of("header.not-base64!.signature"))
        self.assertIsNone(ms_mail._account_of(None))


if __name__ == "__main__":
    unittest.main()
