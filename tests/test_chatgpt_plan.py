"""ChatGPT-plan sign-in and the Responses client, tested against local fakes of OpenAI's servers.

No network and no real account: a fake sign-in server issues codes and tokens (ID tokens are
signed with a throwaway RSA key), a fake API streams Responses events, and a fake "browser"
follows the sign-in redirect to the loopback callback.
"""

import base64
import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from flask import Flask, Response, jsonify, redirect, request
from werkzeug.serving import make_server

from cic.agent import siwc
from cic.agent.llm import LLMError
from cic.agent.responses import ResponsesClient, StopBatch, read_stream
from tests.test_e2e import _browser_available
from tests.test_llm_policy import FakeModel


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------- a throwaway RSA key (test only)

def _probable_prime(n, rounds=24):
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _probable_prime(candidate):
            return candidate


def make_key(bits=1024):
    e = 65537
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and math.gcd(e, phi) == 1:
            return {"n": p * q, "e": e, "d": pow(e, -1, phi), "kid": "test-key"}


def sign_rs256(key, header, claims):
    head = b64url(json.dumps(header).encode())
    body = b64url(json.dumps(claims).encode())
    signing_input = f"{head}.{body}".encode()
    size = (key["n"].bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), key["d"], key["n"]).to_bytes(size, "big")
    return f"{head}.{body}.{b64url(signature)}"


def jwk_of(key):
    size = (key["n"].bit_length() + 7) // 8
    return {"kty": "RSA", "kid": key["kid"], "alg": "RS256", "use": "sig",
            "n": b64url(key["n"].to_bytes(size, "big")), "e": b64url(key["e"].to_bytes(3, "big"))}


def _has_cryptography():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------- fakes

class Served:
    def __init__(self, app):
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class FakeAuth:
    def __init__(self, key):
        self.key = key
        self.base = ""
        self.issued_client = "oaiapp_test123"
        self.grant_scope = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
        self.callback_extra = {}
        self.refresh_error = None
        self.codes, self.refresh_tokens = {}, set()
        self.authorize_params, self.token_forms, self.revoked = [], [], []
        app = Flask("fake-auth")

        @app.get("/.well-known/openid-configuration")
        def discovery():
            return jsonify({"issuer": self.base, "jwks_uri": self.base + "/jwks",
                            "revocation_endpoint": self.base + "/oauth/revoke"})

        @app.get("/jwks")
        def jwks():
            return jsonify({"keys": [jwk_of(self.key)]})

        @app.get("/api/accounts/authorize")
        def authorize():
            q = request.args.to_dict()
            self.authorize_params.append(q)
            client = self.issued_client if q["client_id"] == "dynamic_agent_client" else q["client_id"]
            code = secrets.token_hex(8)
            self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"],
                                "nonce": q["nonce"], "client": client}
            answer = {"code": code, "scope": self.grant_scope, "state": q["state"], "client_id": client}
            answer.update(self.callback_extra)
            answer = {k: v for k, v in answer.items() if v is not None}
            return redirect(q["redirect_uri"] + "?" + urllib.parse.urlencode(answer), code=302)

        @app.post("/api/accounts/oauth/token")
        def token():
            form = request.form.to_dict()
            self.token_forms.append(form)
            if form.get("grant_type") == "authorization_code":
                entry = self.codes.pop(form.get("code"), None)
                if not entry:
                    return jsonify({"error": "invalid_grant"}), 400
                challenge = b64url(hashlib.sha256(form["code_verifier"].encode()).digest())
                if (challenge != entry["challenge"] or form["redirect_uri"] != entry["redirect_uri"]
                        or form["client_id"] != entry["client"] or form.get("resource") != siwc.RESOURCE):
                    return jsonify({"error": "invalid_request"}), 400
                return jsonify(self._tokens(entry["client"], entry["nonce"]))
            if form.get("grant_type") == "refresh_token":
                if self.refresh_error:
                    return jsonify({"error": self.refresh_error}), 400
                if form.get("refresh_token") not in self.refresh_tokens:
                    return jsonify({"error": "invalid_grant"}), 400
                self.refresh_tokens.discard(form["refresh_token"])
                return jsonify(self._tokens(form["client_id"], None, with_id=False))
            return jsonify({"error": "unsupported_grant_type"}), 400

        @app.post("/oauth/revoke")
        def revoke():
            self.revoked.append(request.form.to_dict())
            return "", 200

        self.app = app

    def _tokens(self, client, nonce, with_id=True):
        refresh = "rt_" + secrets.token_hex(8)
        self.refresh_tokens.add(refresh)
        out = {"access_token": "at_" + secrets.token_hex(8), "refresh_token": refresh, "token_type": "Bearer",
               "expires_in": 3600, "scope": self.grant_scope}
        if with_id:
            now = int(time.time())
            claims = {"iss": self.base, "aud": client, "sub": "user-123", "exp": now + 3600, "iat": now,
                      "nonce": nonce}
            out["id_token"] = sign_rs256(self.key, {"alg": "RS256", "kid": self.key["kid"], "typ": "JWT"}, claims)
        return out


def fake_browser(url, preconnect=False):
    """Follow the sign-in page's redirect to the loopback callback, as a browser would.

    With preconnect, first open an idle connection to the callback port and leave it open.
    """
    def go():
        if preconnect:
            import socket
            redirect_uri = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["redirect_uri"]
            port = urllib.parse.urlsplit(redirect_uri).port
            idle = socket.create_connection(("127.0.0.1", port))
            idle_sockets.append(idle)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            opener.open(url, timeout=10).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


idle_sockets = []


def sse(events, request_id="req_stream"):
    def make():
        def generate():
            for event in events:
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        return Response(generate(), mimetype="text/event-stream", headers={"x-request-id": request_id})
    return make


def http_error(status, body, request_id="req_http"):
    return lambda: (jsonify(body), status, {"x-request-id": request_id})


def delta(text):
    return {"type": "response.output_text.delta", "delta": text}


def completed(text=None, usage=(120, 8)):
    output = [{"type": "message", "content": [{"type": "output_text", "text": text}]}] if text else []
    return {"type": "response.completed",
            "response": {"output": output, "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}}


class FakeAPI:
    def __init__(self, responder=None):
        self.plan, self.requests, self.responder = [], [], responder
        app = Flask("fake-api")

        @app.post("/v1/responses")
        def responses():
            body = request.get_json()
            self.requests.append({"auth": request.headers.get("Authorization"), "body": body})
            if self.responder:
                return self.responder(body)()
            step = self.plan.pop(0) if self.plan else sse([completed("OK")])
            return step()

        @app.get("/v1/models")
        def models():
            return jsonify({"models": [
                {"slug": "gpt-test-1", "display_name": "GPT Test 1", "visibility": "list"},
                {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
                {"slug": "gpt-unmarked", "display_name": "Unmarked"}]})

        self.app = app


class _Env:
    """Temporary environment: credentials in a temp folder, no proxy for 127.0.0.1."""

    def __init__(self):
        self.saved = {k: os.environ.get(k) for k in ("CIC_CHATGPT_DIR", "NO_PROXY", "no_proxy")}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CIC_CHATGPT_DIR"] = self.tmp.name
        for k in ("NO_PROXY", "no_proxy"):
            os.environ[k] = "127.0.0.1,localhost" + ("," + self.saved[k] if self.saved[k] else "")

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


# ---------------------------------------------------------------- tests

class ConfigTests(unittest.TestCase):
    def test_env_file_later_lines_win_and_comments_are_dropped(self):
        from cic.config import load_env
        keys = ("CIC_T_A", "CIC_T_B", "CIC_T_C", "CIC_T_D", "CIC_T_E")
        saved = {k: os.environ.pop(k, None) for k in keys}
        os.environ["CIC_T_D"] = "from-environment"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                path.write_text("CIC_T_A=api\n# CIC_T_A=ignored\nCIC_T_A=chatgpt\n"
                                "CIC_T_B=low     # optional note\nCIC_T_C=\"a # b\"\nCIC_T_D=from-file\n"
                                "CIC_T_E=\"quoted\"  # note\n",
                                encoding="utf-8")
                load_env(path)
            self.assertEqual([os.environ[k] for k in keys], ["chatgpt", "low", "a # b", "from-environment", "quoted"])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class KeyFileTests(unittest.TestCase):
    def test_key_file_formats(self):
        from cic.config import read_key_file
        with tempfile.TemporaryDirectory() as tmp:
            def key_from(text):
                path = Path(tmp) / "k.env"
                path.write_text(text, encoding="utf-8")
                return read_key_file(str(path))
            self.assertEqual(key_from("# note\nOPENROUTER_API_KEY=sk-or-1\n"), "sk-or-1")
            self.assertEqual(key_from("OTHER=x\nCIC_API_KEY='sk-2'\n"), "sk-2")
            self.assertEqual(key_from("export SOME_KEY=sk-3\n"), "sk-3")
            self.assertEqual(key_from("sk-bare-4\n"), "sk-bare-4")
            with self.assertRaisesRegex(ValueError, "No API key"):
                key_from("A=1\nB=2\n")
            with self.assertRaisesRegex(ValueError, "does not exist"):
                read_key_file(str(Path(tmp) / "missing.env"))


class SignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = make_key()

    def setUp(self):
        self.env = _Env()
        self.fake = FakeAuth(self.key)
        self.served = Served(self.fake.app)
        self.fake.base = self.served.base
        self.said = []

    def tearDown(self):
        while idle_sockets:
            idle_sockets.pop().close()
        self.served.close()
        self.env.restore()

    def login(self, **kw):
        return siwc.login(open_browser=fake_browser, port=0, timeout=20, auth_base=self.fake.base,
                          say=self.said.append, **kw)

    def test_pkce_is_s256(self):
        verifier, challenge = siwc.pkce_pair()
        self.assertEqual(challenge, b64url(hashlib.sha256(verifier.encode()).digest()))
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotIn("=", challenge)

    def test_first_sign_in_registers_and_saves_privately(self):
        record = self.login()
        q = self.fake.authorize_params[0]
        self.assertEqual(q["client_id"], "dynamic_agent_client")
        self.assertEqual(q["agent_name_hint"], siwc.AGENT_NAME)
        self.assertEqual(q["scope"].split(),
                         "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct".split())
        self.assertEqual(q["resource"], "https://api.openai.com/v1")
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertTrue(q["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertRegex(q["redirect_uri"], r"^http://127\.0\.0\.1:\d+/auth/callback$")
        form = self.fake.token_forms[0]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["client_id"], "oaiapp_test123")
        self.assertEqual(form["redirect_uri"], q["redirect_uri"])
        self.assertTrue(record["plan_usage"])
        path = siwc.credentials_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["client_id"], "oaiapp_test123")
        printed = "\n".join(self.said)
        for secret in (saved["access_token"], saved["refresh_token"], saved["id_token"]):
            self.assertNotIn(secret, printed)

    def test_returning_sign_in_reuses_client_and_host(self):
        self.login()
        self.login()
        first, second = self.fake.authorize_params
        self.assertEqual(second["client_id"], "oaiapp_test123")
        self.assertNotIn("agent_name_hint", second)
        self.assertEqual(second["ext_agent_host_id"], first["ext_agent_host_id"])

    def test_wrong_state_is_ignored_and_declined_saves_nothing(self):
        self.fake.callback_extra = {"state": "someone-else"}
        with self.assertRaisesRegex(siwc.SignInError, "No answer from the browser within 3 seconds"):
            siwc.login(open_browser=fake_browser, port=0, timeout=3, auth_base=self.fake.base, say=self.said.append)
        self.fake.callback_extra = {"error": "access_denied", "code": None}
        with self.assertRaisesRegex(siwc.SignInError, "cancelled"):
            self.login()
        self.assertFalse(siwc.credentials_path().exists())
        self.assertEqual(self.fake.token_forms, [])

    def test_an_idle_browser_connection_does_not_block_the_callback(self):
        started = time.time()
        record = siwc.login(open_browser=lambda url: fake_browser(url, preconnect=True), port=0, timeout=20,
                            auth_base=self.fake.base, say=self.said.append)
        self.assertTrue(record["plan_usage"])
        self.assertLess(time.time() - started, 8)

    def test_a_different_client_in_the_answer_is_refused(self):
        siwc.save_record({"client_id": "oaiapp_saved"})
        self.fake.callback_extra = {"client_id": "oaiapp_other"}
        with self.assertRaisesRegex(siwc.SignInError, "different client"):
            self.login()
        self.assertEqual(siwc.load_record(), {"client_id": "oaiapp_saved"})

    def test_signing_in_again_revokes_the_old_session_first(self):
        self.login()
        old_refresh = siwc.load_record()["refresh_token"]
        self.login()
        self.assertEqual([r["token"] for r in self.fake.revoked], [old_refresh])
        self.assertNotEqual(siwc.load_record()["refresh_token"], old_refresh)

    def test_a_damaged_credentials_file(self):
        siwc.credentials_path().parent.mkdir(parents=True, exist_ok=True)
        siwc.credentials_path().write_text("{not json", encoding="utf-8")
        with self.assertRaises(siwc.DamagedCredentials):
            siwc.access_token(auth_base=self.fake.base)
        self.assertIs(siwc.logout(auth_base=self.fake.base), False)
        self.assertFalse(siwc.credentials_path().exists())

    def test_without_plan_permission_there_is_no_inference(self):
        self.fake.grant_scope = "email offline_access openid profile"
        record = self.login()
        self.assertFalse(record["plan_usage"])
        with self.assertRaisesRegex(siwc.SignInError, "consent"):
            siwc.access_token(auth_base=self.fake.base)

    def test_refresh_rotates_and_an_unusable_refresh_token_clears_tokens(self):
        self.login()
        record = siwc.load_record()
        old_refresh = record["refresh_token"]
        record["expires_at"] = time.time() - 10
        siwc.save_record(record)
        token = siwc.access_token(auth_base=self.fake.base)
        record = siwc.load_record()
        self.assertEqual(token, record["access_token"])
        self.assertNotEqual(record["refresh_token"], old_refresh)
        form = self.fake.token_forms[-1]
        self.assertEqual((form["grant_type"], form["client_id"], form["resource"]),
                         ("refresh_token", "oaiapp_test123", siwc.RESOURCE))
        self.assertNotIn("scope", form)
        self.fake.refresh_error = "refresh_token_expired"
        with self.assertRaises(siwc.SignInRequired):
            siwc.access_token(force_refresh=True, auth_base=self.fake.base)
        record = siwc.load_record()
        self.assertIsNone(record["access_token"])
        self.assertEqual(record["client_id"], "oaiapp_test123")

    def test_a_refresh_answer_without_an_access_token_keeps_the_sign_in(self):
        self.login()
        before = siwc.load_record()
        with mock.patch.object(siwc, "_request_json", return_value={"refresh_token": "x"}):
            with self.assertRaisesRegex(siwc.SignInError, "no access token"):
                siwc.access_token(force_refresh=True, auth_base=self.fake.base)
        self.assertEqual(siwc.load_record()["refresh_token"], before["refresh_token"])

    def test_plan_permission_is_checked_again_after_a_refresh(self):
        self.login()
        self.fake.grant_scope = "email offline_access openid profile"
        with self.assertRaisesRegex(siwc.SignInError, "not granted"):
            siwc.access_token(force_refresh=True, auth_base=self.fake.base)

    def test_other_refresh_failures_keep_the_sign_in(self):
        self.login()
        self.fake.refresh_error = "temporarily_unavailable"
        with self.assertRaises(siwc.SignInError) as caught:
            siwc.access_token(force_refresh=True, auth_base=self.fake.base)
        self.assertNotIsInstance(caught.exception, siwc.SignInRequired)
        self.assertTrue(siwc.load_record()["refresh_token"])

    def test_id_token_checks(self):
        key, issuer = self.key, "https://issuer.test"
        good = {"iss": issuer, "aud": "c1", "exp": time.time() + 600, "nonce": "n1", "sub": "s"}
        header = {"alg": "RS256", "kid": key["kid"]}
        jwks = {"keys": [jwk_of(key)]}
        token = sign_rs256(key, header, good)
        self.assertEqual(siwc.verify_id_token(token, "c1", "n1", issuer, jwks)["sub"], "s")
        for change, pattern in [({"nonce": "n2"}, "nonce"), ({"aud": "c2"}, "different client"),
                                ({"iss": "https://other.test"}, "issuer"), ({"exp": time.time() - 4000}, "expired")]:
            with self.assertRaisesRegex(siwc.SignInError, pattern):
                siwc.verify_id_token(sign_rs256(key, header, dict(good, **change)), "c1", "n1", issuer, jwks)
        head, _, signature = token.split(".")
        tampered = f"{head}.{b64url(json.dumps(dict(good, sub='other')).encode())}.{signature}"
        with self.assertRaisesRegex(siwc.SignInError, "signature"):
            siwc.verify_id_token(tampered, "c1", "n1", issuer, jwks)
        with self.assertRaisesRegex(siwc.SignInError, "signing keys"):
            siwc.verify_id_token(token, "c1", "n1", issuer, None)
        hs256 = f"{b64url(json.dumps({'alg': 'HS256'}).encode())}.{token.split('.')[1]}.{signature}"
        with self.assertRaisesRegex(siwc.SignInError, "HS256"):
            siwc.verify_id_token(hs256, "c1", "n1", issuer, jwks)

    @unittest.skipUnless(_has_cryptography(), "cryptography is not installed")
    def test_signature_check_agrees_with_an_independent_implementation(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private.public_key().public_numbers()
        jwk = {"kty": "RSA", "n": b64url(numbers.n.to_bytes(256, "big")), "e": b64url(numbers.e.to_bytes(3, "big"))}
        signature = private.sign(b"head.body", padding.PKCS1v15(), hashes.SHA256())
        self.assertTrue(siwc.rs256_valid(jwk, b"head.body", signature))
        self.assertFalse(siwc.rs256_valid(jwk, b"head.bodx", signature))

    @unittest.skipUnless(_has_cryptography(), "cryptography is not installed")
    def test_es256_check_agrees_with_an_independent_implementation(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        private = ec.generate_private_key(ec.SECP256R1())
        numbers = private.public_key().public_numbers()
        jwk = {"kty": "EC", "crv": "P-256", "x": b64url(numbers.x.to_bytes(32, "big")),
               "y": b64url(numbers.y.to_bytes(32, "big"))}
        r, s_value = decode_dss_signature(private.sign(b"head.body", ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        self.assertTrue(siwc.es256_valid(jwk, b"head.body", signature))
        self.assertFalse(siwc.es256_valid(jwk, b"head.bodx", signature))
        claims = {"iss": "https://issuer.test", "aud": "c1", "exp": time.time() + 600, "nonce": "n1", "sub": "s"}
        head = b64url(json.dumps({"alg": "ES256", "kid": "ec1"}).encode())
        body = b64url(json.dumps(claims).encode())
        r, s_value = decode_dss_signature(private.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        token = f"{head}.{body}.{b64url(r.to_bytes(32, 'big') + s_value.to_bytes(32, 'big'))}"
        jwks = {"keys": [dict(jwk, kid="ec1")]}
        self.assertEqual(siwc.verify_id_token(token, "c1", "n1", "https://issuer.test", jwks)["sub"], "s")

    def test_logout_revokes_and_keeps_the_client_registration(self):
        self.login()
        refresh = siwc.load_record()["refresh_token"]
        self.assertTrue(siwc.logout(auth_base=self.fake.base))
        self.assertEqual(self.fake.revoked[0]["token"], refresh)
        self.assertEqual(self.fake.revoked[0]["token_type_hint"], "refresh_token")
        record = siwc.load_record()
        self.assertNotIn("access_token", record)
        self.assertEqual(record["client_id"], "oaiapp_test123")
        with self.assertRaises(siwc.SignInRequired):
            siwc.access_token(auth_base=self.fake.base)


class ResponsesClientTests(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.fake = FakeAPI()
        self.served = Served(self.fake.app)
        self.tokens = []

        def provider(force_refresh=False):
            self.tokens.append(force_refresh)
            return "tok-new" if force_refresh else "tok-old"

        self.client = ResponsesClient("gpt-test-1", token_provider=provider, api_base=self.served.base + "/v1",
                                      backoff=0)
        self.messages = [{"role": "system", "content": "POLICY"}, {"role": "user", "content": "PAGE"}]

    def tearDown(self):
        self.served.close()
        self.env.restore()

    def test_streamed_reply_and_request_shape(self):
        self.fake.plan = [sse([{"type": "response.created"}, delta('{"action": '), delta('"done"}'), completed()])]
        text, usage = self.client.complete(self.messages, max_tokens=900)
        self.assertEqual(text, '{"action": "done"}')
        self.assertEqual(usage, {"prompt_tokens": 120, "completion_tokens": 8})
        sent = self.fake.requests[0]
        body = sent["body"]
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["instructions"], "POLICY")
        self.assertEqual(body["input"], [{"role": "user", "content": "PAGE"}])
        for field in ("temperature", "top_p", "max_output_tokens", "max_tokens", "metadata", "user",
                      "previous_response_id", "truncation"):
            self.assertNotIn(field, body)
        self.assertEqual(sent["auth"], "Bearer tok-old")

    def test_text_from_the_completed_event_when_there_are_no_deltas(self):
        self.fake.plan = [sse([completed("OK")])]
        self.assertEqual(self.client.complete(self.messages)[0], "OK")

    def test_a_401_refreshes_once(self):
        self.fake.plan = [http_error(401, {"detail": "token expired"}), sse([delta("OK"), completed()])]
        self.assertEqual(self.client.complete(self.messages)[0], "OK")
        self.assertEqual(self.tokens, [False, True])
        self.assertEqual(self.fake.requests[1]["auth"], "Bearer tok-new")

    def test_a_second_401_stops_after_one_refresh(self):
        self.fake.plan = [http_error(401, {"detail": "no"}), http_error(401, {"detail": "still no"}, "req_2")]
        with self.assertRaises(StopBatch) as caught:
            self.client.complete(self.messages)
        self.assertEqual(self.tokens, [False, True])
        self.assertEqual((caught.exception.status, caught.exception.request_id), (401, "req_2"))

    def test_a_503_is_retried(self):
        self.fake.plan = [http_error(503, {"detail": "busy"}), sse([delta("OK"), completed()])]
        self.assertEqual(self.client.complete(self.messages)[0], "OK")
        self.assertEqual(len(self.fake.requests), 2)

    def test_error_events_keep_their_code_and_request_id(self):
        top = {"type": "error", "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}
        nested = {"type": "error", "error": {"code": "subscription_sharing_user_not_eligible", "message": "no"}}
        for event, code in [(top, "subscription_sharing_usage_limit_exceeded"),
                            (nested, "subscription_sharing_user_not_eligible")]:
            self.fake.plan = [sse([event], request_id="req_ev")]
            with self.assertRaises(StopBatch) as caught:
                self.client.complete(self.messages)
            self.assertEqual((caught.exception.code, caught.exception.request_id), (code, "req_ev"))
            self.assertIn("req_ev", str(caught.exception)[:120])
        self.fake.plan = [sse([{"type": "response.incomplete", "response": {"incomplete_details": {
            "reason": "max_output_tokens"}}}])]
        with self.assertRaisesRegex(LLMError, "max_output_tokens"):
            self.client.complete(self.messages)

    def test_plan_limit_stops_without_retrying(self):
        failed = {"type": "response.failed", "response": {"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}
        self.fake.plan = [sse([delta("partial"), failed])]
        with self.assertRaisesRegex(StopBatch, "usage limit"):
            self.client.complete(self.messages)
        self.assertEqual(len(self.fake.requests), 1)
        self.fake.plan = [http_error(429, {"error": {"code": "subscription_sharing_usage_limit_exceeded",
                                                     "message": "limit"}})]
        with self.assertRaises(StopBatch):
            self.client.complete(self.messages)
        self.assertEqual(len(self.fake.requests), 2)

    def test_unsupported_capability_names_the_param(self):
        self.fake.plan = [http_error(400, {"error": {"code": "subscription_sharing_unsupported_capability",
                                                     "param": "reasoning", "message": "not here"}})]
        with self.assertRaisesRegex(StopBatch, "param: reasoning"):
            self.client.complete(self.messages)
        self.assertEqual(len(self.fake.requests), 1)

    def test_a_cut_stream_is_retried(self):
        self.fake.plan = [sse([delta("half")]), sse([delta("OK"), completed()])]
        self.assertEqual(self.client.complete(self.messages)[0], "OK")
        self.fake.plan = [sse([delta("half")])] * 4
        with self.assertRaisesRegex(LLMError, "ended before"):
            self.client.complete(self.messages)
        self.assertEqual(len(self.fake.requests), 6)

    def test_a_sign_in_problem_stops_the_batch(self):
        def provider(force_refresh=False):
            raise siwc.SignInRequired("Not signed in with ChatGPT. Run: python -m cic login")
        client = ResponsesClient("gpt-test-1", token_provider=provider, api_base=self.served.base + "/v1", backoff=0)
        with self.assertRaisesRegex(StopBatch, "Not signed in"):
            client.complete(self.messages)
        self.assertEqual(self.fake.requests, [])

    def test_models_lists_only_listed_entries(self):
        self.assertEqual(siwc.list_models("tok", api_base=self.served.base + "/v1"),
                         [{"slug": "gpt-test-1", "display_name": "GPT Test 1"}])

    def test_read_stream_handles_crlf_split_data_and_done(self):
        lines = [b"event: response.output_text.delta\r\n",
                 b'data: {"type": "response.output_text.delta",\r\n', b'data:  "delta": "A"}\r\n', b"\r\n",
                 b"data: [DONE]\n", b"\n",
                 ("data: " + json.dumps(completed()) + "\n").encode()]
        self.assertEqual(read_stream(lines), ("A", {"prompt_tokens": 120, "completion_tokens": 8}))


@unittest.skipUnless(_browser_available(), "Playwright Chromium is not installed")
class ResponsesAgentEndToEnd(unittest.TestCase):
    def test_the_agent_runs_a_task_through_the_responses_client(self):
        from cic.agent.policy import LLMPolicy
        from cic.agent.runner import run_task
        from cic.agent.scripted import RuleAgent
        from cic.checks.report import evaluate_run
        from cic.sim.data import load_policy

        agent = RuleAgent("careful")

        def responder(body):
            obs = FakeModel._obs_from_prompt(body["input"][-1]["content"])
            return sse([delta(json.dumps(agent._next(obs))), completed()])

        env, fake = _Env(), FakeAPI(responder)
        served = Served(fake.app)
        try:
            client = ResponsesClient("gpt-test-1", token_provider=lambda force_refresh=False: "tok",
                                     api_base=served.base + "/v1", backoff=0)
            with tempfile.TemporaryDirectory() as out:
                path = run_task("T08", LLMPolicy(client, load_policy()), out)
                record = json.loads((path / "run.json").read_text(encoding="utf-8"))
        finally:
            served.close()
            env.restore()
        ev = evaluate_run(record)
        self.assertTrue(ev["accepted"])
        self.assertEqual([r["id"] for r in ev["process"]["results"] if not r["passed"]], [])
        self.assertEqual(record["agent"]["billing"], "chatgpt_plan")
        self.assertEqual(record["usage"]["prompt_tokens"], 120 * len(fake.requests))
        self.assertTrue(all(r["body"]["instructions"].startswith("You work in the patient-message inbox")
                            for r in fake.requests))
        self.assertTrue(all(i["role"] != "system" for r in fake.requests for i in r["body"]["input"]))


@unittest.skipUnless(_browser_available(), "Playwright Chromium is not installed")
class BatchStopAndResume(unittest.TestCase):
    def test_an_error_stops_the_batch_and_resume_finishes_it(self):
        import cic.__main__ as cli
        from cic.checks.report import build_report

        from cic.agent.scripted import RuleAgent

        class Fresh(FakeModel):
            """A fake model that starts each run afresh, as a stateless model client does."""
            def complete(self, messages, max_tokens=None):
                if "STEP 1 of at most" in messages[-1]["content"]:
                    self.agent, self.calls = RuleAgent("careful"), 0
                return super().complete(messages, max_tokens)

        class Broken(Fresh):
            def complete(self, messages, max_tokens=None):
                if self.calls >= 3:
                    raise StopBatch("HTTP 429 subscription_sharing_usage_limit_exceeded")
                return super().complete(messages, max_tokens)

        with tempfile.TemporaryDirectory() as out:
            with mock.patch.object(cli, "_client", return_value=Broken()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(["run", "--tasks", "T01", "--repeats", "2", "--out", out])
            self.assertEqual(caught.exception.code, 2)
            ended = [json.loads(p.read_text(encoding="utf-8"))["ended_by"] for p in Path(out).glob("*/run.json")]
            self.assertEqual(ended, ["error"])
            with mock.patch.object(cli, "_client", return_value=Fresh()):
                cli.main(["run", "--tasks", "T01", "--repeats", "2", "--out", out, "--resume"])
            ended = sorted(json.loads(p.read_text(encoding="utf-8"))["ended_by"] for p in Path(out).glob("*/run.json"))
            self.assertEqual(ended, ["done", "done", "error"])
            report = build_report(out)
            self.assertEqual((report["runs"], report["left_out_errors"]), (2, 1))
            self.assertEqual(report["measurement_1"]["accepted"], 2)
            with mock.patch.object(cli, "_client", return_value=Fresh()):
                cli.main(["run", "--tasks", "T01", "--repeats", "2", "--out", out, "--resume"])
            self.assertEqual(len(list(Path(out).glob("*/run.json"))), 3)


if __name__ == "__main__":
    unittest.main()
