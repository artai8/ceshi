"""Validate running Compose services and public HTTP routes."""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--skip-compose", action="store_true")
    return parser.parse_args()


def get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.read()


def compose_services() -> list[dict]:
    result = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    text = result.stdout.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def main() -> None:
    args = parse_args()
    base = args.base_url.rstrip("/")
    health_status, health_body = get(f"{base}/health")
    miniapp_status, _ = get(f"{base}/miniapp")
    dashboard_status, _ = get(f"{base}/dashboard")
    if health_status != 200 or json.loads(health_body) != {"status": "ok"}:
        raise SystemExit("API health check failed")
    if miniapp_status != 200:
        raise SystemExit("Mini App route failed")
    if dashboard_status != 200:
        raise SystemExit("Dashboard route failed")

    services = [] if args.skip_compose else compose_services()
    bad = [
        item
        for item in services
        if item.get("State") != "running"
        or item.get("Health") not in {"", None, "healthy"}
    ]
    if bad:
        raise SystemExit(f"Unhealthy Compose services: {bad}")
    print(
        json.dumps(
            {
                "base_url": base,
                "health": "ok",
                "miniapp": "ok",
                "dashboard": "ok",
                "compose_services": len(services),
            }
        )
    )


if __name__ == "__main__":
    main()
