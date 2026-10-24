"""WebUI の認証とブラウザ要求の制限。"""

import base64

import httpx
import pytest

from app import _parse_args, build_allowed_hosts, create_app, is_loopback_host


@pytest.mark.parametrize("host", ["127.0.0.1", "127.42.0.9", "::1", "[::1]", "localhost"])
def test_loopback_hosts(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "camera.local"])
def test_non_loopback_hosts(host):
    assert not is_loopback_host(host)


def test_wildcard_host_check_is_disabled_without_allowed_names():
    assert build_allowed_hosts("0.0.0.0", []) is None
    assert build_allowed_hosts("::", ["camera.local"]) == [
        "[::]", "localhost", "127.0.0.1", "[::1]", "camera.local",
    ]
    assert build_allowed_hosts("2001:db8::1", [])[0] == "[2001:db8::1]"


def test_non_loopback_start_requires_password(monkeypatch, capsys):
    monkeypatch.delenv("TAPO_WEBUI_PASSWORD", raising=False)

    with pytest.raises(SystemExit) as error:
        _parse_args(["--host", "192.0.2.1"])

    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "--password" in message
    assert "TAPO_WEBUI_PASSWORD" in message


def test_password_option_overrides_environment(monkeypatch):
    monkeypatch.setenv("TAPO_WEBUI_PASSWORD", "environment")

    assert _parse_args(["--host", "0.0.0.0", "--password", "argument"]).password == "argument"


def test_allowed_hosts_combine_environment_and_options(monkeypatch):
    monkeypatch.setenv("TAPO_WEBUI_ALLOWED_HOSTS", "first.local, second.local")

    args = _parse_args(["--host", "0.0.0.0", "--password", "secret", "--allowed-host", "third.local"])

    assert args.allowed_hosts[-3:] == ["first.local", "second.local", "third.local"]


def _authorization(password: str) -> str:
    value = base64.b64encode(f"anything:{password}".encode()).decode()
    return f"Basic {value}"


async def test_basic_authentication_and_host_check():
    app = create_app(password="secret", allowed_hosts=["camera.local"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://camera.local") as client:
        unauthorized = await client.get("/")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == 'Basic realm="Tapo C220"'
        wrong_password = await client.get("/openapi.json", headers={"Authorization": _authorization("wrong")})
        assert wrong_password.status_code == 401

        authorized = await client.get("/api/record", headers={"Authorization": _authorization("secret")})
        assert authorized.status_code == 200
        assert authorized.json() == {"recording": False}
        no_origin = await client.post("/not-found", headers={"Authorization": _authorization("secret")})
        assert no_origin.status_code == 404

        rejected_host = await client.get(
            "http://attacker.local/", headers={"Authorization": _authorization("secret")},
        )
        assert rejected_host.status_code == 400


async def test_post_from_different_origin_is_rejected():
    app = create_app(password="secret", allowed_hosts=["camera.local"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://camera.local") as client:
        response = await client.post(
            "/api/ptz/stop",
            headers={"Authorization": _authorization("secret"), "Origin": "http://attacker.local"},
        )

    assert response.status_code == 403


async def test_create_app_requires_password_for_non_loopback_request(monkeypatch):
    monkeypatch.delenv("TAPO_WEBUI_PASSWORD", raising=False)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://192.0.2.1") as client:
        rejected = await client.get("/api/record")
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        allowed = await client.get("/api/record")

    assert rejected.status_code == 403
    assert "TAPO_WEBUI_PASSWORD" in rejected.json()["detail"]
    assert allowed.status_code == 200
    assert allowed.json() == {"recording": False}


async def test_create_app_reads_password_from_environment(monkeypatch):
    monkeypatch.setenv("TAPO_WEBUI_PASSWORD", "environment")

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/record")

    assert response.status_code == 401


async def test_passwordless_app_rejects_unknown_server_address(monkeypatch):
    monkeypatch.delenv("TAPO_WEBUI_PASSWORD", raising=False)
    app = create_app()
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/api/record", "raw_path": b"/api/record", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1")], "client": ("127.0.0.1", 50000), "server": None}
    await app(scope, receive, send)

    assert sent[0]["status"] == 403
