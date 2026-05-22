"""One-shot eBay OAuth bootstrap.

Walks you through:
  1. Building the consent URL
  2. Opening it in your browser
  3. Pasting back the redirect URL eBay sends you to
  4. Exchanging the auth code for a long-lived refresh token (~18 months)
  5. Saving the encrypted credential to tenant_credentials

Usage:
    python3 scripts/ebay_oauth_bootstrap.py

You'll be prompted for App ID, Cert ID, RuName, the redirect URL, and
your Accounting-Ops service token. Nothing is logged; secrets only live
in memory during the script's run.

What gets persisted:
  - tenant_credentials row with kind='ebay_finances' for your workspace,
    encrypted via AES-256-GCM + per-tenant HKDF DEK
  - The JSON blob shape the ebay-finances-mcp expects:
      {refresh_token, client_id, client_secret, ru_name}
"""
from __future__ import annotations

import base64
import getpass
import json
import sys
import urllib.parse
import webbrowser

import httpx

EBAY_AUTH_BASE = "https://auth.ebay.com/oauth2/authorize"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

DEFAULT_SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.finances",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
]


def prompt(label: str, *, secret: bool = False) -> str:
    if secret:
        v = getpass.getpass(f"{label}: ").strip()
    else:
        v = input(f"{label}: ").strip()
    if not v:
        sys.exit(f"Empty value for {label}, aborting.")
    return v


def build_consent_url(client_id: str, ru_name: str, scopes: list[str]) -> str:
    """eBay's quirk: redirect_uri is the RuName (a name), NOT the actual URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": ru_name,
        "scope": " ".join(scopes),
        "prompt": "login",
    }
    return f"{EBAY_AUTH_BASE}?{urllib.parse.urlencode(params)}"


def extract_code_from_redirect(redirect_url: str) -> str:
    """eBay URL-encodes the code itself; httpx + urllib handle it correctly."""
    parsed = urllib.parse.urlparse(redirect_url)
    qs = urllib.parse.parse_qs(parsed.query)
    code = qs.get("code", [None])[0]
    if not code:
        if "error" in qs:
            sys.exit(f"eBay rejected the consent: {qs.get('error_description', qs['error'])}")
        sys.exit("No ?code= found in the redirect URL. Did consent fail?")
    return urllib.parse.unquote(code)


def exchange_code(client_id: str, client_secret: str, ru_name: str, code: str) -> dict:
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ru_name,
    }
    resp = httpx.post(
        EBAY_TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=body,
        timeout=15,
    )
    if resp.status_code != 200:
        sys.exit(f"eBay token exchange failed [{resp.status_code}]: {resp.text}")
    return resp.json()


def save_credential(
    *,
    api_base: str,
    service_token: str,
    workspace_id: str,
    label: str,
    blob: dict,
) -> None:
    """POST to Accounting-Ops /credentials/{kind}. Server encrypts + stores."""
    payload = {"label": label, "plaintext_json": json.dumps(blob)}
    resp = httpx.post(
        f"{api_base.rstrip('/')}/api/workspaces/{workspace_id}/credentials/ebay_finances",
        headers={
            "Authorization": f"Bearer {service_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        sys.exit(f"Credential save failed [{resp.status_code}]: {resp.text}")
    print(f"  → encrypted and stored as kind='ebay_finances', label='{label}'")


def main() -> None:
    print("eBay OAuth bootstrap — Accounting-Ops")
    print("=" * 50)
    print()
    print("This will mint an 18-month refresh token for the Finances API and")
    print("store it encrypted in your workspace. Nothing is logged; secrets")
    print("only live in memory during this script.")
    print()

    client_id = prompt("App ID / Client ID (e.g. MagicUni-MUUTpost-PRD-...)")
    client_secret = prompt("Cert ID / Client Secret", secret=True)
    ru_name = prompt(
        "RuName (e.g. Magic_Unicorn_U-MagicUni-MUUTpo-xfngkob)",
    )

    consent_url = build_consent_url(client_id, ru_name, DEFAULT_SCOPES)
    print()
    print("Opening consent URL in your default browser...")
    print(f"  {consent_url}")
    print()
    try:
        webbrowser.open(consent_url)
    except Exception:
        pass
    print("After approving on eBay, your browser will redirect to the RuName's")
    print("'auth accepted URL' with a ?code=... query string. Copy the entire")
    print("URL bar (even if the destination page 404s — eBay still puts the")
    print("code in the URL) and paste it below.")
    print()

    redirect_url = prompt("Paste the redirect URL")
    code = extract_code_from_redirect(redirect_url)
    print(f"  → extracted auth code (len={len(code)})")

    print("Exchanging code for refresh token...")
    tokens = exchange_code(client_id, client_secret, ru_name, code)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        sys.exit(f"No refresh_token in response: {json.dumps(tokens)}")
    expires_in = tokens.get("refresh_token_expires_in", 0)
    days = expires_in // 86400 if expires_in else "?"
    print(f"  → got refresh_token (valid ~{days} days)")

    print()
    print("Save to Accounting-Ops? (y/n)")
    if input("> ").strip().lower() not in ("y", "yes"):
        print()
        print("Skipping save. The refresh_token is in memory; the JSON blob to")
        print("store in tenant_credentials should look like:")
        print(
            json.dumps(
                {
                    "refresh_token": "<the long token>",
                    "client_id": client_id,
                    "client_secret": "<your cert id>",
                    "ru_name": ru_name,
                },
                indent=2,
            )
        )
        return

    api_base = prompt(
        "Accounting-Ops API base URL "
        "(e.g. https://accounting-ops.magicunicorn.dev or http://100.121.80.175:8089)",
    )
    service_token = prompt("Service token (sk-... from your .env)", secret=True)
    workspace_id = prompt(
        "Workspace ID (UUID; e.g. 77753ea5-efe9-5539-a846-6b20ddc841b9)"
    )
    label = input("Label for this credential [eBay Finances]: ").strip() or "eBay Finances"

    blob = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "ru_name": ru_name,
    }
    save_credential(
        api_base=api_base,
        service_token=service_token,
        workspace_id=workspace_id,
        label=label,
        blob=blob,
    )
    print()
    print("Done. The ebay-finances-mcp can now fetch this credential and start")
    print("producing transactions in the canonical Accounting-Ops shape.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
