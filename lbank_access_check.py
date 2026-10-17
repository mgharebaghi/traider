"""Signed, read-only LBank access checks. Never submits orders or changes account settings.

Sources:
https://www.lbank.com/docs/contract.html (account signing example)
https://www.lbank.com/docs/index.html (API restrictions)
https://github.com/LBank-exchange/lbank-connector-python (signing implementation)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import string
import sys
import time
from typing import Any

import requests

from config import _str

PERMISSIONS_URL = "https://api.lbank.info/v2/supplement/api_Restrictions.do"
TIME_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/pub/getTime"
ACCOUNT_URL = "https://lbkperp.lbank.com/cfd/openApi/v1/prv/account"
PERMISSION_FLAGS = ("enableReading", "enableFuturesTrading", "enableSpotTrading",
                    "enableWithdrawals", "enableTransfer", "ipRestrict")


def sign_payload(api_key: str, secret: str, params: dict[str, str], *,
                 timestamp: str | None = None, nonce: str | None = None) -> tuple[dict, dict]:
    if not api_key or not secret:
        raise ValueError("Exchange credentials are missing")
    stamp = timestamp or str(int(time.time() * 1000))
    nonce = nonce or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(35))
    method = "HmacSHA256" if len(secret) <= 32 else "RSA"
    auth = {**params, "api_key": api_key, "timestamp": stamp,
            "echostr": nonce, "signature_method": method}
    canonical = "&".join(f"{key}={value}" for key, value in sorted(auth.items()))
    digest = hashlib.md5(canonical.encode()).hexdigest().upper().encode()
    if method == "HmacSHA256":
        signature = hmac.new(secret.encode(), digest, hashlib.sha256).hexdigest()
    else:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        material = secret.encode() if "BEGIN" in secret else base64.b64decode(secret, validate=True)
        private_key = (serialization.load_pem_private_key(material, password=None)
                       if b"BEGIN" in material else serialization.load_der_private_key(material, password=None))
        signature = base64.b64encode(private_key.sign(digest, padding.PKCS1v15(), hashes.SHA256())).decode()
    return ({**params, "api_key": api_key, "sign": signature},
            {"timestamp": stamp, "echostr": nonce, "signature_method": method})


def summarize(response, *, permissions: bool = False) -> dict[str, Any]:
    """Only whitelist non-secret status fields; never include response text, URLs or balances."""
    result: dict[str, Any] = {
        "http_status": response.status_code,
        "content_type": response.headers.get("Content-Type", "").split(";")[0],
    }
    # A proxy/edge block must not be reported as a rejected API key.
    for header in ("cf-mitigated", "x-mitmproxy-blocked-reason"):
        value = response.headers.get(header)
        if value and value.lower() in ("challenge", "robots_denied", "geo_restricted"):
            result["edge_block"] = value
    try:
        data = response.json()
    except ValueError:
        result["api_response"] = False
        return result
    if not isinstance(data, dict):
        result["api_response"] = False
        return result
    result["api_response"] = True
    for field in ("code", "error_code", "result", "success"):
        value = data.get(field)
        if isinstance(value, (bool, int)) or (isinstance(value, str) and value.lower() in ("true", "false")):
            result[field] = value
        elif isinstance(value, str) and value.isdigit() and len(value) <= 8:
            result[field] = value
    if permissions and isinstance(data.get("data"), dict):
        result["permissions"] = {k: data["data"][k] for k in PERMISSION_FLAGS
                                 if isinstance(data["data"].get(k), bool)}
    return result


def check_access(api_key: str, secret: str, *, proxy: str = "", account_post: bool = False,
                 session=None) -> dict[str, Any]:
    """The request allowlist is fixed: permissions, public clock, and account read only.

    POST is allowed only for documented read operations. No arbitrary URL/path,
    order, cancellation, transfer, leverage or withdrawal method is exposed.
    """
    own_session = session is None
    session = session or requests.Session()
    options: dict[str, Any] = {"timeout": 20, "allow_redirects": False}
    if proxy:
        options["proxies"] = {"http": proxy, "https": proxy}
    results = {}
    try:
        for label in ("permissions", "public_clock", "futures_account"):
            try:
                if label == "public_clock":
                    response = session.get(TIME_URL, **options)
                else:
                    params = {} if label == "permissions" else {"asset": "USDT", "productGroup": "SwapU"}
                    payload, headers = sign_payload(api_key, secret, params)
                    if label == "permissions":
                        response = session.post(PERMISSIONS_URL, data=payload, headers=headers, **options)
                    elif account_post:
                        headers["Content-Type"] = "application/json"
                        response = session.post(ACCOUNT_URL, json=payload, headers=headers, **options)
                    else:
                        headers["Content-Type"] = "application/json"
                        response = session.get(ACCOUNT_URL, params=payload, headers=headers, **options)
                results[label] = summarize(response, permissions=label == "permissions")
            except Exception as exc:
                # Requests exceptions may embed signed URLs. Only report the class.
                results[label] = {"error_type": type(exc).__name__}
        results["live_trading_ready"] = False
        results["note_fa"] = "این ابزار فقط دسترسی را بررسی می‌کند؛ ثبت سفارش و حد سود/ضرر را تأیید نمی‌کند."
        return results
    finally:
        if own_session:
            session.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-post", action="store_true", help="Read the account using JSON POST instead of GET")
    args = parser.parse_args()
    key, secret = _str("EXCHANGE_API_KEY"), _str("EXCHANGE_API_SECRET")
    if not key or not secret:
        print("EXCHANGE_API_KEY / EXCHANGE_API_SECRET are missing; values are never printed.")
        raise SystemExit(1)
    report = check_access(key, secret, proxy=_str("EXCHANGE_PROXY_URL"), account_post=args.account_post)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(2)  # Even authenticated reads cannot certify live trading.


if __name__ == "__main__":
    main()
