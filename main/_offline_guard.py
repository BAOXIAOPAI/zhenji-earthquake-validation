"""验收的网络拦截守卫：证明严格离线下零外联。

装在子进程最前面（先于业务代码 import）：
  - socket 层拦截非回环 connect（回环放行——界面/本地服务不是外联）；
  - urllib 层拦截非回环 urlopen（双保险）；
  - atexit 把拦截记录写到 INTERCEPT_LOG 指向的文件。

被拦截不是失败掩盖：任何被记录的外联尝试都会让验收场景判失败。
"""
from __future__ import annotations

import atexit
import json
import os
import socket
import sys
import urllib.parse
import urllib.request

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
_ATTEMPTS: list[dict] = []


def _is_loopback(host) -> bool:
    try:
        return str(host) in _LOOPBACK
    except Exception:  # noqa: BLE001
        return False


def _record(layer: str, target) -> None:
    _ATTEMPTS.append({"layer": layer, "target": str(target),
                      "argv": sys.argv[:3]})


def _blocked_socket_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if not _is_loopback(host):
        _record("socket.connect", address)
        raise RuntimeError(
            f"NETWORK INTERCEPTED (socket): {address} —— 严格离线验收"
            "不允许非回环外联")
    return _orig_socket_connect(self, address)


_orig_socket_connect = socket.socket.connect
socket.socket.connect = _blocked_socket_connect


def _blocked_urlopen(url, *args, **kwargs):
    try:
        host = urllib.parse.urlsplit(str(url)).hostname
    except Exception:  # noqa: BLE001
        host = None
    if not _is_loopback(host):
        _record("urllib.urlopen", url)
        raise RuntimeError(
            f"NETWORK INTERCEPTED (urllib): {url} —— 严格离线验收"
            "不允许非回环外联")
    return _orig_urlopen(url, *args, **kwargs)


_orig_urlopen = urllib.request.urlopen
urllib.request.urlopen = _blocked_urlopen


def _dump() -> None:
    path = os.environ.get("INTERCEPT_LOG")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"attempts": _ATTEMPTS,
                       "count": len(_ATTEMPTS)}, fh,
                      ensure_ascii=False, indent=1)


atexit.register(_dump)
