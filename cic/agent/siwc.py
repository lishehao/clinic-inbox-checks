"""Sign in with ChatGPT, so the agent can run on the signed-in user's ChatGPT plan.

This follows OpenAI's documentation for open-source and locally hosted apps
(https://developers.openai.com/siwc/token-sharing-open-source):

- sign-in in the system browser with PKCE and a loopback callback on 127.0.0.1;
- the "Use your ChatGPT plan" permission is the scope chatgpt.tokens.use.direct;
- the issued client id is saved and reused; tokens are refreshed before they expire;
- requests then go to the Responses API (see cic/agent/responses.py).

Sign-in must run on the computer whose browser you use, because the browser sends
the answer to 127.0.0.1 on that computer. Credentials are kept in a file only this
user can read, outside the project folder. Tokens are never printed or logged.
"""

import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from pathlib import Path

AUTH_BASE = "https://auth.openai.com"
API_BASE = "https://api.openai.com/v1"
RESOURCE = "https://api.openai.com/v1"
IDENTITY_SCOPES = "openid profile email"
PLAN_SCOPES = "offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
REGISTRATION_CLIENT = "dynamic_agent_client"
AGENT_NAME = "Clinic Inbox Checks"
CALLBACK_PATH = "/auth/callback"
DEFAULT_PORT = 1455
REFRESH_MARGIN = 120  # seconds before expiry
UNUSABLE_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                    "refresh_token_invalidated", "refresh_token_reused"}
NETWORK_ERRORS = (urllib.error.URLError, OSError, ValueError)
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class SignInError(Exception):
    """Sign-in did not work, or the saved sign-in cannot be used as it is."""


class SignInRequired(SignInError):
    """There is no usable sign-in: run python -m cic login."""


class DamagedCredentials(SignInError):
    """The saved credentials file cannot be read."""


class OAuthError(Exception):
    def __init__(self, status, code, description=""):
        super().__init__(f"HTTP {status}: {code}{' - ' + description if description else ''}")
        self.status, self.code, self.description = status, code, description


# ---------------------------------------------------------------- storage

def config_dir():
    override = os.environ.get("CIC_CHATGPT_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "clinic-inbox-checks"


def credentials_path():
    return config_dir() / "chatgpt-credentials.json"


def _write_private(path, text):
    """Write atomically with owner-only permissions; never leave a partial copy behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_record():
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise DamagedCredentials(f"The saved sign-in file cannot be read: {path}. Run python -m cic logout "
                                 f"(it removes the file), then python -m cic login.") from None
    if not isinstance(record, dict):
        raise DamagedCredentials(f"The saved sign-in file is damaged: {path}. Run python -m cic logout, then login.")
    return record


def save_record(record):
    _write_private(credentials_path(), json.dumps(record, indent=1))


def host_id():
    """A stable, opaque id for this computer (urn:uuid), created once and kept outside the project."""
    path = config_dir() / "host_id"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"urn:uuid:{uuid.uuid4()}"
    _write_private(path, value + "\n")
    return value


@contextmanager
def _locked():
    """Serialise refreshes across processes, because each refresh replaces the refresh token."""
    config_dir().mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # not on Unix: no cross-process lock
        yield
        return
    with open(config_dir() / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------- HTTP helpers

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _error_fields(status, text):
    try:
        body = json.loads(text)
    except ValueError:
        return OAuthError(status, f"http_{status}", text[:200])
    if not isinstance(body, dict):
        return OAuthError(status, f"http_{status}", text[:200])
    if isinstance(body.get("error"), dict):
        inner = body["error"]
        return OAuthError(status, str(inner.get("code") or inner.get("type") or f"http_{status}"),
                          str(inner.get("message") or "")[:300])
    return OAuthError(status, str(body.get("error") or f"http_{status}"),
                      str(body.get("error_description") or body.get("detail") or "")[:300])


def _open(url, form=None, headers=None, timeout=30):
    data = urllib.parse.urlencode(form).encode("ascii") if form is not None else None
    hdrs = {"Accept": "application/json"}
    if form is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    hdrs.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if form is not None else "GET")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise _error_fields(error.code, error.read().decode("utf-8", errors="replace")) from None
    return response


def _request_json(url, form=None, headers=None, timeout=30):
    with _open(url, form, headers, timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _get_json_retrying(url, attempts=3):
    """GET a JSON document, retrying network and server errors. Returns {} if it cannot be fetched."""
    for attempt in range(attempts):
        try:
            return _request_json(url)
        except OAuthError as error:
            if error.status < 500:
                return {}
        except NETWORK_ERRORS:
            pass
        if attempt + 1 < attempts:
            time.sleep(1.0 * (attempt + 1))
    return {}


def discovery(auth_base=AUTH_BASE):
    return _get_json_retrying(f"{auth_base}/.well-known/openid-configuration")


# ---------------------------------------------------------------- PKCE, authorization URL, ID token

def pkce_pair():
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorization_url(redirect_uri, state, nonce, challenge, host, client_id=None, consent=False,
                      auth_base=AUTH_BASE):
    params = {"client_id": client_id or REGISTRATION_CLIENT}
    if not client_id:
        params["agent_name_hint"] = AGENT_NAME
    params.update({
        "ext_agent_host_id": host,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": f"{IDENTITY_SCOPES} {PLAN_SCOPES}",
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })
    if consent:
        params["prompt"] = "consent"
    return f"{auth_base}/api/accounts/authorize?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def rs256_valid(jwk, signing_input, signature):
    """RSASSA-PKCS1-v1_5 with SHA-256, checked with integer arithmetic (no extra packages)."""
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    padding = size - len(digest_info) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return hmac.compare_digest(decoded, expected)


# NIST P-256, for ES256 ID tokens
_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_A = _P - 3
_B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
      0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _ec_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        slope = (3 * x1 * x1 + _A) * pow(2 * y1, _P - 2, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _ec_mul(k, point):
    result = None
    while k:
        if k & 1:
            result = _ec_add(result, point)
        point = _ec_add(point, point)
        k >>= 1
    return result


def es256_valid(jwk, signing_input, signature):
    """ECDSA on P-256 with SHA-256; the JWS signature is r || s (64 bytes)."""
    if jwk.get("crv") != "P-256" or len(signature) != 64:
        return False
    x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
    y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
    if (y * y - (x * x * x + _A * x + _B)) % _P != 0:
        return False
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    if not (0 < r < _N and 0 < s < _N):
        return False
    e = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
    w = pow(s, _N - 2, _N)
    point = _ec_add(_ec_mul(e * w % _N, _G), _ec_mul(r * w % _N, (x, y)))
    return point is not None and point[0] % _N == r


_VERIFIERS = {"RS256": ("RSA", rs256_valid), "ES256": ("EC", es256_valid)}


def verify_id_token(id_token, client_id, nonce, issuer, jwks, now=None, leeway=300):
    """Check the ID token's signature (RS256 or ES256, against the published keys), issuer, audience,
    expiry and nonce. Returns the claims. Anything that cannot be checked is refused."""
    if not id_token:
        raise SignInError("The token response had no ID token.")
    try:
        header_b64, payload_b64, signature_b64 = id_token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)
    except ValueError:
        raise SignInError("The ID token could not be read.") from None
    alg = header.get("alg")
    if alg not in _VERIFIERS:
        raise SignInError(f"The ID token uses a signing method this tool does not check ({alg}).")
    keys = (jwks or {}).get("keys") or []
    if not keys:
        raise SignInError("OpenAI's signing keys could not be fetched, so the ID token cannot be checked. "
                          "Nothing was saved; run python -m cic login again.")
    kty, check = _VERIFIERS[alg]
    candidates = [k for k in keys if k.get("kid") == header.get("kid")] or (keys if len(keys) == 1 else [])
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    if not any(k.get("kty") == kty and check(k, signing_input, signature) for k in candidates):
        raise SignInError("The ID token signature did not verify against OpenAI's published keys.")
    now = time.time() if now is None else now
    issuers = {i for i in (issuer if isinstance(issuer, (set, list, tuple)) else [issuer]) if i}
    if claims.get("iss") not in issuers | {i.rstrip("/") + "/" for i in issuers}:
        raise SignInError("The ID token came from an unexpected issuer.")
    audience = claims.get("aud")
    if client_id not in (audience if isinstance(audience, list) else [audience]):
        raise SignInError("The ID token was issued for a different client.")
    if float(claims.get("exp", 0)) < now - leeway:
        raise SignInError("The ID token has expired.")
    if claims.get("nonce") != nonce:
        raise SignInError("The ID token does not carry this sign-in's nonce.")
    return claims


# ---------------------------------------------------------------- loopback callback

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    timeout = 10  # an idle connection (for example a browser preconnect) is closed instead of blocking

    def do_GET(self):  # noqa: N802 (http.server naming)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        if query.get("state") != self.server.expected_state:
            message, status = "This answer is not for the sign-in that is waiting in the terminal.", 400
        else:
            if self.server.result is None:
                self.server.result = query
            message, status = "Received. You can close this tab; the terminal shows whether sign-in finished.", 200
        body = f"<!doctype html><meta charset='utf-8'><title>Clinic Inbox Checks</title><p>{message}</p>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # the callback URL holds the authorization code: never log it
        pass


class _CallbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # do not wait for idle browser connections when closing


def _bind_callback(port, expected_state):
    for candidate in ([port, 0] if port else [0]):
        try:
            server = _CallbackServer(("127.0.0.1", candidate), _CallbackHandler)
        except OSError:
            continue
        server.result = None
        server.expected_state = expected_state
        server.timeout = 0.5
        return server
    raise SignInError("Could not open a local port for the sign-in callback.")


def _wait_for_callback(server, deadline):
    while server.result is None and time.time() < deadline:
        server.handle_request()


def _duration(seconds):
    return f"{seconds // 60} minutes" if seconds >= 120 else f"{seconds} seconds"


# ---------------------------------------------------------------- sign-in, refresh, sign-out

def login(open_browser=webbrowser.open, port=DEFAULT_PORT, timeout=300, consent=False, auth_base=AUTH_BASE,
          say=print):
    """Run the browser sign-in on this computer and save the credentials. Returns the saved record."""
    saved = load_record()
    saved_client = saved.get("client_id")
    host = host_id()
    if saved.get("refresh_token") and saved_client:
        # A new sign-in replaces the saved one: revoke the old session first (best effort) and drop its tokens.
        _revoke(saved["refresh_token"], saved_client, discovery(auth_base))
        with _locked():
            save_record({k: saved.get(k) for k in ("issuer", "subject", "client_id", "ext_agent_host_id")})
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    server = _bind_callback(port, state)
    redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
    url = authorization_url(redirect_uri, state, nonce, challenge, host, saved_client, consent, auth_base)
    waiter = threading.Thread(target=_wait_for_callback, args=(server, time.time() + timeout), daemon=True)
    waiter.start()  # listening before the browser opens
    say("Opening your browser to sign in with ChatGPT. If it does not open, paste this address into a browser "
        "on this computer:")
    say(url)
    try:
        open_browser(url)
    except Exception:  # no browser here; the printed address still works
        pass
    waiter.join(timeout + 2)
    server.server_close()
    answer = server.result
    if not answer:
        raise SignInError(f"No answer from the browser within {_duration(timeout)}. Nothing was saved.")
    if answer.get("error"):
        if answer["error"] == "access_denied":
            raise SignInError("Sign-in was cancelled or the permission was declined. Nothing was saved.")
        hint = (" The saved client registration may be gone: run python -m cic logout --forget-client, then login."
                if answer["error"] == "invalid_client" else "")
        raise SignInError(f"Sign-in failed: {answer['error']} {answer.get('error_description', '')}".strip() + hint)
    returned_client = answer.get("client_id")
    if saved_client and returned_client and returned_client != saved_client:
        raise SignInError("The browser's answer named a different client than the saved one. Nothing was saved.")
    client_id = returned_client or saved_client
    if not client_id or client_id == REGISTRATION_CLIENT or not answer.get("code"):
        raise SignInError("The browser's answer had no issued client id or no code. Nothing was saved.")
    try:
        tokens = _request_json(f"{auth_base}/api/accounts/oauth/token", form={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": answer["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        })
    except OAuthError as error:
        raise SignInError(f"The code exchange was refused ({error}). Run python -m cic login again.") from None
    except NETWORK_ERRORS as error:
        raise SignInError(f"Could not reach the sign-in server for the code exchange ({error}). Nothing was "
                          f"saved; run python -m cic login again.") from None
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise SignInError("The token response had no access or refresh token. Nothing was saved.")
    meta = discovery(auth_base)
    jwks = _get_json_retrying(meta["jwks_uri"]) if meta.get("jwks_uri") else {}
    issuers = {auth_base, meta.get("issuer")} if meta.get("issuer") else {auth_base}
    claims = verify_id_token(tokens.get("id_token"), client_id, nonce, issuer=issuers, jwks=jwks)
    granted = sorted(set((tokens.get("scope") or answer.get("scope") or "").replace("+", " ").split()))
    now = time.time()
    record = {
        "issuer": auth_base,
        "subject": claims.get("sub"),
        "client_id": client_id,
        "ext_agent_host_id": host,
        "id_token": tokens.get("id_token"),
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in"),
        "expires_at": now + float(tokens.get("expires_in") or 3600),
        "earliest_refresh_at": _timestamp(tokens.get("earliest_refresh_at")),
        "scopes": granted,
        "plan_usage": PLAN_SCOPE in granted,
        "saved_at": int(now),
    }
    with _locked():
        save_record(record)
    return record


def _timestamp(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _refresh(record, auth_base):
    try:
        tokens = _request_json(f"{auth_base}/api/accounts/oauth/token", form={
            "grant_type": "refresh_token",
            "client_id": record["client_id"],
            "refresh_token": record["refresh_token"],
            "resource": RESOURCE,
        })
    except OAuthError as error:
        if error.code in UNUSABLE_REFRESH:
            for key in ("access_token", "refresh_token", "expires_at", "earliest_refresh_at"):
                record[key] = None
            save_record(record)  # the client id and host id are kept for the next sign-in
            raise SignInRequired(f"The ChatGPT sign-in has ended ({error.code}). Run: python -m cic login") from None
        if error.code == "invalid_client":
            raise SignInError("The saved client registration was refused (invalid_client). Run python -m cic logout "
                              "--forget-client, then python -m cic login.") from None
        raise SignInError(f"Could not refresh the sign-in ({error}). The saved sign-in is kept; try again later.") \
            from None
    except NETWORK_ERRORS as error:
        raise SignInError(f"Could not reach OpenAI to refresh the sign-in ({error}). The saved sign-in is kept; "
                          f"try again later.") from None
    if not tokens.get("access_token"):
        raise SignInError("The refresh answer had no access token. The saved sign-in is kept; try again later.")
    now = time.time()
    record["access_token"] = tokens["access_token"]
    record["refresh_token"] = tokens.get("refresh_token") or record["refresh_token"]
    record["id_token"] = tokens.get("id_token") or record.get("id_token")
    record["expires_in"] = tokens.get("expires_in")
    record["expires_at"] = now + float(tokens.get("expires_in") or 3600)
    record["earliest_refresh_at"] = _timestamp(tokens.get("earliest_refresh_at"))
    if tokens.get("scope"):
        record["scopes"] = sorted(set(tokens["scope"].split()))
        record["plan_usage"] = PLAN_SCOPE in record["scopes"]
    record["saved_at"] = int(now)
    save_record(record)
    return record


_NO_PLAN = ("Signed in, but the 'Use your ChatGPT plan' permission is not granted. Run python -m cic login "
            "--consent to grant it, or use an API key instead (see .env.example).")


def access_token(force_refresh=False, auth_base=AUTH_BASE):
    """Return a current access token for plan usage, refreshing it first when it is about to expire."""
    with _locked():
        record = load_record()  # read inside the lock: another process may have refreshed already
        if not record.get("access_token") or not record.get("refresh_token"):
            raise SignInRequired("Not signed in with ChatGPT. Run: python -m cic login")
        if not record.get("plan_usage"):
            raise SignInError(_NO_PLAN)
        now = time.time()
        expiring = now >= float(record.get("expires_at") or 0) - REFRESH_MARGIN
        too_early = record.get("earliest_refresh_at") and now < float(record["earliest_refresh_at"])
        if force_refresh and too_early and not expiring:
            raise SignInError("The access token was not accepted. Check that you signed in with the right ChatGPT "
                              "account; if this keeps happening, run python -m cic login.")
        if expiring or force_refresh:
            record = _refresh(record, auth_base)
            if not record.get("plan_usage"):
                raise SignInError(_NO_PLAN)
        return record["access_token"]


def _revoke(refresh_token, client_id, meta, attempts=3):
    """Revoke a refresh token. Any 2xx answer counts as success. Returns True if confirmed."""
    endpoint = (meta or {}).get("revocation_endpoint")
    if not endpoint:
        return False
    for attempt in range(attempts):
        try:
            with _open(endpoint, form={"token": refresh_token, "token_type_hint": "refresh_token",
                                       "client_id": client_id}) as response:
                if 200 <= response.status < 300:
                    return True
        except OAuthError as error:
            if error.status < 500:
                return False
        except NETWORK_ERRORS:
            pass
        if attempt + 1 < attempts:
            time.sleep(2.0 * (attempt + 1))
    return False


def logout(forget_client=False, auth_base=AUTH_BASE):
    """Revoke the refresh token, then remove the saved tokens.

    Returns None if no tokens were saved, else True or False for whether OpenAI confirmed the revocation.
    A credentials file that cannot be read is removed (its tokens could not be revoked; returns False).
    """
    with _locked():
        try:
            record = load_record()
        except DamagedCredentials:
            credentials_path().unlink()
            return False
        if not record.get("refresh_token") and not record.get("access_token"):
            if forget_client and credentials_path().exists():
                credentials_path().unlink()
            return None
        confirmed = False
        if record.get("refresh_token") and record.get("client_id"):
            confirmed = _revoke(record["refresh_token"], record["client_id"], discovery(auth_base))
        if forget_client or not record.get("client_id"):
            if credentials_path().exists():
                credentials_path().unlink()
        else:
            save_record({k: record.get(k) for k in ("issuer", "subject", "client_id", "ext_agent_host_id")})
        return confirmed


def list_models(token, api_base=API_BASE):
    """Models this account may use with plan usage: (slug, display name), in the server's order."""
    payload = _request_json(f"{api_base}/models", headers={"Authorization": f"Bearer {token}"})
    if isinstance(payload.get("models"), list):  # the plan-usage format: keep entries marked "list"
        items = [m for m in payload["models"] if m.get("visibility") == "list"]
    else:  # the standard format has no visibility field
        items = payload.get("data") or []
    models = []
    for item in items:
        slug = item.get("slug") or item.get("id")
        if slug:
            models.append({"slug": slug, "display_name": item.get("display_name") or slug})
    return models
