"""Pydantic AI chat model for workers on an OpenAI-compatible endpoint.

Workers use native OpenAI tool calling. The principal, manager and curator
stay on :mod:`agent_team.models.http_model` and its text protocol.
"""

from __future__ import annotations

import openai
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

REQUEST_TIMEOUT_SECONDS = 120.0

# Fixed header values for every worker request. The OpenAI SDK merges
# organization, project and ``OPENAI_CUSTOM_HEADERS`` values from the
# environment into its headers, and the custom ones may even replace Host,
# Content-Type or Content-Length. The request hook therefore discards every
# header and rebuilds this exact set; nothing from the environment survives.
# Accept-Encoding is pinned to the codings the HTTP client always decodes.
_FIXED_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Content-Type": "application/json",
    "User-Agent": "agent-team-worker",
    # One request per connection, like SimpleChatModel's per-request client:
    # no pooled socket outlives the request or its event loop.
    "Connection": "close",
}

# The SDK refuses to build a client without some credential. This placeholder
# never reaches the wire: the request hook replaces or removes Authorization.
_NO_KEY_PLACEHOLDER = "agent-team-no-key"


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url) -> tuple:
    """Scheme, host and port of a URL from the HTTP client's own URL type.

    That type lowercases the scheme and host, and usually reports a default
    port as ``None``, but it keeps an explicit ``:80`` when the configured
    scheme was written in upper case. Filling in the default port makes
    ``http://h/v1``, ``http://h:80/v1`` and ``HTTP://H:80/v1`` one origin.
    """
    return url.scheme, url.host, url.port if url.port is not None else _DEFAULT_PORTS.get(url.scheme)


def create_worker_model(base_url: str, model_name: str, api_key: str = "") -> OpenAIChatModel:
    """Create the worker model with the same endpoint, auth and timeout as ``SimpleChatModel``.

    ``Authorization: Bearer`` is sent only when ``api_key`` is non-empty, and
    only to the scheme, host and port of ``base_url``. Redirects are refused. No
    ``OPENAI_*`` environment variable changes the base URL, key or headers, and
    SDK retries are disabled so every request is one counted worker turn.
    """

    base_url = base_url.rstrip("/")

    async def restrict_headers(request) -> None:
        for name in list(request.headers):
            del request.headers[name]
        request.headers["Host"] = request.url.netloc.decode("ascii")
        request.headers.update(_FIXED_HEADERS)
        request.headers["Content-Length"] = str(len(request.content))
        if api_key and _origin(request.url) == _origin(type(request.url)(base_url)):
            request.headers["Authorization"] = f"Bearer {api_key}"

    http_client = openai.DefaultAsyncHttpxClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        event_hooks={"request": [restrict_headers]},
    )
    client = openai.AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or _NO_KEY_PLACEHOLDER,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
        http_client=http_client,
    )
    return OpenAIChatModel(model_name, provider=OpenAIProvider(openai_client=client))
