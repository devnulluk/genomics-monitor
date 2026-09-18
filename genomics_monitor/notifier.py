from __future__ import annotations

import os

import apprise


def send(title: str, body: str, kind: str = "info") -> bool:
    urls = os.getenv("APPRISE_URLS", "").split()
    if not urls:
        return False
    service = apprise.Apprise()
    for url in urls:
        service.add(url)
    notify_type = getattr(apprise.NotifyType, kind.upper(), apprise.NotifyType.INFO)
    return bool(service.notify(title=title, body=body, notify_type=notify_type))
