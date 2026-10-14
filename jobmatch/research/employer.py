"""Who is behind a posting: the question, and the check that a homepage it names answers."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..llm import openrouter

REACH_DEADLINE = 10     # one reachability check, redirects included
MAX_REDIRECTS = 3       # followed by hand, so each hop's address is checked
# A browser's: some homepages answer a library's User-Agent with 403.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
}


@dataclass(frozen=True, slots=True)
class Poster:
    """What is known about whoever posted the vacancy, before asking."""

    name: str | None        # as the hh employer page gives it
    site_url: str | None    # the link that page publishes; often a careers site
    about: str | None       # hh's city and industries: tells namesakes apart
    posting: str            # the vacancy as Jev reads it (`matching.job_text`)


# Prompt text: the docstring and Field descriptions are sent, so editing one re-pays every identity.
class Identity(BaseModel):
    """Who is behind the posting. Null means unknown, never "there is none"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    employer: str | None = Field(
        description="The organisation that posted the vacancy, by its common name; "
        "for a division, the parent company"
    )
    homepage: str | None = Field(description="That organisation's main website, a full URL")
    careers_page: str | None = Field(description="Its careers or jobs site, a full URL")
    is_agency: bool | None = Field(
        description="True if the poster is a recruiting agency hiring for a client"
    )
    client: str | None = Field(
        description="An agency's client, only if the posting names it"
    )
    notes: str = Field(description="One or two sentences: how you decided, what is unsure")

    @field_validator("homepage", "careers_page")
    @classmethod
    def _with_scheme(cls, url: str | None) -> str | None:
        """A bare domain gains https://; anything not then http(s) (`javascript:`) becomes unknown."""
        if url and "://" not in url:
            url = f"https://{url}"
        if url and urlsplit(url).scheme in ("http", "https"):
            return url
        return None


EMPLOYER_SYSTEM = """\
You identify the organisation behind a job posting from hh.ru. Use only the
facts given and what you already know about real companies. For anything you
do not know, answer null: a null is useful, a guess is not.

- The link from the hh.ru page is whatever the poster chose to publish. It is
  often a careers or HR site (https://yandex.ru/jobs, https://rabota.vtb.ru)
  rather than the homepage; put each where it belongs, as a full URL.
- A posting from a division ("Банк ВТБ (ПАО), Технологический блок") is the
  parent company's ("ВТБ").
- A recruiting agency says so: "наш клиент", "для нашего клиента", "кадровое
  агентство". Then the employer is the agency, and the client is named only if
  the posting names it. Never infer a client from a description such as
  "крупный банк" or "международная IT-компания"."""


def employer_prompt(poster: Poster) -> str:
    return "\n".join([
        f"Name on hh.ru: {poster.name or 'unknown'}",
        f"Link on its hh.ru page: {poster.site_url or 'none'}",
        f"About, from hh.ru: {poster.about or 'unknown'}",
        "",
        "The posting:",
        "",
        poster.posting,
    ])


EMPLOYER = openrouter.Question(
    kind="employer", schema_name="employer_identity",
    system=EMPLOYER_SYSTEM, prompt=employer_prompt, answer=Identity,
)


def employer_request(poster: Poster, model: str) -> dict[str, Any]:
    """Exactly as sent: stored as `llm_calls.request`, and what the fingerprint hashes."""
    return openrouter.request(EMPLOYER, poster, model)


async def ask_employer(
    sent: dict[str, Any],           # `employer_request(poster, model)`
    *,
    vacancy_id: int,
    http_client: httpx2.AsyncClient | None = None,   # tests pass a mocked one
) -> tuple[Identity, int]:
    """The answer and its `llm_calls` id; a failure is recorded, then raised."""
    identity, _, call_id = await openrouter.ask(
        EMPLOYER, sent, vacancy_id=vacancy_id, http_client=http_client
    )
    return identity, call_id


# --- checks on an answer ---

async def reachable(url: str, *, http_client: httpx2.AsyncClient | None = None) -> bool:
    """Keeps a hallucinated homepage from being linked; never raises, and never checks the content."""
    try:
        async with asyncio.timeout(REACH_DEADLINE):
            async with http_client or httpx2.AsyncClient(
                headers=HEADERS, follow_redirects=False
            ) as client:
                return await _answers(client, url)
    except (TimeoutError, httpx2.HTTPError, ValueError):   # ValueError: a malformed URL
        return False


async def _answers(client: httpx2.AsyncClient, url: str) -> bool:
    for _ in range(MAX_REDIRECTS + 1):
        if not await _public(url):
            return False
        async with client.stream("GET", url) as resp:   # the body is never read
            if resp.is_redirect and (location := resp.headers.get("location")):
                url = urljoin(url, location)
                continue
            return resp.status_code < 400
    return False                                        # too many redirects


async def _public(url: str) -> bool:
    """The model's URL must not reach localhost, which serves CV data; resolved here, as names can lie."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)
