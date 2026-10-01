"""Tests for the background HTTP server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from agentboard.server import BackgroundServer


def test_server_refuses_any_non_loopback_interface():
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        with pytest.raises(ValueError, match="loopback"):
            BackgroundServer(host=host)


def test_server_starts_on_a_random_port_and_serves_requests():
    server = BackgroundServer()
    try:
        url = server.start()
        assert url.startswith("http://127.0.0.1:")
        assert server.port > 0
        with urllib.request.urlopen(url + "/api/health", timeout=10) as response:
            assert json.loads(response.read())["ok"] is True
    finally:
        server.stop()


def test_stopping_releases_the_port():
    server = BackgroundServer()
    server.start()
    port = server.port
    server.stop()
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)


def test_shutdown_endpoint_sets_the_servers_event():
    server = BackgroundServer()
    try:
        url = server.start()
        assert not server.shutdown_event.is_set()
        request = urllib.request.Request(url + "/api/shutdown", method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.loads(response.read())["stopping"] is True
        assert server.shutdown_event.wait(timeout=5)
    finally:
        server.stop()
