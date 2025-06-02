import time
import json
import requests
from jose import jwt as jose_jwt, JWTError
from typing import List, Dict, Any, Optional

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------

# OIDC discovery URL for your Identity Provider (replace with real URL)
OIDC_ISSUER = "https://your-idp.example.com"
JWKS_ENDPOINT = f"{OIDC_ISSUER}/.well-known/jwks.json"

# Cache the JWKS for token verification
_JWKS_CACHE: Optional[Dict[str, Any]] = None
_JWKS_LAST_FETCH: float = 0
JWKS_TTL_SECONDS = 3600  # re-fetch every hour

# ------------------------------------------------------------------
# UTILS: Fetch & Cache JWKS
# ------------------------------------------------------------------
def _fetch_jwks() -> Dict[str, Any]:
    global _JWKS_CACHE, _JWKS_LAST_FETCH
    now = time.time()
    if _JWKS_CACHE is None or now - _JWKS_LAST_FETCH > JWKS_TTL_SECONDS:
        resp = requests.get(JWKS_ENDPOINT, timeout=5)
        resp.raise_for_status()
        _JWKS_CACHE = resp.json()
        _JWKS_LAST_FETCH = now
    return _JWKS_CACHE

# ------------------------------------------------------------------
# VERIFY OIDC JWT (RS256)
# ------------------------------------------------------------------
def verify_jwt_token(token: str, audience: str) -> Dict[str, Any]:
    """
    Verify an RS256 JWT against the identity provider’s JWKS.
    Checks signature, 'exp', 'iat', 'aud', and 'iss'.
    Returns the decoded payload.
    Raises JWTError on invalid signature or claims.
    """
    jwks = _fetch_jwks()
    unverified_header = jose_jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")
    if not kid:
        raise JWTError("Missing 'kid' in token header")

    # Find matching JWK
    key_data = None
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            key_data = jwk
            break
    if key_data is None:
        # Possibly the JWKS rotated; force re-fetch once
        _JWKS_LAST_FETCH = 0
        jwks = _fetch_jwks()
        for jwk in jwks.get("keys", []):
            if jwk.get("kid") == kid:
                key_data = jwk
                break
        if key_data is None:
            raise JWTError("Unable to find matching JWK for kid: " + kid)

    # Use jose to decode & verify
    try:
        payload = jose_jwt.decode(
            token,
            key_data,
            algorithms=["RS256"],
            audience=audience,
            issuer=OIDC_ISSUER,
        )
        return payload
    except JWTError as e:
        raise

# ------------------------------------------------------------------
# CHECK CAPABILITY / AUDIENCE / DELEGATION
# ------------------------------------------------------------------
def has_capability(payload: Dict[str, Any], required: str) -> bool:
    """
    Returns True if 'required' matches any entry in payload["capabilities"].
    Supports exact match and wildcard suffix (e.g., "db:inventory:*").
    """
    caps = payload.get("capabilities", [])
    for cap in caps:
        if cap == required:
            return True
        if cap.endswith("*"):
            prefix = cap[:-1]
            if required.startswith(prefix):
                return True
    return False

def has_audience(payload: Dict[str, Any], target: str) -> bool:
    """
    Checks if payload["aud"] (which may be string or list) matches `target` or its wildcard.
    """
    aud_claim = payload.get("aud")
    if isinstance(aud_claim, str):
        audiences = [aud_claim]
    else:
        audiences = list(aud_claim or [])
    for aud in audiences:
        if aud == target:
            return True
        if isinstance(aud, str) and aud.endswith("*"):
            prefix = aud[:-1]
            if target.startswith(prefix):
                return True
    return False

def verify_delegation_proof(
    delegation_jwt: str,
    delegatee: str,
    original_token_payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Verifies that `delegation_jwt`:
      1. Is a valid RS256 JWT issued by the same IdP (issuer matches OIDC_ISSUER).
      2. Has 'sub' equal to original_token_payload['sub'] (i.e., original subject).
      3. Has 'delegatee' == delegatee (the server name calling this function).
      4. Its 'capabilities' is a subset of original_token_payload['capabilities'].
      5. Its 'aud' properly targets this delegatee.

    Returns the decoded delegation payload.
    Raises JWTError or ValueError on any check failure.
    """
    # 1. Decode & verify signature + claims (audience = delegatee)
    payload = verify_jwt_token(delegation_jwt, audience=delegatee)

    # 2. Confirm same issuer and subject
    if payload.get("iss") != original_token_payload.get("iss"):
        raise ValueError("Delegation proof 'iss' does not match original token issuer")
    if payload.get("sub") != original_token_payload.get("sub"):
        raise ValueError("Delegation proof 'sub' must match original token 'sub'")

    # 3. Check delegatee
    if payload.get("delegatee") != delegatee:
        raise ValueError(f"Delegation proof not intended for this server ({delegatee})")

    # 4. Check capability subset
    orig_caps = set(original_token_payload.get("capabilities", []))
    del_caps = set(payload.get("capabilities", []))
    if not del_caps.issubset(orig_caps):
        raise ValueError("Delegated capabilities exceed original token’s capabilities")

    # 5. Audience check already done in verify_jwt_token
    return payload
