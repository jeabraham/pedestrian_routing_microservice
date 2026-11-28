"""Single-owner OAuth authorization server backed by a durable SQLite database."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.config import Settings

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
PENDING_TTL = 600
SCRYPT_N, SCRYPT_R, SCRYPT_P = 32768, 8, 1


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_verifier(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)


class StaleOAuthConfiguration(Exception):
    """This process uses a login secret superseded by another instance."""


class OAuthProvider:
    def __init__(self, settings: Settings):
        base = settings.public_base_url
        secret = settings.mcp_oauth_login_secret
        path = settings.mcp_oauth_db_path
        if not base or not secret or not path or not os.path.isabs(path):
            raise ValueError("OAuth requires PUBLIC_BASE_URL, MCP_OAUTH_LOGIN_SECRET and an absolute MCP_OAUTH_DB_PATH")
        url = urlsplit(base)
        if (url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/")
                or url.query or url.fragment or base.endswith("/")):
            raise ValueError("OAuth PUBLIC_BASE_URL must be an HTTPS origin without a trailing slash")
        if len(secret.encode()) < 32 or (settings.api_key and hmac.compare_digest(secret.encode(), settings.api_key.encode())):
            raise ValueError("OAuth login secret must be at least 32 bytes and distinct from API_KEY")
        if not os.path.isdir(os.path.dirname(path)) or os.path.islink(path):
            raise ValueError("OAuth database must be on an existing persistent directory and not a symlink")
        self.base, self.resource, self.path = base, base + "/mcp", path
        # Open with private permissions even when the process umask is permissive.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    expires REAL NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS events (kind TEXT NOT NULL, at REAL NOT NULL);
            ''')
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT value FROM config WHERE key='resource'").fetchone()
            if old and old[0] != self.resource:
                raise ValueError("OAuth database belongs to a different resource URL")
            db.execute("INSERT OR IGNORE INTO config VALUES ('resource', ?)", (self.resource,))
            old = db.execute("SELECT value FROM config WHERE key='login_verifier'").fetchone()
            if old:
                try:
                    algorithm, salt_hex, verifier_hex = old[0].split("$")
                    salt, verifier = bytes.fromhex(salt_hex), bytes.fromhex(verifier_hex)
                    if algorithm != "scrypt-32768-8-1" or len(salt) != 16 or len(verifier) != 32:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("Invalid OAuth login verifier") from exc
            else:
                salt, verifier = b"", b""
            if not old or not hmac.compare_digest(password_verifier(secret, salt), verifier):
                salt = secrets.token_bytes(16)
                verifier = password_verifier(secret, salt)
                # This also invalidates grants from databases predating the verifier.
                db.execute("DELETE FROM entries")
                db.execute("INSERT OR REPLACE INTO config VALUES ('login_verifier', ?)",
                           (f"scrypt-32768-8-1${salt.hex()}${verifier.hex()}",))
            self.salt, self.verifier = salt, verifier
            self.verifier_record = f"scrypt-32768-8-1${salt.hex()}${verifier.hex()}"

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM config WHERE key='login_verifier'").fetchone()
            if not row or not hmac.compare_digest(row[0], self.verifier_record):
                raise StaleOAuthConfiguration()
            yield db

    def limit(self, kind: str, maximum: int, window: int) -> bool:
        now = time.time()
        with self.db() as db:
            db.execute("DELETE FROM events WHERE at < ?", (now - 3600,))
            count = db.execute("SELECT count(*) FROM events WHERE kind=? AND at>?", (kind, now-window)).fetchone()[0]
            if count >= maximum:
                return False
            db.execute("INSERT INTO events VALUES (?,?)", (kind, now))
            return True

    def put(self, db, kind: str, token: str, data: dict, expires: float):
        db.execute("INSERT INTO entries VALUES (?,?,?,?)", (kind, digest(token), json.dumps(data), expires))

    def get(self, kind: str, token: str):
        with self.db() as db:
            row = db.execute("SELECT data FROM entries WHERE kind=? AND id=? AND expires>?", (kind, digest(token), time.time())).fetchone()
        return json.loads(row[0]) if row else None

    async def get_client(self, client_id: str):
        with self.db() as db:
            row = db.execute("SELECT data FROM clients WHERE id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull):
        for uri in client_info.redirect_uris or []:
            u = urlsplit(str(uri))
            if (not u.hostname or u.username or u.password or u.fragment or
                (u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))) or
                set(parse_qs(u.query, keep_blank_values=True)) & {"code", "state", "error"}):
                raise RegistrationError("invalid_redirect_uri", "Redirect URI must be HTTPS or HTTP loopback")
        with self.db() as db:
            if db.execute("SELECT count(*) FROM clients").fetchone()[0] >= 1000:
                raise RegistrationError("invalid_client_metadata", "Client limit reached")
            db.execute("INSERT INTO clients VALUES (?,?)", (client_info.client_id, client_info.model_dump_json()))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams):
        if params.resource is not None and params.resource != self.resource:
            raise AuthorizeError("invalid_target", "Unknown resource")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "Invalid S256 code challenge")
        pending = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM entries WHERE expires<=?", (time.time(),))
            if db.execute("SELECT count(*) FROM entries WHERE kind='pending'").fetchone()[0] >= 500:
                raise AuthorizeError("temporarily_unavailable", "Too many pending requests")
            self.put(db, "pending", pending, {"client_id": client.client_id, "params": params.model_dump(mode="json")}, time.time()+PENDING_TTL)
        return self.base + "/oauth/login?request=" + pending

    async def load_authorization_code(self, client, authorization_code):
        data = self.get("code", authorization_code)
        return AuthorizationCode(code=authorization_code, **data) if data else None

    def issue(self, db, client_id, scopes, resource):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "resource": resource}
        self.put(db, "access", access, {**common, "expires_at": now + ACCESS_TTL}, now + ACCESS_TTL)
        self.put(db, "refresh", refresh, {**common, "expires_at": now + REFRESH_TTL}, now + REFRESH_TTL)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh, scope=" ".join(scopes))

    def consume_and_issue(self, kind, token, client_id, scopes, resource):
        with self.db() as db:
            row = db.execute("SELECT data FROM entries WHERE kind=? AND id=? AND expires>?", (kind, digest(token), time.time())).fetchone()
            if not row or json.loads(row[0])["client_id"] != client_id:
                raise TokenError("invalid_grant", "Grant already used or expired")
            db.execute("DELETE FROM entries WHERE kind=? AND id=?", (kind, digest(token)))
            return self.issue(db, client_id, scopes, resource)

    async def exchange_authorization_code(self, client, authorization_code):
        return self.consume_and_issue("code", authorization_code.code, client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        data = self.get("refresh", refresh_token)
        return RefreshToken(token=refresh_token, **data) if data else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return self.consume_and_issue("refresh", refresh_token.token, client.client_id, scopes, refresh_token.resource)

    async def load_access_token(self, token):
        data = self.get("access", token)
        return AccessToken(token=token, **data) if data and data.get("resource") == self.resource else None

    async def revoke_token(self, token):
        with self.db() as db:
            db.execute("DELETE FROM entries WHERE kind IN ('access','refresh') AND id=?", (digest(token.token),))

    def routes(self):
        issuer = AnyHttpUrl(self.base)
        protected = create_protected_resource_routes(AnyHttpUrl(self.resource), [issuer], scopes_supported=["mcp"])
        return (create_auth_routes(self, issuer, client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"])) +
                protected + [Route("/.well-known/oauth-protected-resource", protected[0].endpoint, methods=["GET", "OPTIONS"])])

    async def login_get(self, request: Request):
        pending = request.query_params.get("request", "")
        data = self.get("pending", pending)
        if not data:
            return HTMLResponse("Invalid or expired request", status_code=400)
        csrf = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("UPDATE entries SET data=json_set(data, '$.csrf', ?) WHERE kind='pending' AND id=? AND expires>?", (digest(csrf), digest(pending), time.time()))
        client = await self.get_client(data["client_id"])
        name = html.escape(client.client_name or client.client_id, quote=True) if client else "Unknown client"
        redirect = urlsplit(str(AuthorizationParams.model_validate(data["params"]).redirect_uri))
        origin = html.escape(f"{redirect.scheme}://{redirect.netloc}", quote=True)
        page = (f"<!doctype html><title>Authorize MCP client</title><h1>Authorize {name}</h1>"
                f"<p>Redirect origin: {origin}</p>"
                f"<p>Allow this client to use the pedestrian routing MCP service?</p><form method=post action=/oauth/login>"
                f"<input type=hidden name=request value='{html.escape(pending, quote=True)}'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<label>Login secret <input name=password type=password required autocomplete=current-password></label>"
                "<button name=decision value=approve>Approve</button><button name=decision value=deny formnovalidate>Deny</button></form>")
        response = HTMLResponse(page, headers=self.browser_headers())
        response.set_cookie("__Host-mcp_oauth", csrf, secure=True, httponly=True, samesite="strict", path="/", max_age=PENDING_TTL)
        return response

    @staticmethod
    def browser_headers():
        return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}

    async def login_post(self, request: Request):
        if request.headers.get("origin") != self.base:
            return HTMLResponse("Invalid origin", status_code=403, headers=self.browser_headers())
        form = await request.form()
        pending, csrf = form.get("request", ""), form.get("csrf", "")
        if not isinstance(pending, str) or not isinstance(csrf, str) or not hmac.compare_digest(csrf.encode(), request.cookies.get("__Host-mcp_oauth", "").encode()):
            return HTMLResponse("Invalid request", status_code=403, headers=self.browser_headers())
        data = self.get("pending", pending)
        if not data or not hmac.compare_digest(data.get("csrf", ""), digest(csrf)):
            return HTMLResponse("Invalid or expired request", status_code=403, headers=self.browser_headers())
        if not self.limit("login", 10, 300):
            return HTMLResponse("Too many attempts; try later", status_code=429, headers=self.browser_headers())
        decision = form.get("decision")
        if decision not in ("approve", "deny"):
            return HTMLResponse("Invalid decision", status_code=400, headers=self.browser_headers())
        password = form.get("password", "")
        if decision == "approve" and (not isinstance(password, str) or not hmac.compare_digest(password_verifier(password, self.salt), self.verifier)):
            return HTMLResponse("Invalid login secret", status_code=403, headers=self.browser_headers())
        params = AuthorizationParams.model_validate(data["params"])
        code = secrets.token_urlsafe(32)
        with self.db() as db:
            row = db.execute("SELECT data FROM entries WHERE kind='pending' AND id=? AND expires>?", (digest(pending), time.time())).fetchone()
            if not row or json.loads(row[0]).get("csrf") != digest(csrf):
                return HTMLResponse("Invalid or expired request", status_code=403, headers=self.browser_headers())
            db.execute("DELETE FROM entries WHERE kind='pending' AND id=?", (digest(pending),))
            if decision == "approve":
                authcode = AuthorizationCode(code=code, scopes=params.scopes or [], expires_at=time.time()+CODE_TTL,
                    client_id=data["client_id"], code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.resource)
                self.put(db, "code", code, authcode.model_dump(mode="json", exclude={"code"}), authcode.expires_at)
        url = construct_redirect_uri(str(params.redirect_uri), code=code if decision == "approve" else None,
                                     error="access_denied" if decision == "deny" else None, state=params.state)
        response = RedirectResponse(url, status_code=303, headers=self.browser_headers())
        response.delete_cookie("__Host-mcp_oauth", path="/", secure=True, httponly=True, samesite="strict")
        return response
