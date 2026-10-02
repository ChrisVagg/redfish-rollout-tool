import logging
import os
import sys
import time
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from constants import ROOT, SILENT
from helpers import obj, reason

log = logging.getLogger("redfish")
REFUSED = "401 Unauthorized: wrong credentials, or the session ended"  # why a crawl stopped on a 401


# ---- Connection: one persistent HTTPS connection per BMC; every call goes over it ----

# A keep-alive HTTPS connection to one BMC with Basic auth, and the limits of its crawl
def tunnel(server) -> tuple[requests.Session, str, dict]:
    session = requests.Session()
    try:
        session.auth = (os.environ[server["username_env"]], os.environ[server["password_env"]])
    except KeyError as e:
        sys.exit(f"{server['host']}: env var {e} not set: run it through make (make help), or export it first")
    session.verify = server["verify"]
    session.headers["Accept"] = "application/json"
    retry = Retry(total=server.get("retries", 2), backoff_factor=1, status_forcelist=[429, 502, 503, 504])
    session.mount(server["scheme"] + "://", HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=retry))
    limits = {"timeout": tuple(server.get("timeout", (5, 30))), "max_failures": server.get("max_failures", 3),
              "deadline": server.get("deadline", 600), "until": float("inf"), "failures": 0}
    return session, f"{server['scheme']}://{server['host']}", limits


# GET one resource over the BMC's connection; a failed GET comes back as {"error": reason} for the report
def call(tunnel, uri) -> dict:
    """Raises ConnectionAbortedError, which stops the crawl of this host, past the host's deadline, when this is the
    max_failures-th request in a row without an answer, or on a 401: every further request would be another failed
    login, and BMCs lock the account after a few."""
    session, base, limits = tunnel
    if time.monotonic() > limits["until"]:
        raise ConnectionAbortedError(f"stopped at the {limits['deadline']}s deadline")
    try:
        r = session.get(base + uri, timeout=limits["timeout"])
        limits["failures"] = 0  # answer with error status
        if r.status_code == 401:
            raise ConnectionAbortedError(REFUSED)
        r.raise_for_status()
        return r.json()
    except SILENT as e:
        limits["failures"] += 1
        error = f"{type(e).__name__}: {reason(str(e))}"
        if limits["failures"] >= limits["max_failures"]:
            raise ConnectionAbortedError(f"not responding, {limits['failures']} requests in a row: {error}")
        return {"error": error}
    except ValueError:
        content = r.headers.get("Content-Type", "no content type")
        return {"error": f"Answer not from redfish service: {content} from {r.url}"}
    except requests.RequestException as e:
        return {"error": f"{type(e).__name__}: {reason(str(e))}"}


# Swap Basic auth for a Redfish session token; returns the session URI to log out, or None
def login(tunnel) -> str | None:
    """iDRAC re-checks the password on every Basic-auth request (~4s each), a token costs one login. On None,
    Basic auth stays."""
    session, base, limits = tunnel
    uri = obj(obj(call(tunnel, ROOT).get("Links")).get("Sessions")).get("@odata.id")  # None: unreachable, discover says
    if not uri:
        return None
    user, password = session.auth
    try:
        r = session.post(base + uri, json={"UserName": user, "Password": password}, timeout=limits["timeout"])
    except requests.RequestException as e:
        log.warning("%s: login: %s, staying on Basic auth", base, reason(str(e)))
        return None
    token = r.headers.get("X-Auth-Token")  # iDRAC sends one even with an error status
    if not token:
        return None
    session.auth, session.headers["X-Auth-Token"] = None, token
    return r.headers.get("Location")


# Close the session: BMCs allow only a handful, a leaked one holds a slot until it times out
def logout(tunnel, location) -> None:
    session, base, limits = tunnel
    try:
        session.delete(urljoin(base, location), timeout=limits["timeout"])
    except requests.RequestException as e:
        log.warning("%s: logout: %s", base, reason(str(e)))


# One HTTP request with its status: (status, headers, JSON body); status 0 when the BMC doesn't answer
def send(tunnel, method, uri, timeout=None, **kwargs) -> tuple[int, dict, dict]:
    session, base, limits = tunnel
    try:
        r = session.request(method, urljoin(base, uri), timeout=timeout or limits["timeout"], **kwargs)
    except requests.RequestException as e:
        return 0, {}, {"error": {"message": f"{type(e).__name__}: {reason(str(e))}"}}
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    return r.status_code, r.headers, obj(body)
