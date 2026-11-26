"""Offline checks for locked setup, credential-free inventory and session migration."""
import hashlib
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import ROOT, bootstrap, check, declare_suite, finish
P, A = bootstrap()
S = P.qoder_settings
declare_suite(__file__, ["qoder_proxy.py", "qoder_settings.py", "qoder_accounts.py", "docker-compose.yml"])


class Handler(P.Handler):
    def log_message(self, *args):
        pass


def request(server, path, body=None, token=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    h = dict(headers or {})
    if token:
        h["X-Panel-Token"] = token
    if body is not None:
        h["Content-Type"] = "application/json"
    try:
        conn.request("GET" if body is None else "POST", path,
                     body=None if body is None else json.dumps(body), headers=h)
        with conn.getresponse() as r:
            return r.status, json.loads(r.read()), dict(r.getheaders())
    finally:
        conn.close()


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store = str(root / "accounts")
    pool = A.AccountPool(store)
    pool.add(A.Account({"uid": "local-fixture", "realm": "cn", "accessToken": "synthetic-access",
                        "refreshToken": "synthetic-refresh", "personalToken": "synthetic-pat",
                        "note": "private-note-fixture", "credits": {"snapshot": "synthetic-nested"}}))
    S.save(store, {S.PANEL_SECRET_KEY: "fixture-hmac-key" * 4,
                   "panel_password_default": True,
                   "api_keys": [{"key": "fixture-inference-key", "id": "fixture-inference", "enabled": True}]})
    legacy_stamp = "%d.fixture" % (int(time.time()) + 3600)
    legacy_mac = hmac.new(S.panel_session_secret(store).encode(), legacy_stamp.encode(), hashlib.sha256).hexdigest()
    legacy = legacy_stamp + "." + legacy_mac
    panel = S.PanelSessions(store)
    with mock.patch.multiple(P, ACCOUNTS_DIR=store, PANEL=panel, POOL=pool, API_KEY="fixture-inference-key"):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            status, body, _ = request(server, "/panel/status", token=legacy)
            check("legacy credential state locks panel and ignores existing sessions",
                  status == 200 and body["panel_password_is_default"] and not body["authenticated"])
            status, body, _ = request(server, "/panel/login", {"password": S.DEFAULT_PANEL_PASSWORD})
            check("unprovisioned panel does not furnish a session even on loopback",
                  status == 403 and "token" not in body)
            status, _, _ = request(server, "/panel/password", {"new": "fixture-replacement-01"}, token=legacy)
            check("first password cannot be established by HTTP", status == 401)
            status, _, _ = request(server, "/accounts/export", headers={"Authorization": "Bearer fixture-inference-key"})
            check("inference keys cannot authorize management inventory", status == 401)
            status, _, _ = request(server, "/v1/messages/count_tokens",
                                   {"model": "auto", "messages": [{"role": "user", "content": "Hello"}]},
                                   headers={"x-api-key": "fixture-inference-key"})
            check("inference stays available with its own key while panel is locked", status == 200)
            S.set_panel_password(store, "fixture-panel-password")
            status, body, headers = request(server, "/panel/login", {"password": "fixture-panel-password"})
            check("unique password allows ordinary panel login without caches", status == 200 and headers.get("Cache-Control") == "no-store")
            token = body["token"]
            with mock.patch.object(A, "scan_desktop_credentials", return_value=[]), \
                    mock.patch.object(A, "import_local_credential_file") as local_import:
                status, _, _ = request(server, "/accounts/import/desktop",
                                       {"path": str(root / "unlisted.json")}, token=token)
                check("HTTP never reaches the terminal-only file import", status == 400 and not local_import.called)
            previous_realm = P.CURRENT_REALM
            with mock.patch.object(P, "save_persisted_realm") as persist:
                for auth in ({}, {"x-api-key": "fixture-inference-key"}):
                    status, _, _ = request(server, "/realm", {"realm": "intl"}, headers=auth)
                    check("ordinary requests cannot mutate global routing", status == 401 and not persist.called)
                status, _, _ = request(server, "/realm", {"realm": "intl"}, token=token)
                check("authenticated panel can still configure global routing", status == 200 and persist.call_count == 1)
            check("routing fixture did not touch saved state", P.CURRENT_REALM == previous_realm)
            with mock.patch.object(P, "NETWORK_AUTH_REQUIRED", True), mock.patch.object(P, "API_KEY", ""):
                S.set_auth_disabled(store, True)
                check("network listeners still require an API key", P.auth_required())
                status, _, _ = request(server, "/v1/messages/count_tokens?key=fixture-inference-key",
                                       {"model": "auto", "messages": []})
                check("credentials in query strings are not accepted", status == 401)
                status, _, _ = request(server, "/v1/messages/count_tokens",
                                       {"model": "auto", "messages": []}, headers={"x-api-key": "ä"})
                check("malformed key does not cause a server error", status == 401)
                status, _, _ = request(server, "/settings/save", {"auth_disabled": True}, token=token)
                check("panel cannot disable network listener authentication", status == 403)
                S.set_auth_disabled(store, False)
            check("old format stays invalid after setup and restart", not S.PanelSessions(store).valid(legacy))
            check("current format is valid across restart", S.PanelSessions(store).valid(token))
            for suffix in ("", "?secrets=0", "?uid=local-fixture&download=1"):
                status, body, headers = request(server, "/accounts/export" + suffix, token=token)
                check("management inventory contains only metadata", status == 200 and body["count"] == 1
                      and body["accounts"][0]["uid"] == "local-fixture"
                      and set(body["accounts"][0]) <= set(A._EXPORT_METADATA_FIELDS)
                      and all(x not in json.dumps(body) for x in ("synthetic-", "private-note-fixture"))
                      and headers.get("Cache-Control") == "no-store")
            for query in ("secrets=1", "secrets=true", "secrets=", "secrets=0&secrets=yes&download=1"):
                status, body, _ = request(server, "/accounts/export?" + query, token=token)
                check("HTTP credential backup options fail closed", status == 403 and "accounts" not in body)
            status, _, _ = request(server, "/panel/password", {"current": "fixture-panel-password", "new": "admin"}, token=token)
            check("password cannot be reset to the legacy default", status == 400 and S.PanelSessions(store).valid(token))
            status, body, _ = request(server, "/panel/password",
                                       {"current": "fixture-panel-password", "new": "fixture-replacement-01"}, token=token)
            check("password change rotates even across restart", status == 200
                  and not S.PanelSessions(store).valid(token) and S.PanelSessions(store).valid(body["token"]))
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
    check("setup preserves existing API keys", S.api_keys(store)[0]["id"] == "fixture-inference")
    path = root / "backup.json"
    check("only local backup contains the restorable account record", P._write_local_account_backup(store, path) == 1)
    row = json.loads(path.read_text(encoding="utf-8"))["accounts"][0]
    check("local backup retains required fields", all(row[k] for k in ("accessToken", "refreshToken", "personalToken")))
    if os.name == "posix":
        check("backup and settings modes grant access only to owner",
              stat.S_IMODE(path.stat().st_mode) == 0o600
              and stat.S_IMODE(Path(S.settings_path(store)).stat().st_mode) == 0o600)
    before = path.read_bytes()
    try:
        P._write_local_account_backup(store, path)
    except FileExistsError:
        check("backup never overwrites an existing file", path.read_bytes() == before)
    else:
        check("backup refuses to overwrite", False)
    target = Path(S.settings_path(store))
    target.write_text("{broken", encoding="utf-8")
    try:
        S.set_panel_password(store, "fixture-replacement-02")
    except OSError:
        check("unreadable settings fail closed without replacement", target.read_text(encoding="utf-8") == "{broken"
              and not S.verify_panel_password(store, "fixture-replacement-01"))
    else:
        check("do not overwrite unreadable settings", False)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store = root / "accounts"
    fixture = root / "custom credential.json"
    fixture.write_text(json.dumps({"uid": "terminal-fixture", "access_token": "dt-terminal-fixture"}), encoding="utf-8")
    command = [sys.executable, str(Path(ROOT) / "qoder_proxy.py"), "--accounts-dir", str(store),
               "--credential-realm", "cn", "--import-local-credential"]
    output = subprocess.run(command + [str(fixture)], capture_output=True, text=True, timeout=15)
    check("terminal imports an ordinary custom path", output.returncode == 0
          and (store / "terminal-fixture.json").is_file()
          and "dt-terminal-fixture" not in (output.stdout + output.stderr))
    for bad in (str(root), "https://example.invalid/credential", "//example.invalid/file"):
        output = subprocess.run(command + [bad], capture_output=True, text=True, timeout=15)
        check("terminal rejects non-file and network paths", output.returncode == 2)

compose = (Path(ROOT) / "docker-compose.yml").read_text(encoding="utf-8")
check("packaged Compose publishes only on the host loopback", '- "127.0.0.1:8790:8790"' in compose
      and '- "8790:8790"' not in compose)
finish()
