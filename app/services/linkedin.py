"""Everything that talks to LinkedIn, and nothing else.

    authorize_url()  -> where to send the admin to approve the console
    exchange_code()  -> swap LinkedIn's one-time code for an access token
    fetch_member()   -> who signed in (name + member id), via OpenID Connect
    create_post()    -> publish text as that member, or as the company Page
    read_post()      -> read a post back (for the live post watch)

Your LinkedIn app needs two products in the Developer Portal:
  - "Sign In with LinkedIn using OpenID Connect"  (scopes: openid, profile)
  - "Share on LinkedIn"                           (scope:  w_member_social)
Posting as a company Page also needs the "Community Management API" product
(scope w_organization_social), which LinkedIn must approve first.

The tests replace these functions with fakes, so no test ever calls LinkedIn.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

import httpx

from ..config import Settings

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"

MEMBER_SCOPES = ["openid", "profile", "w_member_social"]
ORG_SCOPE = "w_organization_social"


class LinkedInError(Exception):
    """LinkedIn said no. The message is written for the publisher, not a developer."""


def requested_scopes(s: Settings) -> list[str]:
    # Ask for company posting only when a company Page is configured. LinkedIn rejects
    # the whole sign-in if an app asks for a scope it has not been approved for.
    return MEMBER_SCOPES + ([ORG_SCOPE] if s.linkedin_organization_urn else [])


def authorize_url(s: Settings, state: str) -> str:
    query = {
        "response_type": "code",
        "client_id": s.linkedin_client_id,
        "redirect_uri": s.linkedin_redirect_uri,
        "state": state,
        "scope": " ".join(requested_scopes(s)),
    }
    return f"{AUTH_URL}?{urlencode(query)}"


def exchange_code(s: Settings, code: str) -> dict:
    """Returns LinkedIn's token response: access_token, expires_in (seconds), scope, ..."""
    response = httpx.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": s.linkedin_redirect_uri,
              "client_id": s.linkedin_client_id, "client_secret": s.linkedin_client_secret},
        timeout=20,
    )
    if response.status_code >= 300:
        raise LinkedInError(f"LinkedIn did not accept the sign-in ({response.status_code}). Try connecting again.")
    return response.json()


def fetch_member(access_token: str) -> tuple[str, str]:
    """(member URN, display name) of whoever signed in."""
    response = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    if response.status_code >= 300:
        raise LinkedInError("Could not read your LinkedIn profile. Check the app has the OpenID Connect product.")
    info = response.json()
    return f"urn:li:person:{info['sub']}", info.get("name") or "LinkedIn member"


def create_post(access_token: str, api_version: str, author_urn: str, text: str) -> str:
    """Publish a text post. Returns the post's URN (e.g. urn:li:share:7381...)."""
    response = httpx.post(
        POSTS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={
            "author": author_urn,
            "commentary": escape_commentary(text),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        },
        timeout=20,
    )
    if response.status_code >= 300:
        raise LinkedInError(_explain(response))
    return response.headers.get("x-restli-id", "")


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}" if urn else ""


_POST_URN = re.compile(r"urn:li:(?:share|ugcPost):[A-Za-z0-9_-]+")


def urn_from_url(url: str) -> str | None:
    """https://www.linkedin.com/feed/update/urn:li:share:123 -> urn:li:share:123 (None if not a post link)."""
    match = _POST_URN.search(url or "")
    return match.group(0) if match else None


def read_post(access_token: str, api_version: str, urn: str) -> str | None:
    """The post's text as it is live now. None = the post no longer exists."""
    response = httpx.get(
        f"{POSTS_URL}/{quote(urn, safe='')}",
        headers={"Authorization": f"Bearer {access_token}", "LinkedIn-Version": api_version,
                 "X-Restli-Protocol-Version": "2.0.0"},
        timeout=20,
    )
    if response.status_code in (404, 410):
        return None
    if response.status_code == 403:
        raise LinkedInError("LinkedIn doesn't let this connection read posts back. Reading is included in "
                            "LinkedIn's company-page (Community Management API) approval.")
    if response.status_code >= 300:
        raise LinkedInError(_explain(response))
    return unescape_commentary(response.json().get("commentary", ""))


def unescape_commentary(text: str) -> str:
    r"""Undo escape_commentary: '\(' -> '('."""
    return re.sub(r"\\(.)", r"\1", text)


# ---------------------------------------------------------------------------
# LinkedIn's "little text" format treats these characters as markup. Unescaped,
# a "(" or "[" can silently cut the rest of the post off. "#" stays unescaped so
# hashtags keep working as hashtags.
_RESERVED = set("\\|{}@[]()<>*_~")


def escape_commentary(text: str) -> str:
    return "".join(f"\\{ch}" if ch in _RESERVED else ch for ch in text)


def _explain(response: httpx.Response) -> str:
    code = response.status_code
    if code == 401:
        return "LinkedIn says the connection is no longer valid (expired or revoked). An admin needs to reconnect LinkedIn."
    if code == 403:
        return ("LinkedIn refused permission to post here. If this is a company Page, LinkedIn must approve "
                "your app for company posting, and the person who connected must be a Page admin.")
    if code == 426 or "version" in response.text.lower():
        return "LinkedIn no longer supports the API version in LINKEDIN_API_VERSION. Set a version from the last 12 months."
    if code == 429:
        return "LinkedIn's posting limit was reached. Try again later."
    return f"LinkedIn rejected the post ({code}): {response.text[:200]}"
