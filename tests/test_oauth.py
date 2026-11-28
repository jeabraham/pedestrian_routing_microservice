import base64
import hashlib
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.oauth import digest

BASE = "https://walk.example.com"
RESOURCE = BASE + "/mcp"
SECRET = "a secure, separate login password of 32 bytes"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
REDIRECT = "https://chatgpt.com/aip/oauth/callback"


@pytest.fixture
def oauth_settings(settings, tmp_path):
    return replace(settings, api_key="rest-key", mcp_oauth_enabled=True, mcp_oauth_login_secret=SECRET,
                   mcp_oauth_db_path=str(tmp_path / "oauth.sqlite"))


@pytest.fixture
def oauth(oauth_settings):
    with TestClient(create_app(oauth_settings), base_url=BASE, follow_redirects=False) as client:
        yield client


def register(client, **metadata):
    body = {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    body.update(metadata)
    return client.post("/register", json=body)


def start(client, client_id, **kwargs):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
              "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "test-state", "resource": RESOURCE}
    params.update(kwargs)
    return client.get("/authorize", params=params)


def approve(client, auth, password=SECRET, decision="approve", origin=BASE):
    page = client.get(auth.headers["location"])
    assert page.status_code == 200, page.text
    pending = re.search(r"name=request value='([^']+)'", page.text).group(1)
    csrf = re.search(r"name=csrf value='([^']+)'", page.text).group(1)
    return client.post("/oauth/login", headers={"Origin": origin}, data={"request": pending, "csrf": csrf, "password": password, "decision": decision})


def token(client, client_id, code, **kwargs):
    data = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": REDIRECT, "code_verifier": VERIFIER, "resource": RESOURCE}
    data.update(kwargs)
    return client.post("/token", data=data)


def test_full_flow_and_restart(oauth, oauth_settings):
    discovery = oauth.get("/.well-known/oauth-protected-resource/mcp")
    assert discovery.status_code == 200
    assert discovery.json()["resource"] == RESOURCE
    assert oauth.get("/.well-known/oauth-protected-resource").json()["resource"] == RESOURCE
    assert discovery.json()["authorization_servers"] == [BASE + "/"] or discovery.json()["authorization_servers"] == [BASE]
    metadata = oauth.get("/.well-known/oauth-authorization-server").json()
    assert metadata["registration_endpoint"] == BASE + "/register"
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    denied = oauth.post("/mcp")
    assert denied.status_code == 401 and 'resource_metadata=' in denied.headers['www-authenticate']
    assert oauth.get("/providers").status_code == 401
    assert oauth.get("/providers", headers={"x-api-key": "rest-key"}).status_code == 200
    registered = register(oauth)
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    auth = start(oauth, client_id)
    assert auth.status_code == 302, auth.text
    result = approve(oauth, auth)
    assert result.status_code == 303, result.text
    values = parse_qs(urlsplit(result.headers["location"]).query)
    assert values["state"] == ["test-state"]
    code = values["code"][0]
    assert token(oauth, client_id, code, code_verifier="x" * 64).status_code == 400
    assert token(oauth, client_id, code, resource="https://evil.example/mcp").json()["error"] == "invalid_target"
    assert token(oauth, client_id, code, redirect_uri="https://evil.example/").status_code == 400
    result = token(oauth, client_id, code)
    assert result.status_code == 200, result.text
    credentials = result.json()
    assert token(oauth, client_id, code).status_code == 400
    headers = {"Authorization": "Bearer " + credentials["access_token"], "Accept": "application/json, text/event-stream"}
    assert oauth.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 200
    assert oauth.get("/providers", headers=headers).status_code == 401
    assert oauth.post("/mcp", headers={"X-API-Key": "rest-key"}).status_code != 401
    with TestClient(create_app(oauth_settings), base_url=BASE, follow_redirects=False) as restarted:
        assert restarted.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 200
        body = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": credentials["refresh_token"], "resource": RESOURCE}
        refreshed = restarted.post("/token", data=body)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["refresh_token"] != credentials["refresh_token"]
        assert restarted.post("/token", data=body).status_code == 400


def test_negative_cases(oauth):
    assert register(oauth, redirect_uris=["http://evil.example/callback"]).status_code == 400
    client_id = register(oauth).json()["client_id"]
    assert start(oauth, client_id, redirect_uri="https://evil.example").status_code == 400
    assert "error=" in start(oauth, client_id, resource="https://evil.example/mcp").headers["location"]
    assert "error=" in start(oauth, client_id, code_challenge_method="plain").headers["location"]
    assert approve(oauth, start(oauth, client_id), origin="https://evil.example").status_code == 403
    assert approve(oauth, start(oauth, client_id), password="rest-key").status_code == 403
    denied = approve(oauth, start(oauth, client_id), decision="deny")
    assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["access_denied"]
    assert oauth.get("/mcp", headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_expiry_and_concurrent_single_use(oauth, oauth_settings):
    client_id = register(oauth).json()["client_id"]
    result = approve(oauth, start(oauth, client_id))
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: token(oauth, client_id, code), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 400]
    credentials = next(response.json() for response in responses if response.status_code == 200)
    body = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": credentials["refresh_token"]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: oauth.post("/token", data=body), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 400]
    credentials = next(response.json() for response in responses if response.status_code == 200)
    result = approve(oauth, start(oauth, client_id))
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    with sqlite3.connect(oauth_settings.mcp_oauth_db_path) as db:
        for value in (code, credentials["access_token"], credentials["refresh_token"]):
            db.execute("UPDATE entries SET expires=? WHERE id=?", (time.time() - 1, digest(value)))
    assert token(oauth, client_id, code).status_code == 400
    assert oauth.post("/token", data={**body, "refresh_token": credentials["refresh_token"]}).status_code == 400
    assert oauth.get("/mcp", headers={"Authorization": "Bearer " + credentials["access_token"]}).status_code == 401


def test_csrf_and_client_binding(oauth):
    client_id = register(oauth).json()["client_id"]
    other_id = register(oauth).json()["client_id"]
    auth = start(oauth, client_id)
    page = oauth.get(auth.headers["location"])
    pending = re.search(r"name=request value='([^']+)'", page.text).group(1)
    csrf = re.search(r"name=csrf value='([^']+)'", page.text).group(1)
    data = {"request": pending, "csrf": "forged", "password": SECRET, "decision": "approve"}
    assert oauth.post("/oauth/login", data=data, headers={"Origin": BASE}).status_code == 403
    data["csrf"] = csrf
    result = oauth.post("/oauth/login", data=data, headers={"Origin": BASE})
    assert result.status_code == 303
    assert oauth.post("/oauth/login", data=data, headers={"Origin": BASE}).status_code == 403
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    assert token(oauth, other_id, code).status_code == 400
    assert token(oauth, client_id, code).status_code == 200


def test_confidential_client_basic_auth(oauth):
    registration = register(oauth, token_endpoint_auth_method="client_secret_basic").json()
    client_id, secret = registration["client_id"], registration["client_secret"]
    result = approve(oauth, start(oauth, client_id))
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": VERIFIER}
    encoded = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    assert oauth.post("/token", data=data, headers={"Authorization": f"Basic {encoded}"}).status_code == 200


def test_secret_rotation_invalidates_grants_and_stale_instances(oauth, oauth_settings):
    client_id = register(oauth).json()["client_id"]
    response = approve(oauth, start(oauth, client_id))
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    credentials = token(oauth, client_id, code).json()
    unused = approve(oauth, start(oauth, client_id))
    unused_code = parse_qs(urlsplit(unused.headers["location"]).query)["code"][0]
    pending = start(oauth, client_id).headers["location"]

    with sqlite3.connect(oauth_settings.mcp_oauth_db_path) as db:
        old_verifier = db.execute("SELECT value FROM config WHERE key='login_verifier'").fetchone()[0]
    assert SECRET not in old_verifier
    assert hashlib.sha256(SECRET.encode()).hexdigest() not in old_verifier
    assert old_verifier.startswith("scrypt-32768-8-1$")

    with TestClient(create_app(oauth_settings), base_url=BASE, follow_redirects=False) as same:
        assert same.get(pending).status_code == 200
        assert same.post(
            "/mcp",
            headers={"Authorization": "Bearer " + credentials["access_token"],
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ).status_code == 200

    new_secret = "a different and equally long login password"
    changed = replace(oauth_settings, mcp_oauth_login_secret=new_secret)
    with TestClient(create_app(changed), base_url=BASE, follow_redirects=False) as rotated:
        assert rotated.get(pending).status_code == 400
        assert token(rotated, client_id, unused_code).status_code == 400
        assert rotated.get("/mcp", headers={"Authorization": "Bearer " + credentials["access_token"]}).status_code == 401
        assert rotated.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                            "refresh_token": credentials["refresh_token"]}).status_code == 400
        assert oauth.get(pending).status_code == 503
        assert oauth.get("/mcp", headers={"Authorization": "Bearer " + credentials["access_token"]}).status_code == 503
        assert approve(rotated, start(rotated, client_id), password=SECRET).status_code == 403
        assert approve(rotated, start(rotated, client_id), password=new_secret).status_code == 303

    with sqlite3.connect(oauth_settings.mcp_oauth_db_path) as db:
        new_verifier = db.execute("SELECT value FROM config WHERE key='login_verifier'").fetchone()[0]
    assert new_verifier != old_verifier
    assert new_secret not in new_verifier


def test_consent_shows_escaped_redirect_origin(oauth):
    redirect = 'https://evil&"name.example:8443/callback?private=not-shown'
    registration = register(oauth, redirect_uris=[redirect], client_name="<script>alert(1)</script>")
    assert registration.status_code == 201
    auth = start(oauth, registration.json()["client_id"], redirect_uri=redirect)
    page = oauth.get(auth.headers["location"])
    assert "https://evil&amp;&quot;name.example:8443" in page.text
    assert "private=not-shown" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<script>" not in page.text


def test_legacy_database_invalidates_grants_on_verifier_migration(oauth, oauth_settings):
    client_id = register(oauth).json()["client_id"]
    result = approve(oauth, start(oauth, client_id))
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    credentials = token(oauth, client_id, code).json()
    with sqlite3.connect(oauth_settings.mcp_oauth_db_path) as db:
        db.execute("DELETE FROM config WHERE key='login_verifier'")
    with TestClient(create_app(oauth_settings), base_url=BASE, follow_redirects=False) as migrated:
        assert migrated.get("/mcp", headers={"Authorization": "Bearer " + credentials["access_token"]}).status_code == 401
        assert migrated.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                             "refresh_token": credentials["refresh_token"]}).status_code == 400


def test_configuration_fails_closed(oauth_settings):
    for settings in [replace(oauth_settings, public_base_url=None), replace(oauth_settings, public_base_url="http://example.com"),
                     replace(oauth_settings, mcp_oauth_login_secret="rest-key"), replace(oauth_settings, mcp_oauth_db_path="relative"),
                     replace(oauth_settings, mcp_oauth_enabled=False)]:
        with pytest.raises(ValueError):
            create_app(settings)
