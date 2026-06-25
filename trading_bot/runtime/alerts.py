from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib import request


def send_discord_message(*, webhook_url: str, content: str, username: str) -> bool:
    if not webhook_url.strip():
        return False

    payload = json.dumps(
        {
            "content": content,
            "username": username,
        }
    ).encode("utf-8")
    try:
        response = request.urlopen(
            request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=10,
        )
    except (HTTPError, URLError, TimeoutError):
        return False
    response.read()
    return True
