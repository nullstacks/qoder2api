#!/usr/bin/env python3
"""qoder_oauth.py — standalone Qoder (international) device-flow login.

Mirrors cline_oauth.py: prints an authorization URL, you log in in a browser,
the script polls the device-token endpoint and prints the credential JSON.
Tokens are only exchanged server-side-in-script; they never transit a browser.

Usage:
  python3 qoder_oauth.py            # login, prints credential JSON
  python3 qoder_oauth.py --test     # login, then verify quota endpoint

After login, paste the printed credential JSON into the Coding2API dashboard
(凭证管理 → 添加凭证 → qoder), or feed the server its own login flow.

Only dependency: httpx (`pip install httpx`). Python 3.10+.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Windows consoles often default to a legacy code page (cp1252, gbk, ...) that
# cannot encode this script's emoji/CJK output — it crashes before printing
# anything. Reconfigure both streams to UTF-8 (unencodable chars -> "?").
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

OPENAPI = "https://openapi.qoder.sh"
WEBSITE = "https://qoder.com"
CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"
CHALLENGE_METHOD = "S256"
CLIENT_UA = "Go-http-client/2.0"
EP_DEVICE_POLL = "/api/v1/deviceToken/poll"
EP_USERINFO = "/api/v1/userinfo"
POLL_INTERVAL = 5
POLL_TIMEOUT = 600
PKCE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def pkce_pair():
    verifier = "".join(PKCE_ALPHABET[b % len(PKCE_ALPHABET)] for b in secrets.token_bytes(64))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def machine_id():
    """Prefer the official client's stored machine id (shared device id lowers
    risk-control friction); else a stable per-machine value."""
    home = os.path.expanduser("~")
    for path in (os.path.join(home, "qoder.com", ".auth", "machine_id"),
                 os.path.join(home, ".qoder.com", ".auth", "machine_id")):
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
                if value:
                    return value
        except OSError:
            continue
    marker = os.path.join(home, ".qoder_oauth_machine_id")
    if os.path.isfile(marker):
        return open(marker, encoding="utf-8").read().strip()
    value = str(uuid.uuid4())
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value


def build_auth_url(challenge, nonce, mid):
    query = urllib.parse.urlencode({
        "challenge": challenge, "challenge_method": CHALLENGE_METHOD,
        "nonce": nonce, "client_id": CLIENT_ID, "machine_id": mid,
    })
    return f"{WEBSITE}/device/selectAccounts?{query}"


def get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": CLIENT_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="verify quota endpoint after login")
    args = parser.parse_args()

    verifier, challenge = pkce_pair()
    nonce = uuid.uuid4().hex
    mid = machine_id()
    auth_url = build_auth_url(challenge, nonce, mid)

    print("=" * 60)
    print("1. Open this link in your browser:")
    print(f"   {auth_url}")
    print("2. Sign in with your Qoder account and approve")
    print(f"3. The script polls automatically (max {POLL_TIMEOUT}s)")
    print("=" * 60)

    poll_url = f"{OPENAPI}{EP_DEVICE_POLL}?" + urllib.parse.urlencode({
        "nonce": nonce, "verifier": verifier, "challenge_method": CHALLENGE_METHOD})
    deadline = time.time() + POLL_TIMEOUT
    device = None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            device = get_json(poll_url)
        except urllib.error.HTTPError as err:
            if err.code in (400, 404):
                print(f"   [{int(time.time() - (deadline - POLL_TIMEOUT)):>3}s] pending…")
                continue
            print(f"   poll HTTP {err.code}: {err.read().decode()[:120]}")
            continue
        break
    if not device:
        print("❌ timed out — rerun for a fresh link")
        sys.exit(1)

    token = device.get("token") or device.get("device_token")
    if not token:
        # authorized but token not ready yet — one more poll
        time.sleep(POLL_INTERVAL)
        device = get_json(poll_url)
        token = device.get("token") or device.get("device_token")
    if not token:
        print("❌ no token in device response:", json.dumps(device)[:200])
        sys.exit(1)

    try:
        userinfo = get_json(f"{OPENAPI}{EP_USERINFO}",
                            headers={"Authorization": f"Bearer {token}"})
    except Exception:
        userinfo = {}
    nickname = userinfo.get("nickname") or userinfo.get("name") or ""

    credential = {
        "access_token": token,
        "refresh_token": device.get("refresh_token", ""),
        "uid": userinfo.get("userId") or userinfo.get("id") or token[:16],
        "nickname": nickname,
        "realm": "intl",
        "domain": "qoder.com",
        "auth_source": "oauth",
        "user_type": userinfo.get("userType", ""),
        "expires_at": device.get("expiresAt") or int(time.time() + 30 * 86400),
    }

    print("\n" + "=" * 60)
    print(f"✅ Login OK: {nickname or '(no nickname)'}")
    if args.test:
        try:
            req = urllib.request.Request(f"{OPENAPI}/api/v2/quota/usage",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                quota = json.loads(resp.read().decode())
            print(f"   quota: {quota.get('usageType')} | exceeded: {quota.get('isQuotaExceeded')}")
        except Exception as err:
            print(f"   quota check failed: {err}")
    print("\n🔑 Credential JSON (paste into Coding2API → 凭证管理 → 添加凭证 → qoder):")
    print(json.dumps(credential, ensure_ascii=False, indent=2))
    print("=" * 60)


if __name__ == "__main__":
    main()
