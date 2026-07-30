"""Optional AI layer (Tier 2 labeling + Tier 3 insights).

Talks to ANY OpenAI-compatible /chat/completions endpoint — OpenAI, a local Ollama,
vLLM, etc. — configured in the wizard (or via env). Disabled cleanly when unset, so
nothing here is required. Only non-sensitive service metadata is ever sent.
"""

import json
import os
import re
import urllib.request
from typing import Optional
from urllib.parse import urlparse

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# Hosts that must never be reachable via the (operator-configurable) AI endpoint —
# the classic SSRF pivot targets (cloud instance-metadata services). A local Ollama /
# vLLM on localhost or the LAN is the normal case, so we DON'T blanket-block private
# IPs; instead block metadata endpoints outright and allow an explicit host allow-list
# (INFRADOCS_AI_ALLOWED_HOSTS) to lock the endpoint down further when desired.
_AI_BLOCKED_HOSTS = frozenset({
    "169.254.169.254",          # AWS/GCP/Azure/OCI IMDS
    "metadata", "metadata.google.internal", "metadata.goog",
    "fd00:ec2::254",            # AWS IMDS over IPv6
})


def _endpoint_allowed(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    allow = os.environ.get("INFRADOCS_AI_ALLOWED_HOSTS", "").strip()
    if allow:
        allowed = {h.strip().lower() for h in allow.split(",") if h.strip()}
        return host in allowed
    return host not in _AI_BLOCKED_HOSTS


def config(db) -> dict:
    s = db.db.settings.find_one({"_id": "app"}) or {}
    return {
        "endpoint": s.get("ai_endpoint") or os.environ.get("INFRADOCS_AI_ENDPOINT"),
        "key": s.get("ai_key") or os.environ.get("INFRADOCS_AI_KEY", ""),
        "model": s.get("ai_model") or os.environ.get("INFRADOCS_AI_MODEL", "gpt-4o-mini"),
    }


def enabled(db) -> bool:
    return bool(config(db).get("endpoint"))


def chat(db, system: str, user: str, timeout: int = 45, json_mode: bool = True) -> Optional[str]:
    cfg = config(db)
    if not cfg["endpoint"]:
        return None
    url = cfg["endpoint"].rstrip("/") + "/chat/completions"
    if not _endpoint_allowed(url):
        # Refuse a metadata-service / disallowed target — never turn the server into
        # an SSRF proxy just because the AI endpoint was (mis)configured to point there.
        return None
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.1,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 # Some gateways 403 the default python-urllib UA — send a real one.
                 "User-Agent": "InfraDocs/1.0",
                 **({"Authorization": f"Bearer {cfg['key']}"} if cfg["key"] else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"]
    except Exception:
        # Some endpoints reject response_format — retry once without it.
        if json_mode:
            return chat(db, system, user, timeout=timeout, json_mode=False)
        return None


def _parse_json(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):                       # strip ```json … ``` fences
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    for candidate in (t, _JSON_RE.search(t) and _JSON_RE.search(t).group(0)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


_LABEL_SYS = (
    "Identify the infrastructure service from the evidence. Output ONLY a single-line "
    "minified JSON object, no markdown, no code fences, no prose: "
    "{\"label\":\"short product/service name\",\"kind\":\"one of "
    "web|database|cache|monitoring|queue|proxy|app|infra|other\",\"purpose\":\"one "
    "concise sentence\"}. Best guess if unsure."
)


def label_service(db, evidence: dict) -> Optional[dict]:
    out = _parse_json(chat(db, _LABEL_SYS, json.dumps(evidence)))
    if not out or not out.get("label"):
        return None
    return {"label": str(out.get("label"))[:60],
            "kind": str(out.get("kind", "other"))[:20],
            "purpose": str(out.get("purpose", ""))[:200]}


_INSIGHTS_SYS = (
    "You are an infrastructure analyst. Given a JSON inventory of servers, services, "
    "exposures and ports, output ONLY minified JSON, no markdown/fences/prose: "
    "{\"summary\":\"2-3 sentence overview\",\"observations\":[\"up to 6 short strings: "
    "notable facts, exposure/security concerns, unusual services\"],"
    "\"recommendations\":[\"up to 4 short strings\"]}. Keep each string under 25 words. "
    "Be specific and reference real names from the data."
)


def fleet_insights(db, inventory: dict) -> Optional[dict]:
    return _parse_json(chat(db, _INSIGHTS_SYS, json.dumps(inventory), timeout=90))
