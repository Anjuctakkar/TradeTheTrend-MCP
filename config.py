"""Where the app keeps files it writes (.env edits, funding rules, evaluation list).

By default this is the project folder. When hosting (for example in Docker) set the real
environment variable DASHBOARD_DATA_DIR to a persistent folder such as /data."""
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DASHBOARD_DATA_DIR") or CODE_DIR)
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:   # e.g. the persistent disk isn't attached: keep working, but say so
    print(f"[config] Can't use DASHBOARD_DATA_DIR={DATA_DIR} ({exc}). Saving next to the code instead; changes may not survive a redeploy.")
    DATA_DIR = CODE_DIR


def client_ip(request) -> str:
    """The visitor's address, as far as it can be trusted.

    * DASHBOARD_CLIENT_IP_HEADER names a header that your host's proxy sets to the real visitor
      address and that visitors can't forge (Render sits behind Cloudflare: use cf-connecting-ip).
    * Otherwise the RIGHT-most X-Forwarded-For entry is used, the one the nearest proxy added
      (with Caddy that is the visitor). The left-most entry is written by the visitor, so it isn't used.
    * With no proxy, the connecting address."""
    name = os.getenv("DASHBOARD_CLIENT_IP_HEADER", "").strip().lower()
    if name and request.headers.get(name):
        return request.headers[name].split(",")[0].strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"
