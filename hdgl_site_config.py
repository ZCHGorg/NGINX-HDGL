#!/usr/bin/env python3
"""
hdgl_site_config.py
-------------------
Load site- and deploy-specific configuration that should not be hard-coded
into the repository.

The deploy script writes a JSON file consumed by runtime modules so the repo can
ship generic defaults while each installation provides its own domains, peers,
and service definitions.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("hdgl.site_config")

SITE_CONFIG_PATH = Path(os.getenv("LN_SITE_CONFIG", "/opt/hdgl/site_config.json"))

DEFAULT_SITE_CONFIG: Dict[str, Any] = {
    "seed_nodes": [],
    "primary_site": {
        "enabled": False,
        "canonical_domain": "",
        "aliases": [],
        "redirect_domains": [],
        "client_max_body_size": "2048M",
        "storage_paths": ["/storage/"],
        "discourse_socket": "/var/discourse/shared/standalone/nginx.http.sock",
    },
    "services": {},
}


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_primary_site(raw: Dict[str, Any]) -> Dict[str, Any]:
    primary = dict(DEFAULT_SITE_CONFIG["primary_site"])
    primary.update(raw or {})
    primary["enabled"] = bool(primary.get("enabled"))
    primary["canonical_domain"] = str(primary.get("canonical_domain", "")).strip()
    primary["aliases"] = _as_list(primary.get("aliases"))
    primary["redirect_domains"] = _as_list(primary.get("redirect_domains"))
    primary["storage_paths"] = [
        path if path.startswith("/") else f"/{path}"
        for path in _as_list(primary.get("storage_paths"))
    ] or ["/storage/"]
    primary["client_max_body_size"] = str(primary.get("client_max_body_size", "2048M"))
    primary["discourse_socket"] = str(
        primary.get("discourse_socket", DEFAULT_SITE_CONFIG["primary_site"]["discourse_socket"])
    ).strip()
    return primary


def _normalize_service(name: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    service = dict(raw or {})
    service["name"] = name
    service["mode"] = str(service.get("mode", "proxy")).strip().lower() or "proxy"
    service["domain"] = str(service.get("domain", "")).strip()
    service["aliases"] = _as_list(service.get("aliases"))
    service["client_max_body_size"] = str(service.get("client_max_body_size", "512M"))
    if "port" in service and service["port"] not in (None, ""):
        service["port"] = int(service["port"])
    else:
        service["port"] = None
    if service["mode"] == "php_static":
        service["root"] = str(service.get("root", "/var/www/html")).strip()
        service["php_fpm_socket"] = str(
            service.get("php_fpm_socket", "/run/php/php8.3-fpm.sock")
        ).strip()
        demo = service.get("demo_location", "/demo")
        service["demo_location"] = str(demo).strip() or "/demo"
        alias = service.get("demo_alias_path", service["root"])
        service["demo_alias_path"] = str(alias).strip() or service["root"]
    return service


def load_site_config(path: Path | None = None) -> Dict[str, Any]:
    config_path = path or SITE_CONFIG_PATH
    config = {
        "seed_nodes": [],
        "primary_site": dict(DEFAULT_SITE_CONFIG["primary_site"]),
        "services": {},
    }

    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config["seed_nodes"] = _as_list(loaded.get("seed_nodes"))
                config["primary_site"] = _normalize_primary_site(loaded.get("primary_site", {}))
                services = loaded.get("services", {})
                if isinstance(services, dict):
                    config["services"] = {
                        name: _normalize_service(name, service)
                        for name, service in services.items()
                    }
        except Exception as exc:
            log.warning(f"[site-config] could not load {config_path}: {exc}")

    if not config["primary_site"].get("canonical_domain"):
        config["primary_site"]["enabled"] = False

    return config


def get_seed_nodes(config: Dict[str, Any]) -> List[str]:
    return _as_list(config.get("seed_nodes"))


def get_service_registry(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    services = config.get("services", {})
    return services if isinstance(services, dict) else {}


def get_primary_site(config: Dict[str, Any]) -> Dict[str, Any]:
    return _normalize_primary_site(config.get("primary_site", {}))


def get_dns_domain_map(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    domain_map: Dict[str, Dict[str, Any]] = {}
    primary = get_primary_site(config)
    if primary.get("enabled") and primary.get("canonical_domain"):
        domain_map[primary["canonical_domain"]] = {"port": 443}
        for alias in primary.get("aliases", []):
            domain_map[alias] = {"port": 443}

    for service in get_service_registry(config).values():
        domain = service.get("domain", "")
        if domain:
            domain_map[domain] = {"port": service.get("port") or 443}
        for alias in service.get("aliases", []):
            domain_map[alias] = {"port": service.get("port") or 443}

    return domain_map