#!/usr/bin/env python3
"""
EveWatch — a lightweight, personal EVEMon-style tool for EVE Online.

Features: multi-character skill queue monitoring, skill planner with
training-time math and remap suggestions, wallet & assets, market tools.

Uses only the Python standard library. All data stays on your computer.
Run it, then open http://localhost:8635 in your browser.
"""

import base64
import calendar
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# Constants & paths
# ----------------------------------------------------------------------------

PORT = 8635
APP_NAME = "EveWatch"
APP_VERSION = "2.0.0"
# Filled in automatically when the installers are built on GitHub; when running
# from source you can set "update_repo": "user/repo" in config.json instead.
GITHUB_REPO = "@@GITHUB_REPO@@"
FROZEN = bool(getattr(sys, "frozen", False))      # True inside the installed app

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, rel)


def _default_data_dir():
    """Installed app: a per-user folder. Running from source: ./evewatch_data
    (portable). EVEWATCH_DATA overrides both."""
    env = os.environ.get("EVEWATCH_DATA")
    if env:
        return env
    if FROZEN:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support")
        else:
            base = os.environ.get("XDG_DATA_HOME") or \
                os.path.expanduser("~/.local/share")
        return os.path.join(base, "EveWatch")
    return os.path.join(APP_DIR, "evewatch_data")


DATA_DIR = _default_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
TOKENS_PATH = os.path.join(DATA_DIR, "tokens.json")
CATALOG_PATH = os.path.join(DATA_DIR, "skill_catalog.json")
PLANS_PATH = os.path.join(DATA_DIR, "skill_plans.json")
LOCATIONS_PATH = os.path.join(DATA_DIR, "market_locations.json")
TYPES_PATH = os.path.join(DATA_DIR, "type_info.json")
HISTORY_PATH = os.path.join(DATA_DIR, "price_history.json")
TYPENAMES_PATH = os.path.join(DATA_DIR, "type_names.json")

# bump this to force a one-time catalog rebuild when its format changes
CATALOG_VERSION = 2

ESI = "https://esi.evetech.net/latest"
SSO_AUTHORIZE = "https://login.eveonline.com/v2/oauth/authorize/"
SSO_TOKEN = "https://login.eveonline.com/v2/oauth/token"

CALLBACK_URL = f"http://localhost:{PORT}/callback"

SCOPES = [
    "publicData",
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-clones.read_implants.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-assets.read_assets.v1",
    "esi-universe.read_structures.v1",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-characters.read_blueprints.v1",
    "esi-industry.read_character_jobs.v1",
]

# Optional scopes, only requested when switched on in Setup (they must first
# be enabled on your developer application, or EVE's login would reject them)
EXTRA_SCOPES = [
    "esi-characters.read_loyalty.v1",
]


RESEARCH_SCOPE = "esi-characters.read_agents_research.v1"
CONTRACTS_SCOPE = "esi-contracts.read_character_contracts.v1"
# game rules for converting research points into datacores
RP_PER_DATACORE = 100
ISK_PER_DATACORE = 10000


def active_scopes():
    sc = list(SCOPES)
    if config.get("scope_extras"):
        sc += EXTRA_SCOPES
    if config.get("scope_research"):
        sc.append(RESEARCH_SCOPE)
    if config.get("scope_contracts"):
        sc.append(CONTRACTS_SCOPE)
    return sc

USER_AGENT = f"{APP_NAME}/{APP_VERSION} (personal local tool)"

THE_FORGE = 10000002       # region containing Jita
JITA_44 = 60003760         # Jita IV - Moon 4 - Caldari Navy Assembly Plant

# Dogma attribute ids used by skills
ATTR_RANK = 275
ATTR_PRIMARY = 180
ATTR_SECONDARY = 181
# prerequisite skill / level dogma attribute pairs
PREREQ_PAIRS = [(182, 277), (183, 278), (184, 279),
                (1285, 1286), (1289, 1287), (1290, 1288)]
# dogma attribute id -> character attribute name
CHAR_ATTR = {164: "charisma", 165: "intelligence", 166: "memory",
             167: "perception", 168: "willpower"}

# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

_io_lock = threading.Lock()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with _io_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# ----------------------------------------------------------------------------
# Global state
# ----------------------------------------------------------------------------

config = load_json(CONFIG_PATH, {"client_id": ""})
tokens = load_json(TOKENS_PATH, {})          # char_id(str) -> token record
plans = load_json(PLANS_PATH, {})            # plan name -> [[skill_id, level]]
catalog = load_json(CATALOG_PATH, None)      # skills + groups, built once
if catalog and catalog.get("v") != CATALOG_VERSION:
    catalog = None                           # format changed - rebuild
locations = load_json(LOCATIONS_PATH, [{     # saved market/bookmark stations
    "station_id": JITA_44,
    "name": "Jita IV - Moon 4 - Caldari Navy Assembly Plant",
    "system": "Jita", "region": "The Forge", "region_id": THE_FORGE,
    "category": "Sales Hub"}])
for _l in locations:                         # migrate pre-category saves
    _l.setdefault("category", "Sales Hub")
station_cache = {}                           # station_id -> system/region info
pkce_states = {}                             # state -> code_verifier
access_cache = {}                            # char_id -> (access_token, expiry)
esi_cache = {}                               # url -> (expires_ts, etag, body)
catalog_status = {"state": "idle", "done": 0, "total": 0, "error": ""}
_cache_lock = threading.Lock()
_token_lock = threading.Lock()


# ----------------------------------------------------------------------------
# ESI HTTP client (with ETag/expires caching and error-limit respect)
# ----------------------------------------------------------------------------

def http_request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=30)


def esi_fetch(path, token=None, params=None, use_cache=True):
    """GET an ESI endpoint. Returns (json_data, response_headers)."""
    url = ESI + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    with _cache_lock:
        cached = esi_cache.get(url)
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if cached and use_cache:
        expires_ts, etag, body, pages = cached
        if time.time() < expires_ts:
            # keep X-Pages so paginated fetches don't truncate on cache hits
            return body, ({"X-Pages": pages} if pages else {})
        if etag:
            headers["If-None-Match"] = etag

    for attempt in range(3):
        try:
            resp = http_request(url, headers=headers)
            remain = resp.headers.get("X-ESI-Error-Limit-Remain")
            if remain is not None and int(remain) < 10:
                time.sleep(2)
            body = json.loads(resp.read().decode() or "null")
            if use_cache:
                _store_cache(url, resp.headers, body)
            return body, dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                _bump_cache(url, e.headers)
                return cached[2], ({"X-Pages": cached[3]} if cached[3] else {})
            if e.code in (502, 503, 504) and attempt < 2:
                time.sleep(1 + attempt)
                continue
            if e.code in (420, 429):  # error/rate limited - back off
                time.sleep(int(e.headers.get("Retry-After") or
                               e.headers.get("X-ESI-Error-Limit-Reset")
                               or 10) + 1)
                continue
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except OSError:
                pass
            raise RuntimeError(f"ESI {e.code} on {path}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise RuntimeError(f"Network error on {path}: {e}")
    raise RuntimeError(f"ESI request failed after retries: {path}")


def _store_cache(url, resp_headers, body):
    etag = resp_headers.get("ETag", "")
    expires_ts = time.time() + 60
    exp = resp_headers.get("Expires")
    if exp:
        try:
            expires_ts = time.mktime(time.strptime(exp, "%a, %d %b %Y %H:%M:%S %Z"))
            expires_ts -= time.timezone  # header is GMT
        except ValueError:
            pass
    with _cache_lock:
        esi_cache[url] = (expires_ts, etag, body,
                          resp_headers.get("X-Pages"))
        if len(esi_cache) > 2500:          # keep memory bounded
            now = time.time()
            for k in [k for k, v in esi_cache.items() if v[0] < now]:
                del esi_cache[k]
            while len(esi_cache) > 2000:    # still big: drop oldest
                esi_cache.pop(next(iter(esi_cache)))


def _bump_cache(url, resp_headers):
    with _cache_lock:
        if url in esi_cache:
            _, etag, body, pages = esi_cache[url]
            esi_cache[url] = (time.time() + 300, etag, body, pages)


def esi_fetch_pages(path, token=None, params=None, max_pages=20):
    """Fetch all pages of a paginated endpoint, concatenated."""
    params = dict(params or {})
    params["page"] = 1
    first, headers = esi_fetch(path, token, params)
    result = list(first)
    total = int(next((v for k, v in headers.items()
                      if k.lower() == "x-pages"), 1) or 1)
    for page in range(2, min(total, max_pages) + 1):
        params["page"] = page
        data, _ = esi_fetch(path, token, params)
        result.extend(data)
    return result


last_interactive = [0.0]   # when the user last made a live request


def crawl_pause(base):
    """Background crawls slow down while you're actively using the app, so
    appraisals and searches don't fight them for ESI's rate limit."""
    busy = time.time() - last_interactive[0] < 20
    time.sleep(base * (5 if busy else 1))


def esi_names(ids):
    """Resolve ids -> names via POST /universe/names/. Best-effort."""
    out = {}
    # skip player structures (ids >= 10^12): /universe/names/ rejects them
    ids = [i for i in set(ids) if 0 < i < 10 ** 12]
    for chunk_start in range(0, len(ids), 1000):
        chunk = ids[chunk_start:chunk_start + 1000]
        try:
            resp = http_request(ESI + "/universe/names/",
                                data=json.dumps(chunk).encode(),
                                headers={"Content-Type": "application/json"})
            for row in json.loads(resp.read().decode()):
                out[row["id"]] = row
        except (urllib.error.URLError, ValueError, OSError):
            # one bad id fails the whole chunk; retry one by one
            for i in chunk:
                try:
                    resp = http_request(ESI + "/universe/names/",
                                        data=json.dumps([i]).encode(),
                                        headers={"Content-Type": "application/json"})
                    for row in json.loads(resp.read().decode()):
                        out[row["id"]] = row
                except (urllib.error.URLError, ValueError, OSError):
                    pass
    return out


# ----------------------------------------------------------------------------
# SSO / OAuth2 with PKCE
# ----------------------------------------------------------------------------

SCOPE_LABELS = {EXTRA_SCOPES[0]: "Loyalty points", RESEARCH_SCOPE: "Research points",
                CONTRACTS_SCOPE: "Contracts"}
login_dropped = {}                 # login state -> optional scopes left out of that login




def sso_login_url(scopes=None, dropped=None):
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))
    pkce_states[state] = verifier
    login_dropped[state] = list(dropped or [])
    q = urllib.parse.urlencode({
        "response_type": "code",
        "redirect_uri": CALLBACK_URL,
        "client_id": config["client_id"],
        "scope": " ".join(scopes or active_scopes()),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return SSO_AUTHORIZE + "?" + q


def sso_token_request(form):
    data = urllib.parse.urlencode(form).encode()
    resp = http_request(SSO_TOKEN, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "login.eveonline.com",
    })
    return json.loads(resp.read().decode())


def jwt_payload(token):
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def handle_callback(code, state):
    verifier = pkce_states.pop(state, None)
    dropped = login_dropped.pop(state, [])
    if not verifier:
        raise RuntimeError("Unknown login state - please try again.")
    tok = sso_token_request({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config["client_id"],
        "code_verifier": verifier,
    })
    payload = jwt_payload(tok["access_token"])
    char_id = payload["sub"].split(":")[-1]
    tokens[char_id] = {
        "character_id": int(char_id),
        "name": payload.get("name", "Unknown"),
        "refresh_token": tok["refresh_token"],
        "added": int(time.time()),
        "scopes": ([payload["scp"]] if isinstance(payload.get("scp"), str)
                   else list(payload.get("scp") or [])),
        "pending_scopes": dropped,       # optional scopes left out of this login
    }
    access_cache[char_id] = (tok["access_token"],
                             time.time() + tok.get("expires_in", 1000) - 60)
    save_json(TOKENS_PATH, tokens)
    return tokens[char_id]["name"], dropped


def get_access_token(char_id):
    char_id = str(char_id)
    with _token_lock:
        cached = access_cache.get(char_id)
        if cached and time.time() < cached[1]:
            return cached[0]
        rec = tokens.get(char_id)
        if not rec:
            raise RuntimeError("Character not logged in.")
        tok = sso_token_request({
            "grant_type": "refresh_token",
            "refresh_token": rec["refresh_token"],
            "client_id": config["client_id"],
        })
        # SSO may rotate the refresh token - always store the newest one
        rec["refresh_token"] = tok["refresh_token"]
        save_json(TOKENS_PATH, tokens)
        access_cache[char_id] = (tok["access_token"],
                                 time.time() + tok.get("expires_in", 1000) - 60)
        return tok["access_token"]


# ----------------------------------------------------------------------------
# Skill catalog (built once from public ESI, cached on disk)
# ----------------------------------------------------------------------------

def build_catalog():
    global catalog
    try:
        catalog_status.update(state="running", done=0, error="")
        cat, _ = esi_fetch("/universe/categories/16/")
        group_ids = cat["groups"]
        groups, type_ids = {}, []
        for gid in group_ids:
            g, _ = esi_fetch(f"/universe/groups/{gid}/")
            if g.get("published"):
                groups[str(gid)] = g["name"]
                type_ids.extend([(t, gid) for t in g["types"]])
        catalog_status["total"] = len(type_ids)

        skills = {}

        def fetch_type(tid, gid):
            t, _ = esi_fetch(f"/universe/types/{tid}/")
            if not t.get("published"):
                return None
            dogma = {a["attribute_id"]: a["value"]
                     for a in t.get("dogma_attributes", [])}
            prereqs = []
            for sid_attr, lvl_attr in PREREQ_PAIRS:
                if sid_attr in dogma and lvl_attr in dogma:
                    prereqs.append([int(dogma[sid_attr]), int(dogma[lvl_attr])])
            return {
                "id": tid,
                "name": t["name"],
                "group": gid,
                "rank": int(dogma.get(ATTR_RANK, 1) or 1),
                "primary": CHAR_ATTR.get(int(dogma.get(ATTR_PRIMARY, 0)), ""),
                "secondary": CHAR_ATTR.get(int(dogma.get(ATTR_SECONDARY, 0)), ""),
                "prereqs": prereqs,
                "desc": " ".join((t.get("description") or "").split())[:900],
            }

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fetch_type, tid, gid): tid
                       for tid, gid in type_ids}
            for fut in as_completed(futures):
                info = fut.result()
                catalog_status["done"] += 1
                if info:
                    skills[str(info["id"])] = info

        catalog = {"v": CATALOG_VERSION, "groups": groups, "skills": skills,
                   "built": int(time.time())}
        save_json(CATALOG_PATH, catalog)
        catalog_status["state"] = "done"
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        catalog_status.update(state="error", error=str(e))


def ensure_catalog():
    if catalog is None and catalog_status["state"] not in ("running",):
        threading.Thread(target=build_catalog, daemon=True).start()


# ----------------------------------------------------------------------------
# Skill math
# ----------------------------------------------------------------------------

SQRT32 = 32 ** 0.5


def sp_at_level(rank, level):
    if level <= 0:
        return 0
    return int(round(250 * rank * SQRT32 ** (level - 1)))


def sp_per_minute(attrs, primary, secondary):
    return attrs.get(primary, 20) + attrs.get(secondary, 20) / 2.0


def expand_plan(entries, trained):
    """entries: [[skill_id, target_level]]. Returns ordered list including
    prerequisites, deduplicated, respecting dependency order."""
    skills = catalog["skills"]
    ordered, want = [], {}

    def add(sid, level):
        sid = str(sid)
        if sid not in skills:
            return
        have = max(trained.get(sid, 0), want.get(sid, 0))
        if level <= have:
            return
        for pid, plvl in skills[sid]["prereqs"]:
            add(pid, plvl)
        for lvl in range(have + 1, level + 1):
            ordered.append((sid, lvl))
        want[sid] = level

    for sid, level in entries:
        add(sid, level)
    return ordered


def compute_plan(entries, trained, trained_sp, attrs, ordered=None):
    """Returns per-step and total SP/time for a plan at given attributes."""
    skills = catalog["skills"]
    steps, total_sp, total_min = [], 0, 0.0
    if ordered is None:
        ordered = expand_plan(entries, trained)
    for sid, lvl in ordered:
        s = skills[sid]
        start_sp = sp_at_level(s["rank"], lvl - 1)
        end_sp = sp_at_level(s["rank"], lvl)
        # partial training counts for the first missing level
        cur_sp = trained_sp.get(sid, 0) if lvl == trained.get(sid, 0) + 1 else 0
        need = max(0, end_sp - max(start_sp, cur_sp))
        rate = sp_per_minute(attrs, s["primary"], s["secondary"])
        minutes = need / rate if rate else 0
        total_sp += need
        total_min += minutes
        steps.append({"skill_id": int(sid), "name": s["name"], "level": lvl,
                      "sp": need, "minutes": round(minutes, 1),
                      "primary": s["primary"], "secondary": s["secondary"]})
    return steps, total_sp, total_min


def best_remap(entries, trained, trained_sp, implant_bonus=0):
    """Brute-force the optimal attribute remap for a plan.
    Base 17 per attribute + 14 assignable points, max +10 per attribute."""
    names = ["charisma", "intelligence", "memory", "perception", "willpower"]
    ordered = expand_plan(entries, trained)
    best = None
    for a in range(min(10, 14) + 1):
        for b in range(min(10, 14 - a) + 1):
            for c in range(min(10, 14 - a - b) + 1):
                for d in range(min(10, 14 - a - b - c) + 1):
                    e = 14 - a - b - c - d
                    if not 0 <= e <= 10:
                        continue
                    dist = [a, b, c, d, e]
                    attrs = {n: 17 + p + implant_bonus
                             for n, p in zip(names, dist)}
                    _, _, minutes = compute_plan(entries, trained,
                                                 trained_sp, attrs, ordered)
                    if best is None or minutes < best[0]:
                        best = (minutes, attrs)
    return best


# ----------------------------------------------------------------------------
# Character data assembly
# ----------------------------------------------------------------------------

IMPLANT_SLOT_ATTR = 331                      # implantness
IMPLANT_BONUS_ATTRS = {175: "charisma", 176: "intelligence", 177: "memory",
                       178: "perception", 179: "willpower"}


def implant_details(type_ids, names):
    """Slot (1-10) and attribute bonus of each plugged-in implant."""
    out = []

    def one(tid):
        try:
            t, _ = esi_fetch(f"/universe/types/{tid}/")
        except RuntimeError:
            return {"type_id": tid, "name": names.get(tid, {}).get("name", str(tid)),
                    "slot": None, "bonus": {}}
        dogma = {a["attribute_id"]: a["value"] for a in t.get("dogma_attributes", [])}
        return {"type_id": tid, "name": t.get("name", str(tid)),
                "slot": int(dogma[IMPLANT_SLOT_ATTR]) if IMPLANT_SLOT_ATTR in dogma else None,
                "bonus": {nm: int(dogma[a]) for a, nm in IMPLANT_BONUS_ATTRS.items()
                          if dogma.get(a)}}
    if type_ids:
        with ThreadPoolExecutor(max_workers=5) as pool:
            out = list(pool.map(one, type_ids))
    return out


def apply_finished_skills(skills, queue):
    """ESI only writes finished skills into /skills/ when the queue is next
    processed (usually when the character logs in), and leaves them in the
    queue until then. Count queue entries whose finish time has passed as
    trained and drop them from the queue. Returns (skills, queue) copies -
    the ESI results are cached and must not be changed in place."""
    now = time.time()
    have = {r["skill_id"]: dict(r) for r in skills.get("skills", [])}
    total = skills.get("total_sp", 0)
    left = []
    for r in queue:
        ts = _iso_ts(r.get("finish_date"))
        if ts is None or ts > now:
            left.append(r)
            continue
        cur = have.setdefault(r["skill_id"], {
            "skill_id": r["skill_id"], "trained_skill_level": 0,
            "active_skill_level": 0, "skillpoints_in_skill": 0})
        lvl = r.get("finished_level") or 0
        if lvl > cur["trained_skill_level"]:
            gain = max(0, (r.get("level_end_sp") or 0) - cur["skillpoints_in_skill"])
            cur["trained_skill_level"] = cur["active_skill_level"] = lvl
            cur["skillpoints_in_skill"] += gain
            total += gain
    return {**skills, "skills": list(have.values()), "total_sp": total}, left


def char_summary(char_id):
    tok = get_access_token(char_id)
    cid = int(char_id)
    pub, _ = esi_fetch(f"/characters/{cid}/")
    queue, _ = esi_fetch(f"/characters/{cid}/skillqueue/", tok)
    skills, _ = esi_fetch(f"/characters/{cid}/skills/", tok)
    wallet, _ = esi_fetch(f"/characters/{cid}/wallet/", tok, use_cache=False)
    attrs, _ = esi_fetch(f"/characters/{cid}/attributes/", tok)
    implants, _ = esi_fetch(f"/characters/{cid}/implants/", tok)
    skills, queue = apply_finished_skills(skills, queue)
    names = esi_names(implants) if implants else {}
    corp_name = esi_names([pub["corporation_id"]]).get(
        pub.get("corporation_id"), {}).get("name", "") if pub.get("corporation_id") else ""
    return {
        "character_id": cid,
        "name": tokens[str(char_id)]["name"],
        "corporation_id": pub.get("corporation_id"),
        "corporation": corp_name,
        "implant_details": implant_details(implants, names),
        "wallet": wallet,
        "total_sp": skills.get("total_sp", 0),
        "unallocated_sp": skills.get("unallocated_sp", 0),
        "attributes": {k: attrs.get(k) for k in
                       ["charisma", "intelligence", "memory",
                        "perception", "willpower"]},
        "implants": [names.get(i, {}).get("name", str(i)) for i in implants],
        "queue": queue,
        "skills": {str(s["skill_id"]): {
            "level": s["trained_skill_level"],
            "sp": s["skillpoints_in_skill"]} for s in skills.get("skills", [])},
    }


type_info = load_json(TYPES_PATH, {})  # type_id -> {name, grp, cat}


def get_type_info(tid):
    """Name + market group + category for a type, disk-cached forever."""
    rec = type_info.get(str(tid))
    if rec:
        return rec
    try:
        t, _ = esi_fetch(f"/universe/types/{tid}/")
        g, _ = esi_fetch(f"/universe/groups/{t['group_id']}/")
        c, _ = esi_fetch(f"/universe/categories/{g['category_id']}/")
        rec = {"name": t["name"], "grp": g["name"], "cat": c["name"]}
        type_info[str(tid)] = rec        # persisted by the caller
        return rec
    except RuntimeError:
        return {"name": f"Type {tid}", "grp": "Unknown", "cat": "Unknown"}


FITTING_PREFIXES = ("HiSlot", "MedSlot", "LoSlot", "RigSlot", "SubSystemSlot")
FLAG_LABELS = {
    "Hangar": "Hangar", "Cargo": "Cargo", "DroneBay": "Drone bay",
    "FleetHangar": "Fleet hangar", "ShipHangar": "Ship hangar",
    "Deliveries": "Deliveries", "AssetSafety": "Asset safety",
    "Unlocked": "In container", "Locked": "In container (locked)",
    "AutoFit": "Auto-fit", "FrigateEscapeBay": "Escape bay",
    "SpecializedFuelBay": "Fuel bay", "SpecializedOreHold": "Ore hold",
    "FighterBay": "Fighter bay", "SubSystemBay": "Subsystem bay",
    "BoosterBay": "Booster bay", "Implant": "Plugged in",
}
_struct_names = {}


def _flag_label(flag):
    if flag.startswith(FITTING_PREFIXES):
        return "Fitted"
    if flag in FLAG_LABELS:
        return FLAG_LABELS[flag]
    # fall back to splitting the CamelCase flag name
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", flag or "Other")


def structure_name(sid, tok):
    if sid not in _struct_names:
        try:
            s, _ = esi_fetch(f"/universe/structures/{sid}/", tok)
            _struct_names[sid] = s.get("name", "Player structure")
        except RuntimeError:
            _struct_names[sid] = "Player structure (no docking access)"
    return _struct_names[sid]


def active_ship(cid, tok):
    """Where the character is and what they're flying (needs the
    esi-location scopes; returns None for tokens added before those)."""
    try:
        l, _ = esi_fetch(f"/characters/{cid}/location/", tok, use_cache=False)
        s, _ = esi_fetch(f"/characters/{cid}/ship/", tok, use_cache=False)
        return {"docked": bool(l.get("station_id") or l.get("structure_id")),
                "system_id": l["solar_system_id"],
                "ship_item_id": s["ship_item_id"],
                "ship_type_id": s["ship_type_id"],
                "ship_name": s.get("ship_name", "")}
    except RuntimeError:
        return None


def char_card(char_id):
    """Ship + location for the character-select screen (needs the esi-location
    scopes; reports available=False for characters added without them)."""
    tok = get_access_token(char_id)
    cid = int(char_id)
    try:
        loc, _ = esi_fetch(f"/characters/{cid}/location/", tok, use_cache=False)
        ship, _ = esi_fetch(f"/characters/{cid}/ship/", tok, use_cache=False)
    except RuntimeError:
        return {"available": False}
    sysrec, _ = esi_fetch(f"/universe/systems/{loc['solar_system_id']}/")
    raw = sysrec.get("security_status", 0.0)
    sec = 0.1 if 0 < raw < 0.05 else int(raw * 10 + 0.5) / 10 if raw > 0 else 0.0
    station = None
    if loc.get("station_id"):
        station = esi_names([loc["station_id"]]).get(
            loc["station_id"], {}).get("name")
    elif loc.get("structure_id"):
        station = structure_name(loc["structure_id"], tok)
    sinfo = get_type_info(ship["ship_type_id"])
    save_json(TYPES_PATH, type_info)
    return {"available": True, "system": sysrec["name"], "security": sec,
            "station": station, "docked": bool(station),
            "ship": sinfo["name"], "ship_type_id": ship["ship_type_id"],
            "ship_name": ship.get("ship_name", "")}


def char_assets(char_id):
    tok = get_access_token(char_id)
    cid = int(char_id)
    rows = esi_fetch_pages(f"/characters/{cid}/assets/", tok, max_pages=40)
    by_item = {r["item_id"]: r for r in rows}
    active = active_ship(cid, tok)

    # resolve categories/groups for types we haven't seen before
    missing = {r["type_id"] for r in rows if str(r["type_id"]) not in type_info}
    if missing:
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(get_type_info, missing))
        save_json(TYPES_PATH, type_info)

    # walk each item up its parent chain (ship/container) to the real root
    agg, root_ids = {}, set()
    for r in rows:
        cur, parent0 = r, None
        for _ in range(12):
            parent = by_item.get(cur["location_id"])
            if parent is None:
                break
            if parent0 is None:
                parent0 = parent
            cur = parent
        root = cur["location_id"]
        root_ids.add(root)
        where = _flag_label(r.get("location_flag", ""))
        if parent0 is not None:
            hname = type_info.get(str(parent0["type_id"]), {}) \
                .get("name", "container")
            where = f"{where} — {hname}"
        is_copy = bool(r.get("is_blueprint_copy"))
        key = (r["type_id"], is_copy, root, where)
        agg[key] = agg.get(key, 0) + r.get("quantity", 1)

    name_ids = [i for i in root_ids if i < 10 ** 12]
    if active:
        name_ids.append(active["system_id"])
    names = esi_names(name_ids)
    prices = market_prices()

    # ESI omits the hull you're sitting in while undocked (esi-issues #433):
    # add it back, and label its fitted/cargo items properly
    ship_root_label = None
    if active and not active["docked"]:
        sinfo = get_type_info(active["ship_type_id"])
        save_json(TYPES_PATH, type_info)
        sysname = names.get(active["system_id"], {}).get("name", "space")
        label = sinfo["name"] + \
            (f' "{active["ship_name"]}"' if active["ship_name"] else "")
        ship_root_label = (active["ship_item_id"], sysname,
                           sinfo["name"], label)
        agg[(active["ship_type_id"], False, -1, "Active ship — in space")] = 1
        names[-1] = {"name": sysname}

    out = []
    for (tid, is_copy, root, where), qty in agg.items():
        info = type_info.get(str(tid)) or \
            {"name": f"Type {tid}", "grp": "Unknown", "cat": "Unknown"}
        price = 0.0 if is_copy else prices.get(tid, 0.0)
        name = info["name"] + (" (Copy)" if is_copy else "")
        if ship_root_label and root == ship_root_label[0]:
            loc = ship_root_label[1]
            where = f"{where} — {ship_root_label[2]} (active ship)"
        elif ship_root_label and root == -1:
            loc = ship_root_label[1]
            name = ship_root_label[3]   # include the custom ship name
        else:
            loc = names.get(root, {}).get("name") or \
                (structure_name(root, tok) if root >= 10 ** 12 else str(root))
        out.append({
            "type_id": tid,
            "name": name,
            "category": info["cat"],
            "group": info["grp"],
            "quantity": qty,
            "unit_price": price,
            "total_value": price * qty,
            "location": loc,
            "where": where,
        })
    out.sort(key=lambda x: -x["total_value"])
    return out


JOURNAL_PATH = os.path.join(DATA_DIR, "journal_archive.json")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "standing_snapshots.json")
journal_archive = load_json(JOURNAL_PATH, {})    # char_id -> {entry_id: [d,t,a,b,x]}
snapshots = load_json(SNAPSHOT_PATH, {})         # char_id -> {"lp": {...}, "st": {...}}
_archive_lock = threading.Lock()


def archive_journal(char_id):
    """Fetch the journal (ESI keeps ~30 days) and merge it into the local
    archive, so history keeps growing beyond ESI's window."""
    tok = get_access_token(char_id)
    cid = str(char_id)
    rows = esi_fetch_pages(f"/characters/{cid}/wallet/journal/", tok,
                           max_pages=60)
    with _archive_lock:
        store = journal_archive.setdefault(cid, {})
        for r in rows:
            store[str(r["id"])] = [r.get("date", ""), r.get("ref_type", ""),
                                   r.get("amount", 0), r.get("balance", 0),
                                   (r.get("description") or "")[:140]]
        save_json(JOURNAL_PATH, journal_archive)
    return store


def char_journal(char_id):
    store = archive_journal(char_id)
    entries = [{"id": int(k), "date": v[0], "ref_type": v[1], "amount": v[2],
                "balance": v[3], "description": v[4]}
               for k, v in store.items()]
    entries.sort(key=lambda e: e["date"], reverse=True)
    return {"entries": entries, "count": len(entries),
            "since": entries[-1]["date"][:10] if entries else None}


def _snap_day():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _first_snapshot(cid, kind):
    snaps = snapshots.get(str(cid), {}).get(kind, {})
    if not snaps:
        return None, {}
    day = min(snaps)
    return day, snaps[day]


# internal/hidden in-game corporations that show up in the API but can't be used
HIDDEN_LP_CORPS = {"paragon"}


def char_loyalty(char_id):
    tok = get_access_token(char_id)
    cid = str(char_id)
    rows, _ = esi_fetch(f"/characters/{cid}/loyalty/points/", tok)
    cur = {str(r["corporation_id"]): r["loyalty_points"] for r in rows}
    with _archive_lock:
        snapshots.setdefault(cid, {}).setdefault("lp", {})[_snap_day()] = cur
        save_json(SNAPSHOT_PATH, snapshots)
    day0, first = _first_snapshot(cid, "lp")
    names = esi_names([int(k) for k in cur])
    out = [{"corporation": names.get(int(k), {}).get("name", k),
            "corporation_id": int(k), "points": v, "since": day0,
            "delta": v - first.get(k, v) if first else 0}
           for k, v in cur.items()]
    out = [r for r in out
           if str(r["corporation"]).lower() not in HIDDEN_LP_CORPS]
    out.sort(key=lambda r: -r["points"])
    return out


_datacore_ids = {}                # datacore name (lowercase) -> type id


def char_research(char_id):
    """Research-point agents with their current RP and the datacore each one
    pays out (the datacore is named after the research skill)."""
    import calendar
    tok = get_access_token(char_id)
    cid = int(char_id)
    rows, _ = esi_fetch(f"/characters/{cid}/agents_research/", tok,
                        use_cache=False)
    out = {"rp_per_datacore": RP_PER_DATACORE,
           "isk_per_datacore": ISK_PER_DATACORE, "agents": []}
    if not rows:
        return out
    names = esi_names([r["agent_id"] for r in rows])

    def corp_of(aid):
        try:
            pub, _ = esi_fetch(f"/characters/{aid}/")
            return pub.get("corporation_id")
        except RuntimeError:
            return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        corps = dict(zip([r["agent_id"] for r in rows],
                         pool.map(corp_of, [r["agent_id"] for r in rows])))
    cnames = esi_names([c for c in corps.values() if c])
    skills = {r["skill_type_id"]: get_type_info(r["skill_type_id"])["name"]
              for r in rows}
    save_json(TYPES_PATH, type_info)
    want = [f"Datacore - {n}" for n in skills.values()
            if f"datacore - {n}".lower() not in _datacore_ids]
    if want:
        for k, h in _ids_lookup(want).items():
            _datacore_ids[k] = h["id"]
    now = time.time()
    for r in rows:
        sk = skills[r["skill_type_id"]]
        started = calendar.timegm(time.strptime(r["started_at"],
                                                "%Y-%m-%dT%H:%M:%SZ"))
        ppd = r.get("points_per_day", 0) or 0
        rp = r.get("remainder_points", 0) + ppd * max(0, now - started) / 86400
        dc_name = f"Datacore - {sk}"
        out["agents"].append({
            "agent": names.get(r["agent_id"], {}).get("name", str(r["agent_id"])),
            "corporation": cnames.get(corps.get(r["agent_id"]), {}).get("name", ""),
            "skill": sk, "datacore": dc_name,
            "datacore_id": _datacore_ids.get(dc_name.lower()),
            "ppd": round(ppd, 2), "rp": round(rp, 1),
            "ready": int(rp // RP_PER_DATACORE),
            "days_to_next": round((RP_PER_DATACORE - rp % RP_PER_DATACORE)
                                  / ppd, 1) if ppd > 0 else None,
        })
    out["agents"].sort(key=lambda a: -a["rp"])
    return out


def char_standings(char_id):
    tok = get_access_token(char_id)
    cid = str(char_id)
    rows, _ = esi_fetch(f"/characters/{cid}/standings/", tok)
    cur = {str(r["from_id"]): r["standing"] for r in rows}
    kinds = {str(r["from_id"]): r["from_type"] for r in rows}
    with _archive_lock:
        snapshots.setdefault(cid, {}).setdefault("st", {})[_snap_day()] = cur
        save_json(SNAPSHOT_PATH, snapshots)
    day0, first = _first_snapshot(cid, "st")
    names = esi_names([int(k) for k in cur])
    out = [{"name": names.get(int(k), {}).get("name", k),
            "type": kinds[k], "standing": round(v, 2), "since": day0,
            "delta": round(v - first.get(k, v), 2) if first else 0}
           for k, v in cur.items()]
    out.sort(key=lambda r: -r["standing"])
    return out


def archiver_loop():
    """Quietly keeps the journal archive growing while the app runs, so a
    month away from the Wallet tab doesn't lose history."""
    time.sleep(30)
    while True:
        for cid in list(tokens):
            try:
                archive_journal(cid)
                if config.get("scope_extras"):
                    char_loyalty(cid)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
        time.sleep(2 * 3600)


# ----------------------------------------------------------------------------
# Notifications: a quiet background watcher. ESI is asked once per check
# interval (default 1 hour); "skill finished" and "queue running low" are then
# worked out locally from the stored queue, so they fire on time without
# extra requests.
# ----------------------------------------------------------------------------
NOTIFY_PATH = os.path.join(DATA_DIR, "notifications.json")
NOTIFY_STATE_PATH = os.path.join(DATA_DIR, "notify_state.json")
NOTIFY_KINDS = [
    ("skill_done", "A skill finishes training"),
    ("queue_low", "The skill queue is running low or empty"),
    ("item_sold", "An item is sold on the market"),
    ("contract_done", "One of my contracts is sold / accepted"),
    ("job_done", "An industry job is finished"),
]
CHECK_CHOICES = [15, 30, 60, 120, 240]
notif_feed = load_json(NOTIFY_PATH, [])
notif_state = load_json(NOTIFY_STATE_PATH, {})
_notif_lock = threading.Lock()
_notify_force = threading.Event()
notify_info = {"last_check": 0, "errors": {}}


def notify_cfg():
    kinds = config.get("notify") or {}
    return {
        "enabled": config.get("notify_on", True),
        "os": config.get("notify_os", True),
        "kinds": {k: bool(kinds.get(k, True)) for k, _ in NOTIFY_KINDS},
        "check_minutes": int(config.get("check_minutes") or 60),
        "queue_low_hours": float(config.get("queue_low_hours") or 24),
    }


def notify_os(title, text):
    """Native desktop notice: tray balloon/toast, else notify-send."""
    try:
        import evewatch_tray
        if evewatch_tray.notify(title, text):
            return
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("linux"):
        try:
            import subprocess
            subprocess.Popen(["notify-send", "-a", "EveWatch", title, text])
        except Exception:  # noqa: BLE001
            pass


def push_notification(kind, title, text, os_notice=True):
    with _notif_lock:
        # time-based so ids keep increasing after "Clear list"
        nid = max(int(time.time() * 1000),
                  (notif_feed[-1]["id"] + 1) if notif_feed else 0)
        notif_feed.append({"id": nid, "ts": int(time.time()), "kind": kind,
                           "title": title, "text": text})
        del notif_feed[:-100]
        save_json(NOTIFY_PATH, notif_feed)
    if os_notice and notify_cfg()["os"]:
        notify_os(title, text)


def _iso_ts(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _isk(v):
    return f"{v:,.0f} ISK"


def _hours_text(h):
    if h < 1:
        return f"{int(h * 60)} min"
    return f"{h:.1f} h" if h < 10 else f"{int(h)} h"


def notify_poll(cid, st, kinds):
    """The ESI part: refresh what the local checks work from."""
    tok = get_access_token(cid)
    name = tokens[cid]["name"]
    errs = notify_info["errors"]
    errs.pop(cid, None)
    if kinds["skill_done"] or kinds["queue_low"]:
        q, _ = esi_fetch(f"/characters/{cid}/skillqueue/", tok, use_cache=False)
        st["queue"] = [{"skill": r["skill_id"], "level": r.get("finished_level"),
                        "finish": r.get("finish_date")} for r in q]
        st["queue_ts"] = time.time()
        if "done" not in st:           # first look: don't announce old news
            st["done"] = [f'{r["skill"]}:{r["level"]}:{r["finish"]}'
                          for r in st["queue"]
                          if (_iso_ts(r["finish"]) or 9e12) < time.time()]
    if kinds["item_sold"]:
        rows, _ = esi_fetch(f"/characters/{cid}/wallet/transactions/", tok,
                            use_cache=False)
        top = max([r["transaction_id"] for r in rows], default=0)
        if "tx_last" in st:
            sells = [r for r in rows
                     if r["transaction_id"] > st["tx_last"] and not r["is_buy"]]
            if sells:
                total = sum(r["quantity"] * r["unit_price"] for r in sells)
                names = []
                for r in sells[:3]:
                    names.append(f'{r["quantity"]:,} × '
                                 f'{get_type_info(r["type_id"])["name"]}')
                more = f" and {len(sells) - 3} more" if len(sells) > 3 else ""
                push_notification(
                    "item_sold", f"{name}: sold on the market",
                    f'{", ".join(names)}{more} — {_isk(total)}')
                save_json(TYPES_PATH, type_info)
        st["tx_last"] = max(top, st.get("tx_last", 0))
    if kinds["contract_done"]:
        scopes = tokens[cid].get("scopes")
        if scopes is not None and CONTRACTS_SCOPE not in scopes:
            errs[cid] = "contracts: character needs the contracts permission"
        else:
            try:
                rows = esi_fetch_pages(f"/characters/{cid}/contracts/", tok,
                                       max_pages=3)
            except RuntimeError as e:
                if " 403 " in str(e) or " 401 " in str(e):
                    errs[cid] = "contracts: character needs the contracts permission"
                    rows = None
                else:
                    raise
            if rows is not None:
                mine = [r for r in rows
                        if r.get("issuer_id") == int(cid)
                        and not r.get("for_corporation")
                        and r.get("type") in ("item_exchange", "auction")
                        and str(r.get("status", "")).startswith("finished")]
                seen = set(st.get("contracts_done", []))
                if "contracts_done" in st:
                    for r in mine:
                        if r["contract_id"] not in seen:
                            amount = r.get("price") or r.get("buyout") or 0
                            push_notification(
                                "contract_done", f"{name}: contract completed",
                                f'{r.get("title") or "Contract"} — '
                                f'{_isk(amount)}')
                st["contracts_done"] = sorted(
                    seen | {r["contract_id"] for r in mine})[-300:]
    if kinds["job_done"]:
        rows, _ = esi_fetch(f"/characters/{cid}/industry/jobs/", tok,
                            use_cache=False)
        st["jobs"] = {str(r["job_id"]): {
            "end": r.get("end_date"),
            "name": get_type_info(r.get("product_type_id")
                                  or r["blueprint_type_id"])["name"]}
            for r in rows if r.get("status") == "active"}
        save_json(TYPES_PATH, type_info)
        st.setdefault("jobs_done", [])


def notify_local(cid, st, kinds, cfg):
    """No network: works from the stored queue/jobs, so it's on time."""
    now = time.time()
    name = tokens[cid]["name"]
    q = st.get("queue")
    if q is not None and kinds["skill_done"]:
        done = st.setdefault("done", [])
        for r in q:
            ts = _iso_ts(r["finish"])
            key = f'{r["skill"]}:{r["level"]}:{r["finish"]}'
            if ts and ts <= now and key not in done:
                done.append(key)
                if now - ts < 12 * 3600:      # not stale news after downtime
                    push_notification(
                        "skill_done", f"{name}: skill finished",
                        f'{get_type_info(r["skill"])["name"]} '
                        f'{["", "I", "II", "III", "IV", "V"][r["level"] or 0]}')
        del done[:-200]
    if q is not None and kinds["queue_low"]:
        ends = [_iso_ts(r["finish"]) for r in q if r.get("finish")]
        if q and not ends:                        # queue paused
            remain = 0
        else:
            remain = (max(ends) - now) / 3600 if ends else 0
            remain = max(remain, 0)
        if remain > cfg["queue_low_hours"]:
            st["low"] = False
        elif not st.get("low"):
            st["low"] = True
            if not ends or remain <= 0:
                push_notification(
                    "queue_low", f"{name}: skill queue is empty",
                    "Nothing is training — add skills to the queue.")
            else:
                push_notification(
                    "queue_low", f"{name}: skill queue running low",
                    f"About {_hours_text(remain)} of training left.")
    if kinds["job_done"]:
        done = st.setdefault("jobs_done", [])
        for jid, j in (st.get("jobs") or {}).items():
            ts = _iso_ts(j["end"])
            if ts and ts <= now and jid not in done:
                done.append(jid)
                if now - ts < 12 * 3600:
                    push_notification("job_done", f"{name}: industry job finished",
                                      j["name"])
        del done[:-200]


def notify_loop():
    time.sleep(45)
    last_poll = 0
    while True:
        try:
            cfg = notify_cfg()
            if cfg["enabled"] and config.get("client_id"):
                due = (time.time() - last_poll >= cfg["check_minutes"] * 60
                       or _notify_force.is_set())
                _notify_force.clear()
                if due:
                    last_poll = time.time()
                    notify_info["last_check"] = int(last_poll)
                for cid in list(tokens):
                    st = notif_state.setdefault(cid, {})
                    try:
                        if due:
                            notify_poll(cid, st, cfg["kinds"])
                        notify_local(cid, st, cfg["kinds"], cfg)
                    except Exception as e:  # noqa: BLE001
                        notify_info["errors"][cid] = str(e)[:160]
                    if due:
                        time.sleep(2)
                save_json(NOTIFY_STATE_PATH, notif_state)
        except Exception as e:  # noqa: BLE001
            print("notify loop:", e)
        for _ in range(30):               # wake early on "Check now"
            if _notify_force.is_set():
                break
            time.sleep(1)


def notify_status():
    cfg = notify_cfg()
    return {
        **cfg,
        "kinds_list": [{"id": k, "label": l, "on": cfg["kinds"][k]}
                       for k, l in NOTIFY_KINDS],
        "choices": CHECK_CHOICES,
        "last_check": notify_info["last_check"],
        "errors": list(notify_info["errors"].values()),
        "contracts_scope": bool(config.get("scope_contracts")),
        "feed": notif_feed[-30:][::-1],
        "latest": notif_feed[-1]["id"] if notif_feed else 0,
    }


def char_permissions(rec):
    """Optional permissions switched on in Setup that this character's login
    doesn't include: skipped (left out at login, e.g. EVE didn't accept it
    yet) or missing (added before it was switched on)."""
    have = rec.get("scopes")
    out = []
    for sc in active_scopes():
        if sc in SCOPES:
            continue
        if sc in (rec.get("pending_scopes") or []):
            st = "skipped"
        elif have is not None and sc not in have:
            st = "missing"
        else:
            continue
        out.append({"scope": sc, "label": SCOPE_LABELS.get(sc, sc), "state": st})
    return out


_prices_cache = {"ts": 0, "data": {}}


def market_prices():
    if time.time() - _prices_cache["ts"] < 3600:
        return _prices_cache["data"]
    rows, _ = esi_fetch("/markets/prices/")
    data = {r["type_id"]: r.get("average_price") or r.get("adjusted_price") or 0
            for r in rows}
    _prices_cache.update(ts=time.time(), data=data)
    return data


def jita_orders(type_id):
    rows = esi_fetch_pages(f"/markets/{THE_FORGE}/orders/",
                           params={"type_id": type_id}, max_pages=100)
    sells = sorted([r["price"] for r in rows
                    if not r["is_buy_order"] and r["location_id"] == JITA_44])
    buys = sorted([r["price"] for r in rows
                   if r["is_buy_order"] and r["location_id"] == JITA_44],
                  reverse=True)
    return {"sell": sells[0] if sells else None,
            "buy": buys[0] if buys else None,
            "sell_volume": len(sells), "buy_volume": len(buys)}


def char_blueprints(char_id):
    tok = get_access_token(char_id)
    cid = int(char_id)
    rows = esi_fetch_pages(f"/characters/{cid}/blueprints/", tok, max_pages=15)
    missing = {r["type_id"] for r in rows if str(r["type_id"]) not in type_info}
    if missing:
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(get_type_info, missing))
        save_json(TYPES_PATH, type_info)
    names = esi_names([r["location_id"] for r in rows
                       if r["location_id"] < 10 ** 12])
    out = []
    for r in rows:
        out.append({
            "name": type_info.get(str(r["type_id"]), {})
                    .get("name", str(r["type_id"])),
            "me": r.get("material_efficiency", 0),
            "te": r.get("time_efficiency", 0),
            "runs": r.get("runs", -1),
            "is_copy": r.get("quantity") == -2,
            "count": r["quantity"] if r.get("quantity", 1) > 0 else 1,
            "location": names.get(r["location_id"], {}).get("name", "—"),
        })
    out.sort(key=lambda x: (x["is_copy"], x["name"]))
    return out


ACTIVITIES = {1: "Manufacturing", 3: "TE research", 4: "ME research",
              5: "Copying", 8: "Invention", 9: "Reactions"}


def char_jobs(char_id):
    tok = get_access_token(char_id)
    cid = int(char_id)
    rows, _ = esi_fetch(f"/characters/{cid}/industry/jobs/", tok)
    tids = {r["blueprint_type_id"] for r in rows} | \
           {r["product_type_id"] for r in rows if r.get("product_type_id")}
    missing = {t for t in tids if str(t) not in type_info}
    if missing:
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(get_type_info, missing))
        save_json(TYPES_PATH, type_info)
    return [{
        "activity": ACTIVITIES.get(r["activity_id"], "Other"),
        "blueprint": type_info.get(str(r["blueprint_type_id"]), {})
                     .get("name", "?"),
        "product": type_info.get(str(r.get("product_type_id") or ""), {})
                   .get("name", ""),
        "runs": r.get("runs", 1),
        "status": r.get("status", ""),
        "end_date": r.get("end_date", ""),
    } for r in rows]


def _has_contracts_scope(char_id):
    scopes = tokens[str(char_id)].get("scopes")
    return scopes is None or CONTRACTS_SCOPE in scopes   # None: added before we tracked scopes


def _scope_error(e):
    return " 403 " in str(e) or " 401 " in str(e)


def char_contracts(char_id):
    """The character's own contracts (issued, assigned to them or accepted).
    ESI only keeps the last 30 days plus anything still open/in progress."""
    if not _has_contracts_scope(char_id):
        return {"needs_scope": True, "contracts": []}
    tok = get_access_token(char_id)
    cid = int(char_id)
    try:
        rows = esi_fetch_pages(f"/characters/{cid}/contracts/", tok, max_pages=10)
    except RuntimeError as e:
        if _scope_error(e):
            return {"needs_scope": True, "contracts": []}
        raise
    names = esi_names([r.get(k) or 0 for r in rows for k in
                       ("issuer_id", "assignee_id", "acceptor_id",
                        "start_location_id", "end_location_id")])

    def who(i):
        return names.get(i, {}).get("name", "") if i else ""

    def where(i):
        if not i:
            return ""
        if i >= 10 ** 12:
            return structure_name(i, tok)
        return names.get(i, {}).get("name", str(i))

    out = []
    for r in rows:
        role = ("issuer" if r.get("issuer_id") == cid else
                "assignee" if r.get("assignee_id") == cid else "acceptor")
        out.append({
            "id": r["contract_id"], "type": r.get("type"), "status": r.get("status"),
            "title": r.get("title") or "", "role": role,
            "for_corp": bool(r.get("for_corporation")),
            "availability": r.get("availability"),
            "price": r.get("price") or 0, "reward": r.get("reward") or 0,
            "collateral": r.get("collateral") or 0, "buyout": r.get("buyout") or 0,
            "volume": r.get("volume") or 0, "days": r.get("days_to_complete") or 0,
            "issued": r.get("date_issued"), "expires": r.get("date_expired"),
            "accepted": r.get("date_accepted"), "completed": r.get("date_completed"),
            "issuer": who(r.get("issuer_id")), "assignee": who(r.get("assignee_id")),
            "acceptor": who(r.get("acceptor_id")),
            "start": where(r.get("start_location_id")),
            "end": where(r.get("end_location_id")),
        })
    out.sort(key=lambda x: x["issued"] or "", reverse=True)
    return {"needs_scope": False, "contracts": out}


def char_contract_items(char_id, contract_id, ctype):
    """Items in one of the character's contracts, valued at EVE's average
    price, plus the bids if it's an auction."""
    tok = get_access_token(char_id)
    cid = int(char_id)
    rows, _ = esi_fetch(f"/characters/{cid}/contracts/{contract_id}/items/", tok)
    missing = {r["type_id"] for r in rows if str(r["type_id"]) not in type_info}
    if missing:
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(get_type_info, missing))
        save_json(TYPES_PATH, type_info)
    try:
        avg = market_prices()
    except RuntimeError:
        avg = {}
    items = []
    for r in rows:
        bpc = r.get("raw_quantity") == -2
        unit = None if bpc else avg.get(r["type_id"])
        items.append({
            "type_id": r["type_id"], "qty": r.get("quantity", 1),
            "name": type_info.get(str(r["type_id"]), {}).get("name", str(r["type_id"])),
            "included": r.get("is_included", True), "bpc": bpc,
            "value": unit * r.get("quantity", 1) if unit else None})
    items.sort(key=lambda x: (not x["included"], -(x["value"] or 0)))
    bids = []
    if ctype == "auction":
        try:
            b, _ = esi_fetch(f"/characters/{cid}/contracts/{contract_id}/bids/", tok)
            bids = sorted([{"amount": x.get("amount", 0), "date": x.get("date_bid")}
                           for x in b], key=lambda x: -x["amount"])
        except RuntimeError:
            pass
    return {"items": items, "bids": bids,
            "value": sum(i["value"] or 0 for i in items if i["included"])}


def parse_appraisal_lines(text):
    """Each pasted line becomes a list of candidate (name, qty) readings,
    in priority order. The reading that resolves to a real item wins, so
    'Cap Booster 800' stays an item name while 'Missile x 200000' splits."""
    def _n(s):
        return int(s.replace(",", ""))
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        cands = []
        if "\t" in raw:                      # in-game inventory copy
            parts = [p.strip() for p in raw.split("\t")]
            name, qty = parts[0].rstrip("*").strip(), 1
            for p in parts[1:3]:
                pc = p.replace(",", "").replace(" ", "").replace("\u00a0", "")
                if pc and pc.replace(".", "", 1).isdigit():
                    qty = int(float(pc))
                    break
            cands.append((name, qty))
        else:
            cands.append((raw, 1))                       # whole line = item
            m = re.match(r"^(.+?)\s+x\s*([\d,]+)$", raw, re.I)
            if m:                                        # "Name x 200000"
                cands.append((m.group(1).strip(), _n(m.group(2))))
            m = re.match(r"^([\d,]+)\s*x?\s+(.+)$", raw, re.I)
            if m:                                        # "200000 Name"
                cands.append((m.group(2).strip(), _n(m.group(1))))
            m = re.match(r"^(.+?)\s+([\d,]+)$", raw)
            if m:                                        # "Name 200000"
                cands.append((m.group(1).strip(), _n(m.group(2))))
        lines.append((raw, cands))
    return lines


def _ids_lookup(names):
    """POST /universe/ids/ in small chunks with retry/backoff.
    Returns {lowercase name: hit}."""
    out = {}
    for i in range(0, len(names), 200):
        chunk = names[i:i + 200]
        for attempt in range(4):
            try:
                resp = http_request(
                    ESI + "/universe/ids/", data=json.dumps(chunk).encode(),
                    headers={"Content-Type": "application/json"})
                for h in json.loads(resp.read().decode()).get(
                        "inventory_types", []):
                    out[h["name"].lower()] = h
                break
            except urllib.error.HTTPError as e:
                if e.code in (420, 429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(int(e.headers.get("Retry-After")
                                   or 3 * (attempt + 1)))
                    continue
                raise RuntimeError(f"Name lookup failed (HTTP {e.code}).")
            except (urllib.error.URLError, TimeoutError):
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError("Name lookup failed (network problem).")
    return out


def appraise_resolve(text):
    """Step 1 of a batched appraisal: pasted text -> item types + quantities.
    Pricing happens afterwards, a few items at a time."""
    last_interactive[0] = time.time()
    lines = parse_appraisal_lines(text)
    if not lines:
        return {"error": "Nothing to appraise - paste item lines first."}
    names = list({n for _, cands in lines for n, _ in cands
                  if 0 < len(n) <= 100})
    by_lower = _ids_lookup(names)
    merged, unresolved = {}, []
    for raw, cands in lines:
        for name, qty in cands:
            h = by_lower.get(name.lower())
            if h:
                rec = merged.setdefault(h["id"], [h["name"], 0])
                rec[1] += qty
                break
        else:
            unresolved.append(raw)
    return {"items": [{"type_id": t, "name": n, "quantity": q}
                      for t, (n, q) in merged.items()],
            "unresolved": unresolved, "lines": len(lines)}


_station_price_cache = {}     # (station_id, type_id) -> (ts, sell, buy)


def appraise_batch(station_id, type_ids):
    """Step 2: price one small batch of types at a station, reading every
    page of the region's orders so busy items aren't missed."""
    last_interactive[0] = time.time()
    loc = next((l for l in locations if l["station_id"] == station_id),
               locations[0] if locations else None)
    if not loc:
        return {"error": "Add a market location first."}
    try:
        avg = market_prices()
    except RuntimeError:
        avg = {}
    out = {}

    def one(tid):
        key = (loc["station_id"], tid)
        hit = _station_price_cache.get(key)
        if hit and time.time() - hit[0] < 300:
            out[tid] = {"sell": hit[1], "buy": hit[2]}
            return
        try:
            orders = esi_fetch_pages(
                f"/markets/{loc['region_id']}/orders/",
                params={"type_id": tid}, max_pages=120)
        except RuntimeError as e:
            out[tid] = {"error": str(e)[:160]}
            return
        sells = [o["price"] for o in orders if not o["is_buy_order"]
                 and o["location_id"] == loc["station_id"]]
        buys = [o["price"] for o in orders if o["is_buy_order"]
                and o["location_id"] == loc["station_id"]]
        s = min(sells) if sells else None
        b = max(buys) if buys else None
        _station_price_cache[key] = (time.time(), s, b)
        out[tid] = {"sell": s, "buy": b}

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(one, type_ids))
    for tid in type_ids:
        out.setdefault(tid, {"error": "no result"})["average"] = avg.get(tid)
    return {"station": loc["name"], "prices": {str(k): v for k, v in out.items()}}


# ---------------------------------------------------------------------------
# Price history & movers
# ---------------------------------------------------------------------------

history_cache = load_json(HISTORY_PATH, {})

# liquid, well-known high-sec items for the market overview
BASKET_NAMES = [
    "Tritanium", "Pyerite", "Mexallon", "Isogen", "Nocxium", "Zydrine",
    "Megacyte", "Morphite", "PLEX", "Large Skill Injector",
    "Small Skill Injector", "Skill Extractor", "Nanite Repair Paste",
    "Nitrogen Fuel Block", "Hydrogen Fuel Block", "Helium Fuel Block",
    "Oxygen Fuel Block", "Heavy Water", "Liquid Ozone",
    "Strontium Clathrates", "Helium Isotopes", "Nitrogen Isotopes",
    "Oxygen Isotopes", "Hydrogen Isotopes", "Rifter", "Caracal", "Vexor",
    "Drake", "Gila", "Praxis", "Orca", "Venture", "Retriever", "Hulk",
    "Damage Control II", "Gyrostabilizer II", "Magnetic Field Stabilizer II",
    "Heat Sink II", "Ballistic Control System II", "Tobacco", "Antibiotics",
]
_basket_ids = None


def resolve_basket():
    global _basket_ids
    if _basket_ids is None:
        resp = http_request(ESI + "/universe/ids/",
                            data=json.dumps(BASKET_NAMES).encode(),
                            headers={"Content-Type": "application/json"})
        hits = json.loads(resp.read().decode()).get("inventory_types", [])
        _basket_ids = [(h["name"], h["id"]) for h in hits]
    return _basket_ids


def get_history(tid, region_id=THE_FORGE):
    """Daily price history, cached on disk until the next (UTC) day."""
    key = f"{region_id}:{tid}"
    today = time.strftime("%Y-%m-%d", time.gmtime())
    rec = history_cache.get(key)
    if rec and rec.get("fetched") == today:
        return rec["data"]
    rows, _ = esi_fetch(f"/markets/{region_id}/history/",
                        params={"type_id": tid})
    data = [[r["date"], r.get("average", 0), r.get("volume", 0)]
            for r in rows][-92:]
    history_cache[key] = {"fetched": today, "data": data}
    return data


def _pct(now, then):
    return round((now / then - 1) * 100, 1) if then else None


MIN_RELIABLE_VOL = 5    # units traded/day below this, big % moves are suspect


def analyze_history(data):
    if not data or len(data) < 2:
        return None
    avgs = [d[1] for d in data]
    vols = [d[2] for d in data]
    price, prev = avgs[-1], avgs[-2]
    vol_last, vol_prev = vols[-1], vols[-2]
    # A single fat-fingered order (e.g. an 800M ISK module sold for 8 ISK by
    # mistake) trivially clears any ISK-notional liquidity filter since
    # notional = price x volume. Guard against it directly: a large swing
    # on thin trade volume is treated as unreliable rather than a real move.
    ratio = (max(price, prev) / min(price, prev)) if min(price, prev) > 0 else 1
    thin = min(vol_last, vol_prev) < MIN_RELIABLE_VOL
    reliable = not (ratio > 3 and thin)
    return {
        "price": price, "prev": prev,
        "d1": _pct(price, prev),
        "d7": _pct(price, avgs[-8]) if len(avgs) >= 8 else None,
        "d90": _pct(price, avgs[0]),
        "as_of": data[-1][0],
        "spark": [round(a, 4) for a in avgs[-90:]],
        "vol_last": vol_last, "vol_prev": vol_prev,
        "reliable": reliable,
    }


crawl_status = {"state": "idle", "done": 0, "total": 0, "error": ""}
MIN_DAILY_ISK = 250e6      # liquidity floor for "biggest movers" (ISK/day)


def crawl_index():
    """Fetch daily history for every type traded in The Forge (one-time
    ~2h build, then daily refreshes are incremental)."""
    try:
        crawl_status.update(state="running", done=0, error="")
        tids = esi_fetch_pages(f"/markets/{THE_FORGE}/types/", max_pages=25)
        crawl_status["total"] = len(tids)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for i, tid in enumerate(tids):
            rec = history_cache.get(f"{THE_FORGE}:{tid}")
            if not (rec and rec.get("fetched") == today):
                try:
                    get_history(tid)
                except RuntimeError:
                    pass
                crawl_pause(0.45)  # ~130 req/min, well under history limits
            crawl_status["done"] = i + 1
            if (i + 1) % 500 == 0:
                save_json(HISTORY_PATH, history_cache)
        save_json(HISTORY_PATH, history_cache)
        config["index_date"] = today
        save_json(CONFIG_PATH, config)
        crawl_status["state"] = "done"
    except Exception as e:  # noqa: BLE001
        crawl_status.update(state="error", error=str(e))


def index_movers(limit=50):
    """Top movers across the whole indexed market, liquid items only."""
    prefix = f"{THE_FORGE}:"
    candidates = []
    for key, rec in history_cache.items():
        if not key.startswith(prefix):
            continue
        data = rec.get("data") or []
        if len(data) < 8:
            continue
        # liquidity: ISK traded on the last recorded day
        if data[-1][1] * data[-1][2] < MIN_DAILY_ISK:
            continue
        a = analyze_history(data)
        if a and a["d1"] is not None and a["reliable"]:
            candidates.append((int(key[len(prefix):]), a))
    candidates.sort(key=lambda t: -abs(t[1]["d1"]))
    top = candidates[:limit]
    names = esi_names([tid for tid, _ in top])
    return [{"type_id": tid, "qty": None, "value": None,
             "name": names.get(tid, {}).get("name", str(tid)), **a}
            for tid, a in top]


def market_movers(mode, char_id=None, threshold=10.0):
    if mode == "overview":
        rows = index_movers()
        if len(rows) >= 20:      # full index available
            alerts = [r for r in rows
                      if r["d1"] is not None and abs(r["d1"]) >= threshold]
            return {"rows": rows, "alerts": alerts, "threshold": threshold,
                    "mode": mode, "index": True,
                    "index_date": config.get("index_date")}
        mode = "basket"          # index not built yet - fall back
    if mode == "owned":
        per = {}
        for r in char_assets(char_id):
            if r["unit_price"] <= 0 or "(Copy)" in r["name"]:
                continue
            p = per.setdefault(r["type_id"], {"qty": 0, "value": 0.0})
            p["qty"] += r["quantity"]
            p["value"] += r["total_value"]
        targets = sorted(per.items(), key=lambda kv: -kv[1]["value"])[:120]
        items = [(tid, type_info.get(str(tid), {}).get("name", str(tid)),
                  m["qty"], m["value"]) for tid, m in targets]
    else:
        items = [(tid, name, None, None) for name, tid in resolve_basket()]

    out = []
    def work(item):
        tid, name, qty, value = item
        try:
            a = analyze_history(get_history(tid))
            if a:
                out.append({"type_id": tid, "name": name,
                            "qty": qty, "value": value, **a})
        except RuntimeError:
            pass
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, items))
    save_json(HISTORY_PATH, history_cache)

    out.sort(key=lambda r: -abs(r["d1"] or 0))
    # rows stay visible even when thin/unreliable (you own the item, so it's
    # still worth seeing) but only reliable moves trigger an alert
    alerts = [r for r in out if r["d1"] is not None and r["reliable"]
              and abs(r["d1"]) >= threshold]
    return {"rows": out, "alerts": alerts, "threshold": threshold,
            "mode": mode, "index": False,
            "index_progress": dict(crawl_status)}


# ---------------------------------------------------------------------------
# ISK/hour trade routes
# ---------------------------------------------------------------------------

def _loc_system_id(loc):
    if not loc.get("system_id"):
        st, _ = esi_fetch(f"/universe/stations/{loc['station_id']}/")
        loc["system_id"] = st["system_id"]
        save_json(LOCATIONS_PATH, locations)
    return loc["system_id"]


def _station_book(orders, station_id):
    sells = sorted((o["price"], o["volume_remain"]) for o in orders
                   if not o["is_buy_order"] and o["location_id"] == station_id)
    buys = sorted(((o["price"], o["volume_remain"]) for o in orders
                   if o["is_buy_order"] and o["location_id"] == station_id),
                  reverse=True)
    return sells, buys


def route_jumps(sys_a, sys_b):
    route, _ = esi_fetch(f"/route/{sys_a}/{sys_b}/",
                         params={"flag": "secure"})
    return max(0, len(route) - 1)


def trade_scan(type_id, mpj=1.2, cargo=5000.0, tax=0.05,
               from_cat=None, to_cat=None):
    """Best hub-to-hub hauls for one item, ranked by ISK/hour.
    One-way trip; 'secure' routing (prefers high-sec)."""
    t, _ = esi_fetch(f"/universe/types/{type_id}/")
    vol = t.get("packaged_volume") or t.get("volume") or 0.01
    froms = [l for l in locations if not from_cat or l["category"] == from_cat]
    tos = [l for l in locations if not to_cat or l["category"] == to_cat]
    needed = {l["station_id"]: l for l in froms + tos}.values()
    by_region = {}
    for l in needed:
        by_region.setdefault(l["region_id"], []).append(l)
    books = {}
    for rid, locs in by_region.items():
        orders = esi_fetch_pages(f"/markets/{rid}/orders/",
                                 params={"type_id": type_id}, max_pages=100)
        for l in locs:
            books[l["station_id"]] = _station_book(orders, l["station_id"])
    results = []
    for a in froms:
        sells = books[a["station_id"]][0]
        if not sells:
            continue
        ask = sells[0][0]
        ask_vol = sum(v for p, v in sells if p <= ask * 1.02)
        for b in tos:
            if b["station_id"] == a["station_id"]:
                continue
            buys = books[b["station_id"]][1]
            if not buys:
                continue
            bid = buys[0][0]
            bid_vol = sum(v for p, v in buys if p >= bid * 0.98)
            profit_unit = bid * (1 - tax) - ask
            if profit_unit <= 0:
                continue
            units_cargo = int(cargo // vol) if vol > 0 else 10 ** 9
            units = min(ask_vol, bid_vol, units_cargo)
            if units <= 0:
                continue
            try:
                jumps = route_jumps(_loc_system_id(a), _loc_system_id(b))
            except RuntimeError:
                continue
            hours = (jumps * mpj + 6) / 60      # +6 min dock/undock/load
            profit = profit_unit * units
            results.append({
                "from": a["name"], "from_system": a["system"],
                "to": b["name"], "to_system": b["system"],
                "buy_price": ask, "sell_price": bid,
                "profit_unit": round(profit_unit, 2),
                "jumps": jumps, "units": units,
                "avail": min(ask_vol, bid_vol),
                "profit_trip": round(profit, 2),
                "hours": round(hours, 2),
                "iskph": round(profit / hours, 2) if hours > 0 else profit,
            })
    results.sort(key=lambda r: -r["iskph"])
    return {"volume_m3": vol, "rows": results}


# ---------------------------------------------------------------------------
# Hub scanner: where is the same item cheap in one hub and dear in another?
# ---------------------------------------------------------------------------

HUBSCAN_PATH = os.path.join(DATA_DIR, "hub_scan.json")
hubscan = {"state": "idle", "phase": "", "done": 0, "total": 0, "error": "",
           "cancel": False}
hubscan_result = load_json(HUBSCAN_PATH, {"rows": [], "finished": None,
                                          "scope": "", "stations": []})
_taxonomy = {"cats": None, "groups": {}}


def taxonomy_categories():
    if _taxonomy["cats"] is None:
        ids, _ = esi_fetch("/universe/categories/")

        def one(cid):
            try:
                c, _ = esi_fetch(f"/universe/categories/{cid}/")
                return {"id": cid, "name": c["name"], "groups": c.get("groups", []),
                        "published": c.get("published", True)}
            except RuntimeError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            cats = [c for c in pool.map(one, ids) if c and c["published"]]
        _taxonomy["cats"] = {c["id"]: c for c in cats}
    return [{"id": c["id"], "name": c["name"]}
            for c in sorted(_taxonomy["cats"].values(), key=lambda c: c["name"])]


def _group(gid):
    if gid not in _taxonomy["groups"]:
        g, _ = esi_fetch(f"/universe/groups/{gid}/")
        _taxonomy["groups"][gid] = {"id": gid, "name": g["name"],
                                    "published": g.get("published", True),
                                    "types": g.get("types", [])}
    return _taxonomy["groups"][gid]


def taxonomy_groups(cat_id):
    taxonomy_categories()
    cat = _taxonomy["cats"].get(cat_id)
    if not cat:
        return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        groups = list(pool.map(lambda g: _try(_group, g), cat["groups"]))
    return [{"id": g["id"], "name": g["name"]}
            for g in sorted([g for g in groups if g and g["published"]],
                            key=lambda g: g["name"])]


def _try(fn, *a):
    try:
        return fn(*a)
    except RuntimeError:
        return None


def _scan_pages(path, params, absorb):
    """Stream every page of an order book without keeping it in memory."""
    params = dict(params)
    params["page"] = 1
    first, headers = esi_fetch(path, params=params, use_cache=False)
    total = int(next((v for k, v in headers.items() if k.lower() == "x-pages"), 1) or 1)
    absorb(first)
    hubscan["done"] += 1
    for start in range(2, total + 1, 8):
        if hubscan["cancel"]:
            return
        pages = list(range(start, min(total, start + 7) + 1))

        def get(pg):
            q = dict(params)
            q["page"] = pg
            return esi_fetch(path, params=q, use_cache=False)[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for rows in pool.map(get, pages):
                absorb(rows)
                hubscan["done"] += 1
        crawl_pause(0.1)


def _region_pages(region_id):
    first, headers = esi_fetch(f"/markets/{region_id}/orders/",
                               params={"page": 1}, use_cache=False)
    return int(next((v for k, v in headers.items() if k.lower() == "x-pages"), 1) or 1)


def hub_scan_run(station_ids, category_ids, group_ids):
    st = hubscan
    st.update(state="running", phase="Preparing", done=0, total=0, error="", cancel=False)
    try:
        locs = [l for l in locations if l["station_id"] in set(station_ids)]
        if len(locs) < 2:
            raise RuntimeError("Pick at least two market locations to compare.")
        allowed, scope = None, "Whole market"
        if category_ids or group_ids:
            allowed, labels = set(), []
            taxonomy_categories()
            if group_ids:
                for gid in group_ids:
                    g = _group(gid)
                    allowed.update(g["types"])
                    labels.append(g["name"])
            else:
                for cid in category_ids:
                    cat = _taxonomy["cats"][cid]
                    labels.append(cat["name"])
                    for gid in cat["groups"]:
                        g = _try(_group, gid)
                        if g:
                            allowed.update(g["types"])
            scope = ", ".join(labels[:6]) + ("…" if len(labels) > 6 else "")
        stationset = {l["station_id"] for l in locs}
        by_region = {}
        for l in locs:
            by_region.setdefault(l["region_id"], []).append(l)
        st["phase"] = "Counting order pages"
        pages = {rid: _region_pages(rid) for rid in by_region}
        st.update(total=sum(pages.values()), done=0, phase="Reading order books")
        books = {}

        def absorb(rows):
            for o in rows:
                lid = o["location_id"]
                if lid not in stationset:
                    continue
                tid = o["type_id"]
                if allowed is not None and tid not in allowed:
                    continue
                rec = books.get((lid, tid))
                if rec is None:
                    rec = books[(lid, tid)] = ([], [])
                rec[1 if o["is_buy_order"] else 0].append((o["price"], o["volume_remain"]))
        for rid in by_region:
            if st["cancel"]:
                raise RuntimeError("Cancelled.")
            _scan_pages(f"/markets/{rid}/orders/", {}, absorb)
        if st["cancel"]:
            raise RuntimeError("Cancelled.")

        st["phase"] = "Comparing hubs"
        quotes = {}
        for (lid, tid), (sells, buys) in books.items():
            q = {}
            if sells:
                sells.sort()
                ask = sells[0][0]
                q["ask"] = ask
                q["ask_vol"] = sum(v for p, v in sells if p <= ask * 1.02)
                q["sell_total"] = sum(v for p, v in sells)
            if buys:
                buys.sort(reverse=True)
                bid = buys[0][0]
                q["bid"] = bid
                q["bid_vol"] = sum(v for p, v in buys if p >= bid * 0.98)
            quotes.setdefault(tid, {})[lid] = q
        books.clear()
        by_station = {l["station_id"]: l for l in locs}
        cands = []
        for tid, per in quotes.items():
            for a, qa in per.items():
                if not qa.get("ask") or qa["ask"] <= 0:
                    continue
                for b, qb in per.items():
                    if a == b:
                        continue
                    inst = qb.get("bid", 0) >= qa["ask"] * 1.02
                    pat = qb.get("ask", 0) >= qa["ask"] * 1.08
                    if not (inst or pat):
                        continue
                    vol = min(qa["ask_vol"], qb.get("bid_vol", 0) if inst else qa["ask_vol"])
                    gain = (qb["bid"] - qa["ask"]) if inst else (qb["ask"] - qa["ask"])
                    cands.append((gain * vol, tid, a, b))
        cands.sort(reverse=True)
        cands = cands[:450]

        st.update(phase="Checking trading activity", done=0, total=len(cands))
        tids = sorted({c[1] for c in cands})
        names = esi_names(tids)
        avg = market_prices()
        vols = {}

        def type_vol(tid):
            t = _try(lambda: esi_fetch(f"/universe/types/{tid}/")[0])
            vols[tid] = (t.get("packaged_volume") or t.get("volume") or 0.01) if t else 0.01
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(type_vol, tids))
        hist = {}

        def dest_hist(key):
            rid, tid = key
            try:
                data = get_history(tid, rid)
            except RuntimeError:
                data = []
            last = data[-7:]
            if last:
                hist[key] = (sum(d[2] for d in last) / len(last),
                             sum(d[1] * d[2] for d in last) / max(1, sum(d[2] for d in last)))
        keys = sorted({(by_station[c[3]]["region_id"], c[1]) for c in cands})
        st["total"] = len(keys)
        with ThreadPoolExecutor(max_workers=5) as pool:
            for _ in pool.map(dest_hist, keys):
                st["done"] += 1
                if st["cancel"]:
                    raise RuntimeError("Cancelled.")
        save_json(HISTORY_PATH, history_cache)
        jumps = {}
        for _, tid, a, b in cands:
            sa, sb = _loc_system_id(by_station[a]), _loc_system_id(by_station[b])
            if (sa, sb) not in jumps:
                try:
                    jumps[(sa, sb)] = route_jumps(sa, sb)
                except RuntimeError:
                    jumps[(sa, sb)] = None
        rows = []
        for _, tid, a, b in cands:
            qa, qb, A, B = quotes[tid][a], quotes[tid][b], by_station[a], by_station[b]
            hv = hist.get((B["region_id"], tid))
            j = jumps.get((_loc_system_id(A), _loc_system_id(B)))
            if j is None:
                continue
            rows.append({
                "tid": tid, "name": names.get(tid, {}).get("name", str(tid)),
                "from": A["name"], "from_system": A["system"],
                "to": B["name"], "to_system": B["system"],
                "ask": qa["ask"], "ask_vol": qa["ask_vol"],
                "bid": qb.get("bid"), "bid_vol": qb.get("bid_vol", 0),
                "dst_ask": qb.get("ask"), "dst_sell_vol": qb.get("sell_total", 0),
                "avg": avg.get(tid), "m3": vols.get(tid, 0.01), "jumps": j,
                "day_units": round(hv[0], 1) if hv else None,
                "day_price": round(hv[1], 2) if hv else None})
        global hubscan_result
        hubscan_result = {"rows": rows, "finished": time.time(), "scope": scope,
                          "stations": [l["system"] for l in locs]}
        save_json(HUBSCAN_PATH, hubscan_result)
        st["state"] = "done"
    except Exception as e:  # noqa: BLE001
        st.update(state="error" if str(e) != "Cancelled." else "idle", error=str(e)[:300])


# ---------------------------------------------------------------------------
# LP store: what is each loyalty point worth?
# ---------------------------------------------------------------------------

def lp_corp_search(name):
    """Find NPC corporations by (exact) name via /universe/ids/."""
    name = (name or "").strip()
    if not name:
        return []
    resp = http_request(ESI + "/universe/ids/",
                        data=json.dumps([name]).encode(),
                        headers={"Content-Type": "application/json"})
    hits = json.loads(resp.read().decode()).get("corporations", [])
    return [{"id": h["id"], "name": h["name"]} for h in hits]


def lp_offers(corp_id):
    """Step 1: the store's offers with names. Prices are fetched afterwards,
    a few at a time, with the same batch call the appraisal uses."""
    last_interactive[0] = time.time()
    try:
        rows, _ = esi_fetch(f"/loyalty/stores/{int(corp_id)}/offers/")
    except RuntimeError as e:
        return {"error": f"Could not load that corporation's LP store: {e}"}
    tids = set()
    for r in rows:
        tids.add(r["type_id"])
        for ri in r.get("required_items", []):
            tids.add(ri["type_id"])
    names = esi_names(list(tids) + [int(corp_id)])
    offers = []
    for r in rows:
        offers.append({
            "offer_id": r["offer_id"], "type_id": r["type_id"],
            "name": names.get(r["type_id"], {}).get("name", str(r["type_id"])),
            "qty": r.get("quantity", 1),
            "lp": r.get("lp_cost", 0), "isk": r.get("isk_cost", 0),
            "req": [{"type_id": ri["type_id"], "qty": ri["quantity"],
                     "name": names.get(ri["type_id"], {}).get(
                         "name", str(ri["type_id"]))}
                    for ri in r.get("required_items", [])],
        })
    return {"corp_id": int(corp_id),
            "corp": names.get(int(corp_id), {}).get("name", str(corp_id)),
            "offers": offers, "type_ids": sorted(tids)}


# ---------------------------------------------------------------------------
# Update check (looks at the latest GitHub release; never installs anything)
# ---------------------------------------------------------------------------

_update_cache = {"ts": 0, "data": None}


def _ver(v):
    nums = re.findall(r"\d+", str(v).split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def update_repo():
    r = config.get("update_repo") or GITHUB_REPO
    return "" if (not r or r.startswith("@@")) else r


def _autostart_cmd():
    """Command line that starts EveWatch quietly (no browser) at login."""
    if FROZEN:
        return [sys.executable, "--no-browser"]
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("python.exe"):
        pw = py[:-10] + "pythonw.exe"
        py = pw if os.path.exists(pw) else py
    return [py, os.path.abspath(__file__), "--no-browser"]


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_LINUX_AUTOSTART = os.path.join(os.path.expanduser("~"), ".config",
                                "autostart", "evewatch.desktop")


def _win_startup_lnk():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup", "EveWatch.lnk")


def autostart_enabled():
    try:
        if os.name == "nt":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                    winreg.QueryValueEx(k, "EveWatch")
                    return True
            except OSError:
                return os.path.exists(_win_startup_lnk())
        return os.path.exists(_LINUX_AUTOSTART)
    except Exception:
        return False


def set_autostart(on):
    if os.name == "nt":
        import winreg
        if on:
            cmd = " ".join('"%s"' % c if " " in c else c
                           for c in _autostart_cmd())
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.SetValueEx(k, "EveWatch", 0, winreg.REG_SZ, cmd)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, "EveWatch")
            except OSError:
                pass
            # also remove the shortcut the installer may have created
            try:
                os.remove(_win_startup_lnk())
            except OSError:
                pass
        return
    if on:
        os.makedirs(os.path.dirname(_LINUX_AUTOSTART), exist_ok=True)
        cmd = " ".join('"%s"' % c if " " in c else c
                       for c in _autostart_cmd())
        with open(_LINUX_AUTOSTART, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=EveWatch\n"
                    "Comment=Personal EVE Online tool\nExec=%s\n"
                    "Terminal=false\nX-GNOME-Autostart-enabled=true\n" % cmd)
    else:
        try:
            os.remove(_LINUX_AUTOSTART)
        except OSError:
            pass


def check_update(force=False):
    if not force and _update_cache["data"] and \
            time.time() - _update_cache["ts"] < 3600:
        return _update_cache["data"]
    repo = update_repo()
    out = {"current": APP_VERSION, "repo": repo}
    if not repo:
        out["error"] = ("Update checking isn't set up in this copy "
                        "(no GitHub repository configured).")
        return out
    try:
        resp = http_request(f"https://api.github.com/repos/{repo}/releases/latest",
                            headers={"Accept": "application/vnd.github+json"})
        rel = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        out["error"] = ("No releases have been published yet."
                        if e.code == 404 else f"GitHub answered HTTP {e.code}.")
        return out
    except (urllib.error.URLError, OSError, ValueError):
        out["error"] = "Couldn't reach GitHub - check your internet connection."
        return out
    latest = rel.get("tag_name", "")
    want = ".exe" if sys.platform == "win32" else ".tar.gz"
    asset = next((a for a in rel.get("assets", [])
                  if a["name"].lower().endswith(want)), None)
    out.update({"latest": latest.lstrip("vV"),
                "newer": _ver(latest) > _ver(APP_VERSION),
                "page": rel.get("html_url"),
                "download": asset["browser_download_url"] if asset else None,
                "notes": (rel.get("body") or "")[:2000]})
    _update_cache.update(ts=time.time(), data=out)
    return out


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(path)                                 # noqa: S606
    elif sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["open", path])
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------------------
# Ship mastery data (from CCP's Static Data Export - ESI has no mastery API)
# ---------------------------------------------------------------------------

MASTERY_PATH = os.path.join(DATA_DIR, "ship_masteries.json")
SDE_URL = ("https://developers.eveonline.com/static-data/"
           "eve-online-static-data-latest-jsonl.zip")
mastery_status = {"state": "idle", "error": "", "done": 0, "total": 0}
_mastery = {"data": None}


def build_masteries(mastery_lines, cert_lines, build=None,
                    group_lines=None, type_lines=None):
    """Compact form: ships {shipId: [tier1certs..tier5certs]} and certs
    {certId: {n: name, g: group, s: {skillId: [req at I..V]}}}. Tier N needs
    every certificate of that tier trained to grade N (basic..elite).
    With groups/types also info {shipId: [ship group, tech level, name]}
    (used by the Accomplishments tab)."""
    groups = {}
    for line in group_lines or []:
        g = json.loads(line)
        groups[g["_key"]] = (g.get("name") or {}).get("en", "")
    ships = {}
    for line in mastery_lines:
        d = json.loads(line)
        tiers = sorted(d["_value"], key=lambda x: x["_key"])
        ships[str(d["_key"])] = [t["_value"] for t in tiers]
    used = {c for tiers in ships.values() for t in tiers for c in t}
    certs = {}
    for line in cert_lines:
        d = json.loads(line)
        if d["_key"] not in used:
            continue
        certs[str(d["_key"])] = {
            "n": d["name"]["en"],
            "g": groups.get(d.get("groupID"), ""),
            "s": {str(sk["_key"]): [sk.get("basic", 0), sk.get("standard", 0),
                                     sk.get("improved", 0), sk.get("advanced", 0),
                                     sk.get("elite", 0)]
                  for sk in d.get("skillTypes", [])}}
    info = {}
    for line in type_lines or []:          # types.jsonl is big: skip non-ships fast
        if isinstance(line, bytes):
            m = re.match(rb'\{"_key":\s*(\d+)', line)
            if not m or m.group(1).decode() not in ships:
                continue
        t = json.loads(line)
        if str(t["_key"]) in ships:
            info[str(t["_key"])] = [groups.get(t.get("groupID"), ""),
                                    t.get("techLevel") or 1,
                                    (t.get("name") or {}).get("en", "")]
    out = {"build": build, "ships": ships, "certs": certs}
    if info:
        out["info"] = info
    return out


BONUS_PATH = os.path.join(DATA_DIR, "ship_bonuses.json")
_bonus = {"data": None}
_UNIT_SUFFIX = {105: "%", 108: "%", 109: "%", 111: "%", 124: "%", 127: "%",
                139: "+", 104: "x", 1: " m"}


def _clean_bonus_text(t):
    t = re.sub(r"<[^>]+>", "", t or "")
    return t.replace("\u00a0", " ").strip()


def build_bonuses(lines):
    """Ship/type bonuses: {typeId: {r: role bonuses, s: [[skillId, bonuses]],
    m: misc}} where a bonus is [value, unit suffix, text]. Skill bonuses are
    'per level' of that skill."""
    def conv(b):
        if not b:
            return None
        text = _clean_bonus_text((b.get("bonusText") or {}).get("en"))
        if not text:
            return None
        return [b.get("bonus"), _UNIT_SUFFIX.get(b.get("unitID"), ""), text]
    out = {}
    for line in lines:
        d = json.loads(line)
        rec = {}
        r = [x for x in map(conv, d.get("roleBonuses", [])) if x]
        if r:
            rec["r"] = r
        sk = []
        for t in d.get("types", []):
            bl = [x for x in map(conv, t["_value"]) if x]
            if bl:
                sk.append([t["_key"], bl])
        if sk:
            rec["s"] = sk
        m = [x for x in map(conv, d.get("miscBonuses", [])) if x]
        if m:
            rec["m"] = m
        if rec:
            out[str(d["_key"])] = rec
    return out


def load_bonuses():
    if _bonus["data"]:
        return _bonus["data"]
    best = None
    for path in (BONUS_PATH, resource_path(os.path.join("assets", "ship_bonuses.json"))):
        d = load_json(path, None)
        if d and d.get("types") and (best is None or (d.get("build") or 0) > (best.get("build") or 0)):
            best = d
    _bonus["data"] = best or {"build": None, "types": {}}
    return _bonus["data"]


def load_masteries():
    if _mastery["data"]:
        return _mastery["data"]
    best = None
    for path in (MASTERY_PATH, resource_path(os.path.join("assets", "ship_masteries.json"))):
        d = load_json(path, None)
        rank = lambda x: ((x.get("build") or 0), bool(x.get("info")))   # noqa: E731
        if d and d.get("ships") and (best is None or rank(d) > rank(best)):
            best = d
    _mastery["data"] = best or {"build": None, "ships": {}, "certs": {}}
    return _mastery["data"]


def refresh_masteries():
    """Download CCP's latest SDE (about 100 MB, once) and rebuild the mastery
    file from it. Runs in the background; progress in mastery_status."""
    import tempfile
    import zipfile
    if mastery_status["state"] == "running":
        return
    mastery_status.update(state="running", error="", done=0, total=0)

    def work():
        tmp = None
        try:
            req = urllib.request.Request(SDE_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                mastery_status["total"] = int(resp.headers.get("Content-Length") or 0)
                fd, tmp = tempfile.mkstemp(suffix=".zip")
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        mastery_status["done"] += len(chunk)
            with zipfile.ZipFile(tmp) as z:
                names = set(z.namelist())
                build = None
                if "_sde.jsonl" in names:
                    build = json.loads(z.read("_sde.jsonl").decode().splitlines()[0]
                                       ).get("buildNumber")
                with z.open("types.jsonl") as types:
                    data = build_masteries(
                        z.read("masteries.jsonl").decode("utf-8").splitlines(),
                        z.read("certificates.jsonl").decode("utf-8").splitlines(), build,
                        z.read("groups.jsonl").decode("utf-8").splitlines(), types)
                bonus = None
                if "typeBonus.jsonl" in names:
                    bonus = {"build": build, "types": build_bonuses(
                        z.read("typeBonus.jsonl").decode("utf-8").splitlines())}
            if not data["ships"]:
                raise RuntimeError("The download had no mastery data.")
            os.makedirs(DATA_DIR, exist_ok=True)
            save_json(MASTERY_PATH, data)
            _mastery["data"] = data
            if bonus and bonus["types"]:
                save_json(BONUS_PATH, bonus)
                _bonus["data"] = bonus
            mastery_status["state"] = "done"
        except Exception as e:  # noqa: BLE001
            mastery_status.update(state="error", error=str(e)[:200])
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    threading.Thread(target=work, daemon=True).start()


type_names = load_json(TYPENAMES_PATH, None)   # type_id(str) -> name


def get_type_names():
    """Names of every type traded in The Forge, for autocomplete.
    Built once (~16 requests), then cached on disk."""
    global type_names
    if not type_names or len(type_names) < 1000:
        tids = esi_fetch_pages(f"/markets/{THE_FORGE}/types/", max_pages=25)
        nm = esi_names(tids)
        type_names = {str(t): nm[t]["name"] for t in set(tids) if t in nm}
        save_json(TYPENAMES_PATH, type_names)
    return type_names


TYPE_STAT_ATTRS = [(14, "High slots"), (13, "Mid slots"), (12, "Low slots"),
                   (1137, "Rig slots"), (102, "Turret hardpoints"),
                   (101, "Launcher hardpoints"), (283, "Drone bay (m³)")]


def _clean_desc(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def type_details(type_id):
    """Info panel data for any type + the skills required to use it."""
    t, _ = esi_fetch(f"/universe/types/{type_id}/")
    g, _ = esi_fetch(f"/universe/groups/{t['group_id']}/")
    c, _ = esi_fetch(f"/universe/categories/{g['category_id']}/")
    dogma = {a["attribute_id"]: a["value"]
             for a in t.get("dogma_attributes", [])}
    required = []
    for sid_attr, lvl_attr in PREREQ_PAIRS:
        if sid_attr in dogma and lvl_attr in dogma:
            required.append([int(dogma[sid_attr]), int(dogma[lvl_attr])])
    skill_names = {}
    for sid, _lvl in required:
        sk = ((catalog or {}).get("skills") or {}).get(str(sid))
        skill_names[str(sid)] = sk["name"] if sk else get_type_info(sid)["name"]
    stats = [[label, int(dogma[a])] for a, label in TYPE_STAT_ATTRS
             if dogma.get(a, 0) > 0]
    return {
        "type_id": type_id, "name": t["name"],
        "category": c["name"], "group": g["name"],
        "description": _clean_desc(t.get("description")),
        "volume": t.get("volume"), "packaged_volume": t.get("packaged_volume"),
        "mass": t.get("mass"), "capacity": t.get("capacity"),
        "stats": stats, "required": required, "skill_names": skill_names,
        "catalog_ready": bool(catalog),
    }


# ---------------------------------------------------------------------------
# Public contract index (ESI can't search contracts by item, so we index them)
# ---------------------------------------------------------------------------

CONTRACTS_PATH = os.path.join(DATA_DIR, "contracts_cache.json")
contract_cache = load_json(CONTRACTS_PATH, {})   # region_id -> {updated, contracts}
contract_status = {"state": "idle", "region_id": None,
                   "done": 0, "total": 0, "error": ""}


def _utc_now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def crawl_contracts(region_id):
    """Index a region's public item-exchange/auction contracts. Items in a
    contract never change, so they're fetched once and cached; later runs
    only fetch contracts that are new and drop ones that have expired."""
    try:
        contract_status.update(state="running", region_id=region_id,
                               done=0, total=0, error="")
        rec = contract_cache.setdefault(str(region_id),
                                        {"updated": 0, "contracts": {}})
        store = rec["contracts"]
        listing = esi_fetch_pages(f"/contracts/public/{region_id}/",
                                  max_pages=400)
        now = _utc_now_iso()
        live = {str(c["contract_id"]): c for c in listing
                if c.get("type") in ("item_exchange", "auction")
                and c.get("date_expired", "") > now}
        for cid in [k for k in store if k not in live]:
            del store[cid]                      # expired or taken
        todo = [cid for cid in live if cid not in store]
        contract_status["total"] = len(todo)
        for n, cid in enumerate(todo, 1):
            c = live[cid]
            try:
                items, _ = esi_fetch(f"/contracts/public/items/{cid}/",
                                     use_cache=False)
            except RuntimeError:
                items = []                      # gone since the listing
            store[cid] = {
                "price": c.get("price") or 0, "buyout": c.get("buyout"),
                "type": c["type"], "title": c.get("title", ""),
                "loc": c.get("start_location_id"), "exp": c["date_expired"],
                "items": [[i["type_id"], i.get("quantity", 1),
                           bool(i.get("is_included")),
                           bool(i.get("is_blueprint_copy")),
                           i.get("material_efficiency", 0),
                           i.get("time_efficiency", 0), i.get("runs", -1)]
                          for i in (items or [])]}
            contract_status["done"] = n
            if n % 300 == 0:
                rec["updated"] = time.time()
                save_json(CONTRACTS_PATH, contract_cache)
            crawl_pause(0.2)
        rec["updated"] = time.time()
        save_json(CONTRACTS_PATH, contract_cache)
        contract_status["state"] = "done"
    except Exception as e:  # noqa: BLE001
        contract_status.update(state="error", error=str(e))


def contract_search(type_id, region_id, limit=150, max_price=None,
                    min_qty=None, ctype=None, bpc_only=False):
    rec = contract_cache.get(str(region_id))
    if not rec or not rec["contracts"]:
        return {"indexed": False}
    now = _utc_now_iso()
    hits = []
    for cid, c in list(rec["contracts"].items()):
        if c["exp"] <= now:
            continue
        included = [i for i in c["items"] if i[2]]
        match = [i for i in included if i[0] == type_id]
        if not match:
            continue
        qty = sum(i[1] for i in match)
        others = len(included) - len(match)
        if max_price and c["price"] > max_price:
            continue
        if min_qty and qty < min_qty:
            continue
        if ctype and c["type"] != ctype:
            continue
        if bpc_only and not any(i[3] for i in match):
            continue
        hits.append({
            "contract_id": int(cid), "type": c["type"], "title": c["title"],
            "price": c["price"], "buyout": c["buyout"], "qty": qty,
            "other_items": others, "expires": c["exp"], "loc": c["loc"],
            "unit_price": (c["price"] / qty) if (others == 0 and qty) else None,
            "bpc": any(i[3] for i in match),
            "me": match[0][4], "te": match[0][5], "runs": match[0][6],
        })
    hits.sort(key=lambda h: (h["unit_price"] is None,
                             h["unit_price"] if h["unit_price"] is not None
                             else h["price"]))
    shown = hits[:limit]
    ids = {h["loc"] for h in shown if h["loc"]}
    names = esi_names([l for l in ids if l < 10 ** 12])
    tok = None
    try:
        if tokens:
            tok = get_access_token(next(iter(tokens)))
    except Exception:  # noqa: BLE001
        tok = None
    for h in shown:
        l = h.pop("loc")
        h["location"] = names.get(l, {}).get("name") or (
            structure_name(l, tok) if (tok and l and l >= 10 ** 12)
            else f"Structure {l}")
    return {"indexed": True, "updated": rec["updated"],
            "total": len(rec["contracts"]), "count": len(hits),
            "hits": shown}


def contract_regions():
    """Regions you can search: those of your saved locations plus any
    that already have an index."""
    names = {str(l["region_id"]): l["region"] for l in locations}
    ids = set(names) | set(contract_cache)
    out = []
    for rid in sorted(ids, key=lambda r: names.get(r, r)):
        if rid not in names:
            try:
                r, _ = esi_fetch(f"/universe/regions/{rid}/")
                names[rid] = r["name"]
            except RuntimeError:
                names[rid] = f"Region {rid}"
        rec = contract_cache.get(rid) or {}
        out.append({"region_id": int(rid), "name": names[rid],
                    "cached": len(rec.get("contracts", {})),
                    "updated": rec.get("updated")})
    if not out:
        out.append({"region_id": THE_FORGE, "name": "The Forge",
                    "cached": 0, "updated": None})
    return out


def search_types(query):
    resp = http_request(ESI + "/universe/ids/",
                        data=json.dumps([query]).encode(),
                        headers={"Content-Type": "application/json"})
    data = json.loads(resp.read().decode())
    return data.get("inventory_types", [])


def stations_in_system(system_name):
    """Resolve a solar system name -> its NPC stations (with region)."""
    resp = http_request(ESI + "/universe/ids/",
                        data=json.dumps([system_name]).encode(),
                        headers={"Content-Type": "application/json"})
    data = json.loads(resp.read().decode())
    systems = data.get("systems", [])
    if not systems:
        return {"error": "No solar system with that exact name."}
    sysid = systems[0]["id"]
    system, _ = esi_fetch(f"/universe/systems/{sysid}/")
    const, _ = esi_fetch(f"/universe/constellations/{system['constellation_id']}/")
    region_id = const["region_id"]
    region, _ = esi_fetch(f"/universe/regions/{region_id}/")
    station_ids = system.get("stations", [])
    names = esi_names(station_ids)
    return {
        "system": systems[0]["name"],
        "region": region["name"],
        "stations": [{
            "station_id": sid,
            "name": names.get(sid, {}).get("name", str(sid)),
            "system": systems[0]["name"],
            "system_id": sysid,
            "region": region["name"],
            "region_id": region_id,
        } for sid in station_ids],
    }


def station_full_info(sid):
    """System + region for an NPC station id, cached for the session."""
    if sid in station_cache:
        return station_cache[sid]
    st, _ = esi_fetch(f"/universe/stations/{sid}/")
    sysid = st["system_id"]
    sysrec, _ = esi_fetch(f"/universe/systems/{sysid}/")
    const, _ = esi_fetch(f"/universe/constellations/{sysrec['constellation_id']}/")
    region, _ = esi_fetch(f"/universe/regions/{const['region_id']}/")
    info = {"system": sysrec["name"], "system_id": sysid,
            "region": region["name"], "region_id": const["region_id"]}
    station_cache[sid] = info
    return info


def bulk_import_stations(text, category):
    """Bookmark-folder-style import: one station name per line."""
    names = list({n.strip() for n in text.splitlines() if n.strip()})
    if not names:
        return {"error": "Paste at least one station name."}
    found = {}
    for i in range(0, len(names), 500):
        resp = http_request(ESI + "/universe/ids/",
                            data=json.dumps(names[i:i + 500]).encode(),
                            headers={"Content-Type": "application/json"})
        for h in json.loads(resp.read().decode()).get("stations", []):
            found[h["name"]] = h["id"]

    added, updated = [], []
    def add_one(name):
        sid = found[name]
        try:
            info = station_full_info(sid)
        except RuntimeError:
            return
        existing = next((l for l in locations if l["station_id"] == sid), None)
        rec = {"station_id": sid, "name": name, "system": info["system"],
               "system_id": info["system_id"], "region": info["region"],
               "region_id": info["region_id"], "category": category}
        if existing:
            locations.remove(existing)
            updated.append(name)
        else:
            added.append(name)
        locations.append(rec)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(add_one, found))
    save_json(LOCATIONS_PATH, locations)
    not_found = [n for n in names if n not in found]
    return {"added": added, "updated": updated, "not_found": not_found,
            "locations": locations}


# Each hub lists name variants in order of preference; the first one ESI
# recognizes is used.
HUB_STATIONS = [
    ["Jita IV - Moon 4 - Caldari Navy Assembly Plant"],
    ["Amarr - Emperor Family Academy",
     "Amarr VIII (Oris) - Emperor Family Academy"],
    ["Dodixie IX - Moon 20 - Federation Navy Assembly Plant"],
    ["Rens VI - Moon 8 - Brutor Tribe Treasury"],
    ["Hek VIII - Moon 12 - Boundless Creation Factory"],
]
HUBS_VERSION = 2
# names that were seeded earlier but no longer exist in the game
HUBS_REMOVED = ["Jarizza II - Moon 1 - Ammatar Fleet Assembly Plant",
                "Esescama VIII - Moon 3 - Imperial Armaments Warehouse"]


def seed_hubs():
    """Adds the built-in trade hubs once. Stations you delete afterwards stay
    deleted (the flag is only set when every hub resolved)."""
    if config.get("hubs_seeded") == HUBS_VERSION:
        return
    try:
        before = len(locations)
        locations[:] = [l for l in locations if l["name"] not in HUBS_REMOVED]
        ok = True
        for variants in HUB_STATIONS:
            if any(l["name"] == variants[0] for l in locations):
                continue
            res = bulk_import_stations("\n".join(variants), "Sales Hub")
            got = [v for v in variants if any(l["name"] == v for l in locations)]
            if not got:
                print("Hub station not found:", variants[0])
                ok = False
                continue
            # keep only the preferred variant that resolved
            for v in variants:
                if v != got[0]:
                    locations[:] = [l for l in locations if l["name"] != v]
        save_json(LOCATIONS_PATH, locations)
        if ok:
            config["hubs_seeded"] = HUBS_VERSION
            save_json(CONFIG_PATH, config)
    except Exception as e:  # noqa: BLE001
        print("Could not add built-in hubs yet:", e)


def add_structure_location(char_id, structure_id, category):
    """Manually add a player-owned structure (refinery, citadel, etc.) by
    ID - public name search can't find these, only lookup-by-known-ID with
    docking access from a character who has visited it."""
    tok = get_access_token(char_id)
    name = structure_name(structure_id, tok)
    if "no docking access" in name:
        return {"error": name}
    s, _ = esi_fetch(f"/universe/structures/{structure_id}/", tok)
    sysid = s["solar_system_id"]
    sysrec, _ = esi_fetch(f"/universe/systems/{sysid}/")
    const, _ = esi_fetch(f"/universe/constellations/{sysrec['constellation_id']}/")
    region, _ = esi_fetch(f"/universe/regions/{const['region_id']}/")
    locations[:] = [l for l in locations if l["station_id"] != structure_id]
    locations.append({"station_id": structure_id, "name": name,
                      "system": sysrec["name"], "system_id": sysid,
                      "region": region["name"], "region_id": const["region_id"],
                      "category": category})
    save_json(LOCATIONS_PATH, locations)
    return {"ok": True, "name": name, "locations": locations}


def compare_locations(type_id):
    """Best sell/buy for a type at every saved station (one region fetch
    per distinct region)."""
    by_region = {}
    for loc in locations:
        by_region.setdefault(loc["region_id"], []).append(loc)
    out = []
    for region_id, locs in by_region.items():
        try:
            rows = esi_fetch_pages(f"/markets/{region_id}/orders/",
                                   params={"type_id": type_id}, max_pages=100)
        except RuntimeError as e:
            for loc in locs:
                out.append({**loc, "error": str(e)})
            continue
        for loc in locs:
            sells = [r["price"] for r in rows if not r["is_buy_order"]
                     and r["location_id"] == loc["station_id"]]
            buys = [r["price"] for r in rows if r["is_buy_order"]
                    and r["location_id"] == loc["station_id"]]
            out.append({**loc,
                        "sell": min(sells) if sells else None,
                        "buy": max(buys) if buys else None,
                        "sell_orders": len(sells), "buy_orders": len(buys)})
    return out


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode() or "{}")

    def do_GET(self):
        try:
            self.route(self.path, "GET")
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            self.route(self.path, "POST")
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def route(self, path, method):
        url = urllib.parse.urlparse(path)
        q = urllib.parse.parse_qs(url.query)
        p = url.path.rstrip("/") or "/"
        if p.startswith("/api/") and p not in (
                "/api/market/index", "/api/contracts/index", "/api/status",
                "/api/heartbeat", "/api/closing", "/api/hubscan/status"):
            last_interactive[0] = time.time()   # let background crawls yield

        if p == "/" and method == "GET":
            self._send(200, PAGE.encode(), "text/html")

        elif p == "/login" and method == "GET":
            if not config.get("client_id"):
                self._send(400, {"error": "Set your Client ID first."})
            else:
                opt = [sc for sc in active_scopes() if sc not in SCOPES]
                if "go" in q or not opt:
                    want = q.get("skip", [""])[0]
                    skip = opt if want == "all" else [x for x in want.split(",") if x in opt]
                    self._redirect(sso_login_url(
                        [sc for sc in active_scopes() if sc not in skip], skip))
                else:
                    self._send(200, login_page(opt).encode(), "text/html")

        elif p == "/callback" and method == "GET":
            name, dropped = handle_callback(q["code"][0], q["state"][0])
            note = ""
            if dropped:
                note = ("<p style='color:#e0b455;max-width:560px;margin:16px auto'>Added without the "
                        + ", ".join(SCOPE_LABELS.get(x, x) for x in dropped)
                        + " permission. Once your developer application lists it, use "
                        "⚙ → Update permissions on the Characters screen.</p>")
            self._send(200, CALLBACK_PAGE.replace("{NAME}", name)
                       .replace("{NOTE}", note)
                       .replace("{WAIT}", "6000" if dropped else "1500").encode(),
                       "text/html")

        elif p == "/api/status" and method == "GET":
            ensure_catalog()
            self._send(200, {
                "client_id_set": bool(config.get("client_id")),
                "version": APP_VERSION, "data_dir": DATA_DIR,
                "quit_on_close": bool(config.get("quit_on_close")),
                "autostart": autostart_enabled(),
                "update_repo": update_repo(),
                "callback_url": CALLBACK_URL,
                "scopes": SCOPES,
                "extra_scopes": EXTRA_SCOPES,
                "scope_extras": bool(config.get("scope_extras")),
                "scope_research": bool(config.get("scope_research")),
                "research_scope": RESEARCH_SCOPE,
                "scope_contracts": bool(config.get("scope_contracts")),
                "contracts_scope": CONTRACTS_SCOPE,
                "characters": [{"character_id": r["character_id"],
                                "name": r["name"],
                                "perm": char_permissions(r)} for r in tokens.values()],
                "catalog": ({"state": "done",
                             "skills": len(catalog["skills"])}
                            if catalog else dict(catalog_status)),
            })

        elif p == "/api/config" and method == "POST":
            body = self._json_body()
            if "client_id" in body:
                config["client_id"] = body["client_id"].strip()
            if "scope_extras" in body:
                config["scope_extras"] = bool(body["scope_extras"])
            if "scope_research" in body:
                config["scope_research"] = bool(body["scope_research"])
            if "scope_contracts" in body:
                config["scope_contracts"] = bool(body["scope_contracts"])
            if "quit_on_close" in body:
                config["quit_on_close"] = bool(body["quit_on_close"])
            save_json(CONFIG_PATH, config)
            self._send(200, {"ok": True})

        elif p == "/api/catalog" and method == "GET":
            ensure_catalog()
            if catalog:
                self._send(200, catalog)
            else:
                self._send(202, dict(catalog_status))

        elif p.startswith("/api/character/") :
            parts = p.split("/")
            char_id, sub = parts[3], (parts[4] if len(parts) > 4 else "summary")
            if str(char_id) not in tokens:
                self._send(404, {"error": "Character not found."})
                return
            if sub == "remove" and method == "POST":
                tokens.pop(str(char_id), None)
                save_json(TOKENS_PATH, tokens)
                self._send(200, {"ok": True})
            elif sub == "summary":
                self._send(200, char_summary(char_id))
            elif sub == "card":
                self._send(200, char_card(char_id))
            elif sub == "assets":
                self._send(200, char_assets(char_id))
            elif sub == "journal":
                self._send(200, char_journal(char_id))
            elif sub == "loyalty":
                self._send(200, char_loyalty(char_id))
            elif sub == "research":
                self._send(200, char_research(char_id))
            elif sub == "standings":
                self._send(200, char_standings(char_id))
            elif sub == "blueprints":
                self._send(200, char_blueprints(char_id))
            elif sub == "jobs":
                self._send(200, char_jobs(char_id))
            elif sub == "contracts":
                self._send(200, char_contracts(char_id))
            elif sub == "contract" and len(parts) > 5 and parts[5].isdigit():
                self._send(200, char_contract_items(
                    char_id, int(parts[5]), q.get("type", [""])[0]))
            else:
                self._send(404, {"error": "Unknown endpoint."})

        elif p == "/api/plan/compute" and method == "POST":
            body = self._json_body()
            if not catalog:
                self._send(400, {"error": "Skill catalog still loading."})
                return
            entries = body.get("entries", [])
            trained = {str(k): v["level"] for k, v in
                       body.get("skills", {}).items()}
            trained_sp = {str(k): v["sp"] for k, v in
                          body.get("skills", {}).items()}
            attrs = body.get("attributes") or {}
            attrs = {k: v or 20 for k, v in attrs.items()} or \
                {n: 20 for n in CHAR_ATTR.values()}
            steps, total_sp, total_min = compute_plan(
                entries, trained, trained_sp, attrs)
            remap = (best_remap(entries, trained, trained_sp)
                     if body.get("remap", True) else None)
            self._send(200, {
                "steps": steps, "total_sp": total_sp,
                "total_minutes": round(total_min, 1),
                "remap_minutes": round(remap[0], 1) if remap else None,
                "remap": remap[1] if remap else None,
            })

        elif p == "/api/plans" and method == "GET":
            self._send(200, plans)

        elif p == "/api/plans" and method == "POST":
            body = self._json_body()
            name = body.get("name", "").strip()
            if body.get("delete"):
                plans.pop(name, None)
            elif name:
                plans[name] = body.get("entries", [])
            save_json(PLANS_PATH, plans)
            self._send(200, plans)

        elif p == "/api/market/search" and method == "GET":
            self._send(200, search_types(q.get("q", [""])[0]))

        elif p == "/api/market/orders" and method == "GET":
            tid = int(q["type_id"][0])
            result = jita_orders(tid)
            result["average"] = market_prices().get(tid)
            self._send(200, result)

        elif p == "/api/market/stations" and method == "GET":
            self._send(200, stations_in_system(q.get("system", [""])[0].strip()))

        elif p == "/api/market/locations" and method == "GET":
            self._send(200, locations)

        elif p == "/api/market/locations" and method == "POST":
            body = self._json_body()
            sid = body.get("station_id")
            locations[:] = [l for l in locations if l["station_id"] != sid]
            if not body.get("delete"):
                rec = {k: body[k] for k in
                       ["station_id", "name", "system", "region", "region_id"]}
                rec["category"] = body.get("category") or "Sales Hub"
                if body.get("system_id"):
                    rec["system_id"] = body["system_id"]
                locations.append(rec)
            save_json(LOCATIONS_PATH, locations)
            self._send(200, locations)

        elif p == "/api/market/locations/bulk" and method == "POST":
            body = self._json_body()
            self._send(200, bulk_import_stations(
                body.get("text", ""), body.get("category") or "Other"))

        elif p == "/api/market/locations/structure" and method == "POST":
            body = self._json_body()
            self._send(200, add_structure_location(
                body["char_id"], int(body["structure_id"]),
                body.get("category") or "Other"))

        elif p == "/api/market/typeinfo" and method == "GET":
            self._send(200, type_details(int(q["type_id"][0])))

        elif p == "/api/market/typenames" and method == "GET":
            self._send(200, get_type_names())

        elif p == "/api/contracts/index" and method == "GET":
            rid = q.get("region_id", [""])[0]
            rec = contract_cache.get(rid) or {}
            self._send(200, {**contract_status,
                             "cached": len(rec.get("contracts", {})),
                             "updated": rec.get("updated")})

        elif p == "/api/contracts/index" and method == "POST":
            body = self._json_body()
            if contract_status["state"] != "running":
                threading.Thread(target=crawl_contracts,
                                 args=(int(body["region_id"]),),
                                 daemon=True).start()
            self._send(200, {"ok": True})

        elif p == "/api/contracts/regions" and method == "GET":
            self._send(200, contract_regions())

        elif p == "/api/contracts/search" and method == "GET":
            def _num(k):
                v = q.get(k, [""])[0]
                return float(v) if v else None
            self._send(200, contract_search(
                int(q["type_id"][0]), int(q["region_id"][0]),
                max_price=_num("max_price"), min_qty=_num("min_qty"),
                ctype=q.get("ctype", [""])[0] or None,
                bpc_only=q.get("bpc", [""])[0] == "1"))

        elif p == "/api/market/index" and method == "GET":
            self._send(200, {**crawl_status,
                             "index_date": config.get("index_date")})

        elif p == "/api/market/index" and method == "POST":
            if crawl_status["state"] != "running":
                threading.Thread(target=crawl_index, daemon=True).start()
            self._send(200, {"ok": True})

        elif p == "/api/market/trade_scan" and method == "GET":
            self._send(200, trade_scan(
                int(q["type_id"][0]),
                mpj=float(q.get("mpj", ["1.2"])[0] or 1.2),
                cargo=float(q.get("cargo", ["5000"])[0] or 5000),
                tax=float(q.get("tax", ["5"])[0] or 5) / 100,
                from_cat=q.get("from_cat", [""])[0] or None,
                to_cat=q.get("to_cat", [""])[0] or None))

        elif p == "/api/market/movers" and method == "GET":
            mode = q.get("mode", ["overview"])[0]
            cid = q.get("char_id", [None])[0]
            thr = float(q.get("threshold", ["10"])[0] or 10)
            if mode == "owned" and (not cid or str(cid) not in tokens):
                self._send(400, {"error": "Add a character first."})
            else:
                self._send(200, market_movers(mode, cid, thr))

        elif p == "/api/market/appraise_resolve" and method == "POST":
            self._send(200, appraise_resolve(self._json_body().get("text", "")))

        elif p == "/api/market/appraise_batch" and method == "POST":
            body = self._json_body()
            self._send(200, appraise_batch(
                body.get("station_id"),
                [int(t) for t in body.get("type_ids", [])][:40]))

        elif p == "/api/heartbeat" and method == "POST":
            tab_alive(self._json_body().get("tab", "?"))
            self._send(200, {"ok": True})

        elif p == "/api/closing" and method == "POST":
            tab_closed(self._json_body().get("tab", "?"))
            self._send(200, {"ok": True})

        elif p == "/api/masteries" and method == "GET":
            self._send(200, load_masteries())

        elif p == "/api/bonuses" and method == "GET":
            self._send(200, load_bonuses())

        elif p == "/api/masteries/refresh" and method == "POST":
            refresh_masteries()
            self._send(200, dict(mastery_status))

        elif p == "/api/masteries/status" and method == "GET":
            self._send(200, {**mastery_status,
                             "build": load_masteries().get("build")})

        elif p == "/api/update" and method == "GET":
            self._send(200, check_update(force="force" in q))

        elif p == "/api/notify" and method == "GET":
            self._send(200, notify_status())

        elif p == "/api/notify/config" and method == "POST":
            b = self._json_body()
            if "enabled" in b:
                config["notify_on"] = bool(b["enabled"])
            if "os" in b:
                config["notify_os"] = bool(b["os"])
            if isinstance(b.get("kinds"), dict):
                cur = dict(config.get("notify") or {})
                for k, _ in NOTIFY_KINDS:
                    if k in b["kinds"]:
                        cur[k] = bool(b["kinds"][k])
                config["notify"] = cur
            if "check_minutes" in b and int(b["check_minutes"]) in CHECK_CHOICES:
                config["check_minutes"] = int(b["check_minutes"])
            if "queue_low_hours" in b:
                config["queue_low_hours"] = max(1, min(float(b["queue_low_hours"]), 720))
            save_json(CONFIG_PATH, config)
            self._send(200, notify_status())

        elif p == "/api/notify/test" and method == "POST":
            push_notification("test", "EveWatch test notification",
                              "If you can see this, notifications work.")
            self._send(200, notify_status())

        elif p == "/api/notify/check" and method == "POST":
            _notify_force.set()
            self._send(200, {"ok": True})

        elif p == "/api/notify/clear" and method == "POST":
            with _notif_lock:
                notif_feed.clear()
                save_json(NOTIFY_PATH, notif_feed)
            self._send(200, notify_status())

        elif p == "/api/autostart" and method == "POST":
            try:
                set_autostart(bool(self._json_body().get("on")))
                self._send(200, {"ok": True, "autostart": autostart_enabled()})
            except Exception as e:
                self._send(500, {"error": f"Could not change it: {e}"})

        elif p == "/api/open_data" and method == "POST":
            open_folder(DATA_DIR)
            self._send(200, {"ok": True})

        elif p == "/api/quit" and method == "POST":
            self._send(200, {"ok": True})
            request_shutdown()

        elif p == "/api/hubscan/taxonomy" and method == "GET":
            if "category" in q:
                self._send(200, taxonomy_groups(int(q["category"][0])))
            else:
                self._send(200, taxonomy_categories())

        elif p == "/api/hubscan/start" and method == "POST":
            if hubscan["state"] == "running":
                self._send(200, {k: v for k, v in hubscan.items()})
            else:
                b = self._json_body()
                threading.Thread(target=hub_scan_run, daemon=True, args=(
                    [int(x) for x in b.get("stations", [])],
                    [int(x) for x in b.get("categories", [])],
                    [int(x) for x in b.get("groups", [])])).start()
                hubscan.update(state="running", phase="Starting", done=0, total=0, error="")
                self._send(200, {k: v for k, v in hubscan.items()})

        elif p == "/api/hubscan/cancel" and method == "POST":
            hubscan["cancel"] = True
            self._send(200, {"ok": True})

        elif p == "/api/hubscan/status" and method == "GET":
            self._send(200, {**hubscan, "finished": hubscan_result.get("finished"),
                             "scope": hubscan_result.get("scope"),
                             "stations": hubscan_result.get("stations")})

        elif p == "/api/hubscan/result" and method == "GET":
            self._send(200, hubscan_result)

        elif p == "/api/lp/offers" and method == "GET":
            self._send(200, lp_offers(q["corp_id"][0]))

        elif p == "/api/lp/corp" and method == "GET":
            self._send(200, lp_corp_search(q.get("q", [""])[0]))

        elif p == "/api/market/compare" and method == "GET":
            tid = int(q["type_id"][0])
            self._send(200, {"rows": compare_locations(tid),
                             "average": market_prices().get(tid)})

        else:
            self._send(404, {"error": "Not found"})


CALLBACK_PAGE = """<!doctype html><html><body style="background:#0b0e14;
color:#c8d3e0;font-family:sans-serif;text-align:center;padding-top:80px">
<h2>{NAME} added ✔</h2>{NOTE}<p>You can close this tab and return to EveWatch.</p>
<script>setTimeout(()=>location.href='/',{WAIT})</script></body></html>"""

def login_page(opt):
    """EveWatch's step before EVE's login. EVE answers a permission that the
    developer application doesn't list (yet) with its own "invalid_scope"
    page, which EveWatch can't see - so this page offers the way out."""
    lab = lambda sc: SCOPE_LABELS.get(sc, sc)          # noqa: E731
    skips = "".join(
        f'<a class="b2" href="/login?go=1&skip={urllib.parse.quote(sc)}" onclick="done()">'
        f'Log in without {lab(sc)}</a>' for sc in opt)
    return LOGIN_PAGE.replace("{OPT}", ", ".join(lab(sc) for sc in opt)) \
                     .replace("{SKIPS}", skips)


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>EveWatch - log in</title>
<style>body{background:#0b0e14;color:#c8d3e0;font-family:"Segoe UI",system-ui,sans-serif;
text-align:center;padding:70px 16px} .box{max-width:620px;margin:0 auto}
a.b{display:inline-block;background:#58a6ff;color:#08131f;padding:10px 18px;border-radius:7px;
font-weight:600;text-decoration:none;margin:8px} a.b2{display:inline-block;background:#0e1420;
color:#c8d3e0;border:1px solid #263349;padding:7px 13px;border-radius:7px;text-decoration:none;
margin:4px} a{color:#58a6ff} code{background:#0e1420;padding:2px 6px;border-radius:4px}
#fb{margin-top:34px;padding:14px 18px;border:1px solid #263349;border-radius:9px;
font-size:14px;line-height:1.5} #fb.back{border-color:#e0b455;box-shadow:0 0 0 1px #e0b455}
#fb h3{margin:0 0 6px;font-size:15px;color:#e0b455}</style></head><body><div class="box">
<h2>Log in with EVE</h2>
<p>EveWatch asks for the standard permissions plus the optional ones you switched on:
<b>{OPT}</b>.</p>
<a class="b" href="/login?go=1" onclick="sessionStorage.setItem('evwLogin','1')">Continue to EVE's login</a>
<div id="fb"><h3 id="fbh">If EVE answers with "invalid_scope"</h3>
That means your developer application at developers.eveonline.com doesn't list that
permission yet (or the change hasn't reached EVE's login - that can take a few minutes).
Press your browser's <b>Back</b> button to return here and add the character without it:
<div style="margin-top:10px">{SKIPS}
<a class="b2" href="/login?go=1&skip=all" onclick="done()">Standard permissions only</a></div>
<div style="margin-top:8px;color:#7a8aa0;font-size:13px">You can add it later with
⚙ → Update permissions on the Characters screen.</div></div>
<p style="margin-top:22px"><a href="/">Cancel</a></p></div>
<script>function done(){try{sessionStorage.removeItem('evwLogin')}catch(e){}}
window.addEventListener('pageshow',()=>{let b=null;try{b=sessionStorage.getItem('evwLogin')}catch(e){}
if(b){document.getElementById('fb').className='back';
document.getElementById('fbh').textContent='Back from EVE without finishing the login? If it said "invalid_scope":';}});
</script></body></html>"""


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>EveWatch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#0b0e14; --panel:#121826; --panel2:#0e1420; --line:#263349;
  --txt:#c8d3e0; --dim:#7a8aa0; --acc:#58a6ff; --acc-ink:#08131f; --gold:#e0b455;
  --good:#4cc38a; --bad:#e5534b; --warn:#e0b455;
  --radius:12px; --radius-sm:6px; --shadow:0 4px 18px rgba(0,0,0,.35); --blur:0px;
  --font:"Segoe UI",system-ui,sans-serif; --h2-transform:uppercase;
  --h2-spacing:.5px; --h2-size:15px; --gap:34px;
  --hdr:linear-gradient(180deg,#16203a,#121826);
  --stage-bg:radial-gradient(1100px 700px at 85% -180px,#131c30 0%,#0b0e14 60%),#0b0e14;
}
/* ---- themes: body (active character) and .themed cards (select screen) ---- */
.themed{--radius:12px;--radius-sm:6px;--shadow:0 4px 18px rgba(0,0,0,.35);--blur:0px;
  --font:"Segoe UI",system-ui,sans-serif;--h2-transform:uppercase;--h2-spacing:.5px;
  --h2-size:15px;--gap:34px;--pop:#0e1420;color:var(--txt);font-family:var(--font)}
:is(body,.themed)[data-style=classic][data-mode=dark]{--bg:#0b0e14;--panel:#121826;--panel2:#0e1420;--line:#263349;
  --txt:#c8d3e0;--dim:#7a8aa0;--acc:#58a6ff;--acc-ink:#08131f;--gold:#e0b455;--good:#4cc38a;--bad:#e5534b;--warn:#e0b455}
:is(body,.themed)[data-style=classic][data-mode=light]{--bg:#eef1f6;--panel:#ffffff;--panel2:#f3f5f9;--line:#d5dbe6;
  --txt:#1d2736;--dim:#6a778b;--acc:#1f6feb;--acc-ink:#fff;--gold:#a8710c;--good:#1a8f5c;--bad:#cc3b33;--warn:#a8710c;
  --shadow:0 4px 16px rgba(40,60,100,.10);--hdr:linear-gradient(180deg,#ffffff,#f1f4fa);
  --stage-bg:radial-gradient(1100px 700px at 85% -180px,#dfe6f2 0%,#eef1f6 60%),#eef1f6}
:is(body,.themed)[data-style=minimal]{--radius:8px;--radius-sm:6px;--font:Inter,"Segoe UI",system-ui,sans-serif;
  --h2-size:13px;--h2-transform:none;--h2-spacing:0;--gap:24px;--shadow:none}
:is(body,.themed)[data-style=minimal][data-mode=dark]{--panel:#16181d;--panel2:#1b1e24;--line:#262a31;
  --txt:#e6e8ec;--dim:#8b919c;--acc:#6ea8fe;--acc-ink:#0b1220;--gold:#e6e8ec;--good:#58c78f;--bad:#ef6b63;--warn:#e6e8ec;
  --hdr:#0f1115;--stage-bg:#0f1115}
:is(body,.themed)[data-style=minimal][data-mode=light]{--panel:#ffffff;--panel2:#f5f5f6;--line:#e6e7ea;
  --txt:#16181d;--dim:#737985;--acc:#2563eb;--acc-ink:#fff;--gold:#16181d;--good:#12805c;--bad:#c93c37;--warn:#16181d;
  --hdr:#fafafa;--stage-bg:#fafafa}
:is(body,.themed)[data-style=tactical]{--radius:2px;--radius-sm:2px;--font:Consolas,"Cascadia Mono","Courier New",monospace;
  --h2-size:13px;--h2-spacing:1.5px;--gap:26px;--shadow:none}
:is(body,.themed)[data-style=tactical][data-mode=dark]{--panel:#0a110c;--panel2:#070c08;--line:#1f3a26;
  --txt:#b6f2c2;--dim:#5e8f6b;--acc:#2ee66b;--acc-ink:#04120a;--gold:#ffb000;--good:#2ee66b;--bad:#ff4d3d;--warn:#ffb000;
  --hdr:#070c08;--stage-bg:repeating-linear-gradient(0deg,rgba(46,230,107,.045) 0 1px,transparent 1px 3px),#060a07}
:is(body,.themed)[data-style=tactical][data-mode=light]{--panel:#f6f3e6;--panel2:#ece8d6;--line:#8a8666;
  --txt:#1e2a1f;--dim:#6a6d55;--acc:#1f5a2e;--acc-ink:#f6f3e6;--gold:#8a5a00;--good:#1f6b2e;--bad:#a8261b;--warn:#8a5a00;
  --hdr:#ece8d6;--stage-bg:repeating-linear-gradient(0deg,rgba(60,60,30,.05) 0 1px,transparent 1px 3px),#e9e6d8}
:is(body,.themed)[data-style=glass]{--radius:18px;--radius-sm:12px;--blur:14px;--h2-spacing:.3px;--gap:30px}
:is(body,.themed)[data-style=glass][data-mode=dark]{--panel:rgba(255,255,255,.07);--panel2:rgba(255,255,255,.06);--line:rgba(255,255,255,.15);
  --txt:#eceaff;--dim:#a9acd0;--acc:#8b7bff;--acc-ink:#fff;--gold:#7ee7ff;--good:#5ef2b0;--bad:#ff7a8a;--warn:#7ee7ff;
  --shadow:0 10px 30px rgba(0,0,0,.35);--hdr:rgba(255,255,255,.06);
  --stage-bg:radial-gradient(900px 600px at 10% -10%,#4a2bb0 0%,transparent 60%),radial-gradient(800px 600px at 100% 10%,#0b6e8f 0%,transparent 60%),linear-gradient(135deg,#150d33,#0a1f3d 60%,#0a2f33)}
:is(body,.themed)[data-style=glass][data-mode=light]{--panel:rgba(255,255,255,.6);--panel2:rgba(255,255,255,.55);--line:rgba(255,255,255,.9);
  --txt:#25264a;--dim:#6a6c93;--acc:#6c5ce7;--acc-ink:#fff;--gold:#4b3fc9;--good:#0f9d6b;--bad:#d6455d;--warn:#4b3fc9;
  --shadow:0 10px 30px rgba(90,80,170,.18);--hdr:rgba(255,255,255,.55);
  --stage-bg:radial-gradient(900px 600px at 10% -10%,#d9c9ff 0%,transparent 60%),radial-gradient(800px 600px at 100% 10%,#bfe9ff 0%,transparent 60%),linear-gradient(135deg,#efe6ff,#dff1ff 60%,#e1f8ee)}
:is(body,.themed)[data-style=blueprint]{--radius:2px;--radius-sm:2px;--h2-spacing:2px;--gap:30px;--shadow:none}
:is(body,.themed)[data-style=blueprint][data-mode=dark]{--panel:rgba(10,49,87,.88);--panel2:rgba(255,255,255,.05);--line:#5fa8e6;
  --txt:#e6f3ff;--dim:#9cc3e8;--acc:#ffd24a;--acc-ink:#0a3157;--gold:#ffffff;--good:#7dffb5;--bad:#ff9a8a;--warn:#ffd24a;
  --hdr:rgba(8,40,72,.95);
  --stage-bg:linear-gradient(rgba(255,255,255,.07) 1px,transparent 1px) 0 0/24px 24px,linear-gradient(90deg,rgba(255,255,255,.07) 1px,transparent 1px) 0 0/24px 24px,#0a3157}
:is(body,.themed)[data-style=blueprint][data-mode=light]{--panel:rgba(255,255,255,.92);--panel2:rgba(30,100,180,.05);--line:#2a6fb5;
  --txt:#123a63;--dim:#5b7fa3;--acc:#1b5fa8;--acc-ink:#fff;--gold:#1b5fa8;--good:#137a52;--bad:#c0392b;--warn:#1b5fa8;
  --hdr:rgba(255,255,255,.96);
  --stage-bg:linear-gradient(rgba(30,100,180,.12) 1px,transparent 1px) 0 0/24px 24px,linear-gradient(90deg,rgba(30,100,180,.12) 1px,transparent 1px) 0 0/24px 24px,#f7fbff}

:is(body,.themed)[data-mode=light]{--pop:#ffffff}
:is(body,.themed)[data-style=minimal][data-mode=dark]{--pop:#1b1e24}
:is(body,.themed)[data-style=tactical][data-mode=dark]{--pop:#070c08}
:is(body,.themed)[data-style=tactical][data-mode=light]{--pop:#f6f3e6}
:is(body,.themed)[data-style=glass][data-mode=dark]{--pop:#1c1640}
:is(body,.themed)[data-style=glass][data-mode=light]{--pop:#f4f0ff}
:is(body,.themed)[data-style=blueprint][data-mode=dark]{--pop:#0c3a69}
*{box-sizing:border-box}
body{margin:0;color:var(--txt);
  font:13.5px/1.5 var(--font);
  background:var(--stage-bg) fixed;min-height:100vh}
header{display:flex;align-items:center;gap:16px;padding:10px 20px;
  background:var(--hdr);backdrop-filter:blur(var(--blur));
  border-bottom:1px solid var(--line);
  box-shadow:var(--shadow)}
header h1{font-size:18px;margin:0;color:var(--gold);letter-spacing:2px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav button{background:none;border:none;color:var(--dim);padding:8px 11px;white-space:nowrap;
  cursor:pointer;font-size:14px;border-radius:var(--radius-sm)}
nav button.active{color:var(--txt);background:var(--panel2);
  outline:1px solid var(--line)}
nav button:hover{color:var(--txt)}
main{padding:24px;max-width:1720px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:18px 20px;margin-bottom:var(--gap);
  box-shadow:var(--shadow);backdrop-filter:blur(var(--blur))}
.card{position:relative;z-index:0}
.card:focus-within{z-index:30}
.card.activechar{outline:2px solid var(--acc);outline-offset:-1px}
.card.collapsed > :not(h2){display:none !important}
.card.collapsed{padding-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(370px,1fr));
  gap:16px}
h2{font-size:var(--h2-size);margin:0 0 14px;color:var(--gold);
  border-left:3px solid var(--gold);padding-left:9px;
  letter-spacing:var(--h2-spacing);text-transform:var(--h2-transform);
  border-bottom:1px solid var(--line);padding-bottom:9px}
.card>h2{cursor:pointer;user-select:none}
.card>h2::after{content:"▾";float:right;color:var(--dim);font-size:12px}
.card.collapsed>h2::after{content:"▸"}
.card.collapsed>h2{margin-bottom:0;border-bottom:none}
h3{font-size:13.5px;margin:12px 0 6px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--dim);text-align:left;font-weight:600;padding:5px 9px;
  border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:4px 9px;border-bottom:1px solid var(--panel2)}
tr:hover td{background:var(--panel2)}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill b{color:var(--gold)}
.chip{display:inline-block;padding:0 8px;border-radius:9px;font-size:11.5px;
  background:var(--panel2);outline:1px solid var(--line);white-space:nowrap}
.chip-fit{color:var(--acc)}
.chip-cargo{color:var(--gold)}
.alertbox{border:1px solid var(--line);border-left:3px solid var(--gold);
  background:var(--panel2);border-radius:var(--radius-sm);padding:8px 12px;
  margin:6px 0;font-size:13px}
.alertbox.up{border-left-color:var(--good)}
.alertbox.down{border-left-color:var(--bad)}
.pct-up{color:var(--good);white-space:nowrap}
.pct-down{color:var(--bad);white-space:nowrap}
.sbox{position:relative;display:inline-block;vertical-align:top}
.suggest{position:absolute;top:calc(100% + 6px);left:0;z-index:60;
  background:linear-gradient(var(--panel2),var(--panel2)),var(--pop,#0e1420);border:1px solid var(--line);border-radius:8px;
  max-height:280px;overflow:auto;min-width:300px;
  box-shadow:0 8px 24px rgba(0,0,0,.5)}
.suggest div{padding:6px 12px;cursor:pointer;white-space:nowrap;
  font-size:13px}
.suggest div:hover,.suggest div.sel{background:var(--panel);
  color:var(--acc)}
input,select{background:var(--panel2);border:1px solid var(--line);
  color:var(--txt);padding:7px 10px;border-radius:6px;font-size:14px}
input:focus,select:focus{outline:1px solid var(--acc)}
button.btn{background:var(--acc);color:var(--acc-ink);border:none;padding:8px 14px;
  border-radius:6px;cursor:pointer;font-weight:600;font-size:14px}
button.btn.sec{background:var(--panel2);color:var(--txt);
  outline:1px solid var(--line)}
button.btn.danger{background:var(--bad);color:#fff}
button.btn:hover{filter:brightness(1.15)}
.dim{color:var(--dim)} .good{color:var(--good)} .bad{color:var(--bad)}
.warn{color:var(--warn)}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;
  background:var(--panel2);outline:1px solid var(--line);font-size:12px}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--acc)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.portrait{width:56px;height:56px;border-radius:8px;
  outline:1px solid var(--line)}
header{position:relative;z-index:100}
nav button.locked{opacity:.38;cursor:not-allowed}
.cs-empty.off{opacity:.4;cursor:not-allowed}
.cs-empty.off:hover{border-color:var(--line);color:var(--dim);background:transparent}
.hchip{display:flex;align-items:center;gap:8px;font-weight:600}
.hchip img{width:30px;height:30px;border-radius:var(--radius-sm);outline:1px solid var(--acc)}
.cs-wrap{padding-top:10px}
.cs-title{text-align:center;margin:0 0 26px}
.cs-title h1{margin:0;font-size:22px;letter-spacing:7px;color:var(--gold)}
.cs-title p{margin:4px 0 0;color:var(--dim)}
.cs-row{display:flex;gap:18px;justify-content:center;flex-wrap:wrap}
.cs-slot{position:relative;width:290px}
.cs-strip{background:var(--panel2);border:1px solid var(--line);padding:6px 12px;font-weight:700;
  letter-spacing:1.5px;text-transform:var(--h2-transform);font-size:13px;color:var(--gold);
  border-radius:var(--radius-sm) var(--radius-sm) 0 0;display:flex;align-items:center;gap:8px;
  white-space:nowrap;overflow:hidden}
.cs-strip .nm{flex:1;overflow:hidden;text-overflow:ellipsis}
.cs-dot{width:9px;height:9px;border-radius:50%;background:var(--acc);flex:none}
.cs-gear{cursor:pointer;color:var(--dim);font-size:15px;padding:0 4px;border-radius:4px;background:none;border:0}
.cs-gear:hover{color:var(--acc)}
.cs-card{background:var(--panel);border:1px solid var(--line);border-top:0;
  border-radius:0 0 var(--radius) var(--radius);box-shadow:var(--shadow);
  backdrop-filter:blur(var(--blur));cursor:pointer;transition:.15s;overflow:hidden}
.cs-slot:hover .cs-card{outline:2px solid var(--acc);outline-offset:-1px}
.cs-portrait{aspect-ratio:1/1;background:var(--panel2);position:relative}
.cs-portrait img{width:100%;height:100%;display:block;object-fit:cover}
.cs-train{margin:8px;padding:6px 10px;background:var(--panel2);border:1px solid var(--line);
  border-radius:var(--radius-sm);font-size:12.5px;display:flex;justify-content:space-between;
  gap:8px;position:relative;overflow:hidden}
.cs-train i{position:absolute;left:0;top:0;bottom:0;background:color-mix(in srgb,var(--acc) 28%,transparent)}
.cs-train span{position:relative;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cs-kv{margin:0 8px;font-size:12.5px}
.cs-kv div{display:flex;justify-content:space-between;padding:4px;border-bottom:1px solid var(--line)}
.cs-kv div:last-child{border:0}
.cs-ship{margin:8px;border:1px solid var(--line);background:var(--panel2);border-radius:var(--radius-sm);
  padding:8px 10px;text-align:center;min-height:96px}
.cs-ship img{height:62px;display:block;margin:0 auto 4px}
.cs-loc{padding:10px;text-align:center;font-size:12.5px;border-top:1px solid var(--line)}
.cs-loc b{font-size:15px}
.cs-loc .dim{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cs-empty{width:290px;min-height:560px;border:1.5px dashed var(--line);border-radius:var(--radius);
  display:grid;place-items:center;cursor:pointer;color:var(--dim);transition:.15s}
.cs-empty:hover{border-color:var(--acc);color:var(--acc);background:var(--panel2)}
.cs-empty .plus{font-size:64px;font-weight:200;line-height:1;opacity:.55;text-align:center}
.cs-empty:hover .plus{opacity:1}
.cs-empty small{display:block;text-align:center;letter-spacing:1.5px;text-transform:uppercase;font-size:11.5px;margin-top:6px}
.cs-pop{position:absolute;z-index:50;right:6px;top:34px;width:230px;background:linear-gradient(var(--panel2),var(--panel2)),var(--pop,#0e1420);
  border:1px solid var(--line);border-radius:var(--radius-sm);box-shadow:0 10px 28px rgba(0,0,0,.45);
  padding:12px;font-size:12.5px;color:var(--txt)}
.cs-pop h4{margin:0 0 8px;font-size:11.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim)}
.cs-pop select,.cs-pop button{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);
  padding:6px 8px;border-radius:var(--radius-sm);font:inherit;margin-bottom:8px;cursor:pointer}
.cs-pop button.danger{color:var(--bad);margin:0}
.cs-pop .sw{display:flex;gap:6px;margin-bottom:8px}
.cs-pop .sw button{margin:0}
.cs-pop .sw button.on{background:var(--acc);color:var(--acc-ink);border-color:var(--acc)}
.cs-perm{font-size:12px;color:var(--warn);padding:6px 10px 2px;line-height:1.4}
.cs-perm a{color:var(--acc)}
.cs-foot{text-align:center;margin-top:28px;color:var(--dim);font-size:12.5px}
/* Accomplishments: certificate / mastery level colours (I..V) */
body{--l1:#8592a6;--l2:#3fb27f;--l3:#4a90e2;--l4:#a274f0;--l5:#e0b455}
body[data-mode=light]{--l1:#7a8699;--l2:#1a8f5c;--l3:#1f6feb;--l4:#7a3fe0;--l5:#a8710c}
.lv1{color:var(--l1)} .lv2{color:var(--l2)} .lv3{color:var(--l3)} .lv4{color:var(--l4)} .lv5{color:var(--l5)}
.ac-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:var(--gap)}
.ac-tile{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px;box-shadow:var(--shadow)}
.ac-tile .k{font-size:11.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim)}
.ac-tile .v{font-size:26px;font-weight:700;line-height:1.25}
.ac-tile .s{font-size:12px;color:var(--dim)}
.ac-top{display:grid;grid-template-columns:1.3fr 1fr;gap:var(--gap)}
.ac-top>.card{margin-bottom:var(--gap)}
@media(max-width:1050px){.ac-top{grid-template-columns:1fr}}
.ac-sub{margin:-4px 0 12px;font-size:12.5px;color:var(--dim)}
.ac-medals{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:10px}
.ac-medal{text-align:center;padding:12px 6px;border-radius:var(--radius-sm);background:var(--panel2);border:1px solid var(--line)}
.ac-medal .disc{width:52px;height:52px;margin:0 auto 6px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:16px;
  color:#0b0e14;box-shadow:inset 0 -4px 0 rgba(0,0,0,.25),0 0 0 3px var(--panel),0 0 0 4px var(--line)}
.ac-medal .t{font-size:12.5px;font-weight:600;line-height:1.3}
.ac-medal .d{font-size:11.5px;color:var(--dim)}
.ac-medal.locked{opacity:.45;filter:grayscale(1)}
.ac-next .row{display:flex;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);margin:0}
.ac-next .grow{flex:1;min-width:0}
.ac-pips{display:inline-flex;gap:3px}
.ac-pips i{width:16px;height:6px;border-radius:2px;background:var(--panel2);outline:1px solid var(--line)}
.ac-filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.ac-chip{padding:3px 11px;border-radius:12px;background:var(--panel2);outline:1px solid var(--line);border:0;color:var(--txt);font:inherit;font-size:12.5px;cursor:pointer}
.ac-chip.on{background:var(--acc);color:var(--acc-ink);outline-color:var(--acc)}
.ac-group{margin-bottom:16px}
.ac-group h3{margin:0 0 8px;font-size:12px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim)}
.ac-cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
.ac-cert{background:var(--panel2);border:1px solid var(--line);border-radius:var(--radius-sm);padding:9px 12px;display:flex;flex-direction:column;gap:5px}
.ac-cert .top{display:flex;justify-content:space-between;gap:8px}
.ac-cert.l5{border-color:var(--l5)}
.ac-cert.l0{opacity:.6}
.ac-link{background:none;border:0;color:var(--acc);cursor:pointer;font:inherit;font-size:12.5px;padding:0}
.ac-stack{display:flex;height:10px;border-radius:3px;overflow:hidden;background:var(--panel2);min-width:140px}
.ac-stack i{display:block;height:100%}
.ac-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--dim);margin-top:10px}
.ac-legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px;background:var(--c)}
.hm-bg{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.55);
  display:none;align-items:flex-start;justify-content:center;padding:6vh 16px}
.hm{width:min(780px,100%);max-height:88vh;display:flex;flex-direction:column;
  background:linear-gradient(var(--panel2),var(--panel2)),var(--pop,#0e1420);
  border:1px solid var(--line);border-radius:var(--radius-sm);
  box-shadow:0 12px 40px rgba(0,0,0,.6)}
.hm-head{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
.hm-head h3{margin:0;font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:var(--gold)}
.hm-x{background:none;border:none;color:var(--dim);font-size:18px;cursor:pointer}
.hm-x:hover{color:var(--txt)}
.hm-body{display:flex;flex:1;min-height:0}
.hm-nav{width:190px;flex:none;border-right:1px solid var(--line);padding:8px}
.hm-nav div{padding:8px 12px;cursor:pointer;border-radius:var(--radius-sm)}
.hm-nav div:hover{color:var(--acc)}
.hm-nav div.on{background:var(--panel);color:var(--acc)}
.hm-sec{flex:1;overflow:auto;padding:12px 18px 16px}
.hm-sec h4{margin:16px 0 4px;font-size:11.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim)}
.hm-row{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}
.hm-row .grow{flex:1;min-width:0}
.hm-row label.grow{display:flex;gap:10px;align-items:flex-start;cursor:pointer}
.hm-row label.grow input{margin-top:3px}
.hm-row.off{opacity:.5}
.hm .small{font-size:12.5px}
.hm-info{padding:9px 12px;margin:4px 0 8px;border-radius:var(--radius-sm);
  background:var(--panel);border-left:3px solid var(--gold);font-size:13px}
.hm-feed div{padding:6px 0;border-bottom:1px solid var(--line);font-size:13px}
.hm-foot{display:flex;align-items:center;padding:10px 16px;border-top:1px solid var(--line)}
@media(max-width:620px){.hm-body{flex-direction:column}
  .hm-nav{width:auto;display:flex;border-right:none;border-bottom:1px solid var(--line)}}
.ipanel{border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--panel2);
  margin:0 0 14px;overflow:hidden}
.ipanel>.ihead{padding:7px 14px;border-bottom:1px solid var(--line);font-weight:700;
  letter-spacing:1px;text-transform:uppercase;font-size:12px;color:var(--gold);
  display:flex;gap:10px;align-items:baseline;white-space:nowrap}
.ipanel>.ihead small{font-weight:400;letter-spacing:0;text-transform:none;color:var(--dim);font-size:12px}
.ipanel>.ibody{padding:10px 14px}
.ov-grid{display:grid;grid-template-columns:260px minmax(0,1fr);gap:18px;max-width:1500px;margin:0 auto}
.ov-side{margin:0}
.ov-portrait{width:100%;max-width:300px;aspect-ratio:1/1;object-fit:cover;border-radius:var(--radius-sm);display:block;background:var(--panel2)}
.ov-name{font-size:20px;font-weight:700;color:var(--gold);margin-top:12px;letter-spacing:var(--h2-spacing);text-transform:var(--h2-transform)}
.ov-right{display:grid;gap:18px;align-content:start}
.ov-two{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:18px}
.ov-right .ipanel{margin:0}
.ov-attrs{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.ov-attr{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius-sm);padding:10px 8px;text-align:center;position:relative}
.ov-attr.prim{outline:2px solid var(--acc);outline-offset:-1px}
.ov-attr svg{width:30px;height:30px;stroke:var(--acc);fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.ov-attr .v{font-size:24px;font-weight:700;line-height:1.1;margin-top:2px}
.ov-attr .l{font-size:10.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim)}
.ov-attr .b{font-size:11.5px;color:var(--good)}
.ov-attr .tag{position:absolute;top:4px;right:6px;font-size:9.5px;color:var(--acc);letter-spacing:1px}
.ov-slots{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}
.ov-slot{aspect-ratio:1/1;border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--panel);
  display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative;padding:4px;text-align:center}
.ov-slot.empty{border-style:dashed;opacity:.65}
.ov-slot.hw{border-color:color-mix(in srgb,var(--gold) 55%,var(--line))}
.ov-slot .n{position:absolute;top:3px;left:6px;font-size:10px;color:var(--dim)}
.ov-slot .plus{font-size:20px;color:var(--dim);opacity:.5}
.ov-slot img{width:34px;height:34px;border-radius:4px}
.ov-slot .t{font-size:9.5px;line-height:1.15;margin-top:2px;word-break:break-word;max-height:3.5em;overflow:hidden}
.sl-lab{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);margin:2px 0 6px}
@media(max-width:800px){.ov-grid,.ov-two{grid-template-columns:minmax(0,1fr)}}
.ov-two{grid-template-columns:minmax(0,1.1fr) minmax(0,1fr)}
@media(max-width:1000px){.ov-two{grid-template-columns:minmax(0,1fr)}}
.subtabs{display:flex;gap:6px;margin:0 0 18px;border-bottom:1px solid var(--line)}
.subtabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--dim);padding:8px 16px;cursor:pointer;font-size:14px}
.subtabs button.active{color:var(--txt);border-bottom-color:var(--acc);font-weight:600}
.subtabs button:hover{color:var(--txt)}
.qbar{display:flex;height:18px;border:1px solid var(--line);border-radius:var(--radius-sm);overflow:hidden;background:var(--panel2)}
.qseg{background:color-mix(in srgb,var(--acc) 70%,transparent);border-right:1px solid var(--panel)}
.qseg.alt{background:color-mix(in srgb,var(--gold) 60%,transparent)}
.qticks{position:relative;height:16px;font-size:10.5px;color:var(--dim);margin-top:2px}
.qticks span{position:absolute;transform:translateX(-2px)}
.pips{display:inline-flex;gap:2px;vertical-align:middle}
.pips i{width:10px;height:10px;border:1px solid var(--line);background:var(--panel2);border-radius:2px;display:block}
.pips i.on{background:var(--acc);border-color:var(--acc)}
.hchar{display:flex;align-items:center;gap:8px}
.hchar img{width:30px;height:30px;border-radius:var(--radius-sm);outline:1px solid var(--acc)}
.themerow{display:flex;gap:8px;align-items:center;margin:0 0 10px;flex-wrap:wrap}
.themerow select{padding:3px 6px;font-size:12.5px}
.themerow button{padding:3px 10px;font-size:12.5px}
.charhead{display:flex;gap:12px;align-items:center;margin-bottom:10px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:13px}
.kv b{color:var(--dim);font-weight:600}
code{background:var(--panel2);padding:2px 6px;border-radius:4px;
  font-size:12.5px;user-select:all}
.muted-note{font-size:12.5px;color:var(--dim);margin-top:8px}
#toast{position:fixed;bottom:20px;right:20px;z-index:300;background:var(--panel2);
  border:1px solid var(--line);border-left:3px solid var(--gold);
  padding:10px 16px;border-radius:8px;display:none;max-width:420px}
.steps{max-height:60vh;overflow:auto}
tr.trained td{background:rgba(76,195,138,.06)}
tr.trained td:first-child{color:var(--good)}
ol.setup{line-height:2}
.grp{border:1px solid var(--line);border-radius:8px;margin-bottom:6px;
  overflow:hidden}
.grphead{padding:8px 12px;cursor:pointer;background:var(--panel2);
  user-select:none;font-weight:600}
.grphead:hover{color:var(--acc)}
.grpbody{padding:2px 8px 8px}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--acc)}
.chart{margin-top:14px}
.chart .crow{display:grid;grid-template-columns:220px 1fr 110px;gap:8px;
  align-items:center;margin-bottom:5px;font-size:12.5px}
.chart .cbar{display:inline-block;height:13px;border-radius:3px;
  background:var(--acc);min-width:3px;vertical-align:middle;
  margin:1px 0}
.chart .cbar.buy{background:var(--gold)}
.lvlpill{display:inline-block;min-width:20px;text-align:center;
  padding:1px 4px;border-radius:4px;background:var(--panel2);
  outline:1px solid var(--line);font-size:11.5px;color:var(--dim)}
</style>
</head>
<body>
<header>
  <h1>EVEWATCH</h1>
  <nav id="nav"></nav>
  <div style="flex:1"></div>
  <div id="activeChar" class="hchar"></div>
  <button class="btn sec" id="helpBtn" style="white-space:nowrap" onclick="openHelp()">Help &amp; settings
    <span id="updDot" style="display:none;color:var(--gold)">●</span></button>
</header>
<div id="helpModal" class="hm-bg" onclick="closeHelp()"></div>
<main id="main"></main>
<div id="toast"></div>
<script>
"use strict";
/* Items without a picture (ship skins etc.) answer 404 from the image
   server - hide the broken image instead of showing an empty frame. */
document.addEventListener("error",e=>{
  const t=e.target;
  if(t&&(t.tagName==="IMG"||t.tagName==="image")&&/images\.evetech\.net/.test(t.currentSrc||t.src||t.getAttribute("href")||""))
    t.style.display="none";
},true);
const $ = s => document.querySelector(s);
const state = {status:null, chars:{}, cards:{}, screen:"app", popFor:null, catalog:null, tab:"characters",
               plan:[], plans:{}, planChar:null, openGroups:{},
               locations:null};

const TABS = [["characters","Characters"],["overview","Overview"],["skills","Skills"],
              ["assets","Assets"],["wallet","Wallet"],["lpstore","LP Store"],["market","Market"],
              ["itemsearch","Item Search"],["contracts","Contracts"],
              ["industry","Industry"],["accomplishments","Accomplishments"]];

/* ---------------- Per-character themes ---------------- */
const THEMES=[["classic","Capsuleer Classic"],["glass","Nebula Glass"],
  ["tactical","Tactical Terminal"],["blueprint","Blueprint"],
  ["minimal","Minimal Slate"]];
let charThemes={};
try{ charThemes=JSON.parse(localStorage.getItem("evw_themes")||"{}"); }catch(e){}
function themeOf(cid){
  const t=charThemes[cid]; if(t) return t;
  const i=Math.max(0,(state.status?.characters||[])
    .findIndex(c=>String(c.character_id)===String(cid)));
  return {style:THEMES[i%THEMES.length][0], mode:"dark"};   // distinct by default
}
function applyTheme(){
  const t=state.activeChar?themeOf(state.activeChar):{style:"classic",mode:"dark"};
  document.body.dataset.style=t.style; document.body.dataset.mode=t.mode;
  document.documentElement.style.colorScheme=t.mode;
}
function setCharTheme(cid,style,mode){
  const cur=themeOf(cid);
  charThemes[cid]={style:style||cur.style, mode:mode||cur.mode};
  try{ localStorage.setItem("evw_themes",JSON.stringify(charThemes)); }catch(e){}
  applyTheme(); render();
}
function setActive(cid,rerender){
  state.activeChar=String(cid);
  if(state.planChar!=="" ) state.planChar=String(cid);
  try{ localStorage.setItem("evw_active",String(cid)); }catch(e){}
  applyTheme(); headerChar();
  document.querySelectorAll("select[id$=Char]:not(#pchar)").forEach(
    el=>{ if([...el.options].some(o=>o.value===String(cid))) el.value=String(cid); });
  if(rerender) render();
}
function headerChar(){
  const el=$("#activeChar"), cs=state.status?.characters||[];
  if(!el) return;
  const c=cs.find(x=>String(x.character_id)===String(state.activeChar));
  if(!state.entered||!c){ el.innerHTML=""; return; }
  el.innerHTML=`<div class="hchip"><img src="https://images.evetech.net/characters/${
    c.character_id}/portrait?size=64" alt=""><span>${esc(c.name)}</span></div>`;
}

/* ---------------- Help & settings window / updates ---------------- */
const HELP_SECS=[["about","About & updates"],["general","General"],["notify","Notifications"]];
const help={open:false, sec:"about", notif:null, updNewer:false};
function openHelp(sec){
  if(sec) help.sec=sec;
  help.open=true; $("#helpModal").style.display="flex";
  if(help.sec==="notify") loadNotify(); else helpMain();
}
function closeHelp(){ help.open=false; $("#helpModal").style.display="none"; }
function helpGo(sec){ help.sec=sec; if(sec==="notify") loadNotify(); else helpMain(); }
document.addEventListener("keydown",e=>{ if(e.key==="Escape"&&help.open) closeHelp(); });
const TAB_ID=Math.random().toString(36).slice(2);
function heartbeat(){
  fetch("/api/heartbeat",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({tab:TAB_ID})}).catch(()=>{});
}
window.addEventListener("pagehide",()=>{
  try{ navigator.sendBeacon("/api/closing",
    new Blob([JSON.stringify({tab:TAB_ID})],{type:"application/json"})); }catch(e){}
});
async function refreshMasteries(){
  helpMain(`<span class="dim">Downloading CCP's static data (about 100 MB)…</span>`);
  try{ await api("/api/masteries/refresh",{method:"POST"}); }catch(e){ helpMain(`<span class="bad">${esc(e.message)}</span>`); return; }
  const poll=async()=>{
    const r=await api("/api/masteries/status");
    if(r.state==="running"){
      helpMain(`<span class="dim">Downloading… ${r.total?Math.round(r.done/r.total*100)+"%":""}</span>`);
      setTimeout(poll,1500); return; }
    if(r.state==="error"){ helpMain(`<span class="bad">Failed: ${esc(r.error)}</span>`); return; }
    state.mast=null; helpMain(`<span class="good">Ship mastery data updated</span> (build ${esc(String(r.build||"?"))}).`);
    if(state._info&&$("#itemInfo")) renderItemInfo();
  };
  setTimeout(poll,1500);
}
async function toggleCloseOnExit(){
  const on=!state.status.quit_on_close;
  await api("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({quit_on_close:on})});
  state.status.quit_on_close=on;
  heartbeat(); helpMain(on
    ?"<b>Close on Exit is ON.</b> EveWatch stops when you close its last browser tab. "+
     "Background tracking (wallet archive, contract index) only runs while a tab is open."
    :"<b>Close on Exit is OFF.</b> EveWatch keeps running in the background after you close "+
     "the tab. Quit it from the tray icon or Help & settings → Quit EveWatch.");
}
const AS_LABEL=/Win/.test(navigator.platform)?"Start with Windows":"Start at login";
async function toggleAutostart(){
  const on=!state.status.autostart;
  try{
    const r=await api("/api/autostart",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({on})});
    state.status.autostart=r.autostart;
    helpMain(r.autostart
      ?`<b>${AS_LABEL} is ON.</b> EveWatch starts quietly in the background when you sign in.`
      :`<b>${AS_LABEL} is OFF.</b>`);
  }catch(e){ helpMain(`<span class="bad">${esc(e.message)}</span>`); }
}
function onOff(on){
  return `<b style="color:${on?"var(--good)":"var(--dim)"}">${on?"ON":"OFF"}</b>`; }
function helpMain(extra){
  const st=state.status||{};
  let body="";
  if(help.sec==="about") body=`
    <h4>EveWatch ${esc(st.version||"")}</h4>
    <p style="margin:4px 0 10px">A personal tool using EVE's public API (ESI).
      Everything stays on this computer.</p>
    <div class="hm-row"><div class="grow">Updates<div class="dim small">${
      st.update_repo?"github.com/"+esc(st.update_repo):"No update source in this copy"}</div></div>
      <button class="btn sec" onclick="helpUpdate()">Check for updates</button></div>`;
  else if(help.sec==="general") body=`
    <div class="hm-row"><label class="grow"><input type="checkbox" ${st.autostart?"checked":""}
      onchange="toggleAutostart()"><span>${AS_LABEL}<div class="dim small">EveWatch starts quietly
      in the background when you sign in.</div></span></label></div>
    <div class="hm-row"><label class="grow"><input type="checkbox" ${st.quit_on_close?"checked":""}
      onchange="toggleCloseOnExit()"><span>Close on Exit<div class="dim small">Quit EveWatch when its
      last browser tab closes. Notifications and background tracking stop with it.</div></span></label></div>
    <div class="hm-row"><div class="grow">Setup &amp; permissions<div class="dim small">Developer
      application, loyalty/research/contracts permissions.</div></div>
      <button class="btn sec" onclick="openSettings()">Open…</button></div>
    <div class="hm-row"><div class="grow">Ship mastery data<div class="dim small">Downloads CCP's
      static data (about 100 MB).</div></div>
      <button class="btn sec" onclick="refreshMasteries()">Refresh</button></div>
    <div class="hm-row"><div class="grow">Data folder<div class="dim small" style="word-break:break-all">${
      esc(st.data_dir||"")}</div></div>
      <button class="btn sec" onclick="api('/api/open_data',{method:'POST'}).catch(e=>toast(e.message))">Open</button></div>`;
  else body=notifyHtml();
  $("#helpModal").innerHTML=`<div class="hm" onclick="event.stopPropagation()">
    <div class="hm-head"><h3>Help &amp; settings</h3><div style="flex:1"></div>
      <button class="hm-x" title="Close (Esc)" onclick="closeHelp()">✕</button></div>
    <div class="hm-body"><div class="hm-nav">${HELP_SECS.map(([id,l])=>
      `<div class="${help.sec===id?"on":""}" onclick="helpGo('${id}')">${esc(l)}${
        (id==="notify"&&unreadNotes())||(id==="about"&&help.updNewer)
          ?' <span style="color:var(--gold)">●</span>':""}</div>`).join("")}</div>
    <div class="hm-sec">${extra?`<div class="hm-info">${extra}</div>`:""}${body}</div></div>
    <div class="hm-foot"><span class="dim small">EveWatch ${esc(st.version||"")}</span>
      <div style="flex:1"></div><button class="btn sec" onclick="helpQuit()">Quit EveWatch</button></div></div>`;
}

/* ---- Notifications section ---- */
function agoText(ts){
  const m=Math.round((Date.now()/1000-ts)/60);
  return m<1?"just now":m<60?m+" min ago":Math.floor(m/60)+" h "+(m%60)+" min ago";
}
function minutesLabel(m){ return m<60?m+" minutes":m===60?"1 hour":(m/60)+" hours"; }
function kindNote(k,n){
  if(k.id==="contract_done"&&!n.contracts_scope)
    return `<div class="warn small">Needs the contracts permission —
      <a href="#" onclick="openSettings();return false">Setup &amp; permissions</a>.</div>`;
  if(k.id==="item_sold")
    return `<div class="dim small">EVE refreshes wallet transactions about once an hour, so this can lag.</div>`;
  return "";
}
function notifyHtml(){
  const n=help.notif;
  if(!n) return `<p class="dim">Loading…</p>`;
  const off=n.enabled?"":"off";
  return `
    <div class="hm-row"><label class="grow"><input type="checkbox" ${n.enabled?"checked":""}
      onchange="notifySet({enabled:this.checked})"><span><b>Watch in the background</b>
      <div class="dim small">Checks EVE's API for all your characters and lets you know when
      something happens. Works while EveWatch is running, even with no tab open.</div></span></label></div>
    <div class="hm-row ${off}"><div class="grow">Check EVE's API every
      <div class="dim small">Last check: ${n.last_check?agoText(n.last_check):"not yet"}.
      Skill and industry timers are watched every 30 s in between, without extra API calls.</div></div>
      <select onchange="notifySet({check_minutes:+this.value})">${n.choices.map(m=>
        `<option value="${m}" ${m===n.check_minutes?"selected":""}>${minutesLabel(m)}</option>`).join("")}</select>
      <button class="btn sec" onclick="notifyCheckNow()">Check now</button></div>
    <h4>Notify me when…</h4>
    ${n.kinds_list.map(k=>`<div class="hm-row ${off}"><label class="grow"><input type="checkbox"
      ${k.on?"checked":""} onchange="notifySet({kinds:{${k.id}:this.checked}})"><span>${esc(k.label)}${
      kindNote(k,n)}</span></label>${k.id==="queue_low"?`<span class="small">under
      <input type="number" min="1" max="720" step="1" value="${n.queue_low_hours}" style="width:70px"
        onchange="notifySet({queue_low_hours:+this.value})"> hours</span>`:""}</div>`).join("")}
    <h4>How</h4>
    <div class="hm-row ${off}"><label class="grow"><input type="checkbox" ${n.os?"checked":""}
      onchange="notifySet({os:this.checked})"><span>Show desktop pop-ups<div class="dim small">${
      /Win/.test(navigator.platform)?"A Windows notification from the tray icon.":"A desktop notification (notify-send)."}
      Notices also pop up in an open EveWatch tab.</div></span></label></div>
    <div class="hm-row"><div class="grow"></div>
      <button class="btn sec" onclick="notifyTest()">Send a test notice</button>
      <button class="btn sec" onclick="notifyClear()">Clear list</button></div>
    ${n.errors.length?`<div class="hm-info"><span class="warn">Last check had problems:</span><br>${
      n.errors.map(esc).join("<br>")}</div>`:""}
    <h4>Recent</h4>
    <div class="hm-feed">${n.feed.length?n.feed.map(x=>`<div><span class="dim small">${
      new Date(x.ts*1000).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}</span>
      <b>${esc(x.title)}</b><br><span class="dim">${esc(x.text)}</span></div>`).join("")
      :`<div class="dim">Nothing yet.</div>`}</div>`;
}
function notesSeen(){ let v=0; try{ v=+localStorage.getItem("evw_notif_seen")||0; }catch(e){} return v; }
function unreadNotes(){ return !!(help.notif&&help.notif.latest>notesSeen()); }
function markNotesSeen(){
  if(!help.notif) return;
  try{ localStorage.setItem("evw_notif_seen",String(help.notif.latest)); }catch(e){}
  setHelpDot();
}
function setHelpDot(){
  $("#updDot").style.display=(help.updNewer||unreadNotes())?"inline":"none";
}
async function loadNotify(){
  if(!help.notif) helpMain();
  try{ help.notif=await api("/api/notify"); }catch(e){ helpMain(`<span class="bad">${esc(e.message)}</span>`); return; }
  if(help.open&&help.sec==="notify"){ markNotesSeen(); helpMain(); }
}
async function notifySet(patch){
  try{
    help.notif=await api("/api/notify/config",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)});
    helpMain();
  }catch(e){ helpMain(`<span class="bad">${esc(e.message)}</span>`); }
}
async function notifyCheckNow(){
  try{ await api("/api/notify/check",{method:"POST"}); }catch(e){ toast(e.message); return; }
  helpMain(`<span class="dim">Checking now — results appear here in a minute.</span>`);
  setTimeout(()=>{ if(help.open&&help.sec==="notify") loadNotify(); },15000);
}
async function notifyTest(){
  try{ help.notif=await api("/api/notify/test",{method:"POST"}); }catch(e){ toast(e.message); return; }
  markNotesSeen(); helpMain(); pollNotify();
}
async function notifyClear(){
  try{ help.notif=await api("/api/notify/clear",{method:"POST"}); }catch(e){ toast(e.message); return; }
  markNotesSeen(); helpMain();
}
let notesToasted=null;     // newest notice already shown in this tab
async function pollNotify(){
  let r; try{ r=await api("/api/notify"); }catch(e){ return; }
  help.notif=r;
  if(notesToasted===null) notesToasted=r.latest;   // don't replay old ones on page load
  const fresh=r.feed.filter(x=>x.id>notesToasted);
  if(fresh.length){
    notesToasted=r.latest;
    toast(fresh.length===1?fresh[0].title+" — "+fresh[0].text
      :fresh.length+" new notices — latest: "+fresh[0].title);
  }
  if(help.open&&help.sec==="notify"){
    markNotesSeen();
    const a=document.activeElement;      // don't re-render under the user's typing
    if(!(a&&(a.type==="number"||a.tagName==="SELECT")&&$("#helpModal").contains(a))) helpMain();
  }
  setHelpDot();
}
async function helpUpdate(){
  help.sec="about";
  helpMain(`<span class="dim">Checking…</span>`);
  try{
    const r=await api("/api/update?force=1");
    showUpdate(r);
  }catch(e){ helpMain(`<span class="bad">${esc(e.message)}</span>`); }
}
function showUpdate(r){
  if(r.error){ helpMain(`<span class="warn">${esc(r.error)}</span>`); toast(r.error); return; }
  if(r.newer){
    helpMain(`<b class="good">Version ${esc(r.latest)} is available</b>
      <span class="dim">(you have ${esc(r.current)})</span>
      <div style="white-space:pre-line;max-height:160px;overflow:auto;margin:6px 0"
        class="dim">${esc((r.notes||"").slice(0,600))}</div>
      <div class="row">${r.download?`<a class="btn" href="${esc(r.download)}">Download installer</a>`:""}
      <a class="btn sec" target="_blank" href="${esc(r.page||"#")}">Release page</a></div>
      <div class="muted-note">Install over the top — your data and logins are kept.</div>`);
  }else{
    helpMain(`<span class="good">You're up to date</span> (version ${esc(r.current)}).`);
    toast("You're up to date — version "+r.current+" is the latest.");
  }
}
async function quietUpdateCheck(){
  try{
    const r=await api("/api/update");
    if(r.newer){ help.updNewer=true; setHelpDot();
      toast("EveWatch "+r.latest+" is available — see Help & settings → About & updates."); }
  }catch(e){}
}
async function helpQuit(){
  if(!confirm("Quit EveWatch? Background tracking (journal archive, contract index) stops until you start it again.")) return;
  try{ await api("/api/quit",{method:"POST"}); }catch(e){}
  document.body.innerHTML=`<main style="padding:60px;text-align:center">
    <h2 style="display:inline-block">EveWatch has stopped</h2>
    <p>You can close this tab. Start EveWatch again from your Start menu or applications list.</p></main>`;
}

function toast(msg){
  const t=$("#toast"); t.textContent=msg; t.style.display="block";
  clearTimeout(t._h); t._h=setTimeout(()=>t.style.display="none",6000);
}
async function api(path, opts){
  const r = await fetch(path, opts);
  const d = await r.json().catch(()=>({}));
  if(!r.ok && r.status!==202){ throw new Error(d.error||("HTTP "+r.status)); }
  return d;
}
function esc(s){const d=document.createElement("div");
  d.textContent=s==null?"":s; return d.innerHTML;}
function isk(n){ if(n==null) return "—";
  return n.toLocaleString("en-US",{maximumFractionDigits:0})+" ISK"; }
function num(n){ return (n||0).toLocaleString("en-US"); }
function dur(mins){
  if(mins==null) return "—";
  let s=Math.round(mins*60), d=Math.floor(s/86400); s-=d*86400;
  let h=Math.floor(s/3600); s-=h*3600; let m=Math.floor(s/60);
  const parts=[]; if(d)parts.push(d+"d"); if(h)parts.push(h+"h");
  parts.push(m+"m"); return parts.join(" ");
}
function until(iso){
  const ms=new Date(iso)-Date.now();
  return ms<=0 ? "done" : dur(ms/60000);
}
function roman(n){ return ["","I","II","III","IV","V"][n]||n; }

function nav(){
  $("#nav").innerHTML = TABS.map(([id,label])=>{
    const locked=id!=="characters"&&!state.entered;
    return `<button class="${state.tab===id?'active':''} ${locked?'locked':''}"
      title="${locked?"Select a character on the Characters tab first":""}"
      onclick="go('${id}')">${label}</button>`; }).join("");
}
function go(tab){
  if(tab!=="characters"&&!state.entered){
    toast(state.status&&state.status.client_id_set
      ?"Select a character first."
      :"Finish the setup on the Characters tab first.");
    return; }
  state.tab=tab; nav(); render();
}
function softRender(){          // don't wipe a field the user is typing in
  const a=document.activeElement;
  if(a&&/^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)&&$("#main").contains(a)) return;
  render();
}

/* ---------------- Character select screen ---------------- */
const SEC_COL={1:"#2fefef",.9:"#48f0c0",.8:"#00ef47",.7:"#00f000",.6:"#8fef2f",
  .5:"#eff000",.4:"#d77700",.3:"#f06000",.2:"#f04800",.1:"#d73000",0:"#f00000"};
function secColor(v){ return SEC_COL[Math.max(0,Math.min(1,Math.round(v*10)/10))]||"#f00000"; }
function refreshCards(){
  for(const c of state.status.characters)
    api("/api/character/"+c.character_id+"/card")
      .then(d=>{ state.cards[c.character_id]=d; if(state.tab==="characters") softRender(); })
      .catch(()=>{ state.cards[c.character_id]={available:false}; });
}
function enterChar(cid){
  setActive(cid,false); state.entered=true; state.popFor=null;
  headerChar(); go("overview");
}
function csPop(c){
  if(state.popFor!==c.character_id) return "";
  const th=themeOf(c.character_id);
  return `<div class="cs-pop" onclick="event.stopPropagation()"><h4>Account details</h4>
    <div class="dim" style="margin-bottom:8px">${esc(c.name)}</div><h4>Theme</h4>
    <select onchange="setCharTheme(${c.character_id},this.value,null)">${
      THEMES.map(([id,nm])=>`<option value="${id}" ${th.style===id?"selected":""}>${nm}</option>`).join("")}</select>
    <div class="sw"><button class="${th.mode==="dark"?"on":""}" onclick="setCharTheme(${c.character_id},null,'dark')">☾ Dark</button>
      <button class="${th.mode==="light"?"on":""}" onclick="setCharTheme(${c.character_id},null,'light')">☀ Light</button></div>
    <button onclick="location.href='/login'" title="Log in again with EVE to add newly enabled permissions">Update permissions…</button>
    <button class="danger" onclick="removeChar(${c.character_id})">Remove character</button></div>`;
}
function csCard(c){
  const cid=c.character_id, th=themeOf(cid), d=state.chars[cid], k=state.cards[cid];
  let train=`<div class="cs-train dim"><span>Loading…</span></div>`, kv="";
  if(d){
    const q=d.queue||[];
    const act=q.find(x=>x.finish_date&&new Date(x.finish_date)>Date.now());
    if(act&&act.start_date){
      const t0=new Date(act.start_date),t1=new Date(act.finish_date);
      const pct=Math.max(0,Math.min(100,(Date.now()-t0)/(t1-t0)*100));
      train=`<div class="cs-train"><i style="width:${pct}%"></i><span><b>${
        esc(skillName(act.skill_id))} ${roman(act.finished_level)}</b></span>
        <span class="dim">${until(act.finish_date)}</span></div>`;
    }else train=`<div class="cs-train dim"><span>No skill in training</span></div>`;
    kv=`<div class="cs-kv"><div><span class="dim">Skill points</span><span>${num(d.total_sp)}</span></div>
      <div><span class="dim">Wallet</span><span>${isk(d.wallet)}</span></div></div>`;
  }
  let ship=`<div class="cs-ship dim">${k?"Ship and location need the location permissions — use ⚙ → Update permissions if you added this character early.":"…"}</div>`,
      loc="";
  if(k&&k.available){
    ship=`<div class="cs-ship"><img src="https://images.evetech.net/types/${k.ship_type_id}/render?size=128" alt="">${
      esc(k.ship)}${k.ship_name?`<div class="dim" style="font-size:11.5px">“${esc(k.ship_name)}”</div>`:""}</div>`;
    loc=`<div class="cs-loc"><b><span style="color:${secColor(k.security)}">${
      k.security.toFixed(1)}</span> ${esc(k.system)}</b>
      <div class="dim" title="${esc(k.station||"")}">${k.docked?esc(k.station):"In space"}</div></div>`;
  }
  return `<div class="cs-slot themed" data-style="${th.style}" data-mode="${th.mode}">
    <div class="cs-strip"><span class="cs-dot"></span><span class="nm">${esc(c.name)}</span>
      <button class="cs-gear" title="Account details" onclick="event.stopPropagation();state.popFor=${
        state.popFor===cid?"null":cid};render()">⚙</button></div>${csPop(c)}
    <div class="cs-card" onclick="enterChar(${cid})">
      <div class="cs-portrait"><img src="https://images.evetech.net/characters/${cid}/portrait?size=512" alt=""></div>
      ${train}${kv}${ship}${loc}${permNote(c)}</div></div>`;
}
function permNote(c){
  const ps=c.perm||[]; if(!ps.length) return "";
  const names=ps.map(p=>esc(p.label)).join(", ");
  return `<div class="cs-perm" onclick="event.stopPropagation()"
    title="Switched on in Setup but not part of this character's login. Once your developer application lists it (portal changes can take a few minutes), log in again.">${
    names} permission not on this login — <a href="/login">update</a></div>`;
}
/* Hidden tab, opened from Help: connection setup + optional loyalty points */
function openSettings(){
  closeHelp();
  state.tab="setup"; nav(); render();
}
function rSetupTab(m){
  m.innerHTML=`<div style="max-width:980px;margin:0 auto">
    <p><button class="btn sec" onclick="go('characters')">← Back to characters</button></p>
    ${setupHtml(true)}</div>`;
}
function applyCollapsedLater(){ applyCollapsed(); }
function rSelect(m){
  const cs=state.status.characters, empties=Math.max(3-cs.length,1);
  const ready=!!state.status.client_id_set;
  m.innerHTML=`<div class="cs-wrap" onclick="if(state.popFor!==null){state.popFor=null;render()}">
    <div class="cs-title"><h1>EVEWATCH</h1><p>${!ready?"Step 1: finish the setup below":
      cs.length?"Select a character to continue":
      "Step 2: add your first character"}</p></div>
    ${ready?"":setupHtml()}
    <div class="cs-row">${cs.map(csCard).join("")}${
      Array.from({length:empties},()=>`<div class="cs-slot"><div class="cs-empty ${ready?"":"off"}"
        onclick="${ready?"location.href='/login'":"toast('Finish the setup first (above).')"}"><div><div class="plus">+</div>
        <small>Add character</small></div></div></div>`).join("")}</div>
    ${ready&&!cs.length?`<div style="max-width:980px;margin:40px auto 0">${setupHtml(true)}</div>`:""}
    <div class="cs-foot">Characters are added with EVE's official login. Portraits, ships and
    locations come from EVE's API and refresh each time you open this screen. Click the ⚙ on a
    character for its theme.</div></div>`;
  applyCollapsedLater();
}

async function boot(){
  nav();
  try{ state.status = await api("/api/status"); }
  catch(e){ toast(e.message); return; }
  const cs0=state.status.characters;
  if(cs0.length){
    let a=null; try{ a=localStorage.getItem("evw_active"); }catch(e){}
    if(!cs0.some(c=>String(c.character_id)===a)) a=String(cs0[0].character_id);
    state.activeChar=a; state.planChar=a;
  }
  applyTheme(); headerChar();
  state.tab="characters"; state.entered=false;
  setTimeout(quietUpdateCheck, 4000);
  heartbeat(); setInterval(heartbeat, 5000);
  setTimeout(pollNotify, 3000); setInterval(pollNotify, 30000);
  if(location.hash==="#update"){ history.replaceState(null,"","/");
    openHelp("about"); helpUpdate(); }
  nav(); render();
  loadCatalog();
  refreshChars();
  refreshCards();
  // keep progress bars & countdowns ticking
  setInterval(()=>{ if(["characters","overview"].includes(state.tab)) softRender(); }, 60000);
}
async function loadCatalog(){
  try{
    const d = await api("/api/catalog");
    if(d.skills){ state.catalog=d;
      if(["skills","characters","overview","accomplishments"].includes(state.tab)) softRender(); }
    else if(d.state!=="error"){ setTimeout(loadCatalog, 2500); }
    else toast("Skill catalog failed: "+d.error);
  }catch(e){ setTimeout(loadCatalog, 4000); }
}
async function refreshChars(){
  for(const c of state.status.characters){
    api("/api/character/"+c.character_id+"/summary")
      .then(d=>{ state.chars[c.character_id]=d;
                 if(["characters","overview","accomplishments"].includes(state.tab)) softRender();
                 else if(state.tab==="skills"&&state.skillsSub==="planner") renderSkillPick();
                 else if(state.tab==="skills") softRender(); })
      .catch(e=>toast(c.name+": "+e.message));
  }
}

function render(){
  const m=$("#main");
  ({characters:rSelect, overview:rChars, skills:rSkills, assets:rAssets,
    wallet:rWallet, lpstore:rLPStore, market:rMarket, itemsearch:rItemSearch, contracts:rContracts,
    industry:rIndustry, accomplishments:rAccomplishments, setup:rSetupTab}[state.tab])(m);
  applyCollapsed();
}
// click a gold card header to fold/unfold it (remembered per header)
let _collapsed={};
try{ _collapsed=JSON.parse(localStorage.getItem("evw_collapsed")||"{}"); }
catch(e){}
document.addEventListener("click",e=>{
  const h=e.target.closest(".card > h2"); if(!h) return;
  const card=h.parentElement;
  card.classList.toggle("collapsed");
  _collapsed[h.textContent.trim()]=card.classList.contains("collapsed");
  try{ localStorage.setItem("evw_collapsed",JSON.stringify(_collapsed)); }
  catch(e){}
});
function applyCollapsed(){
  document.querySelectorAll(".card > h2").forEach(h=>{
    const k=h.textContent.trim(), c=h.parentElement;
    if(_collapsed[k]===true || (_collapsed[k]===undefined && c.dataset.defcollapsed))
      c.classList.add("collapsed");
  });
}

/* ---------------- Setup ---------------- */
function setupHtml(open){
  const st=state.status||{};
  return `
  <div class="card" ${st.client_id_set&&!open?'data-defcollapsed="1"':''}>
    <h2>Setup — connect to EVE</h2>
    <p>EveWatch talks to EVE's official API (ESI). To log characters in you
    need a free <b>developer application</b> — it takes 2 minutes:</p>
    <ol class="setup">
      <li>Go to <a href="https://developers.eveonline.com/applications"
        target="_blank" style="color:var(--acc)">developers.eveonline.com/applications</a>
        and log in with your EVE account.</li>
      <li>Click <b>Create new application</b>. Name and description can be
        anything (e.g. "EveWatch personal").</li>
      <li>Connection type: <b>Authentication &amp; API Access</b>.</li>
      <li>Permissions (scopes) — add these:<br>
        ${ (st.scopes||[]).map(s=>`<code>${s}</code>`).join(" ") }</li>
      <li>Callback URL — paste exactly:
        <code>${esc(st.callback_url||"")}</code></li>
      <li>Save, then copy the <b>Client ID</b> and paste it below.</li>
    </ol>
    <div class="row">
      <input id="cid" size="40" placeholder="Client ID"
        value="${st.client_id_set?'(already saved — paste to replace)':''}">
      <button class="btn" onclick="saveCid()">Save</button>
    </div>
    <p class="muted-note">Your Client ID and character tokens are stored only
    on this computer, in the <b>evewatch_data</b> folder next to the app.</p>
  </div>
  <div class="card" ${open?"":'data-defcollapsed="1"'}>
    <h2>Optional permissions: loyalty points, research points &amp; contracts</h2>
    <p>Each of these read-only permissions unlocks a feature: loyalty-point balances and
    research-agent points (with datacore values) on the LP Store tab, and the
    "contract sold" notification (Help &amp; settings → Notifications).<br>
    ${(st.extra_scopes||[]).concat([st.research_scope,st.contracts_scope]).filter(Boolean).map(s=>`<code>${s}</code>`).join(" ")}</p>
    <p class="muted-note">Order matters: first tick them on your developer
    application at developers.eveonline.com (under esi-characters / esi-contracts), then switch
    this on, then add each character again with the + slot above. If you switch
    it on before the portal is updated, EVE's login will reject the request
    ("invalid_scope"). Portal changes can take several minutes to reach EVE's login.</p>
    <label style="display:block"><input type="checkbox" id="extrasBox"
      ${st.scope_extras?"checked":""} onchange="saveExtras()">
      Request the loyalty-points permission when logging in</label>
    <label style="display:block;margin-top:6px"><input type="checkbox" id="researchBox"
      ${st.scope_research?"checked":""} onchange="saveExtras()">
      Request the research-points permission when logging in</label>
    <label style="display:block;margin-top:6px"><input type="checkbox" id="contractsBox"
      ${st.scope_contracts?"checked":""} onchange="saveExtras()">
      Request the contracts permission when logging in</label>
  </div>`;
}
async function saveExtras(){
  const on=$("#extrasBox").checked, rs=$("#researchBox").checked, ct=$("#contractsBox").checked;
  await api("/api/config",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({scope_extras:on,scope_research:rs,scope_contracts:ct})});
  state.status.scope_extras=on; state.status.scope_research=rs; state.status.scope_contracts=ct;
  if(help.notif) help.notif.contracts_scope=ct;
  toast(on||rs||ct?"Saved — enable the permissions in the developer portal first, then use ⚙ → Update permissions on each character."
          :"Off — logins will use the standard permissions only.");
}
async function saveCid(){
  const v=$("#cid").value.trim();
  if(!v || v.startsWith("(")){ toast("Paste your Client ID first."); return; }
  await api("/api/config",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({client_id:v})});
  state.status.client_id_set=true;
  toast("Saved. Now click the + slot to add your first character.");
  render();
}

/* ---------------- Characters ---------------- */
const ATTR_LIST=[["intelligence","Intelligence"],["memory","Memory"],["perception","Perception"],
  ["willpower","Willpower"],["charisma","Charisma"]];
const ATTR_ICON={
 perception:'<svg viewBox="0 0 32 32"><path d="M2 16c4-7 9-10 14-10s10 3 14 10c-4 7-9 10-14 10S6 23 2 16z"/><circle cx="16" cy="16" r="4.5"/></svg>',
 willpower:'<svg viewBox="0 0 32 32"><path d="M16 3c1 5 8 8 8 15a8 8 0 0 1-16 0c0-4 2-5 3-8 2 1 3 2 3 4 2-3 2-7 2-11z"/></svg>',
 intelligence:'<svg viewBox="0 0 32 32"><path d="M11 24h10M12 28h8"/><path d="M16 3a8 8 0 0 0-4.500 14.600c.9.7 1.500 1.700 1.500 2.900V22h6v-1.500c0-1.200.6-2.200 1.500-2.900A8 8 0 0 0 16 3z"/></svg>',
 memory:'<svg viewBox="0 0 32 32"><ellipse cx="16" cy="7" rx="10" ry="4"/><path d="M6 7v9c0 2.200 4.500 4 10 4s10-1.800 10-4V7"/><path d="M6 16v9c0 2.200 4.500 4 10 4s10-1.800 10-4v-9"/></svg>',
 charisma:'<svg viewBox="0 0 32 32"><path d="M4 7h24v15H14l-6 5v-5H4z"/><path d="M10 13h12M10 17h7"/></svg>'};
function ovAttrs(d){
  // ESI reports the attributes without implants; the bonus comes from the plugged-in implants
  const imp={}; (d.implant_details||[]).forEach(i=>Object.entries(i.bonus||{}).forEach(
    ([k,v])=>{ imp[k]=(imp[k]||0)+v; }));
  const out={}; ATTR_LIST.forEach(([k])=>{ const b=(d.attributes||{})[k]||0;
    out[k]={base:b,imp:imp[k]||0,total:b+(imp[k]||0)}; });
  return out;
}
function ovSlot(d,n){
  const it=(d.implant_details||[]).find(i=>i.slot===n), hw=n>5;
  if(!it) return `<div class="ov-slot empty ${hw?"hw":""}"><span class="n">${n}</span><span class="plus">+</span></div>`;
  const bonus=Object.entries(it.bonus||{}).map(([k,v])=>`+${v} ${k}`).join(", ");
  return `<div class="ov-slot ${hw?"hw":""}" title="${esc(it.name)}${bonus?" — "+bonus:""}">
    <span class="n">${n}</span><img src="https://images.evetech.net/types/${it.type_id}/icon?size=64" alt="">
    <div class="t">${esc(it.name)}</div></div>`;
}
function rChars(m){
  const cs=state.status.characters, cid=state.activeChar;
  const c=cs.find(x=>String(x.character_id)===String(cid));
  if(!c){ m.innerHTML=`<div class="card"><h2>No character selected</h2>
    <p>Pick one on the <b>Characters</b> tab.</p></div>`; return; }
  const d=state.chars[cid];
  if(!d){ m.innerHTML=`<div class="card"><h2>${esc(c.name)}</h2><p class="dim">Loading…</p></div>`; return; }
  const q=d.queue||[];
  const active=q.find(s=>s.finish_date && new Date(s.finish_date)>Date.now());
  const lastFinish=q.length?q[q.length-1].finish_date:null;
  const at=ovAttrs(d);
  const sk=active&&state.catalog&&state.catalog.skills[active.skill_id];
  const pri=sk&&sk.primary, sec=sk&&sk.secondary;
  const spm=sk&&at[pri]&&at[sec]?(at[pri].total+at[sec].total/2):null;
  const tiles=ATTR_LIST.map(([k,l])=>`<div class="ov-attr ${k===pri?"prim":""}">
    ${k===pri?'<span class="tag">PRIMARY</span>':k===sec?'<span class="tag">SECONDARY</span>':""}
    ${ATTR_ICON[k]}<div class="v">${at[k].total}</div><div class="l">${l}</div>
    <div class="b">${at[k].imp?"+"+at[k].imp+" implant":"&nbsp;"}</div></div>`).join("");
  let queueHtml;
  if(!q.length||!lastFinish) queueHtml=`<p class="bad">⚠ Skill queue is empty!</p>`;
  else{
    const warn=(new Date(lastFinish)-Date.now())<86400000;
    let prog="";
    if(active&&active.start_date){
      const t0=new Date(active.start_date),t1=new Date(active.finish_date);
      const pct=Math.max(0,Math.min(100,(Date.now()-t0)/(t1-t0)*100));
      prog=`<div style="margin:0 0 10px"><div class="row" style="justify-content:space-between;margin-bottom:3px">
        <span>▶ <b>${esc(skillName(active.skill_id))} ${roman(active.finished_level)}</b></span>
        <span class="dim">${pct.toFixed(1)}% · ${until(active.finish_date)} left</span></div>
        <div class="bar"><i style="width:${pct}%"></i></div></div>`;
    }
    queueHtml=prog+(warn?`<p class="warn">⚠ Queue ends in ${until(lastFinish)}</p>`:"")+
      `<table><tr><th>Skill</th><th class="num">Finishes</th></tr>`+
      q.slice(0,9).map(s=>{ const cur=active&&s.queue_position===active.queue_position;
        return `<tr><td>${cur?"▶ ":""}${esc(skillName(s.skill_id))} ${roman(s.finished_level)}</td>
        <td class="num">${s.finish_date?until(s.finish_date):"paused"}</td></tr>`; }).join("")+
      (q.length>9?`<tr><td colspan="2" class="dim">…and ${q.length-9} more (total ${until(lastFinish)})</td></tr>`:"")+`</table>`;
  }
  let nowHtml;
  if(!q.length||!lastFinish) nowHtml=`<p class="bad">⚠ Skill queue is empty!</p>`;
  else if(active&&active.start_date){
    const t0=new Date(active.start_date),t1=new Date(active.finish_date);
    const pct=Math.max(0,Math.min(100,(Date.now()-t0)/(t1-t0)*100));
    nowHtml=`<div class="row" style="justify-content:space-between;margin-bottom:3px">
      <span>▶ <b>${esc(skillName(active.skill_id))} ${roman(active.finished_level)}</b></span>
      <span class="dim">${pct.toFixed(1)}% · ${until(active.finish_date)} left</span></div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <p class="dim" style="margin-top:8px">${q.length} skill${q.length===1?"":"s"} queued · queue ends in ${until(lastFinish)}</p>
      ${(new Date(lastFinish)-Date.now())<86400000?`<p class="warn">⚠ Queue ends in ${until(lastFinish)}</p>`:""}`;
  } else nowHtml=`<p class="dim">Nothing is training right now.</p>`;
  const nImp=(d.implant_details||[]).filter(i=>i.slot).length;
  m.innerHTML=`<div class="ov-grid">
    <div class="card ov-side"><img class="ov-portrait" src="https://images.evetech.net/characters/${d.character_id}/portrait?size=256" alt="">
      <div class="ov-name">${esc(d.name)}</div><div class="dim">${esc(d.corporation||"")}</div>
      <div class="kv" style="margin-top:12px"><b>Skill points</b><span>${num(d.total_sp)}</span>
        ${d.unallocated_sp?`<b>Unallocated</b><span class="gold">${num(d.unallocated_sp)}</span>`:""}
        <b>Wallet</b><span>${isk(d.wallet)}</span>
        <b>Training speed</b><span>${spm?spm.toFixed(1)+" SP/min":"—"}</span></div></div>
    <div class="ov-right">
      <div class="ipanel"><div class="ihead">Attributes<small>base + implants · training attributes outlined</small></div>
        <div class="ibody"><div class="ov-attrs">${tiles}</div></div></div>
      <div class="ov-two">
        <div class="ipanel"><div class="ihead">Implants<small>${nImp} of 10 slots</small></div><div class="ibody">
          <div class="sl-lab">Attribute implants (slots 1–5)</div><div class="ov-slots">${[1,2,3,4,5].map(n=>ovSlot(d,n)).join("")}</div>
          <div class="sl-lab" style="margin-top:12px">Hardwirings (slots 6–10)</div><div class="ov-slots">${[6,7,8,9,10].map(n=>ovSlot(d,n)).join("")}</div></div></div>
        <div class="ipanel"><div class="ihead">Now training</div><div class="ibody">${nowHtml}
          <p style="margin-top:10px"><button class="btn sec" onclick="state.skillsSub='queue';go('skills')">Open full skill queue →</button></p></div></div>
      </div></div></div>
    <p class="muted-note">Queue times refresh when you reload the page. ESI caches the skill queue for a few minutes.</p>`;
}
function skillName(id){
  return state.catalog?.skills?.[id]?.name || ("Skill "+id);
}
async function removeChar(id){
  if(!confirm("Remove this character from EveWatch?")) return;
  await api("/api/character/"+id+"/remove",{method:"POST"});
  state.status.characters=state.status.characters
    .filter(c=>c.character_id!==id);
  delete state.chars[id];
  if(String(state.activeChar)===String(id)){
    if(state.status.characters.length)
      setActive(state.status.characters[0].character_id,false);
    state.entered=false; state.tab="characters"; headerChar(); nav();
  }
  render();
}

/* ---------------- Planner ---------------- */
function openPlanner(){ state.skillsSub="planner"; go("skills"); }
function skillsSubGo(k){ state.skillsSub=k; render(); }
function rSkills(m){
  const sub=state.skillsSub||"queue";
  const tabs=[["queue","Queue"],["skills","My skills"],["planner","Planner"]];
  m.innerHTML=`<div class="subtabs">${tabs.map(([k,l])=>
    `<button class="${sub===k?"active":""}" onclick="skillsSubGo('${k}')">${l}</button>`).join("")}</div>
    <div id="skillsBody"></div>`;
  const body=$("#skillsBody");
  ({queue:rQueue,skills:rMySkills,planner:rPlanner}[sub])(body);
}
function actChar(){
  const cid=state.activeChar;
  return {cid, c:(state.status.characters||[]).find(x=>String(x.character_id)===String(cid)), d:state.chars[cid]};
}
function rQueue(m){
  const {c,d}=actChar();
  if(!c){ m.innerHTML=`<div class="card"><h2>Skill queue</h2><p class="dim">Select a character first.</p></div>`; return; }
  if(!d){ m.innerHTML=`<div class="card"><h2>Skill queue — ${esc(c.name)}</h2><p class="dim">Loading…</p></div>`; return; }
  const q=(d.queue||[]).filter(x=>x.finish_date||x.start_date);
  const now=Date.now();
  const last=q.length?q[q.length-1].finish_date:null;
  if(!q.length||!last){
    m.innerHTML=`<div class="card"><h2>Skill queue — ${esc(c.name)}</h2>
      <p class="bad">⚠ The skill queue is empty — nothing is training.</p>
      <p class="muted-note">EveWatch can only read your queue; add skills to it in-game.</p></div>`;
    return;
  }
  const t0=Math.max(now,new Date(q[0].start_date||now).getTime()), tEnd=new Date(last).getTime();
  const span=Math.max(1,tEnd-t0);
  const remainSp=q.reduce((a,x)=>{ const end=x.level_end_sp||0, st=Math.max(x.training_start_sp||0,x.level_start_sp||0);
    return a+Math.max(0,end-st); },0);
  const warn=(tEnd-now)<86400000;
  const segs=q.map((x,i)=>{
    const a=Math.max(t0,new Date(x.start_date||t0).getTime()), b=new Date(x.finish_date||tEnd).getTime();
    const w=Math.max(0.5,(b-a)/span*100);
    return `<div class="qseg ${i%2?"alt":""}" style="width:${w}%" title="${esc(skillName(x.skill_id))} ${roman(x.finished_level)} — ${dur((b-a)/60000)}"></div>`;
  }).join("");
  const days=Math.ceil(span/86400000), ticks=Array.from({length:Math.min(days,12)},(_,i)=>{
    const step=days/Math.min(days,12); const d0=Math.round(step*i);
    return `<span style="left:${d0*86400000/span*100}%">${d0?d0+"d":"now"}</span>`; }).join("");
  const fmt=iso=>iso?new Date(iso).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):"—";
  const planLeft=(state.plan||[]).filter(e=>{ const s=state.catalog&&state.catalog.skills[e[0]];
    return s&&((d.skills[e[0]]||{}).level||0)<e[1]; }).slice(0,6);
  m.innerHTML=`<div class="card"><h2>Skill queue — ${esc(c.name)}</h2>
    <div class="row" style="margin-bottom:10px">
      <span class="pill">${q.length} skill${q.length===1?"":"s"} queued</span>
      <span class="pill">Queue time left: <b>${until(last)}</b></span>
      <span class="pill">Ends: <b>${fmt(last)}</b></span>
      <span class="pill">SP in queue: <b>${num(remainSp)}</b></span></div>
    ${warn?`<div class="alertbox down">⚠ The queue runs out in ${until(last)} — queue more skills in-game soon.</div>`:""}
    <div class="qbar">${segs}</div><div class="qticks">${ticks}</div>
    <table style="margin-top:14px"><tr><th>#</th><th>Skill</th><th class="num">SP</th><th>Starts</th><th>Finishes</th><th class="num">Duration</th></tr>
    ${q.map((x,i)=>{
      const act=i===0&&x.finish_date&&new Date(x.start_date)<=now;
      const a=new Date(x.start_date||0).getTime(), b=new Date(x.finish_date||0).getTime();
      const sp=Math.max(0,(x.level_end_sp||0)-Math.max(x.training_start_sp||0,x.level_start_sp||0));
      return `<tr><td>${i+1}</td><td>${act?"▶ ":""}<b>${esc(skillName(x.skill_id))}</b> ${roman(x.finished_level)}</td>
        <td class="num">${sp?num(sp):"—"}</td><td class="dim">${act?"training now":fmt(x.start_date)}</td>
        <td>${x.finish_date?fmt(x.finish_date):"paused"}</td>
        <td class="num">${x.finish_date&&x.start_date?dur(Math.max(0,b-Math.max(a,act?now:a))/60000):"—"}</td></tr>`;
    }).join("")}</table>
    <p class="muted-note">EveWatch can only read your queue — add or reorder skills in-game. Times refresh when you reload the page (ESI caches the queue for a few minutes).</p></div>
    ${planLeft.length?`<div class="card"><h2>Next from your plan</h2>
      <p class="dim">Plan items you haven't finished yet — queue these next:</p>
      ${planLeft.map(e=>`<div>• ${esc(state.catalog.skills[e[0]].name)} <b>${roman(e[1])}</b>
        <span class="dim">(you have ${(d.skills[e[0]]||{}).level?roman(d.skills[e[0]].level):"none"})</span></div>`).join("")}
      <p><button class="btn sec" onclick="skillsSubGo('planner')">Open planner</button></p></div>`:""}`;
}
function prereqsMet(d,s){ return (s.prereqs||[]).every(([pid,pl])=>((d.skills[pid]||{}).level||0)>=pl); }
function rMySkills(m){
  const {c,d}=actChar();
  if(!c){ m.innerHTML=`<div class="card"><h2>My skills</h2><p class="dim">Select a character first.</p></div>`; return; }
  if(!d||!state.catalog){ m.innerHTML=`<div class="card"><h2>My skills</h2><p class="dim">Loading${state.catalog?"":" skill catalog (one-time)"}…</p></div>`; 
    if(state.tab==="skills") setTimeout(()=>{ if(state.tab==="skills"&&state.skillsSub==="skills") softRender(); },2000); return; }
  const f=state.msFilter||"trained", q=(state.msQ||"").toLowerCase();
  const all=Object.values(state.catalog.skills);
  const lvl=s=>(d.skills[s.id]||{}).level||0, sp=s=>(d.skills[s.id]||{}).sp||0;
  const trainingId=((d.queue||[]).find(x=>x.finish_date&&new Date(x.finish_date)>Date.now())||{}).skill_id;
  const shown=all.filter(s=>(f==="all"||(f==="trained"?lvl(s)>0:f==="ready"?(lvl(s)===0&&prereqsMet(d,s)):lvl(s)===0))
    &&(!q||s.name.toLowerCase().includes(q)));
  const totalSp=Object.values(d.skills).reduce((a,x)=>a+x.sp,0);
  const nTrained=all.filter(s=>lvl(s)>0).length, nV=all.filter(s=>lvl(s)===5).length;
  const groups=Object.entries(state.catalog.groups).sort((a,b)=>a[1].localeCompare(b[1]));
  const pips=l=>Array.from({length:5},(_,i)=>`<i class="${i<l?"on":""}"></i>`).join("");
  m.innerHTML=`<div class="card"><h2>My skills — ${esc(c.name)}</h2>
    <div class="row" style="margin-bottom:10px">
      <span class="pill">${num(totalSp)} SP</span><span class="pill">${nTrained} skills trained</span>
      <span class="pill">${nV} at level V</span></div>
    <div class="row" style="margin-bottom:10px">
      <input id="msq" placeholder="Search skills…" value="${esc(state.msQ||"")}" oninput="state.msQ=this.value;msRefresh()">
      <select onchange="state.msFilter=this.value;rMySkills($('#skillsBody'))">
        ${[["trained","Trained skills"],["ready","Untrained — ready to train"],["untrained","All untrained"],["all","All skills"]].map(
          ([k,l])=>`<option value="${k}" ${f===k?"selected":""}>${l}</option>`).join("")}</select></div>
    <div id="msList"></div></div>`;
  window.msRefresh=()=>{
    const qq=(state.msQ||"").toLowerCase();
    $("#msList").innerHTML=groups.map(([gid,gname])=>{
      const rows=shown.filter(s=>String(s.group)===gid&&(!qq||s.name.toLowerCase().includes(qq)))
        .sort((a,b)=>a.name.localeCompare(b.name));
      if(!rows.length) return "";
      const open=qq?true:state.msOpen&&state.msOpen[gid];
      const gsp=all.filter(s=>String(s.group)===gid).reduce((a,s)=>a+sp(s),0);
      return `<div class="grp"><div class="grphead" onclick="state.msOpen=state.msOpen||{};state.msOpen['${gid}']=!state.msOpen['${gid}'];msRefresh()">
        ${open?"▾":"▸"} ${esc(gname)} <span class="dim" style="font-weight:400">(${rows.length}${gsp?" · "+num(gsp)+" SP":""})</span></div>
        ${open?`<div class="grpbody"><table><tr><th>Skill</th><th>Level</th><th>Rank</th><th class="num">SP</th><th class="num">Status</th></tr>
        ${rows.map(s=>{ const l=lvl(s);
          const st=s.id===trainingId?`<span class="gold">▶ training</span>`:l>=5?`<span class="good">✔ maxed</span>`:
            l===0&&!prereqsMet(d,s)?`<span class="dim">needs prerequisites</span>`:l===0?`<span class="dim">ready to train</span>`:`<span class="dim">→ V in ${dur(timeToVof(d,s))}</span>`;
          return `<tr class="${l?"trained":""}"><td title="${esc(s.desc)}">${esc(s.name)}</td>
            <td><span class="pips">${pips(l)}</span> <span class="dim">${l?roman(l):""}</span></td>
            <td>${s.rank}</td><td class="num">${sp(s)?num(sp(s)):"—"}</td><td class="num">${st}</td></tr>`; }).join("")}
        </table></div>`:""}</div>`;
    }).join("")||`<p class="dim">Nothing matches.</p>`;
  };
  msRefresh();
}
function timeToVof(d,s){
  const l=(d.skills[s.id]||{}).level||0, cur=(d.skills[s.id]||{}).sp??spAt(s.rank,l);
  const a=d.attributes||{}, rate=((a[s.primary]||20)+(a[s.secondary]||20)/2);
  return (spAt(s.rank,5)-cur)/rate;
}
function rPlanner(m){
  if(!state.catalog){
    const cs=state.status.catalog||{};
    m.innerHTML=`<div class="card"><h2>Skill Planner</h2>
    <p>Building the skill catalog from ESI (one-time, then cached)…
    ${cs.done||0} / ${cs.total||"?"} skills</p></div>`;
    setTimeout(()=>{ if(state.tab==="skills") render(); }, 2000);
    return;
  }
  const chars=state.status.characters;
  m.innerHTML=`
  <div class="card">
    <h2>Skill Planner</h2>
    <div class="row">
      <select id="pchar"
        onchange="if(this.value) setActive(this.value,false); else state.planChar='';renderSkillPick();autoCompute()">
        ${chars.map(c=>`<option value="${c.character_id}"
          ${String(state.planChar)===String(c.character_id)?"selected":""}>
          ${esc(c.name)}</option>`).join("")}
        <option value="" ${state.planChar?"":"selected"}>— no character
          (all skills untrained, attrs 20) —</option>
      </select>
      <input id="psearch" placeholder="Search all skills…"
        oninput="renderSkillPick()">
      <select id="pfilter" onchange="renderSkillPick()">
        <option value="all" ${state.pFilter==="all"?"selected":""}>
          All skills</option>
        <option value="trained" ${state.pFilter==="trained"?"selected":""}>
          Only trained</option>
        <option value="untrained" ${state.pFilter==="untrained"?"selected":""}>
          Only untrained</option>
      </select>
      <span class="dim" style="font-size:12.5px">trained skills are
        highlighted green</span>
    </div>
    <div id="skillpick" style="margin-top:10px;max-height:62vh;
      overflow:auto"></div>
  </div>
  <div class="card" id="plancard"></div>`;
  renderSkillPick();
  renderPlanList();
  loadPlansOnce();
}
function renderPlanList(){
  const el=$("#plancard"); if(!el) return;
  el.innerHTML=`
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Current plan${state.plan.length&&planTitle()?": "+esc(planTitle()):""} (${state.plan.length})</h2>
      <span class="row">
        <select id="plansel" onchange="loadPlan(this.value)">
          <option value="">— saved plans —</option>
          ${Object.keys(state.plans).map(n=>
            `<option>${esc(n)}</option>`).join("")}
        </select>
        <button class="btn sec" onclick="savePlan()">Save plan</button>
        <button class="btn sec" onclick="deletePlan()">Delete</button>
        <button class="btn danger"
          onclick="state.plan=[];state.planParts=[];renderPlanList()">Clear</button>
      </span>
    </div>
    <div id="planlist">${state.plan.length?
      `<table><tr><th>Skill</th><th>Target</th><th></th></tr>`+
      state.plan.map((e,i)=>{
        const s=state.catalog.skills[e[0]];
        return `<tr><td>${esc(s?.name||e[0])}</td>
        <td>${roman(e[1])}</td>
        <td class="num"><button class="btn sec"
          onclick="state.plan.splice(${i},1);renderPlanList()">✕</button>
        </td></tr>`;
      }).join("")+`</table>`
      :`<p class="dim">Add skills from the browser above — results update
        automatically. Prerequisites are added for you.</p>`}
    </div>
    <div id="planresult"></div>`;
  autoCompute();
}
let _computeTimer=null;
function autoCompute(){
  clearTimeout(_computeTimer);
  if(!state.plan.length) return;
  _computeTimer=setTimeout(computePlan, 350);
}
let _plansLoaded=false;
async function loadPlansOnce(){
  if(_plansLoaded) return; _plansLoaded=true;
  try{ state.plans=await api("/api/plans"); render(); }catch(e){}
}
function levelTooltip(s,l){
  const sp=Math.round(250*s.rank*Math.pow(Math.sqrt(32),l-1));
  let t=`Train ${s.name} to level ${roman(l)}\nTotal SP at this level: `+
        num(sp);
  const m=s.desc.match(/(\d+(?:[.,]\d+)?)\s*(?:%|percent)([^.]*?)per\s+(?:skill\s+)?level/i);
  if(m){
    const per=parseFloat(m[1].replace(",","."));
    const what=m[2].replace(/\s+/g," ").trim();
    t+=`\nAt ${roman(l)}: ${+(per*l).toFixed(2)}% ${what}`+
       ` (${per}% per level)`;
  }
  return t;
}
function trainedLevel(s){
  return state.chars[state.planChar]?.skills?.[s.id]?.level||0;
}
function passesFilter(s){
  const f=state.pFilter||"all";
  if(f==="trained") return trainedLevel(s)>0;
  if(f==="untrained") return trainedLevel(s)===0;
  return true;
}
function spAt(rank,l){ return l<=0?0:Math.round(250*rank*Math.pow(Math.sqrt(32),l-1)); }
function charAttrs(){
  const a=state.chars[state.planChar]?.attributes;
  return a||{charisma:20,intelligence:20,memory:20,perception:20,willpower:20};
}
function timeToV(s){
  const tr=trainedLevel(s);
  if(tr>=5) return null;
  const curSp=state.chars[state.planChar]?.skills?.[s.id]?.sp ?? spAt(s.rank,tr);
  const a=charAttrs();
  const rate=(a[s.primary]||20)+(a[s.secondary]||20)/2;
  return (spAt(s.rank,5)-curSp)/rate;
}
function skillRowsTable(rows){
  return `<table><tr><th>Skill</th><th>Rank</th><th>Attributes</th>
    <th>Trained</th><th>Add at level</th><th class="num">Time → V</th></tr>`+
    rows.map(s=>{
      const tr=trainedLevel(s);
      const t5=timeToV(s);
      return `<tr class="${tr?"trained":""}">
      <td title="${esc(s.desc)}">${esc(s.name)}</td>
      <td>${s.rank}</td>
      <td class="dim">${s.primary.slice(0,3)}/${s.secondary.slice(0,3)}</td>
      <td><span class="lvlpill">${tr?roman(tr):"—"}</span></td>
      <td>${[1,2,3,4,5].map(l=>`<button class="btn sec"
        style="padding:2px 8px;margin-right:2px"
        title="${esc(levelTooltip(s,l)).replaceAll("\n","&#10;")}"
        onclick="addToPlan(${s.id},${l})">${roman(l)}</button>`).join("")}${
        tr<5?`<button class="btn" style="padding:2px 9px"
        title="Add to plan: train straight to V"
        onclick="addToPlan(${s.id},5)">+</button>`:""}
      </td>
      <td class="num ${tr>=5?"good":"dim"}">${tr>=5?"✔ done":dur(t5)}</td>
      </tr>`;
    }).join("")+`</table>`;
}
function renderSkillPick(){
  const qEl=$("#psearch");
  if(!qEl) return;
  const box=$("#skillpick");
  const keepScroll=box?box.scrollTop:0;
  state.pFilter=$("#pfilter")?.value||"all";
  const q=qEl.value.toLowerCase();
  const all=Object.values(state.catalog.skills).filter(passesFilter);
  if(q){
    const rows=all.filter(s=>s.name.toLowerCase().includes(q))
      .sort((a,b)=>a.name.localeCompare(b.name)).slice(0,100);
    $("#skillpick").innerHTML = rows.length ? skillRowsTable(rows)
      : `<p class="dim">No skills match “${esc(q)}”.</p>`;
    $("#skillpick").scrollTop=keepScroll;
    return;
  }
  // market-style tree: top-level groups, expandable, like the in-game market
  const groups=Object.entries(state.catalog.groups)
    .sort((a,b)=>a[1].localeCompare(b[1]));
  $("#skillpick").innerHTML = groups.map(([gid,gname])=>{
    const inGroup=all.filter(s=>String(s.group)===gid)
      .sort((a,b)=>a.name.localeCompare(b.name));
    if(!inGroup.length) return "";
    const open=state.openGroups[gid];
    const trained=inGroup.filter(s=>trainedLevel(s)>0).length;
    return `<div class="grp">
      <div class="grphead" onclick="toggleGroup('${gid}')">
        ${open?"▾":"▸"} ${esc(gname)}
        <span class="dim" style="font-weight:400">(${inGroup.length}${
          trained?`, <span class="good">${trained} trained</span>`:""})</span>
      </div>
      ${open?`<div class="grpbody">${skillRowsTable(inGroup)}</div>`:""}
    </div>`;
  }).join("") || `<p class="dim">Nothing matches this filter.</p>`;
  $("#skillpick").scrollTop=keepScroll;
}
function toggleGroup(gid){
  state.openGroups[gid]=!state.openGroups[gid];
  renderSkillPick();
}
/* The current plan gets a readable name from what was added to it
   ("Ishtar · Mastery IV", "Armor Tanking · Elite"…), used when saving. */
function notePlan(label){
  if(!state.plan.length||!state.planParts) state.planParts=[];
  if(!state.planParts.includes(label)) state.planParts.push(label);
}
function planTitle(){
  const p=state.planParts||[];
  return !p.length?"":p.length<=2?p.join(" + "):p[0]+" + "+(p.length-1)+" more";
}
function addToPlan(id,lvl){
  notePlan(skillName(id)+" "+roman(lvl));
  const i=state.plan.findIndex(e=>e[0]===id);
  if(i>=0) state.plan[i][1]=lvl; else state.plan.push([id,lvl]);
  renderPlanList();   // skill browser stays exactly where it was
}
async function savePlan(){
  const name=(prompt("Plan name:",planTitle())||"").trim(); if(!name) return;
  state.plans=await api("/api/plans",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name,entries:state.plan})});
  state.planParts=[name];
  renderPlanList();
}
async function deletePlan(){
  const name=$("#plansel").value; if(!name) return;
  state.plans=await api("/api/plans",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name,delete:true})});
  renderPlanList();
}
function loadPlan(name){
  if(name && state.plans[name]){ state.plan=state.plans[name].map(e=>[...e]); state.planParts=[name];
    renderPlanList(); }
}
async function computePlan(){
  if(!state.plan.length) return;
  const cid=state.planChar;
  const d=cid?state.chars[cid]:null;
  $("#planresult").innerHTML=`<p class="dim">Computing…</p>`;
  try{
    const r=await api("/api/plan/compute",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({entries:state.plan,
        skills:d?d.skills:{}, attributes:d?d.attributes:null})});
    const saved=r.total_minutes-(r.remap_minutes??r.total_minutes);
    $("#planresult").innerHTML=`
      <h3>Result</h3>
      <div class="row">
        <span class="pill">Total: <b>${num(r.total_sp)} SP</b></span>
        <span class="pill">Training time:
          <b>${dur(r.total_minutes)}</b></span>
        ${r.remap?`<span class="pill">With optimal remap:
          <b>${dur(r.remap_minutes)}</b>
          <span class="good">(−${dur(saved)})</span></span>`:""}
      </div>
      ${r.remap?`<p class="dim">Suggested remap:
        Int ${r.remap.intelligence} · Mem ${r.remap.memory}
        · Per ${r.remap.perception} · Wil ${r.remap.willpower}
        · Cha ${r.remap.charisma} (implants not included)</p>`:""}
      <div class="steps"><table>
        <tr><th>#</th><th>Skill</th><th class="num">SP needed</th>
        <th class="num">Time</th></tr>
        ${r.steps.map((s,i)=>`<tr><td>${i+1}</td>
          <td>${esc(s.name)} ${roman(s.level)}</td>
          <td class="num">${num(s.sp)}</td>
          <td class="num">${dur(s.minutes)}</td></tr>`).join("")}
      </table></div>`;
  }catch(e){ $("#planresult").innerHTML=""; toast(e.message); }
}

/* ---------------- Assets ---------------- */
function rAssets(m){
  m.innerHTML=`<div class="card"><h2>Assets</h2>
    <div class="row">${charSelect("aChar")}
    <button class="btn" onclick="loadAssets()">Load assets</button>
    <input id="aSearch" size="22" placeholder="Search items…"
      oninput="renderAssets()">
    <select id="aCat" onchange="fillGroupFilter();renderAssets()">
      <option value="">All categories</option></select>
    <select id="aGrp" onchange="renderAssets()">
      <option value="">All groups</option></select>
    <span class="dim">Value:</span>
    <input id="aMin" size="8" placeholder="min ISK" inputmode="numeric"
      oninput="renderAssets()">
    <input id="aMax" size="8" placeholder="max ISK" inputmode="numeric"
      oninput="renderAssets()">
    </div>
    <p class="muted-note">Values use ESI's global average prices. Category =
    broad type (Ship, Module, Charge, Drone…), Group = precise type (Missile
    Launchers, Projectile Weapons…). Min/max filter on total value per item
    (e.g. min 10000 hides the junk). Click column headers to sort. The first
    load fetches category data for your items; afterwards it's cached.</p>
    <div id="assetsOut"></div></div>`;
  if(state._assets){ fillAssetFilters(); renderAssets(); }
}
function fillAssetFilters(){
  const cEl=$("#aCat"); if(!cEl||!state._assets) return;
  const cats=[...new Set(state._assets.map(r=>r.category))].sort();
  const keep=cEl.value;
  cEl.innerHTML=`<option value="">All categories</option>`+
    cats.map(c=>`<option ${c===keep?"selected":""}>${esc(c)}</option>`)
      .join("");
  fillGroupFilter();
}
function fillGroupFilter(){
  const gEl=$("#aGrp"); if(!gEl||!state._assets) return;
  const cat=$("#aCat").value;
  const grps=[...new Set(state._assets
    .filter(r=>!cat||r.category===cat).map(r=>r.group))].sort();
  const keep=gEl.value;
  gEl.innerHTML=`<option value="">All groups</option>`+
    grps.map(g=>`<option ${g===keep?"selected":""}>${esc(g)}</option>`)
      .join("");
}
function charSelect(id){
  return `<select id="${id}" onchange="setActive(this.value,false)">${
    state.status.characters.map(c=>
    `<option value="${c.character_id}" ${
      String(c.character_id)===String(state.activeChar)?"selected":""}>${
      esc(c.name)}</option>`).join("")}
  </select>`;
}
async function loadAssets(){
  const cid=$("#aChar")?.value;
  if(!cid){ toast("Add a character first."); return; }
  $("#assetsOut").innerHTML=`<p class="dim">Loading (may take ~30s for big
    hangars)…</p>`;
  try{
    state._assets=await api("/api/character/"+cid+"/assets");
    state._aSort={key:"total_value",dir:-1};
    fillAssetFilters();
    renderAssets();
  }catch(e){ $("#assetsOut").innerHTML=""; toast(e.message); }
}
function sortAssets(key){
  const s=state._aSort||{key:"total_value",dir:-1};
  state._aSort = s.key===key ? {key,dir:-s.dir} : {key,dir:-1};
  renderAssets();
}
function renderAssets(){
  if(!state._assets || !$("#assetsOut")) return;
  const min=parseFloat($("#aMin")?.value)||0;
  const max=parseFloat($("#aMax")?.value)||Infinity;
  const q=($("#aSearch")?.value||"").toLowerCase().trim();
  const cat=$("#aCat")?.value||"", grp=$("#aGrp")?.value||"";
  const s=state._aSort||{key:"total_value",dir:-1};
  const rows=state._assets
    .filter(r=>r.total_value>=min && r.total_value<=max
      && (!q||(r.name+" "+r.location+" "+r.where).toLowerCase().includes(q))
      && (!cat||r.category===cat) && (!grp||r.group===grp))
    .sort((a,b)=>{
      const x=a[s.key],y=b[s.key];
      return (typeof x==="string" ? x.localeCompare(y) : x-y)*s.dir;
    });
  const total=rows.reduce((a,r)=>a+r.total_value,0);
  const arrow=k=>s.key===k?(s.dir<0?" ↓":" ↑"):"";
  const whereChip=w=>{
    const fit=w.startsWith("Fitted"), cargo=/^(Cargo|Drone bay|Fuel bay|Ore hold|Fleet hangar|Escape bay|Fighter bay)/.test(w);
    return `<span class="chip ${fit?"chip-fit":cargo?"chip-cargo":""}">${esc(w)}</span>`;
  };
  $("#assetsOut").innerHTML=`
    <p><span class="pill">Filtered total: <b>${isk(total)}</b></span>
    <span class="pill">${num(rows.length)} of
      ${num(state._assets.length)} rows</span>
    <span class="dim" style="font-size:12px">blueprint copies are valued
      at 0 and marked (Copy)</span></p>
    <table><tr>
      <th class="sortable" onclick="sortAssets('name')">Item${arrow("name")}</th>
      <th class="sortable" onclick="sortAssets('category')">Category${arrow("category")}</th>
      <th class="sortable" onclick="sortAssets('group')">Group${arrow("group")}</th>
      <th class="sortable num" onclick="sortAssets('quantity')">Qty${arrow("quantity")}</th>
      <th class="sortable num" onclick="sortAssets('unit_price')">Unit${arrow("unit_price")}</th>
      <th class="sortable num" onclick="sortAssets('total_value')">Value${arrow("total_value")}</th>
      <th class="sortable" onclick="sortAssets('location')">Location${arrow("location")}</th>
      <th class="sortable" onclick="sortAssets('where')">Where${arrow("where")}</th>
      </tr>`+
    rows.slice(0,400).map(r=>`<tr><td>${esc(r.name)}</td>
      <td class="dim">${esc(r.category)}</td>
      <td class="dim">${esc(r.group)}</td>
      <td class="num">${num(r.quantity)}</td>
      <td class="num">${isk(r.unit_price)}</td>
      <td class="num">${isk(r.total_value)}</td>
      <td class="dim">${esc(r.location)}</td>
      <td>${whereChip(r.where)}</td>
      </tr>`).join("")+
    (rows.length>400?`<tr><td colspan="8" class="dim">…and
      ${rows.length-400} more (narrow the filter to see them)</td></tr>`:"")+
    `</table>`;
}

/* ---------------- Wallet ---------------- */
const WCAT={
  mission:["agent_mission_reward","agent_mission_time_bonus_reward"],
  bounty:["bounty_prizes","ess_escrow_transfer"],
  market:["market_transaction","market_escrow","transaction_tax","brokers_fee"]};
function rWallet(m){
  m.innerHTML=`<div class="card"><h2>Wallet</h2>
    <div class="row">${charSelect("wChar")}
    <button class="btn" onclick="loadWallet()">Load journal</button>
    <span class="dim">Date:</span>
    <input type="date" id="wFrom" onchange="renderWallet()">
    <span class="dim">to</span>
    <input type="date" id="wTo" onchange="renderWallet()">
    <button class="btn sec" onclick="wPreset(7)">7 days</button>
    <button class="btn sec" onclick="wPreset(30)">30 days</button>
    <button class="btn sec" onclick="wPreset('month')">This month</button>
    <button class="btn sec" onclick="wPreset(0)">All time</button>
    </div>
    <div class="row" style="margin-top:8px">
    <span class="dim">Entry type:</span>
    <select id="wType" onchange="renderWallet()"><option value="">All types</option></select>
    <span class="dim">Amount:</span>
    <input id="wMin" size="9" placeholder="min ISK" inputmode="numeric"
      oninput="renderWallet()">
    <input id="wMax" size="9" placeholder="max ISK" inputmode="numeric"
      oninput="renderWallet()">
    </div>
    <p class="muted-note">ESI only provides the last 30 days of journal, so
    EveWatch saves every entry it sees to a local archive (and quietly tops it
    up every couple of hours while the app is running). History therefore
    grows from the day you first loaded this. The amount filter applies to the
    transaction size, ignoring +/-. Click column headers to sort.</p>
    <div id="walletOut"></div></div>
    <div class="card"><h2>Income summary</h2><div id="incomeOut"
      class="dim">Load the journal to see your income for the selected dates.</div></div>
    <div class="card"><h2>Loyalty points</h2>
    <div class="row">
      <button class="btn sec" onclick="loadLP()">Load loyalty points</button>
    </div>
    <p class="muted-note">Needs the optional loyalty-points scope — switch it on
    under Help &amp; settings → Setup &amp; permissions (and enable them on your developer application first), then
    use ⚙ → Update permissions on your characters. Each time you load these, EveWatch records a
    snapshot, so changes since your first load are shown as trends.</p>
    <div id="lpOut"></div><div id="stOut"></div></div>`;
  if(state._journal){ fillWalletTypes(); renderWallet(); }
}
function isoDay(d){ return d.toISOString().slice(0,10); }
function wPreset(p){
  const to=new Date(); let from=null;
  if(p==="month"){ from=new Date(Date.UTC(to.getUTCFullYear(),to.getUTCMonth(),1)); }
  else if(p>0){ from=new Date(Date.now()-p*86400000); }
  $("#wFrom").value=from?isoDay(from):""; $("#wTo").value=p?isoDay(to):"";
  renderWallet();
}
function fillWalletTypes(){
  const el=$("#wType"); if(!el||!state._journal) return;
  const keep=el.value;
  const types=[...new Set(state._journal.map(r=>r.ref_type))].sort();
  el.innerHTML=`<option value="">All types</option>`+types.map(t=>
    `<option value="${esc(t)}" ${t===keep?"selected":""}>${
      esc(t.replaceAll("_"," "))}</option>`).join("");
}
async function loadWallet(){
  const cid=$("#wChar")?.value;
  if(!cid){ toast("Add a character first."); return; }
  $("#walletOut").innerHTML=`<p class="dim">Loading and archiving the journal…</p>`;
  try{
    const r=await api("/api/character/"+cid+"/journal");
    state._journal=r.entries; state._jSince=r.since; state._jCount=r.count;
    state._jBal=state.chars[cid]?.wallet;
    state._jSort={key:"date",dir:-1};
    fillWalletTypes(); renderWallet();
  }catch(e){ $("#walletOut").innerHTML=""; toast(e.message); }
}
function sortWallet(key){
  const s=state._jSort||{key:"date",dir:-1};
  state._jSort = s.key===key ? {key,dir:-s.dir} : {key,dir:-1};
  renderWallet();
}
function renderWallet(){
  if(!state._journal || !$("#walletOut")) return;
  const min=parseFloat($("#wMin")?.value)||0;
  const max=parseFloat($("#wMax")?.value)||Infinity;
  const from=$("#wFrom")?.value||"", to=$("#wTo")?.value||"";
  const type=$("#wType")?.value||"";
  const s=state._jSort||{key:"date",dir:-1};
  const inRange=state._journal.filter(r=>{
    const d=(r.date||"").slice(0,10);
    return (!from||d>=from)&&(!to||d<=to);
  });
  const rows=inRange
    .filter(r=>{const a=Math.abs(r.amount||0);
      return a>=min&&a<=max&&(!type||r.ref_type===type);})
    .sort((a,b)=>{
      let x=a[s.key],y=b[s.key];
      if(s.key==="amount"){x=Math.abs(x||0);y=Math.abs(y||0);}
      return (typeof x==="string" ? x.localeCompare(y) : (x||0)-(y||0))*s.dir;
    });
  renderIncome(inRange,from,to);
  const sum=rows.reduce((a,r)=>a+(r.amount||0),0);
  const arrow=k=>s.key===k?(s.dir<0?" ↓":" ↑"):"";
  const arch=`<span class="pill">Archive: <b>${num(state._jCount)}</b> entries
    since <b>${esc(state._jSince||"—")}</b></span>`;
  if(!state._journal.length){
    $("#walletOut").innerHTML=`
      <p><span class="pill">Balance: <b>${isk(state._jBal)}</b></span></p>
      <p class="dim">No wallet activity yet — ESI only provides the last 30
      days, so quiet periods show an empty list. Anything that happens from
      now on will be archived.</p>`;
    return;
  }
  $("#walletOut").innerHTML=`
    <p><span class="pill">Balance: <b>${isk(state._jBal)}</b></span>${arch}
    <span class="pill">Shown: ${num(rows.length)} entries, net
      <b class="${sum>=0?'good':'bad'}">${isk(sum)}</b></span></p>
    <div style="max-height:60vh;overflow:auto"><table><tr>
      <th class="sortable" onclick="sortWallet('date')">Date${arrow("date")}</th>
      <th class="sortable" onclick="sortWallet('ref_type')">Type${arrow("ref_type")}</th>
      <th class="sortable num" onclick="sortWallet('amount')">Amount${arrow("amount")}</th>
      <th class="sortable num" onclick="sortWallet('balance')">Balance${arrow("balance")}</th>
      <th>Description</th></tr>`+
    rows.slice(0,1500).map(r=>`<tr>
      <td class="dim">${(r.date||"").replace("T"," ").slice(0,16)}</td>
      <td>${esc((r.ref_type||"").replaceAll("_"," "))}</td>
      <td class="num ${r.amount>=0?'good':'bad'}">${isk(r.amount)}</td>
      <td class="num">${isk(r.balance)}</td>
      <td class="dim">${esc((r.description||"").slice(0,60))}</td>
      </tr>`).join("")+`</table></div>`+
    (rows.length>1500?`<p class="dim">…and ${num(rows.length-1500)} more
      (narrow the date range to see them)</p>`:"");
}
function renderIncome(rows,from,to){
  const el=$("#incomeOut"); if(!el) return;
  const sum={mission:0,bonus:0,bounty:0,market:0,other:0,net:0};
  let missions=0; const perDay={};
  rows.forEach(r=>{
    const a=r.amount||0, t=r.ref_type, d=(r.date||"").slice(0,10);
    sum.net+=a;
    const day=perDay[d]=perDay[d]||{mission:0,bounty:0};
    if(t==="agent_mission_reward"){ sum.mission+=a; missions++; day.mission+=a; }
    else if(t==="agent_mission_time_bonus_reward"){ sum.bonus+=a; day.mission+=a; }
    else if(WCAT.bounty.includes(t)){ sum.bounty+=a; day.bounty+=a; }
    else if(WCAT.market.includes(t)) sum.market+=a;
    else sum.other+=a;
  });
  const days=Object.keys(perDay).sort();
  if(!days.length){ el.textContent="No journal entries in the selected dates."; return; }
  const span=Math.max(1,Math.round((new Date(days[days.length-1])-new Date(days[0]))/86400000)+1);
  const earned=sum.mission+sum.bonus+sum.bounty;
  // fill gaps so the chart shows quiet days too
  const series=[]; let max=1;
  for(let i=0;i<Math.min(span,200);i++){
    const d=isoDay(new Date(new Date(days[0]).getTime()+i*86400000));
    const v=perDay[d]||{mission:0,bounty:0};
    max=Math.max(max,v.mission+v.bounty); series.push([d,v]);
  }
  const bw=Math.max(3,Math.min(26,Math.floor(900/series.length)-2));
  const W=series.length*(bw+2), H=110;
  el.className="";
  el.innerHTML=`<p>
    <span class="pill">Mission rewards: <b>${isk(sum.mission)}</b>
      (${num(missions)} mission${missions===1?"":"s"}${missions?
      `, avg ${isk(sum.mission/missions)}`:""})</span>
    <span class="pill">Time bonuses: <b>${isk(sum.bonus)}</b></span>
    <span class="pill">Bounties: <b>${isk(sum.bounty)}</b></span>
    <span class="pill">Market (net): <b>${isk(sum.market)}</b></span>
    <span class="pill">Other: <b>${isk(sum.other)}</b></span>
    <span class="pill">Net change: <b class="${sum.net>=0?"good":"bad"}">${isk(sum.net)}</b></span></p>
    <p><span class="pill">Missions + bounties: <b>${isk(earned)}</b> over
      ${span} day${span>1?"s":""} → <b>${isk(earned/span)}</b> per day</span></p>
    <div style="overflow-x:auto"><svg width="${W}" height="${H+18}">
      ${series.map(([d,v],i)=>{
        const hm=v.mission/max*H, hb=v.bounty/max*H, x=i*(bw+2);
        return `<g><title>${d}: missions ${num(Math.round(v.mission))} ISK, bounties ${
          num(Math.round(v.bounty))} ISK</title>
          <rect x="${x}" y="${H-hm-hb}" width="${bw}" height="${hb}" fill="var(--gold)"/>
          <rect x="${x}" y="${H-hm}" width="${bw}" height="${hm}" fill="var(--acc)"/></g>`;
      }).join("")}
      <text x="0" y="${H+14}" fill="var(--dim)" font-size="11">${series[0][0]}</text>
      <text x="${W}" y="${H+14}" fill="var(--dim)" font-size="11" text-anchor="end">${series[series.length-1][0]}</text>
    </svg></div>
    <p class="muted-note"><span style="color:var(--acc)">▉</span> mission
    rewards + time bonuses · <span style="color:var(--gold)">▉</span> bounties.
    Missions can't be named individually — ESI doesn't expose which mission or
    agent a payment came from.</p>`;
}
async function loadLP(){
  const cid=$("#wChar")?.value; if(!cid){ toast("Add a character first."); return; }
  $("#lpOut").innerHTML=`<p class="dim">Loading…</p>`;
  try{
    const r=await api("/api/character/"+cid+"/loyalty");
    $("#lpOut").innerHTML=r.length?`<h3>Loyalty points</h3><table><tr>
      <th>Corporation</th><th class="num">LP</th><th class="num">Change${
      r[0].since?` since ${esc(r[0].since)}`:""}</th></tr>`+
      r.map(x=>`<tr><td>${esc(x.corporation)}</td><td class="num">${num(x.points)}</td>
      <td class="num ${x.delta>0?"good":x.delta<0?"bad":"dim"}">${
        x.delta>0?"+":""}${num(x.delta)}</td></tr>`).join("")+`</table>`
      :`<p class="dim">No loyalty points.</p>`;
  }catch(e){ $("#lpOut").innerHTML=""; scopeHint(e); }
}
function scopeHint(e){
  toast(/403|scope|token/i.test(e.message)
    ?"This needs the optional loyalty-points permission — switch it on under Help & settings → General → Setup & permissions, then use ⚙ → Update permissions on the character."
    :e.message);
}
async function loadStandings(){
  const cid=$("#wChar")?.value; if(!cid){ toast("Add a character first."); return; }
  $("#stOut").innerHTML=`<p class="dim">Loading…</p>`;
  try{ state._standings=await api("/api/character/"+cid+"/standings");
    renderStandings(); }
  catch(e){ $("#stOut").innerHTML=""; scopeHint(e); }
}
function renderStandings(){
  const el=$("#stOut"); if(!el||!state._standings) return;
  const t=$("#stType")?.value||"", q=($("#stSearch")?.value||"").toLowerCase();
  const rows=state._standings.filter(x=>(!t||x.type===t)
    &&(!q||x.name.toLowerCase().includes(q)));
  el.innerHTML=`<h3>Standings (${num(rows.length)})</h3>
    <div style="max-height:50vh;overflow:auto"><table><tr><th>Name</th><th>Type</th>
    <th class="num">Standing</th><th class="num">Change${
    state._standings[0]?.since?` since ${esc(state._standings[0].since)}`:""}</th></tr>`+
    rows.slice(0,500).map(x=>`<tr><td>${esc(x.name)}</td>
      <td class="dim">${esc(x.type.replace("npc_corp","corporation"))}</td>
      <td class="num ${x.standing>0?"good":x.standing<0?"bad":""}">${x.standing.toFixed(2)}</td>
      <td class="num ${x.delta>0?"good":x.delta<0?"bad":"dim"}">${
        x.delta>0?"+":""}${x.delta.toFixed(2)}</td></tr>`).join("")+
    `</table></div>`;
}

/* ---------------- Market ---------------- */
const CATEGORY_PRESETS=["Sales Hub","Combat Mission L4","Distro Mission L4",
  "Mining","Refinery","Producer","Other"];
function categorySelect(id,extraOpt){
  return `<select id="${id}">${extraOpt||""}${CATEGORY_PRESETS.map(c=>
    `<option>${esc(c)}</option>`).join("")}</select>`;
}
function rMarket(m){
  m.innerHTML=`
  <div class="card"><h2>My locations</h2>
    <div class="row" style="margin-bottom:8px">
      <span class="dim">Show:</span>
      <select id="locCatFilter" onchange="renderLocations()">
        <option value="">All categories</option>
        ${CATEGORY_PRESETS.map(c=>`<option>${esc(c)}</option>`).join("")}
      </select>
    </div>
    <div id="locList" class="dim">Loading…</div>

    <h3 style="margin-top:16px">Add one by system search</h3>
    <div class="row">
      <input id="sysq" size="24"
        placeholder="Solar system name, e.g. Rens"
        onkeydown="if(event.key==='Enter')findStations()">
      <button class="btn sec" onclick="findStations()">Find stations</button>
      <span class="dim">as:</span>
      ${categorySelect("sysCat")}
    </div>
    <div id="stationResults"></div>

    <h3 style="margin-top:16px">Bulk import (paste a bookmark folder)</h3>
    <p class="muted-note">Paste NPC station names, one per line — exactly
    as they appear in-game (e.g. your Combat Mission L4 or Distro Mission
    L4 bookmark folders), pick a category, and import. Player-owned
    structures (many Refineries/Producers) can't be found by name — use
    "Add structure by ID" below for those instead.</p>
    <div class="row">
      ${categorySelect("bulkCat")}
    </div>
    <textarea id="bulkText" rows="5" style="width:100%;margin-top:8px;
      background:var(--panel2);border:1px solid var(--line);color:var(--txt);
      border-radius:6px;padding:8px;font:12.5px monospace"
      placeholder="Jita IV - Moon 4 - Caldari Navy Assembly Plant
Amarr VIII (Oris) - Emperor Family Academy
…one station name per line"></textarea>
    <div class="row" style="margin-top:8px">
      <button class="btn" onclick="runBulkImport()">Import list</button>
    </div>
    <div id="bulkOut"></div>

    <h3 style="margin-top:16px">Add player structure by ID</h3>
    <p class="muted-note">For Upwell structures (Astrahus, Athanor,
    Raitaru…) that don't show up in name search. You need the structure ID
    (visible in the station list when docked, or in a market/contract URL)
    and a character who has docking access there.</p>
    <div class="row">
      ${charSelect("structChar")}
      <input id="structId" size="16" placeholder="Structure ID"
        inputmode="numeric">
      ${categorySelect("structCat")}
      <button class="btn sec" onclick="addStructure()">Add</button>
    </div>
    <div id="structOut"></div>

    <div class="row" style="margin-top:12px">
      <button class="btn sec" onclick="exportLocations()">Export list</button>
      <button class="btn sec" onclick="importLocations()">Import
        (.json backup)</button>
    </div>
  </div>
  <div class="card"><h2>Price check</h2>
    <div class="row">
      <input id="mq" size="30"
        placeholder="Start typing an item name, e.g. 1400"
        onkeydown="if(event.key==='Enter')marketLookup()">
      <button class="btn" onclick="marketLookup()">Compare prices</button>
    </div>
    <div id="marketOut"></div>
  </div>
  <div class="card"><h2>Price watch — movers &amp; alerts</h2>
    <div class="row">
      ${charSelect("mvChar")}
      <span class="dim">Alert at ±</span>
      <input id="mvThr" size="4" value="10" inputmode="numeric">
      <span class="dim">%</span>
      <button class="btn" onclick="loadMovers('owned')">Scan my items</button>
      <button class="btn sec" onclick="loadMovers('overview')">
        Biggest movers</button>
      <button class="btn sec" onclick="buildIndex()">Build market index</button>
      <span id="idxStatus" class="dim" style="font-size:12px"></span>
    </div>
    <p class="muted-note">Compares Jita (The Forge) daily price history over
    the last 90 days — daily granularity, so "1d" = last completed day vs
    the day before. "Scan my items" watches your top 120 item types by
    value. "Biggest movers" shows the top 50 movers across the whole
    market (liquid items only) once the index is built — building fetches
    history for ~15k items over ~2 hours in the background, then refreshes
    are incremental. Until then it falls back to a commodity basket.
    Click column headers to sort.</p>
    <div id="mvOut"></div>
  </div>
  <div class="card"><h2>Trade routes — ISK/hour</h2>
    <div class="row">
      <input id="trItem" size="26"
        placeholder="Start typing an item name"
        onkeydown="if(event.key==='Enter')runTradeScan()">
      <select id="trShip" onchange="applyShipPreset()"></select>
      <span class="dim">min/jump</span>
      <input id="trMpj" size="4" value="1.2" inputmode="numeric"
        oninput="saveTradePrefs()">
      <span class="dim">cargo m³</span>
      <input id="trCargo" size="8" value="5500" inputmode="numeric"
        oninput="saveTradePrefs()">
      <span class="dim">tax %</span>
      <input id="trTax" size="4" value="5" inputmode="numeric"
        oninput="saveTradePrefs()">
    </div>
    <div class="row" style="margin-top:6px">
      <span class="dim">From:</span>
      <select id="trFromCat" onchange="saveTradePrefs()">
        <option value="">Any category</option>
        ${CATEGORY_PRESETS.map(c=>`<option>${esc(c)}</option>`).join("")}
      </select>
      <span class="dim">To:</span>
      <select id="trToCat" onchange="saveTradePrefs()">
        <option value="">Any category</option>
        ${CATEGORY_PRESETS.map(c=>`<option>${esc(c)}</option>`).join("")}
      </select>
      <button class="btn" onclick="runTradeScan()">Find routes</button>
    </div>
    <p class="muted-note">Checks every pair of your saved hubs: buy from
    sell orders at A, deliver to buy orders at B. One-way trip, prefer-
    high-sec routing, +6 min per trip for docking and loading. Units per
    trip are capped by cargo space and by order depth within 2% of the
    best price.</p>
    <div id="trOut"></div>
  </div>
  <div class="card" id="hsCard"><h2>Hub scanner — find hauling opportunities</h2>
    <p class="muted-note">Finds items that are cheap in one of your hubs and dear in another, with real
    trading activity at the destination — even if you don't own any. It reads every order in the
    hubs' regions (a few minutes for the whole market; much faster for selected categories), then
    ranks the gaps by ISK per hour. Change the filters below at any time without rescanning.</p>
    <div class="row"><b>Hubs:</b><span id="hsHubs"></span></div>
    <div class="row" style="margin-top:8px"><b>Scan:</b>
      <label><input type="radio" name="hsScope" value="all" checked onchange="hsScopeChanged()"> the whole market</label>
      <label><input type="radio" name="hsScope" value="sel" onchange="hsScopeChanged()"> only selected item groups</label>
      <button class="btn" id="hsGo" onclick="hsStart()">Scan hubs</button>
      <button class="btn sec" id="hsStop" style="display:none" onclick="api('/api/hubscan/cancel',{method:'POST'})">Stop</button>
      <span id="hsStatus" class="dim"></span></div>
    <div id="hsSel" style="display:none;margin-top:8px"></div>
    <div id="hsFilters"></div>
    <div id="hsOut"></div></div>
  <div class="card" id="apCard"><h2>Appraisal</h2>
    <p class="muted-note">Copy items from any in-game inventory
    (Ctrl+A, Ctrl+C) and paste them here. Formats like
    "Tritanium&nbsp;x&nbsp;1000" work too. Big lists are priced in small
    batches with running totals — you can pause, cancel, or retry any
    items that fail. Roughly 12 item types are priced every 4–10 seconds.</p>
    <textarea id="apText" rows="7" style="width:100%;background:var(--panel2);
      border:1px solid var(--line);color:var(--txt);border-radius:6px;
      padding:8px;font:12.5px monospace" placeholder="Paste items here…">
    </textarea>
    <div class="row" style="margin-top:8px">
      <span class="dim">Price at:</span>
      <select id="apStation"></select>
      <button class="btn" onclick="runAppraisal()">Appraise</button>
    </div>
    <div id="apOut"></div>
  </div>`;
  loadLocations().then(()=>{
    if(state._pendingMarket){          // jumped here from Item Search
      $("#mq").value=state._pendingMarket; state._pendingMarket=null;
      marketLookup();
    }
  });
  fillShipPresets();
  hsInit();
  pollIndex();
  if(state._movers) renderMovers();
  attachSuggest("mq",marketLookup);
  attachSuggest("trItem");
  ensureTypeNames();
}

/* ---------------- LP Store ---------------- */
const RP = {agents:[], prices:{}, hubs:[], rpEach:100, iskEach:10000};
const LP = {corp:null, offers:[], prices:{}, running:false, cancel:false,
            sort:"per", dir:-1, myLP:{}};
function rLPStore(m){
  m.innerHTML=`<div class="card"><h2>LP store — best ISK per loyalty point</h2>
    <div class="row">
      ${charSelect("lpChar")}
      <button class="btn sec" onclick="lpLoadMine()">Load my corporations</button>
      <select id="lpCorpSel" onchange="lpPickCorp(this.value)">
        <option value="">— pick a corporation —</option></select>
      <span class="dim">or search</span>
      <input id="lpCorpQ" size="26" placeholder="Exact corporation name"
        onkeydown="if(event.key==='Enter')lpSearchCorp()">
      <button class="btn sec" onclick="lpSearchCorp()">Find</button>
    </div>
    <p class="muted-note">Value per LP = (what the item sells for × quantity −
    ISK fee − the cost of any required items) ÷ LP cost, using live Jita 4-4
    prices. Prices load a few items at a time, so a big store takes a minute or
    two; results fill in as they arrive. Loading your corporations needs the
    loyalty-points permission (Help &amp; settings → Setup &amp; permissions); searching by name doesn't.</p>
    <div id="lpStatus"></div>
    <div id="lpControls"></div>
    <div id="lpOut"></div></div>
    <div class="card"><h2>Research points → datacores</h2>
    <div class="row">
      <button class="btn sec" onclick="rpLoad()">Load research agents for the character above</button>
      <span class="dim">Sell datacores at</span>
      <select id="rpBasis" onchange="rpRender()">
        <option value="sell">lowest sell order</option>
        <option value="buy">highest buy order (instant sell)</option></select>
      <span id="rpStatus" class="dim"></span>
    </div>
    <p class="muted-note">Each datacore costs ${RP.rpEach} research points banked with that
    agent plus a ${num(RP.iskEach)} ISK fee, and is always the datacore of the research skill
    you chose with the agent. Current RP = the agent's banked remainder plus points per day
    since you started. Prices come from every market location on the Market tab. Needs the
    research-points permission (Help &amp; settings → Setup &amp; permissions).</p>
    <div id="rpOut"></div></div>`;
  lpFillCorps();
  if(LP.offers.length){ lpControls(); lpRender(); }
}
async function rpLoad(){
  const cid=$("#lpChar")?.value; if(!cid){ toast("Add a character first."); return; }
  $("#rpStatus").textContent="Loading agents…"; $("#rpOut").innerHTML="";
  let r;
  try{ r=await api("/api/character/"+cid+"/research"); }
  catch(e){ $("#rpStatus").innerHTML=`<span class="bad">${esc(e.message)}</span>`;
    toast("Research points: "+e.message+" (needs the research-points permission — Help & settings → Setup & permissions)");
    return; }
  RP.agents=r.agents; RP.rpEach=r.rp_per_datacore; RP.iskEach=r.isk_per_datacore;
  if(!r.agents.length){ $("#rpStatus").textContent="This character has no research agents."; return; }
  const ids=[...new Set(r.agents.map(a=>a.datacore_id).filter(Boolean))];
  for(let i=0;i<ids.length;i++){
    $("#rpStatus").textContent=`Pricing datacores ${i+1} / ${ids.length}…`;
    try{ RP.prices[ids[i]]=await api("/api/market/compare?type_id="+ids[i]); }
    catch(e){ RP.prices[ids[i]]={rows:[],error:e.message}; }
    rpRender();
  }
  $("#rpStatus").textContent=""; rpRender();
}
function rpRender(){
  const el=$("#rpOut"); if(!el||!RP.agents.length) return;
  const basis=$("#rpBasis")?.value||"sell";
  const hubs=[]; Object.values(RP.prices).forEach(p=>(p.rows||[]).forEach(r=>{
    if(!hubs.some(h=>h.station_id===r.station_id)) hubs.push({station_id:r.station_id,name:r.system||r.name}); }));
  let total=0, totalReady=0;
  const rows=RP.agents.map(a=>{
    const p=a.datacore_id?RP.prices[a.datacore_id]:null;
    const per={}; let best=null;
    ((p&&p.rows)||[]).forEach(r=>{
      const v=r[basis]; per[r.station_id]=v;
      if(v!=null&&(best==null||v>best.v)) best={v,name:r.system||r.name}; });
    const net=best?a.ready*(best.v-RP.iskEach):null;
    if(net!=null){ total+=net; } totalReady+=a.ready;
    return {a,per,best,net};
  });
  el.innerHTML=`<table><tr><th>Agent</th><th>Skill / datacore</th><th class="num">RP now</th>
    <th class="num">RP/day</th><th class="num">Datacores ready</th><th class="num">Next in</th>
    ${hubs.map(h=>`<th class="num">${esc(h.name)}</th>`).join("")}
    <th>Best hub</th><th class="num">Net value (ready)</th></tr>`+
  rows.map(({a,per,best,net})=>`<tr>
    <td>${esc(a.agent)}<div class="dim" style="font-size:11.5px">${esc(a.corporation)}</div></td>
    <td>${esc(a.skill)}${a.datacore_id?"":` <span class="warn" title="Couldn't match a datacore named “${esc(a.datacore)}”">⚠</span>`}</td>
    <td class="num">${num(Math.round(a.rp))}</td><td class="num">${num(a.ppd)}</td>
    <td class="num"><b>${num(a.ready)}</b></td>
    <td class="num dim">${a.days_to_next==null?"—":a.days_to_next+" d"}</td>
    ${hubs.map(h=>`<td class="num ${best&&per[h.station_id]===best.v?"good":""}">${
      per[h.station_id]==null?"—":num(Math.round(per[h.station_id]))}</td>`).join("")}
    <td>${best?esc(best.name):"—"}</td>
    <td class="num ${net>=0?"good":"bad"}">${net==null?"—":isk(Math.round(net))}</td></tr>`).join("")+
  `<tr><td colspan="${6+hubs.length}"><b>Total</b> — ${num(totalReady)} datacores ready</td>
    <td></td><td class="num"><b>${isk(Math.round(total))}</b></td></tr></table>
  <p class="muted-note">Net value = datacores ready × (best hub price − ${num(RP.iskEach)} ISK fee).
  Before selling, check how much volume the hub can absorb (Market tab) — a thin market
  will move against you. ⚠ means no datacore was found for that skill's name.</p>`;
}
function lpFillCorps(){
  const sel=$("#lpCorpSel"); if(!sel) return;
  const opts=(LP.corpList||[]).map(c=>`<option value="${c.id}" ${
    LP.corp&&LP.corp.id===c.id?"selected":""}>${esc(c.name)}${
    c.points!=null?" — "+num(c.points)+" LP":""}</option>`).join("");
  sel.innerHTML=`<option value="">— pick a corporation —</option>`+opts;
}
async function lpLoadMine(){
  const cid=$("#lpChar")?.value; if(!cid){ toast("Add a character first."); return; }
  try{
    const rows=await api("/api/character/"+cid+"/loyalty");
    LP.myLP={};
    LP.corpList=rows.filter(r=>r.corporation_id).map(r=>{
      LP.myLP[r.corporation_id]=r.points;
      return {id:r.corporation_id,name:r.corporation,points:r.points}; });
    if(!LP.corpList.length) toast("No loyalty points on that character.");
    lpFillCorps();
  }catch(e){ toast("Loyalty points: "+e.message+
    " (needs the loyalty-points permission — Help & settings → Setup & permissions)"); }
}
async function lpSearchCorp(){
  const q=$("#lpCorpQ").value.trim(); if(!q) return;
  try{
    const r=await api("/api/lp/corp?q="+encodeURIComponent(q));
    if(!r.length){ toast("No corporation with exactly that name."); return; }
    LP.corpList=[...(LP.corpList||[]).filter(c=>!r.some(x=>x.id===c.id)),
      ...r.map(x=>({id:x.id,name:x.name,points:LP.myLP[x.id]}))];
    lpFillCorps(); $("#lpCorpSel").value=r[0].id; lpPickCorp(r[0].id);
  }catch(e){ toast(e.message); }
}
async function lpPickCorp(id){
  if(!id) return;
  LP.cancel=true;                      // stop any pricing run in progress
  LP.corp=(LP.corpList||[]).find(c=>String(c.id)===String(id))||{id:+id,name:id};
  $("#lpStatus").innerHTML=`<p class="dim">Loading the store…</p>`;
  $("#lpOut").innerHTML=""; $("#lpControls").innerHTML="";
  let r;
  try{ r=await api("/api/lp/offers?corp_id="+id); }
  catch(e){ $("#lpStatus").innerHTML=`<p class="bad">${esc(e.message)}</p>`; return; }
  if(r.error){ $("#lpStatus").innerHTML=`<p class="bad">${esc(r.error)}</p>`; return; }
  LP.corp={id:r.corp_id,name:r.corp}; LP.offers=r.offers; LP.typeIds=r.type_ids;
  lpControls(); lpRender();
  await sleep(400); LP.cancel=false;
  lpPriceAll();
}
function lpControls(){
  const el=$("#lpControls"); if(!el) return;
  el.innerHTML=`<div class="row" style="margin:10px 0">
    <span class="dim">Sell items at</span>
    <select id="lpBasis" onchange="lpRender()">
      <option value="buy">highest Jita buy order (instant sell)</option>
      <option value="sell">lowest Jita sell order (patient sell)</option></select>
    <input id="lpSearch" size="18" placeholder="Filter items…" oninput="lpRender()">
    <label><input type="checkbox" id="lpAfford" onchange="lpRender()">
      only what I can afford${LP.myLP[LP.corp.id]!=null?
        " ("+num(LP.myLP[LP.corp.id])+" LP)":""}</label>
    <label><input type="checkbox" id="lpHide" checked onchange="lpRender()">
      hide items with no price</label>
    <button class="btn sec" id="lpStop" onclick="LP.cancel=true">Stop pricing</button>
    <button class="btn sec" onclick="lpPriceAll()">Retry missing prices</button>
  </div>`;
}
async function lpPriceAll(){
  if(LP.running) return;
  const todo=(LP.typeIds||[]).filter(t=>!LP.prices[t]||LP.prices[t].error);
  if(!todo.length){ lpStatus(); return; }
  LP.running=true; LP.cancel=false; let delay=3000;
  for(let i=0;i<todo.length;i+=AP_BATCH){
    if(LP.cancel) break;
    const batch=todo.slice(i,i+AP_BATCH); let failed=false; const t0=Date.now();
    lpStatus(`Pricing ${Math.min(i+AP_BATCH,todo.length)} / ${todo.length}…`);
    try{
      const r=await api("/api/market/appraise_batch",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({station_id:60003760,type_ids:batch})});
      if(r.error){ lpStatus("Price lookup failed: "+r.error,true); break; }
      for(const [tid,p] of Object.entries(r.prices)){
        LP.prices[tid]=p; if(p.error) failed=true; }
    }catch(e){ batch.forEach(t=>{ LP.prices[t]={error:e.message}; }); failed=true; }
    if(state.tab==="lpstore") lpRender();
    delay=failed?Math.min(delay*2,20000):Math.max(2500,Math.min(8000,(Date.now()-t0)*0.5));
    if(i+AP_BATCH<todo.length) await sleep(delay);
  }
  LP.running=false; lpStatus(); if(state.tab==="lpstore") lpRender();
}
function lpStatus(msg,bad){
  const el=$("#lpStatus"); if(!el) return;
  const failed=(LP.typeIds||[]).filter(t=>LP.prices[t]?.error).length;
  const done=(LP.typeIds||[]).filter(t=>LP.prices[t]&&!LP.prices[t].error).length;
  el.innerHTML=msg?`<p class="${bad?"bad":"dim"}">${esc(msg)}</p>`:
    `<p class="dim">${esc(LP.corp?.name||"")} — ${LP.offers.length} offers ·
    ${done}/${(LP.typeIds||[]).length} prices loaded${
    failed?` · <span class="bad">${failed} failed (use Retry)</span>`:""}</p>`;
}
function lpCalc(o,basis){
  const px=(tid,b)=>{ const p=LP.prices[tid]; if(!p||p.error) return undefined;
    const v=p[b]; return v==null?null:v; };
  const unit=px(o.type_id,basis);
  const known=LP.prices[o.type_id]&&!LP.prices[o.type_id].error;
  if(unit===undefined) return {state:"pending"};
  let reqCost=0;
  for(const r of o.req){
    const v=px(r.type_id,"sell");
    if(v===undefined) return {state:"pending"};
    if(v===null) return {state:"noprice",why:"required item "+r.name+" has no Jita sell order"};
    reqCost+=v*r.qty;
  }
  if(unit===null) return {state:"noprice",why:"no Jita order for this item"};
  const gross=unit*o.qty, profit=gross-o.isk-reqCost;
  const avg=LP.prices[o.type_id].average;
  // a price far above the CCP average on a thin market is probably a trap
  const odd=avg&&unit>avg*3;
  return {state:"ok",gross,reqCost,profit,per:o.lp>0?profit/o.lp:null,odd,unit};
}
function lpSort(k){ if(LP.sort===k) LP.dir=-LP.dir; else{ LP.sort=k; LP.dir=-1; }
  lpRender(); }
function lpRender(){
  const el=$("#lpOut"); if(!el||!LP.offers.length) return;
  lpStatus();
  const basis=$("#lpBasis")?.value||"buy", q=($("#lpSearch")?.value||"").toLowerCase();
  const afford=$("#lpAfford")?.checked, hide=$("#lpHide")?.checked;
  const mine=LP.myLP[LP.corp.id];
  let rows=LP.offers.map(o=>({o,c:lpCalc(o,basis)}))
    .filter(r=>!q||r.o.name.toLowerCase().includes(q))
    .filter(r=>!afford||mine==null||r.o.lp<=mine)
    .filter(r=>!(hide&&r.c.state==="noprice"));
  const val=r=>({per:r.c.per,profit:r.c.profit,lp:r.o.lp,isk:r.o.isk,
    name:r.o.name.toLowerCase()})[LP.sort];
  rows.sort((a,b)=>{
    const x=val(a),y=val(b);
    if(x==null||x===undefined) return 1; if(y==null||y===undefined) return -1;
    return (x<y?-1:x>y?1:0)*LP.dir; });
  const th=(k,l,cls)=>`<th class="sortable ${cls||""}" onclick="lpSort('${k}')">${l}${
    LP.sort===k?(LP.dir<0?" ▼":" ▲"):""}</th>`;
  el.innerHTML=`<table><tr>${th("name","Item")}<th class="num">Qty</th>
    ${th("lp","LP cost","num")}${th("isk","ISK fee","num")}
    <th>Also needs</th><th class="num">Value</th>
    ${th("profit","Profit","num")}${th("per","ISK per LP","num")}</tr>`+
  rows.slice(0,400).map(({o,c})=>{
    if(c.state!=="ok") return `<tr><td>${esc(o.name)}</td>
      <td class="num">${num(o.qty)}</td><td class="num">${num(o.lp)}</td>
      <td class="num">${num(o.isk)}</td><td>${o.req.map(r=>
        num(r.qty)+"× "+esc(r.name)).join(", ")}</td>
      <td colspan="3" class="dim num">${c.state==="pending"?"pricing…":esc(c.why)}</td></tr>`;
    return `<tr><td>${esc(o.name)}${c.odd?` <span class="warn"
        title="Price is over 3× the average — likely a thin market, treat with care">⚠</span>`:""}</td>
      <td class="num">${num(o.qty)}</td><td class="num">${num(o.lp)}</td>
      <td class="num">${num(o.isk)}</td>
      <td class="dim">${o.req.map(r=>num(r.qty)+"× "+esc(r.name)).join(", ")}</td>
      <td class="num">${num(Math.round(c.gross))}</td>
      <td class="num ${c.profit>=0?"good":"bad"}">${num(Math.round(c.profit))}</td>
      <td class="num ${c.per>=0?"good":"bad"}"><b>${c.per==null?"—":
        num(Math.round(c.per*100)/100)}</b></td></tr>`;
  }).join("")+`</table>
  ${rows.length>400?`<p class="muted-note">Showing the top 400 of ${rows.length}.</p>`:""}
  <p class="muted-note">Prices are read at Jita 4-4 and don't account for sales
  tax, broker fees, or how much of the item the market can actually absorb —
  check the volume on the Market tab before buying a lot of anything. ⚠ marks
  prices far above the CCP average. Blueprint copies and other items that
  don't trade on the market show "no Jita order".</p>`;
}

/* ---------------- Item Search ---------------- */
function rItemSearch(m){
  m.innerHTML=`<div class="card"><h2>Item search</h2>
    <div class="row">
      <input id="isq" size="40" placeholder="Start typing any item name…"
        onkeydown="if(event.key==='Enter')runItemSearch()">
      <button class="btn" onclick="runItemSearch()">Search</button>
    </div>
    <div id="isActions"></div>
    <div id="itemInfo"></div>
  </div>`;
  attachSuggest("isq",runItemSearch);
  ensureTypeNames();
  if(state._isItem){ $("#isq").value=state._isItem.name; showItemActions();
    if(state._info&&state._info.type_id===state._isItem.id) renderItemInfo();
    else loadItemInfo(state._isItem); }
}
async function runItemSearch(){
  const q=$("#isq").value.trim(); if(!q) return;
  try{
    const t=await resolveType(q);
    if(!t){ $("#isActions").innerHTML="";
      $("#itemInfo").innerHTML=`<p class="dim">Nothing matches
        “${esc(q)}”.</p>`; return; }
    state._isItem=t; state._info=null;
    showItemActions();
    loadItemInfo(t);
  }catch(e){ toast(e.message); }
}
function showItemActions(){
  const t=state._isItem; if(!t) return;
  $("#isActions").innerHTML=`<div class="row" style="margin:8px 0 4px">
    <b style="font-size:15px">${esc(t.name)}</b>
    <button class="btn" onclick="lookOnMarket()">Look for this on the Market
    </button>
    <button class="btn sec" onclick="jumpToContracts(state._isItem)">Look for
      this in Contracts</button></div>`;
}
function lookOnMarket(){
  state._pendingMarket=state._isItem.name;
  go("market");
}

/* ---------------- Contracts tab ---------------- */
function jumpToContracts(item){
  if(!item) return;
  state._pendingContract=item;
  go("contracts");
}
/* ---------------- Contracts: My contracts + Public search ---------------- */
function rContracts(m){
  if(state._pendingContract) state.ctSub="public";    // "find in contracts" from Item Search
  const sub=state.ctSub||"mine";
  m.innerHTML=`<div class="subtabs">${[["mine","My contracts"],["public","Public search"]].map(([k,l])=>
    `<button class="${sub===k?"active":""}" onclick="state.ctSub='${k}';render()">${l}</button>`).join("")}</div>
    <div id="ctBody"></div>`;
  (sub==="mine"?rMyContracts:rContractsPublic)($("#ctBody"));
}
const MC_TYPE={item_exchange:"Item exchange",auction:"Auction",courier:"Courier",loan:"Loan",unknown:"Contract"};
const MC_STATUS={outstanding:"Outstanding",in_progress:"In progress",finished:"Finished",
  finished_issuer:"Finished",finished_contractor:"Finished",cancelled:"Cancelled",rejected:"Rejected",
  failed:"Failed",deleted:"Deleted",reversed:"Reversed",expired:"Expired"};
state.mc=state.mc||{}; state.mcOpen=state.mcOpen||{}; state.mcItems=state.mcItems||{};
function mcChars(){
  const cs=state.status.characters||[], sel=state.mcChar||"all";
  return sel==="all"?cs:cs.filter(c=>String(c.character_id)===String(sel));
}
function rMyContracts(m){
  const cs=state.status.characters||[], sel=state.mcChar||"all";
  m.innerHTML=`<div class="card"><h2>My contracts</h2>
    <div class="row"><select onchange="state.mcChar=this.value;render()">
      <option value="all">All characters</option>${cs.map(c=>`<option value="${c.character_id}" ${
        String(c.character_id)===String(sel)?"selected":""}>${esc(c.name)}</option>`).join("")}</select>
      <button class="btn sec" onclick="mcLoad(true)">Refresh</button>
      <span id="mcStat" class="dim" style="font-size:12.5px"></span></div>
    <p class="muted-note">Contracts you issued, that were assigned to you, or that you accepted.
    EVE keeps the last 30 days plus anything still open, and updates them about every 5 minutes.
    Click a contract to see its items.</p>
    <div id="mcOut"></div></div>`;
  mcRender(); mcLoad(false);
}
async function mcLoad(force){
  const todo=mcChars().filter(c=>force||!state.mc[c.character_id]||Date.now()-state.mc[c.character_id].t>120000);
  if(!todo.length) return;
  const st=$("#mcStat"); if(st) st.textContent="Loading…";
  await Promise.all(todo.map(c=>api("/api/character/"+c.character_id+"/contracts")
    .then(d=>{ state.mc[c.character_id]={t:Date.now(),...d}; })
    .catch(e=>{ state.mc[c.character_id]={t:Date.now(),error:e.message,contracts:[]}; })));
  const s2=$("#mcStat"); if(s2) s2.textContent="Updated "+new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  mcRender();
}
function mcDate(s){ return s?new Date(s).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):""; }
function mcGroup(c){
  const exp=c.status==="outstanding"&&c.expires&&new Date(c.expires)<Date.now();
  if(c.status==="outstanding"&&!exp) return c.role==="issuer"?"out":"assigned";
  if(c.status==="in_progress") return "progress";
  return "done";
}
function mcPrice(c){
  if(c.type==="courier") return `${isk(c.reward)}<div class="dim small">collateral ${isk(c.collateral)}</div>`;
  if(c.type==="auction") return `${isk(c.price)}${c.buyout?`<div class="dim small">buyout ${isk(c.buyout)}</div>`:""}`;
  if(c.reward&&!c.price) return `<span title="You pay this to whoever accepts">${isk(c.reward)}</span><div class="dim small">reward</div>`;
  return isk(c.price)+(c.reward?`<div class="dim small">+ ${isk(c.reward)} reward</div>`:"");
}
function mcParty(c){
  if(c.role==="issuer") return c.acceptor?`→ ${esc(c.acceptor)}`:c.assignee&&c.availability!=="public"?`for ${esc(c.assignee)}`:
    `<span class="dim">${c.availability==="public"?"public":"—"}</span>`;
  return `from ${esc(c.issuer)}`;
}
function mcWhen(c,g){
  if(g==="out"||g==="assigned") return `<span title="${esc(mcDate(c.expires))}">expires in ${until(c.expires)}</span>`;
  if(g==="progress"&&c.accepted&&c.days){
    const due=new Date(new Date(c.accepted).getTime()+c.days*86400000);
    return `due in ${until(due.toISOString())}`; }
  const st=c.status==="outstanding"?"expired":c.status;
  return `<span class="${/^finished/.test(st)?"good":"dim"}">${MC_STATUS[st]||st}</span>
    <div class="dim small">${mcDate(c.completed||c.expires)}</div>`;
}
function mcRender(){
  const el=$("#mcOut"); if(!el) return;
  const chars=mcChars(), multi=chars.length>1;
  const notes=[]; const seen=new Set(); const rows=[];
  chars.forEach(ch=>{
    const d=state.mc[ch.character_id]; if(!d) return;
    if(d.needs_scope) notes.push(`<b>${esc(ch.name)}</b> needs the contracts permission — see
      <a href="#" onclick="openSettings();return false">Setup &amp; permissions</a>, then ⚙ → Update permissions on the Characters screen.`);
    if(d.error) notes.push(`<b>${esc(ch.name)}</b>: <span class="bad">${esc(d.error)}</span>`);
    (d.contracts||[]).forEach(c=>{ if(seen.has(c.id)) return; seen.add(c.id); rows.push({...c,cid:ch.character_id,cname:ch.name}); });
  });
  if(!chars.some(ch=>state.mc[ch.character_id])){ el.innerHTML=`<p class="dim">Loading…</p>`; return; }
  const groups=[["out","Outstanding","your contracts waiting for someone to accept them"],
    ["assigned","Assigned to me","private contracts waiting for you to accept them"],
    ["progress","In progress","couriers on their way"],
    ["done","Finished / expired","last 30 days"]];
  el.innerHTML=(notes.length?`<div class="muted-note" style="margin:0 0 10px">${notes.join("<br>")}</div>`:"")+
    groups.map(([g,label,sub])=>{
      const list=rows.filter(c=>mcGroup(c)===g);
      if(!list.length&&g!=="out") return "";
      return `<h3 style="margin:16px 0 6px">${label} <span class="dim" style="font-weight:400;font-size:12.5px">(${list.length}) · ${sub}</span></h3>${
        list.length?`<table><tr>${multi?"<th>Character</th>":""}<th>Contract</th><th>Type</th><th class="num">Price</th>
          <th>Location</th><th>Other party</th><th>${g==="done"?"Result":"Time"}</th></tr>${
        list.map(c=>{ const open=state.mcOpen[c.id];
          return `<tr style="cursor:pointer" onclick="mcToggle(${c.id},'${c.cid}','${c.type}')">
            ${multi?`<td>${esc(c.cname)}</td>`:""}
            <td>${open?"▾":"▸"} ${esc(c.title||MC_TYPE[c.type]||"Contract")}${c.for_corp?` <span class="pill">corp</span>`:""}</td>
            <td>${MC_TYPE[c.type]||esc(c.type)}</td><td class="num">${mcPrice(c)}</td>
            <td>${esc(c.start)}${c.type==="courier"&&c.end?`<div class="dim small">→ ${esc(c.end)}</div>`:""}</td>
            <td>${mcParty(c)}</td><td>${mcWhen(c,g)}</td></tr>${
            open?`<tr><td colspan="${multi?7:6}" style="background:var(--panel2)">${mcItemsHtml(c)}</td></tr>`:""}`;}).join("")}</table>`
        :`<p class="dim">None right now.</p>`}`;}).join("");
}
function mcItemsHtml(c){
  const it=state.mcItems[c.id];
  if(!it) return `<span class="dim">Loading items…</span>`;
  if(it.error) return `<span class="bad">${esc(it.error)}</span>`;
  const inc=it.items.filter(x=>x.included), want=it.items.filter(x=>!x.included);
  const line=x=>`<div>${num(x.qty)} × ${esc(x.name)}${x.bpc?` <span class="pill">copy</span>`:""}
    <span class="dim">${x.value?" · ≈ "+isk(x.value):""}</span></div>`;
  let cmp="";
  if(it.value&&c.type==="item_exchange"&&c.price&&!want.length){
    const pct=Math.round((c.price-it.value)/it.value*100);
    cmp=` · priced <b class="${pct<0?"warn":"good"}">${Math.abs(pct)}% ${pct<0?"under":"over"}</b> the estimate`;
  }
  return `${inc.length?`<b>Items${c.type==="courier"?" to deliver":""}</b>${inc.map(line).join("")}`:""}
    ${want.length?`<div style="margin-top:6px"><b>Asking for</b>${want.map(line).join("")}</div>`:""}
    ${!inc.length&&!want.length?`<span class="dim">No items.</span>`:""}
    ${it.value?`<div class="dim small" style="margin-top:6px">Estimated value ≈ ${isk(it.value)} (EVE's average market price)${cmp}</div>`:""}
    ${it.bids.length?`<div style="margin-top:6px"><b>Bids (${it.bids.length})</b>${it.bids.slice(0,10).map(b=>
      `<div>${isk(b.amount)} <span class="dim">${mcDate(b.date)}</span></div>`).join("")}</div>`:
      c.type==="auction"?`<div class="dim small" style="margin-top:6px">No bids yet.</div>`:""}`;
}
async function mcToggle(id,cid,type){
  state.mcOpen[id]=!state.mcOpen[id]; mcRender();
  if(!state.mcOpen[id]||state.mcItems[id]) return;
  try{ state.mcItems[id]=await api("/api/character/"+cid+"/contract/"+id+"?type="+encodeURIComponent(type)); }
  catch(e){ state.mcItems[id]={error:e.message}; }
  mcRender();
}
async function rContractsPublic(m){
  m.innerHTML=`<div class="card"><h2>Public contracts</h2>
    <div class="row">
      <span class="dim">Region:</span>
      <select id="ctRegion" onchange="ctRegionChanged()"></select>
      <span id="ctStatus" class="dim" style="font-size:12.5px"></span>
      <button class="btn sec" onclick="buildContractIndex()">Build / refresh
        index</button>
      <span id="ctProg" class="dim" style="font-size:12.5px"></span>
    </div>
    <p class="muted-note">ESI can't search contracts by item, so EveWatch keeps
    its own index of each region's public item-exchange and auction
    contracts. The first build of a big region can take a few hours; refreshes
    only fetch new contracts, and you can search while it runs. The index
    keeps what it has fetched if you close the app.</p>
    <div class="row" style="margin-top:12px">
      <input id="ctq" size="34" placeholder="Start typing an item name…"
        onkeydown="if(event.key==='Enter')runContractSearch()">
      <span class="dim">max price</span>
      <input id="ctMax" size="12" inputmode="numeric" placeholder="ISK">
      <span class="dim">min qty</span>
      <input id="ctMinQ" size="6" inputmode="numeric">
      <select id="ctType"><option value="">Any type</option>
        <option value="item_exchange">Item exchange</option>
        <option value="auction">Auction</option></select>
      <label class="dim"><input type="checkbox" id="ctBpc"> copies only</label>
      <button class="btn" onclick="runContractSearch()">Search</button>
    </div>
    <div id="ctOut"></div>
  </div>`;
  attachSuggest("ctq",runContractSearch);
  ensureTypeNames();
  await loadContractRegions();
  const pend=state._pendingContract;
  if(pend){
    state._pendingContract=null; state._ctItem=pend;
    $("#ctq").value=pend.name; runContractSearch();
  }else if(state._ctItem){ $("#ctq").value=state._ctItem.name; renderContractHits(); }
  pollContractIndex();
}
async function loadContractRegions(){
  try{ state._ctRegions=await api("/api/contracts/regions"); }
  catch(e){ toast(e.message); return; }
  const el=$("#ctRegion"); if(!el) return;
  const keep=state._ctRegion||el.value;
  el.innerHTML=state._ctRegions.map(r=>
    `<option value="${r.region_id}">${esc(r.name)}</option>`).join("");
  if(keep&&state._ctRegions.some(r=>String(r.region_id)===String(keep)))
    el.value=keep;
  state._ctRegion=el.value;
  updateCtStatus();
}
function updateCtStatus(){
  const el=$("#ctStatus"); if(!el) return;
  const r=(state._ctRegions||[]).find(x=>String(x.region_id)===String($("#ctRegion").value));
  if(!r||!r.cached){ el.textContent="not indexed yet"; return; }
  const age=r.updated?Math.round((Date.now()/1000-r.updated)/60):null;
  el.textContent=`${num(r.cached)} contracts indexed`+(age!=null?
    ` · updated ${age<90?age+" min":Math.round(age/60)+" h"} ago`:"");
}
function ctRegionChanged(){
  state._ctRegion=$("#ctRegion").value; updateCtStatus();
  if($("#ctq").value.trim()) runContractSearch();
  pollContractIndex();
}
async function buildContractIndex(){
  const rid=parseInt($("#ctRegion").value); if(!rid) return;
  await api("/api/contracts/index",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({region_id:rid})});
  state._ctPoll=true; pollContractIndex();
}
async function pollContractIndex(){
  const sel=$("#ctRegion"), el=$("#ctProg"); if(!sel||!el) return;
  const rid=sel.value;
  try{
    const st=await api("/api/contracts/index?region_id="+rid);
    if(st.state==="running"){
      el.textContent=String(st.region_id)===String(rid)
        ?` indexing ${num(st.done)} / ${num(st.total)}…`
        :` (another region is being indexed)`;
      state._ctPoll=true;
      clearTimeout(state._ctTimer);
      state._ctTimer=setTimeout(pollContractIndex,6000);
      if(String(st.region_id)===String(rid)) await loadContractRegions();
    }else if(st.state==="error"){
      el.textContent=" index failed: "+st.error; state._ctPoll=false;
    }else{
      el.textContent="";
      if(state._ctPoll){ state._ctPoll=false; await loadContractRegions();
        if($("#ctq").value.trim()) runContractSearch(); }
    }
  }catch(e){}
}
async function runContractSearch(){
  const q=$("#ctq").value.trim(), rid=$("#ctRegion").value;
  if(!q||!rid) return;
  $("#ctOut").innerHTML=`<p class="dim">Searching the contract index…</p>`;
  try{
    const t=await resolveType(q);
    if(!t){ $("#ctOut").innerHTML=
      `<p class="dim">Nothing matches “${esc(q)}”.</p>`; return; }
    state._ctItem=t;
    const qs=new URLSearchParams({type_id:t.id,region_id:rid,
      max_price:($("#ctMax").value||"").replace(/[ ,]/g,""),
      min_qty:($("#ctMinQ").value||"").replace(/[ ,]/g,""),
      ctype:$("#ctType").value,bpc:$("#ctBpc").checked?"1":""});
    const r=await api("/api/contracts/search?"+qs);
    state._ctHits=r; state._ctSort=null;
    renderContractHits();
  }catch(e){ $("#ctOut").innerHTML=""; toast(e.message); }
}
function sortContracts(key){
  const s=state._ctSort||{key:null,dir:1};
  state._ctSort=s.key===key?{key,dir:-s.dir}:{key,dir:1};
  renderContractHits();
}
function renderContractHits(){
  const r=state._ctHits, t=state._ctItem, el=$("#ctOut"); if(!el||!t) return;
  if(!r){ el.innerHTML=""; return; }
  if(!r.indexed){
    el.innerHTML=`<p class="warn">No contract index for this region yet — click
      “Build / refresh index” above. You can search while it builds.</p>`;
    return;
  }
  let hits=[...r.hits];
  const s=state._ctSort;
  if(s&&s.key){
    const v=h=>s.key==="expires"?h.expires:(h[s.key]??1e30);
    hits.sort((a,b)=>(v(a)<v(b)?-1:v(a)>v(b)?1:0)*s.dir);
  }
  const arrow=k=>s&&s.key===k?(s.dir<0?" ↓":" ↑"):"";
  el.innerHTML=`<div class="row" style="margin:12px 0 6px">
      <b style="font-size:15px">${esc(t.name)}</b>
      <span class="pill">${num(r.count)} contract${r.count===1?"":"s"}</span>
      ${r.count>r.hits.length?`<span class="dim" style="font-size:12px">
        showing the cheapest ${r.hits.length}</span>`:""}
      <button class="btn sec" onclick="state._isItem=state._ctItem;state._info=null;go('itemsearch')">Item info</button>
      <button class="btn sec" onclick="state._pendingMarket=state._ctItem.name;go('market')">Market prices</button>
    </div>`+(hits.length?`<table><tr>
      <th class="sortable num" onclick="sortContracts('price')">Price${arrow("price")}</th>
      <th class="sortable num" onclick="sortContracts('qty')">Qty${arrow("qty")}</th>
      <th class="sortable num" onclick="sortContracts('unit_price')">Per unit${arrow("unit_price")}</th>
      <th>Type</th><th>Location</th><th>Notes</th>
      <th class="sortable num" onclick="sortContracts('expires')">Expires in${arrow("expires")}</th></tr>`+
    hits.map(h=>`<tr>
      <td class="num">${isk(h.price)}${h.buyout?`<br><span class="dim">
        buyout ${isk(h.buyout)}</span>`:""}</td>
      <td class="num">${num(h.qty)}</td>
      <td class="num">${h.unit_price!=null?isk(h.unit_price)
        :`<span class="dim">bundle</span>`}</td>
      <td>${h.type==="auction"?"Auction":"Item exchange"}</td>
      <td class="dim">${esc(h.location)}</td>
      <td class="dim">${h.bpc?`Copy · ME ${h.me} TE ${h.te} · ${h.runs} runs<br>`:""}${
        h.other_items?`+ ${h.other_items} other item${h.other_items>1?"s":""}<br>`:""}
        ${esc(h.title)}</td>
      <td class="num">${until(h.expires)}</td></tr>`).join("")+`</table>
      <p class="muted-note">To open one in-game: Contracts → Public → Item
      Exchange, filter by region, and match the price and location.</p>`
    :`<p class="dim">No indexed contract matches those filters.</p>`);
}

function exportLocations(){
  const blob=new Blob([JSON.stringify(state.locations,null,1)],
    {type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="evewatch_market_hubs.json"; a.click();
}
async function importLocations(){
  const txt=prompt("Paste the contents of an exported hub list (JSON):");
  if(!txt) return;
  try{
    const list=JSON.parse(txt);
    for(const l of list){
      if(l.station_id&&l.region_id){
        state.locations=await api("/api/market/locations",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify(l)});
      }
    }
    renderLocations(); fillAppraisalStations();
    toast("Imported "+list.length+" locations.");
  }catch(e){ toast("Couldn't read that: "+e.message); }
}
function fillAppraisalStations(){
  const el=$("#apStation"); if(!el) return;
  el.innerHTML=(state.locations||[]).map(l=>
    `<option value="${l.station_id}">${esc(l.system)} — ${esc(l.name)}
    </option>`).join("");
}
/* ---- batched appraisal: resolve names once, then price a few at a time ---- */
const AP_BATCH=12;
let _ap=null;
function apError(msg){
  $("#apOut").innerHTML=`<div class="alertbox down">✗ ${esc(msg)}</div>`; }
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function apWait(job,ms){
  for(let t=0;t<ms&&!job.cancel;t+=250) await sleep(250);
}
async function runAppraisal(){
  const text=$("#apText").value;
  if(!text.trim()){ toast("Paste some items first."); return; }
  if(_ap&&_ap.running){ toast("An appraisal is already running."); return; }
  const lines=text.split("\n").filter(l=>l.trim()).length;
  $("#apOut").innerHTML=`<p class="dim">Reading ${num(lines)} lines…</p>`;
  let r;
  try{
    r=await api("/api/market/appraise_resolve",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
  }catch(e){ apError("Couldn't read the list: "+e.message); return; }
  if(r.error){ apError(r.error); return; }
  if(!r.items.length){
    apError("None of those lines matched an item"+(r.unresolved.length?
      ` (first unread line: “${r.unresolved[0].slice(0,80)}”)`:"")+"."); return; }
  _ap={items:r.items,unresolved:r.unresolved,lines:r.lines,
    station:parseInt($("#apStation").value)||null,prices:{},running:true,
    paused:false,cancel:false,started:Date.now(),pct:95,stationName:null};
  runApBatches(r.items.map(i=>i.type_id));
}
async function runApBatches(ids){
  const job=_ap; job.running=true; job.cancel=false; job.paused=false;
  const todo=ids.filter(id=>!(job.prices[id]&&!job.prices[id].error));
  job.batchTotal=Math.ceil(todo.length/AP_BATCH);
  let delay=4000;
  for(let i=0;i<todo.length;i+=AP_BATCH){
    while(job.paused&&!job.cancel) await sleep(300);
    if(job.cancel) break;
    const batch=todo.slice(i,i+AP_BATCH);
    job.batchNo=Math.floor(i/AP_BATCH)+1;
    renderAppraisal();
    const t0=Date.now(); let failed=false;
    try{
      const r=await api("/api/market/appraise_batch",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({station_id:job.station,type_ids:batch})});
      if(r.error){ job.running=false; apError(r.error); return; }
      job.stationName=r.station;
      for(const [tid,p] of Object.entries(r.prices)){
        job.prices[tid]=p; if(p.error) failed=true; }
    }catch(e){
      batch.forEach(t=>{ job.prices[t]={error:e.message}; }); failed=true; }
    renderAppraisal();
    const took=Date.now()-t0;
    // be gentle: wait longer after slow or failed batches
    delay=failed?Math.min(delay*2,20000):Math.max(4000,Math.min(10000,took*0.5));
    if(i+AP_BATCH<todo.length) await apWait(job,delay);
  }
  job.running=false; job.finished=Date.now(); renderAppraisal();
}
function apControl(what){
  const j=_ap; if(!j) return;
  if(what==="pause"){ j.paused=!j.paused; renderAppraisal(); }
  else if(what==="cancel"){ j.cancel=true; j.paused=false; }
  else if(what==="retry"){
    const ids=j.items.map(i=>i.type_id).filter(id=>!j.prices[id]||j.prices[id].error);
    if(ids.length&&!j.running) runApBatches(ids);
  }
}
function apPctChanged(){
  if(!_ap) return;
  _ap.pct=parseFloat($("#apPct").value)||0;
  const el=$("#apSuggest"); if(el) el.innerHTML=isk(_ap.effective*_ap.pct/100);
}
function renderAppraisal(){
  const j=_ap, out=$("#apOut"); if(!j||!out) return;
  let sell=0,buy=0,avgOnly=0,failed=0,done=0,noOrders=0;
  const rows=j.items.map(it=>{
    const p=j.prices[it.type_id];
    let state="pending",unit=null,total=0;
    if(p){
      if(p.error){ state="error"; failed++; }
      else{
        done++;
        if(p.sell!=null){ state="ok"; unit=p.sell; total=p.sell*it.quantity;
          sell+=total; }
        else{ state="noorders"; noOrders++;
          if(p.average){ unit=p.average; total=p.average*it.quantity;
            avgOnly+=total; } }
        if(p.buy!=null) buy+=p.buy*it.quantity;
      }
    }
    return {...it,p,state,unit,total};
  });
  j.effective=sell+avgOnly;
  rows.sort((a,b)=>b.total-a.total);
  const n=j.items.length, pending=n-done-failed;
  const pctDone=Math.round((done+failed)/n*100);
  const secs=Math.round(((j.finished||Date.now())-j.started)/1000);
  const status=j.running
    ?`${j.paused?"⏸ Paused":"⏳ Pricing"} — batch ${j.batchNo||1} of ${j.batchTotal||1}
      · ${num(done)} of ${num(n)} priced`
    :(pending>0?`Stopped — ${num(pending)} item${pending>1?"s":""} not priced`
      :`✔ Finished in ${secs}s`);
  out.innerHTML=`
    <div style="margin:12px 0 6px">
      <div class="row" style="justify-content:space-between;font-size:12.5px;
        margin-bottom:4px"><span>${status}</span>
        <span class="dim">${num(j.lines)} lines → ${num(n)} item types${
          j.unresolved.length?` · ${num(j.unresolved.length)} unread`:""}</span></div>
      <div class="bar"><i style="width:${pctDone}%"></i></div>
    </div>
    <div class="row" style="margin:8px 0">
      ${j.running?`<button class="btn sec" onclick="apControl('pause')">
        ${j.paused?"Resume":"Pause"}</button>
        <button class="btn danger" onclick="apControl('cancel')">Cancel
        </button>`:""}
      ${!j.running&&(failed||pending)?`<button class="btn"
        onclick="apControl('retry')">Retry ${num(failed+pending)} unpriced
        </button>`:""}
    </div>
    <p>
      <span class="pill">Sell value: <b>${isk(sell)}</b></span>
      <span class="pill">Buy value: <b>${isk(buy)}</b></span>
      ${noOrders?`<span class="pill">No sell orders here: <b>${num(noOrders)}
        item${noOrders>1?"s":""}</b> (~${isk(avgOnly)} at global average)
        </span>`:""}
      ${failed?`<span class="pill">Couldn't price: <b class="bad">${num(failed)}
        </b></span>`:""}
      <span class="dim" style="font-size:12px">${j.stationName?
        "priced at "+esc(j.stationName):""}</span></p>
    ${!j.running&&done?`<div class="alertbox up">Contract helper — suggested
      price at <input id="apPct" size="3" value="${j.pct}"
      oninput="apPctChanged()" inputmode="numeric"
      style="background:var(--panel2);border:1px solid var(--line);
      color:var(--txt);border-radius:6px;padding:2px 6px"> % of sell value
      (incl. average for items with no orders):
      <b id="apSuggest">${isk(j.effective*j.pct/100)}</b></div>`:""}
    <table><tr><th>Item</th><th class="num">Qty</th><th class="num">Sell</th>
      <th class="num">Buy</th><th class="num">Total</th></tr>`+
    rows.map(x=>`<tr><td>${esc(x.name)}</td>
      <td class="num">${num(x.quantity)}</td>`+(
      x.state==="pending"?`<td class="num dim" colspan="3">waiting…</td>`
      :x.state==="error"?`<td class="num bad" colspan="3"
        title="${esc(x.p.error)}">couldn't price</td>`
      :`<td class="num ${x.state==="noorders"?"dim":""}">${
          x.state==="noorders"?(x.unit?isk(x.unit)+" avg":"no orders"):isk(x.unit)}</td>
        <td class="num">${isk(x.p.buy)}</td>
        <td class="num">${isk(x.total)}</td>`)+`</tr>`).join("")+`</table>`+
    (j.unresolved.length?`<p class="warn" style="font-size:12.5px">Not
      recognised (${num(j.unresolved.length)}):
      ${j.unresolved.slice(0,15).map(l=>esc(l.slice(0,60))).join(" · ")}${
      j.unresolved.length>15?" …":""}</p>`:"");
}
async function loadLocations(){
  try{ state.locations=await api("/api/market/locations"); }
  catch(e){ toast(e.message); return; }
  renderLocations();
  fillAppraisalStations();
}
function renderLocations(){
  const el=$("#locList"); if(!el) return;
  el.className="";
  const filt=$("#locCatFilter")?.value||"";
  const list=state.locations.filter(l=>!filt||l.category===filt);
  if(!state.locations.length){
    el.innerHTML=`<p class="dim">No locations yet — add one below.</p>`;
    return;
  }
  const groups={};
  list.forEach(l=>{ (groups[l.category||"Other"]=groups[l.category||"Other"]||[]).push(l); });
  el.innerHTML=Object.entries(groups).map(([cat,locs])=>`
    <div style="margin-bottom:10px">
      <div class="dim" style="font-size:12px;text-transform:uppercase;
        letter-spacing:.5px;margin-bottom:3px">${esc(cat)}
        (${locs.length})</div>
      <table>${locs.map(l=>`<tr>
        <td>${esc(l.name)}</td>
        <td class="dim">${esc(l.system)} · ${esc(l.region)}</td>
        <td class="num"><button class="btn sec"
          onclick="removeLocation(${l.station_id})">✕</button></td>
        </tr>`).join("")}</table>
    </div>`).join("") ||
    `<p class="dim">No locations in this category.</p>`;
}
async function runBulkImport(){
  const text=$("#bulkText").value;
  const category=$("#bulkCat").value;
  if(!text.trim()){ toast("Paste some station names first."); return; }
  $("#bulkOut").innerHTML=`<p class="dim">Resolving station names…</p>`;
  try{
    const r=await api("/api/market/locations/bulk",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({text,category})});
    if(r.error){ $("#bulkOut").innerHTML=""; toast(r.error); return; }
    state.locations=r.locations; renderLocations();
    $("#bulkOut").innerHTML=`
      <p><span class="pill">Added: <b class="good">${r.added.length}</b></span>
      ${r.updated.length?`<span class="pill">Re-categorized:
        <b>${r.updated.length}</b></span>`:""}
      ${r.not_found.length?`<span class="pill">Not found:
        <b class="bad">${r.not_found.length}</b></span>`:""}</p>
      ${r.not_found.length?`<p class="warn" style="font-size:12.5px">
        Not found (check exact spelling, or these may be player structures —
        use "Add structure by ID" below): ${r.not_found.map(esc).join(", ")}
        </p>`:""}`;
    if(r.added.length||r.updated.length) $("#bulkText").value="";
  }catch(e){ $("#bulkOut").innerHTML=""; toast(e.message); }
}
async function addStructure(){
  const cid=$("#structChar").value;
  const sid=$("#structId").value.trim();
  const category=$("#structCat").value;
  if(!cid){ toast("Add a character first."); return; }
  if(!sid||!/^\d+$/.test(sid)){ toast("Enter a numeric structure ID."); return; }
  $("#structOut").innerHTML=`<p class="dim">Looking up structure…</p>`;
  try{
    const r=await api("/api/market/locations/structure",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({char_id:cid,structure_id:sid,category})});
    if(r.error){ $("#structOut").innerHTML=""; toast(r.error); return; }
    state.locations=r.locations; renderLocations();
    $("#structOut").innerHTML=`<p class="good">Added: ${esc(r.name)}</p>`;
    $("#structId").value="";
  }catch(e){ $("#structOut").innerHTML=""; toast(e.message); }
}
async function findStations(){
  const q=$("#sysq").value.trim(); if(!q) return;
  $("#stationResults").innerHTML=`<p class="dim">Searching…</p>`;
  try{
    const r=await api("/api/market/stations?system="+encodeURIComponent(q));
    if(r.error){ $("#stationResults").innerHTML=
      `<p class="dim">${esc(r.error)} (needs the exact system name)</p>`;
      return; }
    if(!r.stations.length){ $("#stationResults").innerHTML=
      `<p class="dim">${esc(r.system)} has no NPC stations.</p>`; return; }
    state._foundStations = r.stations;
    $("#stationResults").innerHTML=
      `<h3>${esc(r.system)} <span class="dim">(${esc(r.region)})</span></h3>
      <table>`+r.stations.map((s,i)=>{
        const have=state.locations.some(l=>l.station_id===s.station_id);
        return `<tr><td>${esc(s.name)}</td>
        <td class="num">${have?`<span class="dim">added</span>`
          :`<button class="btn sec" onclick="addLocation(${i})">+ Add</button>`}
        </td></tr>`;
      }).join("")+`</table>`;
  }catch(e){ $("#stationResults").innerHTML=""; toast(e.message); }
}
async function addLocation(i){
  const s={...state._foundStations[i], category:$("#sysCat")?.value||"Sales Hub"};
  state.locations=await api("/api/market/locations",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(s)});
  renderLocations(); findStations();
}
async function removeLocation(sid){
  state.locations=await api("/api/market/locations",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({station_id:sid,delete:true})});
  renderLocations();
}
async function marketLookup(){
  const q=$("#mq").value.trim(); if(!q) return;
  if(!state.locations?.length){
    toast("Add at least one market location first."); return; }
  $("#marketOut").innerHTML=`<p class="dim">Searching…</p>`;
  try{
    const t=await resolveType(q);
    if(!t){ $("#marketOut").innerHTML=
      `<p class="dim">Nothing matches “${esc(q)}”.</p>`; return; }
    $("#marketOut").innerHTML=`<div id="itemInfo"></div>
      <div id="priceBlock"><p class="dim">Fetching orders for
      ${esc(t.name)} across ${state.locations.length} location(s)…</p></div>`;
    state._mkItem=t;
    loadItemInfo(t);          // runs in parallel with the price fetch
    const r=await api("/api/market/compare?type_id="+t.id);
    const rows=r.rows;
    const sells=rows.filter(x=>x.sell!=null).map(x=>x.sell);
    const bestSell=sells.length?Math.min(...sells):null;
    const maxVal=Math.max(...rows.flatMap(x=>[x.sell||0,x.buy||0]),1);
    $("#priceBlock").innerHTML=`
      <div class="charhead" style="margin-top:12px">
        <img class="portrait"
          src="https://images.evetech.net/types/${t.id}/icon?size=64">
        <div><b style="font-size:15px">${esc(t.name)}</b><br>
        <span class="dim">Global average: ${isk(r.average)}</span></div>
        <button class="btn sec" onclick="jumpToContracts(state._mkItem)">Look
          for this in Contracts</button>
      </div>
      <table><tr><th>Station</th><th class="num">Sell (lowest)</th>
        <th class="num">Buy (highest)</th><th class="num">Orders</th></tr>`+
      rows.map(x=>x.error?
        `<tr><td>${esc(x.name)}</td>
         <td colspan="3" class="bad">${esc(x.error)}</td></tr>`
        :`<tr><td>${esc(x.name)}</td>
         <td class="num ${x.sell===bestSell?'good':''}">${isk(x.sell)}
           ${x.sell===bestSell&&sells.length>1?" ★":""}</td>
         <td class="num">${isk(x.buy)}</td>
         <td class="num dim">${x.sell_orders}s / ${x.buy_orders}b</td>
         </tr>`).join("")+`</table>
      <div class="chart">
        <h3>Price comparison</h3>
        <p class="dim" style="font-size:12.5px;margin:2px 0 10px">
          <span style="color:var(--acc)">▉</span> lowest <b>sell</b> price —
          what you'd pay to buy there &nbsp;·&nbsp;
          <span style="color:var(--gold)">▉</span> highest <b>buy</b> order —
          what you'd get selling instantly. Longer bar = higher price.</p>
        ${rows.filter(x=>!x.error).map(x=>`
          <div class="crow"><span class="dim"
            title="${esc(x.name)}">${esc(x.system)} —
            ${esc(x.name.split(" - ").pop())}</span>
            <span><span class="cbar"
              style="width:${(x.sell||0)/maxVal*100}%"></span><br>
            <span class="cbar buy"
              style="width:${(x.buy||0)/maxVal*100}%"></span></span>
            <span class="num">${isk(x.sell)}<br>
              <span class="dim">${isk(x.buy)}</span></span>
          </div>`).join("")}
      </div>`;
  }catch(e){ $("#marketOut").innerHTML=""; toast(e.message); }
}

/* ---- item info panel + "can I use this?" skill check ---- */
async function loadItemInfo(t){
  const el=$("#itemInfo"); if(!el) return;
  el.innerHTML=`<p class="dim">Loading item info…</p>`;
  try{
    state._info=await api("/api/market/typeinfo?type_id="+t.id);
    renderItemInfo();
  }catch(e){
    el.innerHTML=`<p class="dim">Item info unavailable: ${esc(e.message)}</p>`;
  }
}
function charLevel(cid,sid){
  return state.chars[cid]?.skills?.[sid]?.level||0;
}
function statVal(d,label){
  const f=d.stats.find(x=>x[0]===label); return f?f[1]:0;
}
function slotPositions(n,centerDeg,R,cx,cy){
  const span=Math.min(84,13*(n-1)), out=[];
  for(let i=0;i<n;i++){
    const deg=n===1?centerDeg:centerDeg-span/2+span*i/(n-1), a=deg*Math.PI/180;
    out.push([cx+R*Math.cos(a),cy+R*Math.sin(a)]);
  }
  return out;
}
/* Fitting-wheel style diagram: slots around the hull, hardpoints marked on
   the high slots. Slot counts come from ESI; physical hardpoint positions on
   the hull aren't published anywhere, so slots are shown on the ring. */
function shipWheel(d){
  const hi=statVal(d,"High slots"), mid=statVal(d,"Mid slots"),
        lo=statVal(d,"Low slots"), rig=statVal(d,"Rig slots"),
        tur=statVal(d,"Turret hardpoints"), lau=statVal(d,"Launcher hardpoints");
  if(!(hi||mid||lo||rig)) return "";
  const cx=230, cy=230, R=165;
  const img=`https://images.evetech.net/types/${d.type_id}/render?size=512`;
  const ring=(n,deg,col,label,mark)=>{
    const pts=slotPositions(n,deg,R,cx,cy);
    const lab=n?`<text x="${cx+(R+34)*Math.cos(deg*Math.PI/180)}"
      y="${cy+(R+34)*Math.sin(deg*Math.PI/180)+4}" text-anchor="middle"
      fill="${col}" font-size="11" font-weight="700">${label} ${n}</text>`:"";
    return lab+pts.map(([x,y],i)=>{
      const m=mark?mark(i):"";
      if(m==="t") return `<g><circle cx="${x}" cy="${y}" r="10" fill="var(--panel2)"
        stroke="${col}" stroke-width="2"/><path d="M${x-5} ${y}h10M${x} ${y-5}v10"
        stroke="${col}" stroke-width="1.6"/></g>`;
      if(m==="l") return `<rect x="${x-6.5}" y="${y-6.5}" width="13" height="13"
        transform="rotate(45 ${x} ${y})" fill="var(--panel2)" stroke="var(--acc)"
        stroke-width="2"/>`;
      return `<circle cx="${x}" cy="${y}" r="7.5" fill="var(--panel2)"
        stroke="${col}" stroke-width="2"/>`;
    }).join("");
  };
  // turrets take the first high slots, launchers the next ones
  const hiMark=i=>i<tur?"t":(i<tur+lau?"l":"");
  return `<div>
    <svg viewBox="0 0 460 460" width="100%" style="max-width:540px;display:block;margin:0 auto"
      id="hullSvg">
      <circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="var(--line)"
        stroke-dasharray="3 5"/>
      <clipPath id="hullClip"><circle cx="${cx}" cy="${cy}" r="118"/></clipPath>
      <circle cx="${cx}" cy="${cy}" r="120" fill="var(--panel2)" stroke="var(--acc)"
        stroke-opacity=".6" stroke-width="2"/>
      <g clip-path="url(#hullClip)">
      <image href="${img}" x="${cx-118}" y="${cy-118}" width="236" height="236"
        preserveAspectRatio="xMidYMid slice"/></g>
      ${ring(hi,-90,"var(--gold)","HIGH",hiMark)}
      ${ring(mid,0,"var(--acc)","MID")}
      ${ring(lo,90,"var(--good)","LOW")}
      ${ring(rig,180,"var(--dim)","RIG")}
    </svg>
    <p class="muted-note" style="text-align:center">
      ${tur?`<span style="color:var(--gold)">⊕</span> ${tur} turret hardpoint${tur>1?"s":""} · `:""}
      ${lau?`<span style="color:var(--acc)">◆</span> ${lau} launcher hardpoint${lau>1?"s":""} · `:""}
      slot positions on the ring are schematic</p></div>`;
}
const _jitaPrice={};
async function loadItemPrice(tid){
  const el=$("#itemPrice"); if(!el) return;
  const show=p=>{
    const e=$("#itemPrice"); if(!e||!state._info||state._info.type_id!==tid) return;
    if(!p||p.error){ e.innerHTML=`<span class="dim">Jita price unavailable${
      p&&p.error?" ("+esc(p.error)+")":""}</span>`; return; }
    if(p.sell==null&&p.buy==null){
      e.innerHTML=`<span class="dim">No Jita 4-4 orders for this item</span>`; return; }
    e.innerHTML=`<span class="pill">Jita sell (lowest): <b>${
      p.sell!=null?isk(p.sell):"—"}</b></span>
      <span class="pill">Jita buy (highest): <b>${
      p.buy!=null?isk(p.buy):"—"}</b></span>
      ${p.average?`<span class="pill">Average: <b>${isk(p.average)}</b></span>`:""}`;
  };
  if(_jitaPrice[tid]&&Date.now()-_jitaPrice[tid].t<300000){ show(_jitaPrice[tid].p); return; }
  el.innerHTML=`<span class="dim">Fetching Jita price…</span>`;
  try{
    const r=await api("/api/market/appraise_batch",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({station_id:60003760,type_ids:[tid]})});
    if(r.error){ show({error:r.error}); return; }
    const p=r.prices[String(tid)]; _jitaPrice[tid]={t:Date.now(),p}; show(p);
  }catch(e){ show({error:e.message}); }
}
/* ---- ship mastery (data from CCP's static data export) ---- */
async function ensureMasteries(){
  if(state.mast||state._mastLoading) return;
  state._mastLoading=true;
  try{ state.mast=await api("/api/masteries"); }catch(e){ state.mast={ships:{},certs:{}}; }
  state._mastLoading=false;
  if(state._info&&$("#itemInfo")) renderItemInfo();
}
/* EVE-style skill tree: the skill, then the prerequisites you still need, indented */
function skillTreeHtml(cid,needs,noFold){          // needs: [[skillId, level, have]]
  const sk=state.catalog&&state.catalog.skills; if(!sk) return needs.map(([sid,lv,have])=>
    `${esc(skillName(sid))} ${roman(lv)}`).join(" · ");
  const seen=new Set(); let count=0;
  const node=(sid,lv,depth)=>{
    const have=charLevel(cid,sid), ok=have>=lv, again=seen.has(sid);
    let html=`<div style="margin-left:${depth*18}px;white-space:nowrap">${depth?`<span class="dim">↳ </span>`:""}<span class="${
      ok?"good":"bad"}">${ok?"✔":"✗"}</span> ${esc(skillName(sid))} ${roman(lv)}${
      ok?"":` <span class="dim">(you have ${have?roman(have):"nothing"})</span>`}${
      again&&!ok?` <span class="dim">(listed above)</span>`:""}</div>`;
    count++;
    if(!ok&&!again&&sk[sid]){
      seen.add(sid);
      const pre=sk[sid].prereqs||[];
      // unmet prerequisites first, then a one-line summary of the met ones
      pre.filter(([pid,pl])=>charLevel(cid,pid)<pl).forEach(([pid,pl])=>{ html+=node(pid,pl,depth+1); });
      const met=pre.filter(([pid,pl])=>charLevel(cid,pid)>=pl);
      if(met.length) html+=`<div class="dim" style="margin-left:${(depth+1)*18}px;font-size:11.5px">✔ ${
        met.map(([pid,pl])=>esc(skillName(pid))+" "+roman(pl)).join(", ")} already trained</div>`;
    }
    return html;
  };
  const html=needs.map(([sid,lv])=>node(sid,lv,0)).join("");
  return count>14&&!noFold?`<details><summary class="dim" style="cursor:pointer">Show the ${count} skills to train
    (with prerequisites)</summary>${html}</details>`:html;
}
function masteryNeeds(shipId,tier){          // tier 0..4 -> {skillId: level needed}
  const m=state.mast, certs=(m.ships[shipId]||[])[tier]||[], need={};
  certs.forEach(cid=>{
    const c=m.certs[cid]; if(!c) return;
    Object.entries(c.s).forEach(([sid,lv])=>{
      need[sid]=Math.max(need[sid]||0,lv[tier]||0); });
  });
  return need;
}
function masteryCalc(cid,shipId){
  const m=state.mast; if(!m||!m.ships[shipId]) return null;
  const tiers=m.ships[shipId].length; let level=0, missing=null;
  for(let t=0;t<tiers;t++){
    const need=masteryNeeds(shipId,t), miss=[];
    Object.entries(need).forEach(([sid,lv])=>{
      const have=charLevel(cid,sid); if(have<lv) miss.push([+sid,lv,have]); });
    if(miss.length){ missing=miss; break; }
    level=t+1;
  }
  return {level,tiers,missing};
}
function masteryMissingUpTo(cid,shipId,tiers){   // everything missing to reach tier N
  const need={};
  for(let t=0;t<tiers;t++) Object.entries(masteryNeeds(shipId,t)).forEach(([sid,lv])=>{
    need[sid]=Math.max(need[sid]||0,lv); });
  return Object.entries(need).filter(([sid,lv])=>charLevel(cid,sid)<lv)
    .map(([sid,lv])=>[+sid,lv,charLevel(cid,sid)]);
}
state._mt=state._mt||{};
async function fillMasteryTimes(){
  const d=state._info; if(!d) return;
  for(const el of document.querySelectorAll("[data-mt]")){
    const [cid,tiers]=el.dataset.mt.split("|"), ch=state.chars[cid]; if(!ch) continue;
    const key=cid+"|"+d.type_id+"|"+tiers+"|"+(ch.total_sp||0);
    if(state._mt[key]){ el.textContent=state._mt[key]; continue; }
    const miss=masteryMissingUpTo(cid,String(d.type_id),+tiers);
    try{
      const r=await api("/api/plan/compute",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({entries:miss.map(([sid,lv])=>[sid,lv]),skills:ch.skills,
          attributes:ch.attributes,remap:false})});
      state._mt[key]=dur(r.total_minutes)+" of training";
    }catch(e){ state._mt[key]="training time unavailable"; }
    if(el.isConnected) el.textContent=state._mt[key];
  }
}
function planMastery(cid,shipId,toTier){     // add everything missing up to tier N
  state.planChar=cid;
  const ship=((state.mast.info||{})[shipId]||[])[2]||(state._info&&state._info.name)||"Ship";
  notePlan(`${ship} · Mastery ${roman(toTier)}`);
  for(let t=0;t<toTier;t++){
    Object.entries(masteryNeeds(shipId,t)).forEach(([sid,lv])=>{
      if(charLevel(cid,sid)>=lv) return;
      const i=state.plan.findIndex(e=>e[0]===+sid);
      if(i>=0) state.plan[i][1]=Math.max(state.plan[i][1],lv); else state.plan.push([+sid,lv]);
    });
  }
  toast("Added to your skill plan — open the Skill Planner to see the training time.");
  openPlanner();
}
async function ensureBonuses(){
  if(state.bon||state._bonLoading) return;
  state._bonLoading=true;
  try{ state.bon=await api("/api/bonuses"); }catch(e){ state.bon={types:{}}; }
  state._bonLoading=false;
  if(state._info&&$("#itemInfo")) renderItemInfo();
}
function fmtBonus(v,u){
  if(v==null) return "";
  const n=Math.round(v*100)/100;
  return u==="%"?`${n}%`:u==="+"?`+${n}`:u==="x"?`${n}x`:`${n}${u}`;
}
function ibox(title,sub,body){
  return `<div class="ipanel"><div class="ihead">${title}${sub?`<small>${sub}</small>`:""}</div>
    <div class="ibody">${body}</div></div>`;
}
function bonusSection(d){
  if(d.category!=="Ship") return "";
  if(!state.bon){ ensureBonuses(); return ""; }
  const rec=state.bon.types[d.type_id]; if(!rec) return "";
  const cid=state.activeChar, c=(state.status.characters||[]).find(x=>String(x.character_id)===String(cid));
  if(!c) return "";
  if(!state.chars[cid]) return `<p class="dim">Loading ${esc(c.name)}'s skills…</p>`;
  const line=(v,u,t,lvl)=>{               // a bonus that scales per skill level
    const cur=v*lvl, max=v*5;
    return `<div style="margin:2px 0 2px 14px">${fmtBonus(v,u)} ${esc(t)} <span class="dim">per level</span>
      &nbsp;→&nbsp; <b class="${lvl?"good":"dim"}">${lvl?fmtBonus(cur,u):"none yet"}</b>
      <span class="dim">now${lvl<5?`, ${fmtBonus(max,u)} at V`:" (maximum)"}</span></div>`; };
  let html="";
  (rec.r||[]).length&&(html+=`<div style="margin:0 0 10px"><b>Role bonuses</b>${rec.r.map(([v,u,t])=>
    `<div style="margin:2px 0 2px 14px">${v==null?"":fmtBonus(v,u)+" "}${esc(t)}</div>`).join("")}</div>`);
  (rec.s||[]).forEach(([sid,bl])=>{
    const lvl=charLevel(cid,sid);
    html+=`<div style="margin:0 0 10px"><b>${esc(skillName(sid))}</b>
      <span class="dim">— you have ${lvl?roman(lvl):"it untrained"}</span>${
      bl.map(([v,u,t])=>line(v,u,t,lvl)).join("")}</div>`;
  });
  (rec.m||[]).length&&(html+=`<div style="margin:0 0 4px"><b>Other</b>${rec.m.map(([v,u,t])=>
    `<div style="margin:2px 0 2px 14px">${v!=null?fmtBonus(v,u)+" ":""}${esc(t)}</div>`).join("")}</div>`);
  return ibox("Ship bonuses",`at ${esc(c.name)}'s current skills`,html);
}
function masterySection(d){
  if(d.category!=="Ship") return "";
  if(!state.mast){ ensureMasteries(); return `<p class="dim">Loading ship mastery data…</p>`; }
  if(!state.mast.ships[d.type_id]) return "";
  const rows=state.status.characters.map(c=>{
    if(!state.chars[c.character_id]) return `<div class="dim">${esc(c.name)}: loading…</div>`;
    const r=masteryCalc(c.character_id,d.type_id); if(!r) return "";
    const pills=Array.from({length:r.tiers},(_,i)=>`<span class="lvlpill" style="${
      i<r.level?"background:var(--gold);color:var(--panel);outline-color:var(--gold)":""}">${roman(i+1)}</span>`).join(" ");
    const cidn=c.character_id, sh=String(d.type_id), nxt=r.level+1;
    let next;
    if(r.level>=r.tiers) next=`<div class="good" style="margin-top:4px">Fully mastered.</div>`;
    else{
      const nextMiss=masteryMissingUpTo(cidn,sh,nxt);
      next=`<div style="margin-top:8px"><div style="margin-bottom:2px"><b>Next: mastery ${roman(nxt)}</b>
        <span class="dim"> · ${nextMiss.length} skill${nextMiss.length===1?"":"s"} · <span data-mt="${cidn}|${nxt}">calculating…</span></span></div>
        ${skillTreeHtml(cidn,nextMiss)}</div>`;
      if(nxt<r.tiers){
        const topMiss=masteryMissingUpTo(cidn,sh,r.tiers);
        next+=`<details style="margin-top:8px"><summary style="cursor:pointer"><b>Mastery ${roman(r.tiers)}</b>
          <span class="dim"> · ${topMiss.length} skills · <span data-mt="${cidn}|${r.tiers}">calculating…</span></span></summary>
          <div style="margin-top:4px">${skillTreeHtml(cidn,topMiss,true)}</div></details>`;
      }
      next+=`<div class="row" style="margin-top:8px">
        <button class="btn sec" onclick="planMastery('${cidn}','${sh}',${nxt})">Plan mastery ${roman(nxt)}</button>
        ${nxt<r.tiers?`<button class="btn sec" onclick="planMastery('${cidn}','${sh}',${r.tiers})">Plan all the way to ${roman(r.tiers)}</button>`:""}
      </div>`;
    }
    return `<div style="margin:0 0 12px"><b>${esc(c.name)}</b> &nbsp; ${pills}${next}</div>`;
  }).join("");
  return ibox("Ship mastery","from CCP's certificate data",
    rows+`<p class="muted-note">Data: CCP static data build ${esc(String(state.mast.build||"?"))}.</p>`);
}
function renderItemInfo(){
  const d=state._info, el=$("#itemInfo"); if(!d||!el) return;
  const isShip=d.category==="Ship";
  const fmt=(v,u)=>v==null?null:`${num(Math.round(v*100)/100)} ${u}`;
  const chips=[["Volume",fmt(d.volume,"m³")],
    ["Packaged",d.packaged_volume&&d.packaged_volume!==d.volume
      ?fmt(d.packaged_volume,"m³"):null],
    ["Mass",d.mass?fmt(d.mass,"kg"):null],
    ["Cargo",d.capacity?fmt(d.capacity,"m³"):null],
    ["Drone bay",statVal(d,"Drone bay (m³)")?fmt(statVal(d,"Drone bay (m³)"),"m³"):null],
    ...(isShip?[]:d.stats.map(x=>[x[0],num(x[1])]))]
    .filter(f=>f[1]);
  const chars=state.status.characters;

  let skillHtml;
  if(!d.required.length){
    skillHtml=`<p class="good">No skill requirements — anyone can use this.</p>`;
  }else if(!chars.length){
    skillHtml=`<p class="dim">Add a character to check skill requirements.</p>`;
  }else{
    const head=chars.map(c=>`<th class="num">${esc(c.name)}</th>`).join("");
    const rowsHtml=d.required.map(([sid,lvl])=>`<tr>
      <td>${esc(d.skill_names[sid]||sid)}</td>
      <td class="num">${roman(lvl)}</td>`+
      chars.map(c=>{
        if(!state.chars[c.character_id])
          return `<td class="num dim">…</td>`;
        const have=charLevel(c.character_id,sid);
        return `<td class="num ${have>=lvl?"good":"bad"}">
          ${have>=lvl?"✔":"✗"} ${have?roman(have):"—"}</td>`;
      }).join("")+`</tr>`).join("");
    const summary=chars.map(c=>{
      const cid=c.character_id;
      if(!state.chars[cid]) return `<div class="dim">${esc(c.name)}:
        character data still loading — look the item up again in a moment.
        </div>`;
      const missing=d.required.filter(([sid,lvl])=>charLevel(cid,sid)<lvl);
      if(!missing.length) return `<div class="alertbox up">✔
        <b>${esc(c.name)}</b> has all the required skills.</div>`;
      return `<div class="alertbox down" id="need_${cid}">✗
        <b>${esc(c.name)}</b> is missing ${missing.length} required
        skill${missing.length>1?"s":""}.
        <span class="dim" id="needTxt_${cid}">Calculating training…</span>
        <div class="row" style="margin-top:6px">
          <button class="btn" id="addBtn_${cid}"
            onclick="addReqToPlan('${cid}')">Add to skill plan</button>
        </div></div>`;
    }).join("");
    skillHtml=`<table><tr><th>Required skill</th><th class="num">Level</th>
      ${head}</tr>${rowsHtml}</table>
      <div style="margin-top:10px">${summary}</div>
      <p class="muted-note">Checks trained skill levels only. EveWatch can't
      see whether an account is Alpha or Omega — Alpha clones can't use some
      ships and modules even when the skills are trained.</p>`;
  }

  const desc=d.description||"";
  const short=desc.length>200?desc.slice(0,200).replace(/\s+\S*$/,"")+"…":desc;
  const wheel=isShip?shipWheel(d):"";
  el.innerHTML=`<div style="margin:4px 0 0;padding-top:8px;border-top:1px solid var(--line)">
    <div class="charhead" style="margin-bottom:4px">
      <div><b style="font-size:18px">${esc(d.name)}</b><br>
        <span class="dim">${esc(d.group)} · ${esc(d.category)}</span></div>
    </div>
    <div id="itemPrice" class="row" style="margin-bottom:6px;gap:6px"></div>
    <div class="row" style="margin-bottom:6px;gap:6px">${chips.map(f=>
      `<span class="pill">${esc(f[0])}: <b>${esc(f[1])}</b></span>`).join("")}</div>
    <div style="margin-top:18px;padding-top:18px;border-top:1px solid var(--line);display:grid;grid-template-columns:${wheel?"minmax(320px,540px) 1fr":"1fr"};gap:16px;align-items:start">
      ${wheel||`<div style="display:none"></div>`}
      <div>
        ${wheel?"":`<img class="portrait" style="width:96px;height:96px;margin-bottom:10px"
          src="https://images.evetech.net/types/${d.type_id}/icon?size=128">`}
        ${ibox(isShip?"Can I fly this?":"Can I use this?","",skillHtml)}
        ${bonusSection(d)}
        ${masterySection(d)}
        ${desc?ibox("Description","",`<div class="dim" style="white-space:pre-line;font-size:12.5px">${esc(desc)}</div>`):""}
      </div>
    </div></div>`;
  fillTrainingNeeds();
  fillMasteryTimes();
  loadItemPrice(d.type_id);
}
async function fillTrainingNeeds(){
  const d=state._info; if(!d||!d.required.length) return;
  for(const c of state.status.characters){
    const cid=c.character_id, ch=state.chars[cid];
    const txt=$("#needTxt_"+cid); if(!ch||!txt) continue;
    try{
      const r=await api("/api/plan/compute",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({entries:d.required,skills:ch.skills,
          attributes:ch.attributes,remap:false})});
      const per={};              // group level-steps by skill
      r.steps.filter(s=>s.sp>0).forEach(s=>{
        const p=per[s.skill_id]=per[s.skill_id]||
          {name:s.name,from:s.level,to:s.level,min:0};
        p.to=Math.max(p.to,s.level); p.from=Math.min(p.from,s.level);
        p.min+=s.minutes;
      });
      txt.innerHTML=`Training needed: <b>${dur(r.total_minutes)}</b>
        (${num(r.total_sp)} SP)<br>`+Object.values(per).map(p=>
        `• ${esc(p.name)} ${p.from>1?roman(p.from-1)+" → ":""}${roman(p.to)}
         <span class="dim">(${dur(p.min)})</span>`).join("<br>");
    }catch(e){
      txt.textContent="Couldn't calculate training time"+
        (state.catalog?"":" — skill catalog still loading")+".";
    }
  }
}
function addReqToPlan(cid){
  const d=state._info; if(!d) return;
  state.planChar=cid;
  notePlan((d.category==="Ship"?"Fly ":"Use ")+d.name);
  d.required.filter(([sid,lvl])=>charLevel(cid,sid)<lvl).forEach(([sid,lvl])=>{
    const i=state.plan.findIndex(e=>e[0]===sid);
    if(i>=0) state.plan[i][1]=Math.max(state.plan[i][1],lvl);
    else state.plan.push([sid,lvl]);   // prerequisites get added automatically
  });
  const b=$("#addBtn_"+cid);
  if(b){ b.outerHTML=`<span class="good">✔ Added to your plan —</span>
    <button class="btn sec" onclick="openPlanner()">Open skill planner</button>`; }
}

function sparkSvg(vals,d1){
  if(!vals||vals.length<2) return "";
  const min=Math.min(...vals),max=Math.max(...vals),rng=(max-min)||1;
  const pts=vals.map((v,i)=>
    `${(i/(vals.length-1)*100).toFixed(1)},${(22-(v-min)/rng*20).toFixed(1)}`)
    .join(" ");
  const col=(d1??0)>=0?"var(--good)":"var(--bad)";
  return `<svg width="110" height="24" viewBox="0 0 100 24"
    preserveAspectRatio="none" style="display:block">
    <polyline points="${pts}" fill="none" stroke="${col}"
      stroke-width="1.6"/></svg>`;
}
const MIN_RELIABLE_VOL=5;
function pct(v,dim){
  if(v==null) return `<span class="dim">—</span>`;
  const cls=dim?"dim":(v>=0?"pct-up":"pct-down");
  return `<span class="${cls}">${v>=0?"▲":"▼"} ${Math.abs(v).toFixed(1)}%</span>`;
}
async function loadMovers(mode){
  const cid=$("#mvChar")?.value;
  const thr=parseFloat($("#mvThr")?.value)||10;
  if(mode==="owned"&&!cid){ toast("Add a character first."); return; }
  $("#mvOut").innerHTML=`<p class="dim">Fetching price history…
    first run can take up to a minute.</p>`;
  try{
    const r=await api(`/api/market/movers?mode=${mode}`+
      (mode==="owned"?`&char_id=${cid}`:"")+`&threshold=${thr}`);
    state._movers=r;
    state._mvSort={key:"d1abs",dir:-1};
    renderMovers();
  }catch(e){ $("#mvOut").innerHTML=""; toast(e.message); }
}
function sortMovers(key){
  const s=state._mvSort||{key:"d1abs",dir:-1};
  state._mvSort = s.key===key ? {key,dir:-s.dir} : {key,dir:-1};
  renderMovers();
}
function renderMovers(){
  const r=state._movers; if(!r||!$("#mvOut")) return;
  const owned=r.mode==="owned";
  const thr=r.threshold;
  const s=state._mvSort;
  const val=(x,k)=>k==="d1abs"?Math.abs(x.d1??0):x[k];
  const rows=[...r.rows].sort((a,b)=>{
    const x=val(a,s.key),y=val(b,s.key);
    return (typeof x==="string"?x.localeCompare(y):(x??-1e18)-(y??-1e18))*s.dir;
  });
  const arrow=k=>s.key===k?(s.dir<0?" ↓":" ↑"):"";
  const winners=rows.filter(x=>(x.d1??0)>=0);
  const losers=rows.filter(x=>(x.d1??0)<0);

  const alertList=(list,label,cls)=>list.length?`
    <div style="margin-bottom:6px">
      <div class="dim" style="font-size:12px;text-transform:uppercase;
        letter-spacing:.5px;margin:8px 0 4px">${label} (${list.length})</div>
      ${list.map(a=>{
        const ownTxt=owned?`You have <b>${num(a.qty)}</b> × `:"";
        return `<div class="alertbox ${cls}">⚠ ${ownTxt}
          <b>${esc(a.name)}</b> — price ${cls==="up"?"up":"down"}
          <b>${Math.abs(a.d1).toFixed(1)}%</b> over the last day
          (${isk(a.prev)} → ${isk(a.price)})${owned?
          `, holdings now worth ~<b>${isk(a.qty*a.price)}</b>`:""}.
          Take notice.</div>`;
      }).join("")}
    </div>`:"";
  const alertsUp=r.alerts.filter(a=>a.d1>=0)
    .sort((a,b)=>b.d1-a.d1);
  const alertsDown=r.alerts.filter(a=>a.d1<0)
    .sort((a,b)=>a.d1-b.d1);
  const alerts=alertList(alertsUp,"▲ Price spikes","up")+
               alertList(alertsDown,"▼ Price drops","down");

  const srcNote = r.mode==="basket"
    ?`<p class="warn" style="font-size:12px">Market index not built yet —
      showing the commodity basket. Click “Build market index” for true
      market-wide movers.</p>`
    :(r.index?`<p class="dim" style="font-size:12px">Top 50 market-wide
      movers (liquid items, index of ${esc(r.index_date||"today")}).</p>`:"");
  const open=state.mvTableOpen??false;

  const headCols=`<tr>
    <th class="sortable" onclick="sortMovers('name')">Item${arrow("name")}</th>
    ${owned?`<th class="sortable num" onclick="sortMovers('qty')">Owned${arrow("qty")}</th>
    <th class="sortable num" onclick="sortMovers('value')">Value${arrow("value")}</th>`:""}
    <th class="sortable num" onclick="sortMovers('price')">Price (1d avg)${arrow("price")}</th>
    <th class="sortable num" onclick="sortMovers('d1')">1d${arrow("d1")}</th>
    <th class="sortable num" onclick="sortMovers('d7')">7d${arrow("d7")}</th>
    <th class="sortable num" onclick="sortMovers('d90')">90d${arrow("d90")}</th>
    <th>90-day trend</th></tr>`;
  const bodyRows=list=>list.map(x=>`<tr>
      <td>${esc(x.name)}${x.reliable===false?
        ` <span class="dim" title="Big swing on very thin trade volume
          (fewer than ${MIN_RELIABLE_VOL} units/day) — likely a stray
          order, not a real price move." style="cursor:help">
          ⚠ thin</span>`:""}</td>
      ${owned?`<td class="num">${num(x.qty)}</td>
      <td class="num">${isk(x.value)}</td>`:""}
      <td class="num">${isk(x.price)}</td>
      <td class="num">${pct(x.d1,x.reliable===false)}</td>
      <td class="num">${pct(x.d7,x.reliable===false)}</td>
      <td class="num">${pct(x.d90,x.reliable===false)}</td>
      <td>${sparkSvg(x.spark,x.d1)}</td></tr>`).join("");

  $("#mvOut").innerHTML=srcNote+
    (alerts||`<p class="dim" style="margin-top:8px">No items moved more
      than ±${thr}% in the last day.</p>`)+
    `<div class="grp" style="margin-top:10px">
    <div class="grphead" onclick="state.mvTableOpen=!state.mvTableOpen;
      renderMovers()">${open?"▾":"▸"} Full movers table
      <span class="dim" style="font-weight:400">(${winners.length} up ·
      ${losers.length} down — click to ${open?"close":"open"})</span></div>`+
    (open?`<div class="grpbody">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;
      margin-top:8px">
      <div>
        <h3 class="good" style="margin-top:0">▲ Biggest winners
          (${winners.length})</h3>
        ${winners.length?`<table>`+headCols+bodyRows(winners)+
          `</table>`:`<p class="dim">None.</p>`}
      </div>
      <div style="border-left:1px solid var(--line);padding-left:24px">
        <h3 class="bad" style="margin-top:0">▼ Biggest losers
          (${losers.length})</h3>
        ${losers.length?`<table>`+headCols+bodyRows(losers)+
          `</table>`:`<p class="dim">None.</p>`}
      </div>
    </div></div>`
    :"")+`</div>`;
}
async function buildIndex(){
  await api("/api/market/index",{method:"POST"});
  pollIndex();
}
async function pollIndex(){
  try{
    const st=await api("/api/market/index");
    const el=$("#idxStatus"); if(!el) return;
    if(st.state==="running"){
      el.textContent=`index: ${st.done}/${st.total}…`;
      setTimeout(pollIndex, 5000);
    }else if(st.state==="done"){ el.textContent="index: complete ✔"; }
    else if(st.state==="error"){ el.textContent="index failed: "+st.error; }
    else if(st.index_date){ el.textContent="index of "+st.index_date; }
  }catch(e){}
}

/* ---- item name autocomplete ---- */
async function ensureTypeNames(){
  if(state.typeNames) return;
  state.typeNames="loading";
  try{
    const d=await api("/api/market/typenames");
    state.typeNames=Object.entries(d).map(([id,n])=>[n,parseInt(id)])
      .sort((a,b)=>a[0].localeCompare(b[0]));
  }catch(e){ state.typeNames=null; }
}
function localMatches(q,limit=15){
  if(!Array.isArray(state.typeNames)) return [];
  q=q.toLowerCase();
  // prefix matches first, then substring matches
  const pre=[],sub=[];
  for(const t of state.typeNames){
    const ln=t[0].toLowerCase();
    if(ln.startsWith(q)) pre.push(t);
    else if(ln.includes(q)) sub.push(t);
    if(pre.length>=limit) break;
  }
  return pre.concat(sub).slice(0,limit);
}
function attachSuggest(inputId,onPick){
  const el=document.getElementById(inputId);
  if(!el||el.dataset.sg) return;
  el.dataset.sg="1";
  el.setAttribute("autocomplete","off");
  const wrap=document.createElement("span");
  wrap.className="sbox";
  el.parentNode.insertBefore(wrap,el);
  wrap.appendChild(el);
  const dd=document.createElement("div");
  dd.className="suggest"; dd.style.display="none";
  wrap.appendChild(dd);
  let hits=[],sel=-1;
  const close=()=>{dd.style.display="none";sel=-1;};
  const show=()=>{
    dd.innerHTML=hits.map((h,i)=>
      `<div class="${i===sel?'sel':''}" data-i="${i}">${esc(h[0])}</div>`)
      .join("");
    dd.style.display=hits.length?"block":"none";
    dd.querySelectorAll("div").forEach(d=>{
      d.onmousedown=e=>{e.preventDefault();
        el.value=hits[+d.dataset.i][0]; close(); if(onPick)onPick();};
    });
  };
  el.addEventListener("input",async()=>{
    await ensureTypeNames();
    const q=el.value.trim();
    if(q.length<3){close();return;}
    hits=localMatches(q); sel=-1; show();
  });
  el.addEventListener("keydown",e=>{
    if(dd.style.display==="none")return;
    if(e.key==="ArrowDown"){sel=Math.min(sel+1,hits.length-1);show();
      e.preventDefault();}
    else if(e.key==="ArrowUp"){sel=Math.max(sel-1,0);show();
      e.preventDefault();}
    else if(e.key==="Enter"&&hits.length){
      el.value=hits[sel>=0?sel:0][0]; close();}
    else if(e.key==="Escape") close();
  });
  el.addEventListener("blur",()=>setTimeout(close,150));
}
function localResolve(q){
  if(!Array.isArray(state.typeNames)) return null;
  const ln=q.trim().toLowerCase();
  const exact=state.typeNames.find(t=>t[0].toLowerCase()===ln);
  if(exact) return {id:exact[1],name:exact[0]};
  const m=localMatches(q,1);
  return m.length?{id:m[0][1],name:m[0][0]}:null;
}
async function resolveType(q){
  await ensureTypeNames();
  const loc=localResolve(q);
  if(loc) return loc;
  const hits=await api("/api/market/search?q="+encodeURIComponent(q));
  return hits.length?hits[0]:null;
}

/* ---- hub scanner ---- */
const HS={cats:null,groups:{},selCats:new Set(),selGroups:new Set(),sort:"iskph",dir:-1,result:null,
          hubs:null,poll:null,opened:false};
function hsPrefs(){ try{ return JSON.parse(localStorage.getItem("evw_hs")||"{}"); }catch(e){ return {}; } }
function hsSavePrefs(){
  const g=id=>document.getElementById(id);
  try{ localStorage.setItem("evw_hs",JSON.stringify({ship:g("hsShip")?.value,mpj:g("hsMpj")?.value,
    cargo:g("hsCargo")?.value,tax:g("hsTax")?.value,broker:g("hsBroker")?.value,minm:g("hsMinM")?.value,
    minp:g("hsMinP")?.value,minu:g("hsMinU")?.value,mini:g("hsMinI")?.value,maxj:g("hsMaxJ")?.value,
    mode:g("hsMode")?.value,hubs:HS.hubs?[...HS.hubs]:null})); }catch(e){}
}
async function hsInit(){
  await loadLocations().catch(()=>{});
  const pr=hsPrefs(), locs=state.locations||[];
  HS.hubs=new Set(pr.hubs&&pr.hubs.length?pr.hubs:locs.map(l=>l.station_id));
  hsRenderHubs(); hsRenderFilters(pr);
  try{
    const st=await api("/api/hubscan/status");
    if(st.state==="running") hsPoll();
    else{ HS.result=await api("/api/hubscan/result"); hsRender(); }
  }catch(e){}
}
function hsRenderHubs(){
  const el=$("#hsHubs"); if(!el) return;
  const locs=state.locations||[];
  el.innerHTML=locs.map(l=>`<label style="margin-right:10px"><input type="checkbox" ${HS.hubs.has(l.station_id)?"checked":""}
    onchange="hsToggleHub(${l.station_id},this.checked)"> ${esc(l.system)}</label>`).join("")||`<span class="dim">Add market locations above first.</span>`;
}
function hsToggleHub(id,on){ on?HS.hubs.add(id):HS.hubs.delete(id); hsSavePrefs(); }
function hsDefaultsFromSkills(){
  const cid=state.activeChar; if(!cid||!state.chars[cid]) return null;
  const acc=charLevel(cid,16622), br=charLevel(cid,3446);
  return {tax:Math.round(7.5*(1-0.11*acc)*100)/100, broker:Math.round((3-0.3*br)*100)/100, acc, br};
}
function hsRenderFilters(pr){
  const el=$("#hsFilters"); if(!el) return;
  const d=hsDefaultsFromSkills()||{tax:7.5,broker:3,acc:0,br:0};
  el.innerHTML=`<div class="row" style="margin-top:12px">
      <select id="hsShip" onchange="hsApplyShip()">${SHIP_PRESETS.map((p,i)=>
        `<option value="${i}" ${(pr.ship!=null?+pr.ship:3)===i?"selected":""}>${p[0]}</option>`).join("")}</select>
      <span class="dim">min/jump</span><input id="hsMpj" size="4" value="${pr.mpj||1.5}" oninput="hsChanged()">
      <span class="dim">cargo m³</span><input id="hsCargo" size="8" value="${pr.cargo||5500}" oninput="hsChanged()">
      <span class="dim">sales tax %</span><input id="hsTax" size="4" value="${pr.tax||d.tax}" oninput="hsChanged()">
      <span class="dim">broker fee %</span><input id="hsBroker" size="4" value="${pr.broker||d.broker}" oninput="hsChanged()">
      <span class="dim" title="Estimated from the active character's Accounting and Broker Relations skills; standings aren't included. Edit to match your in-game numbers.">ⓘ</span></div>
    <div class="row" style="margin-top:6px">
      <span class="dim">Sell by</span>
      <select id="hsMode" onchange="hsChanged()"><option value="instant" ${pr.mode!=="patient"?"selected":""}>selling into buy orders (instant)</option>
        <option value="patient" ${pr.mode==="patient"?"selected":""}>listing a sell order (patient)</option></select>
      <span class="dim">min margin %</span><input id="hsMinM" size="4" value="${pr.minm||5}" oninput="hsChanged()">
      <span class="dim">min profit/trip</span><input id="hsMinP" size="10" value="${pr.minp||1000000}" oninput="hsChanged()">
      <span class="dim">min units/day</span><input id="hsMinU" size="5" value="${pr.minu||10}" oninput="hsChanged()">
      <span class="dim">min ISK/day</span><input id="hsMinI" size="10" value="${pr.mini||50000000}" oninput="hsChanged()">
      <span class="dim">max jumps</span><input id="hsMaxJ" size="4" value="${pr.maxj||30}" oninput="hsChanged()"></div>`;
}
function hsApplyShip(){
  const p=SHIP_PRESETS[parseInt($("#hsShip").value)];
  if(p&&p[1]!=null){ $("#hsMpj").value=p[1]; $("#hsCargo").value=p[2]; }
  hsChanged();
}
function hsChanged(){ hsSavePrefs(); hsRender(); }
async function hsScopeChanged(){
  const sel=document.querySelector("input[name=hsScope]:checked").value==="sel";
  const box=$("#hsSel"); box.style.display=sel?"block":"none";
  if(!sel) return;
  if(!HS.cats){
    box.innerHTML=`<span class="dim">Loading item categories…</span>`;
    try{ HS.cats=await api("/api/hubscan/taxonomy"); }catch(e){ box.innerHTML=`<span class="bad">${esc(e.message)}</span>`; return; }
  }
  hsRenderSel();
}
function hsRenderSel(){
  const box=$("#hsSel");
  const one=HS.selCats.size===1?[...HS.selCats][0]:null;
  box.innerHTML=`<div class="dim" style="margin-bottom:4px">Item categories (tick one or more):</div>
    <div style="columns:4 190px;font-size:13px">${HS.cats.map(c=>`<label style="display:block"><input type="checkbox" ${HS.selCats.has(c.id)?"checked":""}
      onchange="hsToggleCat(${c.id},this.checked)"> ${esc(c.name)}</label>`).join("")}</div>
    ${one&&HS.groups[one]?`<div class="dim" style="margin:8px 0 4px">Narrow to specific groups (optional — leave all unticked for the whole category):</div>
    <div style="columns:3 240px;font-size:13px;max-height:260px;overflow:auto">${HS.groups[one].map(g=>
      `<label style="display:block"><input type="checkbox" ${HS.selGroups.has(g.id)?"checked":""}
      onchange="hsToggleGroup(${g.id},this.checked)"> ${esc(g.name)}</label>`).join("")}</div>`
    :one?`<span class="dim">Loading groups…</span>`:""}`;
}
async function hsToggleCat(id,on){
  on?HS.selCats.add(id):HS.selCats.delete(id);
  HS.selGroups.clear(); hsRenderSel();
  if(HS.selCats.size===1){
    const cid=[...HS.selCats][0];
    if(!HS.groups[cid]){ try{ HS.groups[cid]=await api("/api/hubscan/taxonomy?category="+cid); }catch(e){ HS.groups[cid]=[]; } }
    hsRenderSel();
  }
}
function hsToggleGroup(id,on){ on?HS.selGroups.add(id):HS.selGroups.delete(id); }
async function hsStart(){
  const sel=document.querySelector("input[name=hsScope]:checked").value==="sel";
  if(HS.hubs.size<2){ toast("Tick at least two hubs to compare."); return; }
  if(sel&&!HS.selCats.size){ toast("Tick at least one item category, or choose the whole market."); return; }
  await api("/api/hubscan/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({stations:[...HS.hubs],categories:sel?[...HS.selCats]:[],groups:sel?[...HS.selGroups]:[]})});
  hsPoll();
}
function hsPoll(){
  clearTimeout(HS.poll);
  const tick=async()=>{
    const st=await api("/api/hubscan/status").catch(()=>null);
    const go=$("#hsGo"), stop=$("#hsStop"), msg=$("#hsStatus");
    if(!st||!go){ return; }                       // left the tab
    if(st.state==="running"){
      go.disabled=true; stop.style.display="inline-block";
      msg.textContent=`${st.phase}… ${st.total?Math.round(st.done/st.total*100)+"%":""}`;
      HS.poll=setTimeout(tick,1500); return; }
    go.disabled=false; stop.style.display="none"; msg.textContent="";
    if(st.state==="error"){ msg.innerHTML=`<span class="bad">${esc(st.error)}</span>`; return; }
    HS.result=await api("/api/hubscan/result"); hsRender();
  };
  tick();
}
function hsSort(k){ if(HS.sort===k) HS.dir=-HS.dir; else{ HS.sort=k; HS.dir=-1; } hsRender(); }
function hsRender(){
  const el=$("#hsOut"); if(!el) return;
  const r=HS.result;
  if(!r||!r.rows||!r.rows.length){ el.innerHTML=r&&r.finished?`<p class="dim">The last scan found nothing profitable.</p>`:""; return; }
  const v=id=>parseFloat($("#"+id)?.value)||0;
  const tax=v("hsTax")/100, broker=v("hsBroker")/100, cargo=v("hsCargo")||1, mpj=v("hsMpj")||1.5;
  const patient=$("#hsMode")?.value==="patient";
  const minM=v("hsMinM"), minP=v("hsMinP"), minU=v("hsMinU"), minI=v("hsMinI"), maxJ=v("hsMaxJ")||999;
  const rows=[];
  for(const x of r.rows){
    const sell=patient?x.dst_ask:x.bid; if(!sell) continue;
    const unit=sell*(1-tax-(patient?broker:0))-x.ask;
    if(unit<=0) continue;
    const depth=patient?x.ask_vol:Math.min(x.ask_vol,x.bid_vol);
    const units=Math.min(depth,Math.floor(cargo/x.m3));
    if(units<=0) continue;
    const profit=unit*units, margin=unit/x.ask*100, hours=(x.jumps*mpj+6)/60;
    const dayUnits=x.day_units||0, dayIsk=(x.day_price||0)*dayUnits;
    if(margin<minM||profit<minP||dayUnits<minU||dayIsk<minI||x.jumps>maxJ) continue;
    rows.push({...x,sell,unit,units,profit,margin,hours,iskph:profit/hours,dayUnits,dayIsk,
      odd:x.avg&&(sell>x.avg*2||x.ask<x.avg*0.4)});
  }
  rows.sort((a,b)=>(a[HS.sort]<b[HS.sort]?-1:a[HS.sort]>b[HS.sort]?1:0)*HS.dir||0);
  const th=(k,l)=>`<th class="sortable num" onclick="hsSort('${k}')">${l}${HS.sort===k?(HS.dir<0?" ▼":" ▲"):""}</th>`;
  const when=r.finished?new Date(r.finished*1000).toLocaleString():"";
  el.innerHTML=`<p class="dim" style="margin-top:12px">Last scan: ${esc(r.scope||"")} · ${esc((r.stations||[]).join(", "))} · ${esc(when)}
    · ${rows.length} route${rows.length===1?"":"s"} pass your filters</p>
    ${rows.length?`<div style="overflow-x:auto"><table><tr><th>Item</th><th>Route</th>${th("ask","Buy @")}${th("sell","Sell @")}${th("margin","Margin")}
      ${th("units","Units/trip")}${th("profit","Profit/trip")}${th("jumps","Jumps")}${th("dayUnits","Sells/day")}${th("iskph","ISK/hour")}</tr>`+
    rows.slice(0,200).map(x=>`<tr><td><a href="#" onclick="hsOpenItem(${x.tid},'${esc(x.name).replace(/'/g,"\\'")}');return false" style="color:var(--acc)">${esc(x.name)}</a>${
      x.odd?` <span class="warn" title="Price is far from CCP's average — check that it's real">⚠</span>`:""}</td>
      <td>${esc(x.from_system)} → ${esc(x.to_system)}</td><td class="num">${isk(x.ask)}</td><td class="num">${isk(x.sell)}</td>
      <td class="num good">${x.margin.toFixed(1)}%</td><td class="num">${num(x.units)}</td>
      <td class="num">${isk(Math.round(x.profit))}</td><td class="num">${x.jumps}</td><td class="num">${num(Math.round(x.dayUnits))}</td>
      <td class="num"><b>${isk(Math.round(x.iskph))}</b></td></tr>`).join("")+`</table></div>`
      :`<p class="dim">Nothing passes the filters — try lowering the minimum margin, profit or daily volume.</p>`}
    <p class="muted-note">Profit per trip uses order depth within 2% of the best price at each end, capped by cargo space,
    after ${(tax*100).toFixed(2)}% sales tax${patient?` and ${(broker*100).toFixed(2)}% broker fee`:""}. "Sells/day" is the
    destination region's average over the last 7 days. ⚠ marks prices far from CCP's average — likely a thin or manipulated
    market. Prices change quickly: re-check the Market tab before you haul anything expensive.</p>`;
}
function hsOpenItem(id,name){ state._isItem={id,name}; state._info=null; go("itemsearch"); }

/* ---- trade routes ---- */
const SHIP_PRESETS=[
  ["Shuttle / Interceptor",0.7,150],
  ["Fast frigate",0.9,400],
  ["Cruiser",1.2,500],
  ["Industrial (T1 hauler)",1.5,5500],
  ["Blockade Runner",1.1,10000],
  ["Deep Space Transport",1.8,62500],
  ["Freighter",3.0,435000],
  ["Custom",null,null]];
function fillShipPresets(){
  const el=$("#trShip"); if(!el) return;
  el.innerHTML=SHIP_PRESETS.map((p,i)=>
    `<option value="${i}" ${i===3?"selected":""}>${p[0]}</option>`).join("");
  restoreTradePrefs();
}
function applyShipPreset(){
  const p=SHIP_PRESETS[parseInt($("#trShip").value)];
  if(p&&p[1]!=null){ $("#trMpj").value=p[1]; $("#trCargo").value=p[2]; }
  saveTradePrefs();
}
function saveTradePrefs(){
  try{ localStorage.setItem("evw_trade",JSON.stringify({
    ship:$("#trShip")?.value, mpj:$("#trMpj")?.value,
    cargo:$("#trCargo")?.value, tax:$("#trTax")?.value,
    fromCat:$("#trFromCat")?.value, toCat:$("#trToCat")?.value})); }catch(e){}
}
function restoreTradePrefs(){
  try{
    const p=JSON.parse(localStorage.getItem("evw_trade")||"null");
    if(!p) return;
    if(p.ship!=null) $("#trShip").value=p.ship;
    if(p.mpj) $("#trMpj").value=p.mpj;
    if(p.cargo) $("#trCargo").value=p.cargo;
    if(p.tax) $("#trTax").value=p.tax;
    if(p.fromCat!=null) $("#trFromCat").value=p.fromCat;
    if(p.toCat!=null) $("#trToCat").value=p.toCat;
  }catch(e){}
}
async function runTradeScan(){
  const q=$("#trItem").value.trim(); if(!q) return;
  if((state.locations||[]).length<2){
    toast("Add at least two market locations first."); return; }
  $("#trOut").innerHTML=`<p class="dim">Scanning hubs and routes…</p>`;
  try{
    const t=await resolveType(q);
    if(!t){ $("#trOut").innerHTML=
      `<p class="dim">Nothing matches “${esc(q)}”.</p>`; return; }
    const r=await api(`/api/market/trade_scan?type_id=${t.id}`+
      `&mpj=${$("#trMpj").value}&cargo=${$("#trCargo").value}`+
      `&tax=${$("#trTax").value}`+
      `&from_cat=${encodeURIComponent($("#trFromCat").value)}`+
      `&to_cat=${encodeURIComponent($("#trToCat").value)}`);
    if(!r.rows.length){ $("#trOut").innerHTML=
      `<p class="dim">No profitable route for ${esc(t.name)} between your
      hubs right now.</p>`; return; }
    const best=r.rows[0];
    $("#trOut").innerHTML=`
      <div class="alertbox up">★ Best: buy <b>${esc(t.name)}</b> in
        <b>${esc(best.from_system)}</b> at ${isk(best.buy_price)}, sell in
        <b>${esc(best.to_system)}</b> at ${isk(best.sell_price)} —
        ${best.jumps} jumps, ${num(best.units)} units/trip,
        <b>${isk(best.iskph)}/h</b></div>
      <table><tr><th>From → To</th><th class="num">Buy @</th>
        <th class="num">Sell @</th><th class="num">Profit/unit</th>
        <th class="num">Jumps</th><th class="num">Units/trip</th>
        <th class="num">Profit/trip</th><th class="num">Time</th>
        <th class="num">ISK/h</th></tr>`+
      r.rows.map(x=>`<tr>
        <td>${esc(x.from_system)} → ${esc(x.to_system)}</td>
        <td class="num">${isk(x.buy_price)}</td>
        <td class="num">${isk(x.sell_price)}</td>
        <td class="num">${isk(x.profit_unit)}</td>
        <td class="num">${x.jumps}</td>
        <td class="num">${num(x.units)}</td>
        <td class="num">${isk(x.profit_trip)}</td>
        <td class="num">${dur(x.hours*60)}</td>
        <td class="num"><b>${isk(x.iskph)}</b></td></tr>`).join("")+
      `</table>
      <p class="muted-note">Item volume: ${r.volume_m3} m³. Profit/unit is
      after your ${esc($("#trTax").value)}% tax. Prices use order depth
      within 2% of best price at each station.</p>`;
  }catch(e){ $("#trOut").innerHTML=""; toast(e.message); }
}

/* ---------------- Industry ---------------- */
/* ---------------- Accomplishments ----------------
   Worked out from the character's skills and CCP's certificate/mastery data
   (ESI has no achievements endpoint). */
const CERT_LV=["—","Basic","Standard","Improved","Advanced","Elite"];
const SHIP_CLASSES=[
  ["Frigates",["Frigate","Assault Frigate","Interceptor","Covert Ops","Electronic Attack Ship",
    "Stealth Bomber","Logistics Frigate","Expedition Frigate","Prototype Exploration Ship"]],
  ["Destroyers",["Destroyer","Interdictor","Command Destroyer","Tactical Destroyer"]],
  ["Cruisers",["Cruiser","Heavy Assault Cruiser","Force Recon Ship","Combat Recon Ship","Logistics",
    "Heavy Interdiction Cruiser","Strategic Cruiser","Flag Cruiser"]],
  ["Battlecruisers",["Combat Battlecruiser","Attack Battlecruiser","Command Ship"]],
  ["Battleships",["Battleship","Marauder","Black Ops"]],
  ["Industrial & mining",["Hauler","Blockade Runner","Deep Space Transport","Freighter","Jump Freighter",
    "Mining Barge","Exhumer","Industrial Command Ship","Capital Industrial Ship","Expedition Command Ship"]],
  ["Capitals",["Dreadnought","Lancer Dreadnought","Carrier","Supercarrier","Titan","Force Auxiliary","Command Carrier"]]];
function shipClass(g){ const c=SHIP_CLASSES.find(([,gs])=>gs.includes(g)); return c?c[0]:"Other"; }
function acPips(l){ return `<span class="ac-pips">${[1,2,3,4,5].map(i=>
  `<i style="${i<=l?`background:var(--l${l});outline-color:var(--l${l})`:""}"></i>`).join("")}</span>`; }
function acMinutes(cid,miss){             // training time for [[sid,lvl]] (prerequisites not included)
  const d=state.chars[cid], sk=state.catalog&&state.catalog.skills;
  if(!d||!sk) return null;
  const at=ovAttrs(d); let t=0;
  for(const [sid,lv] of miss){
    const s=sk[sid]; if(!s) continue;
    const rate=(at[s.primary]?.total||20)+(at[s.secondary]?.total||20)/2;
    const cur=Math.max(d.skills?.[sid]?.sp||0, spAt(s.rank,charLevel(cid,sid)));
    t+=Math.max(0,spAt(s.rank,lv)-cur)/rate;
  }
  return t;
}
function certLevel(cid,c){
  let l=0;
  for(let i=0;i<5;i++){
    if(Object.entries(c.s).some(([sid,lv])=>charLevel(cid,sid)<(lv[i]||0))) break;
    l=i+1;
  }
  return l;
}
function acCompute(cid){
  const d=state.chars[cid], m=state.mast;
  const key=cid+"|"+(d.total_sp||0)+"|"+(m.build||"")+"|"+(state.catalog?1:0);
  if(state._ac&&state._ac.key===key) return state._ac;
  const certs=Object.entries(m.certs).map(([id,c])=>{
    const lv=certLevel(cid,c);
    let miss=null, min=null;
    if(lv<5){
      miss=Object.entries(c.s).filter(([sid,l])=>charLevel(cid,sid)<(l[lv]||0)).map(([sid,l])=>[+sid,l[lv]]);
      min=acMinutes(cid,miss);
    }
    return {id,n:c.n,g:c.g||"Other",lv,miss,min};
  }).sort((a,b)=>a.n.localeCompare(b.n));
  const info=m.info||{}, classes={}, ships=[];
  for(const sid of Object.keys(m.ships)){
    const r=masteryCalc(cid,sid); if(!r) continue;
    const inf=info[sid]||["",1,""], cls=shipClass(inf[0]);
    const row=classes[cls]||(classes[cls]={tot:0,c:[0,0,0,0,0]});
    row.tot++; if(r.level) row.c[r.level-1]++;
    ships.push({id:sid,name:inf[2]||skillName(sid),cls,tech:inf[1],lv:r.level,tiers:r.tiers});
  }
  const skills=Object.values(d.skills||{});
  const out={key,certs,classes,ships,
    atV:skills.filter(s=>s.level>=5).length, trained:skills.filter(s=>s.level>0).length,
    elite:certs.filter(c=>c.lv===5).length, m5:ships.filter(s=>s.lv>=5).length};
  // closest unlocks: next certificate level / next mastery on things already started
  const cand=certs.filter(c=>c.lv>=1&&c.lv<5&&c.min).map(c=>({kind:CERT_LV[c.lv+1]+" certificate",
      name:c.n,miss:c.miss,min:c.min}))
    .concat(ships.filter(s=>s.lv>=1&&s.lv<s.tiers).map(s=>{
      const miss=masteryMissingUpTo(cid,s.id,s.lv+1).map(([sid,lv])=>[sid,lv]);
      return {kind:"Mastery "+roman(s.lv+1),name:s.name,miss,min:acMinutes(cid,miss)};
    }).filter(x=>x.min));
  out.next=cand.sort((a,b)=>a.min-b.min).slice(0,6);
  state._ac=out; return out;
}
const AC_MEDALS=[        // family, colour, thresholds, label
  ["sp","#58a6ff",[5e6,10e6,25e6,50e6,100e6,150e6,200e6],n=>(n/1e6)+"M",n=>(n/1e6)+" million SP"],
  ["v","#4cc38a",[10,25,50,100,150,200],n=>n,n=>n+" skills at level V"],
  ["e","#e0b455",[1,5,10,25,50],n=>n,n=>n===1?"First Elite certificate":n+" Elite certificates"],
  ["m","#a274f0",[1,10,25,50,100],n=>n,n=>n===1?"First ship at Mastery V":n+" ships at Mastery V"]];
function acMedals(cid,a){
  const val={sp:state.chars[cid].total_sp||0,v:a.atV,e:a.elite,m:a.m5};
  const out=[];
  AC_MEDALS.forEach(([k,col,ths,short,long])=>{
    const got=ths.filter(t=>val[k]>=t), nxt=ths.find(t=>val[k]<t);
    got.forEach(t=>out.push({col,s:short(t),t:long(t),on:true}));
    if(nxt!=null) out.push({col,s:short(nxt),t:long(nxt),on:false});
  });
  const any=(f)=>a.ships.some(f);
  [["BS","Battleship pilot","Mastery I in a battleship",s=>s.cls==="Battleships"&&s.lv>=1],
   ["T2","Tech II specialist","Mastery V in a Tech II ship",s=>s.tech===2&&s.lv>=5],
   ["T3","Tech III pilot","Mastery I in a Tech III ship",s=>s.tech===3&&s.lv>=1],
   ["CAP","Capital pilot","Mastery I in a capital ship",s=>s.cls==="Capitals"&&s.lv>=1]]
    .forEach(([sh,t,d,f])=>out.push({col:"#e5534b",s:sh,t,d,on:any(f)}));
  return out;
}
function acPlan(cid,i,fromCert){
  const a=state._ac; if(!a) return;
  const item=fromCert?a.certs.find(c=>c.id===i):a.next[i]; if(!item||!item.miss) return;
  const label=fromCert?`${item.n} · ${CERT_LV[item.lv+1]}`
    :`${item.name} · ${item.kind.replace(" certificate","")}`;
  state.planChar=cid;
  notePlan(label);
  item.miss.forEach(([sid,lv])=>{
    const j=state.plan.findIndex(e=>e[0]===+sid);
    if(j>=0) state.plan[j][1]=Math.max(state.plan[j][1],lv); else state.plan.push([+sid,lv]);
  });
  toast("Added to your skill plan: "+label+" — see Skills → Planner for prerequisites and the total time.");
}
function acSetFilter(f){ state.acFilter=f; render(); }
function rAccomplishments(m){
  const cid=state.activeChar, d=state.chars[cid];
  const c=(state.status.characters||[]).find(x=>String(x.character_id)===String(cid));
  if(!c){ m.innerHTML=`<div class="card"><p class="dim">Select a character first.</p></div>`; return; }
  if(!d||!state.mast){
    if(!state.mast) ensureMasteries().then(()=>{ if(state.tab==="accomplishments") render(); });
    m.innerHTML=`<div class="card"><p class="dim">Loading ${esc(c.name)}'s skills and CCP's certificate data…</p></div>`;
    return;
  }
  const a=acCompute(cid), tm=x=>x==null?"":dur(x);
  const anyM=Object.values(a.classes).reduce((s,r)=>s+r.c.reduce((x,y)=>x+y,0),0);
  const lvLine=[5,4,3].map(l=>`${a.certs.filter(x=>x.lv===l).length} ${CERT_LV[l]}`).join(" · ");
  const tiles=`<div class="ac-tiles">
    <div class="ac-tile"><div class="k">Skill points</div><div class="v">${((d.total_sp||0)/1e6).toFixed(1)}M</div>
      <div class="s">${num(d.total_sp)} SP${d.unallocated_sp?` · ${num(d.unallocated_sp)} unallocated`:""}</div></div>
    <div class="ac-tile"><div class="k">Skills at V</div><div class="v">${a.atV}</div><div class="s">of ${a.trained} trained skills</div></div>
    <div class="ac-tile"><div class="k">Elite certificates</div><div class="v lv5">${a.elite}</div><div class="s">${lvLine.replace(/^\d+ Elite · /,"")}</div></div>
    <div class="ac-tile"><div class="k">Mastery V ships</div><div class="v lv5">${a.m5}</div><div class="s">${anyM} ships with any mastery</div></div></div>`;
  const medals=acMedals(cid,a);
  const medalCard=`<div class="card"><h2>Milestones</h2>
    <p class="ac-sub">${medals.filter(x=>x.on).length} earned · the faded one in each row is the next to go for</p>
    <div class="ac-medals">${medals.map(x=>`<div class="ac-medal ${x.on?"":"locked"}" title="${esc(x.d||x.t)}">
      <div class="disc" style="background:${x.col}">${esc(String(x.s))}</div><div class="t">${esc(x.t)}</div>
      <div class="d">${x.on?"Earned":"Not yet"}</div></div>`).join("")}</div></div>`;
  const nextCard=`<div class="card ac-next"><h2>Next up</h2>
    <p class="ac-sub">The closest next levels for certificates and ship masteries you've started</p>
    ${a.next.length?a.next.map((x,i)=>`<div class="row"><div class="grow"><span class="dim small">${esc(x.kind)}</span><br>
      <b>${esc(x.name)}</b><div class="dim small" title="${esc(x.miss.map(([s,l])=>skillName(s)+" "+roman(l)).join(", "))}">needs ${
        x.miss.length===1?esc(skillName(x.miss[0][0]))+" "+roman(x.miss[0][1]):x.miss.length+" skills"}</div></div>
      <div style="text-align:right"><div class="small">${tm(x.min)}</div>
      <button class="btn sec" style="margin-top:4px;padding:3px 10px" onclick="acPlan('${cid}',${i})">+ Plan</button></div></div>`).join("")
      :`<p class="dim">Nothing in progress yet.</p>`}</div>`;
  const f=state.acFilter||"all";
  const show=x=>f==="all"||(f==="elite"&&x.lv===5)||(f==="prog"&&x.lv>0&&x.lv<5)||(f==="none"&&x.lv===0);
  const groups=[...new Set(a.certs.map(x=>x.g))].sort();
  const certCard=`<div class="card"><h2>Certificates</h2>
    <p class="ac-sub">${a.certs.filter(x=>x.lv).length} of ${a.certs.length} held · ${lvLine} · worked out from your skills</p>
    <div class="ac-filters">${[["all","All"],["elite","Elite"],["prog","In progress"],["none","Not started"]].map(([k,l])=>
      `<button class="ac-chip ${f===k?"on":""}" onclick="acSetFilter('${k}')">${l}</button>`).join("")}</div>
    ${groups.map(g=>{ const list=a.certs.filter(x=>x.g===g&&show(x)); if(!list.length) return "";
      return `<div class="ac-group"><h3>${esc(g)}</h3><div class="ac-cgrid">${list.map(x=>`
      <div class="ac-cert l${x.lv}"><div class="top"><span>${esc(x.n)}</span><span class="lv${x.lv}" style="font-weight:600;font-size:12.5px">${CERT_LV[x.lv]}</span></div>
        ${acPips(x.lv)}
        <span class="dim small">${x.miss?`<span title="${esc(x.miss.map(([s,l])=>skillName(s)+" "+roman(l)).join(", "))}">Next: ${
          x.miss.length===1?esc(skillName(x.miss[0][0]))+" "+roman(x.miss[0][1]):x.miss.length+" skills"}${x.min!=null?" · "+tm(x.min):""}</span>
          · <button class="ac-link" onclick="acPlan('${cid}','${x.id}',true)">+ Plan</button>`:"All five levels ✓"}</span></div>`).join("")}</div></div>`;}).join("")
      ||`<p class="dim">No certificates match this filter.</p>`}
    <div class="ac-legend">${[1,2,3,4,5].map(l=>`<span style="--c:var(--l${l})">${roman(l)} · ${CERT_LV[l]}</span>`).join("")}</div></div>`;
  const rows=SHIP_CLASSES.map(([k])=>k).concat(["Other"]).filter(k=>a.classes[k]);
  const mastCard=`<div class="card"><h2>Ship masteries</h2>
    <p class="ac-sub">${a.ships.length} ships with mastery data${state.mast.info?"":" — refresh ship mastery data (Help &amp; settings → General) to group them by class"}</p>
    <table><tr><th>Ship class</th><th>Spread</th>${[1,2,3,4,5].map(l=>`<th class="num">${roman(l)}</th>`).join("")}<th class="num">None</th></tr>${
    rows.map(k=>{ const r=a.classes[k], none=r.tot-r.c.reduce((x,y)=>x+y,0);
      return `<tr><td>${k}</td><td><div class="ac-stack">${r.c.map((n,i)=>`<i style="width:${n/r.tot*100}%;background:var(--l${i+1})"></i>`).join("")}</div></td>${
        r.c.map((n,i)=>`<td class="num ${n?"lv"+(i+1):"dim"}">${n||"·"}</td>`).join("")}<td class="num dim">${none}</td></tr>`;}).join("")}</table>
    <p class="muted-note">Data: CCP static data build ${esc(String(state.mast.build||"?"))}. Look up a ship in Item Search for its mastery details.</p></div>`;
  m.innerHTML=`${tiles}<div class="ac-top">${medalCard}${nextCard}</div>${certCard}${mastCard}`;
}

function rIndustry(m){
  m.innerHTML=`<div class="card"><h2>Industry</h2>
    <div class="row">${charSelect("iChar")}
    <button class="btn" onclick="loadIndustry()">Load</button>
    <input id="bpSearch" size="24" placeholder="Filter blueprints…"
      oninput="renderBlueprints()"></div>
    <p class="muted-note">Needs the blueprint and industry scopes — if you
    added this character before those existed, update the scopes in your
    developer application and use ⚙ → Update permissions on the Characters screen.</p>
    <div id="jobsOut"></div>
    <div id="bpOut"></div></div>`;
}
async function loadIndustry(){
  const cid=$("#iChar")?.value;
  if(!cid){ toast("Add a character first."); return; }
  $("#jobsOut").innerHTML=`<p class="dim">Loading…</p>`;
  $("#bpOut").innerHTML="";
  try{
    const [jobs,bps]=await Promise.all([
      api("/api/character/"+cid+"/jobs"),
      api("/api/character/"+cid+"/blueprints")]);
    const activeJobs=jobs.filter(j=>j.status==="active");
    $("#jobsOut").innerHTML=`<h3>Running jobs (${activeJobs.length})</h3>`+
      (activeJobs.length?`<table><tr><th>Activity</th><th>Blueprint</th>
        <th>Product</th><th class="num">Runs</th>
        <th class="num">Done in</th></tr>`+
      activeJobs.map(j=>`<tr><td>${esc(j.activity)}</td>
        <td>${esc(j.blueprint)}</td><td>${esc(j.product)}</td>
        <td class="num">${num(j.runs)}</td>
        <td class="num">${until(j.end_date)}</td></tr>`).join("")+`</table>`
      :`<p class="dim">No jobs running.</p>`);
    state._bps=bps;
    renderBlueprints();
  }catch(e){
    $("#jobsOut").innerHTML="";
    toast(e.message.includes("403")||e.message.includes("token")
      ?"This character needs ⚙ → Update permissions on the Characters screen (see note)."
      :e.message);
  }
}
function renderBlueprints(){
  if(!state._bps||!$("#bpOut")) return;
  const q=($("#bpSearch")?.value||"").toLowerCase().trim();
  const bps=state._bps.filter(b=>!q||b.name.toLowerCase().includes(q)
    ||b.location.toLowerCase().includes(q));
  const orig=bps.filter(b=>!b.is_copy), copies=bps.filter(b=>b.is_copy);
  const bpTable=list=>`<table><tr><th>Blueprint</th><th class="num">ME</th>
    <th class="num">TE</th><th class="num">Runs</th><th>Location</th></tr>`+
    list.map(b=>`<tr><td>${esc(b.name)}${b.count>1?` ×${b.count}`:""}</td>
      <td class="num">${b.me}</td><td class="num">${b.te}</td>
      <td class="num">${b.runs<0?"∞":num(b.runs)}</td>
      <td class="dim">${esc(b.location)}</td></tr>`).join("")+`</table>`;
  const total=state._bps.length;
  $("#bpOut").innerHTML=
    (q?`<p class="dim">${bps.length} of ${total} blueprints match
      “${esc(q)}”</p>`:"")+
    `<h3>Originals (${orig.length})</h3>`+
    (orig.length?bpTable(orig):`<p class="dim">None.</p>`)+
    `<h3>Copies (${copies.length})</h3>`+
    (copies.length?bpTable(copies):`<p class="dim">None.</p>`);
}

boot();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

SERVER = [None]

# Browser tabs report in with a heartbeat. With "Close on Exit" switched on,
# EveWatch quits once every tab that was open has closed.
_tabs = {}                       # tab id -> last heartbeat time
_tabs_lock = threading.Lock()
_tab_state = {"seen": False, "last_change": time.time()}
TAB_STALE = 150                  # seconds: hidden tabs are throttled by browsers
CLOSE_GRACE = 8                  # seconds: lets a page reload or a second tab appear


def tab_alive(tab):
    with _tabs_lock:
        _tabs[tab] = time.time()
        _tab_state.update(seen=True, last_change=time.time())


def tab_closed(tab):
    with _tabs_lock:
        _tabs.pop(tab, None)
        _tab_state["last_change"] = time.time()


def should_quit_now(now=None):
    """True when 'Close on Exit' is on and no tab remains."""
    now = now or time.time()
    if not config.get("quit_on_close"):
        return False
    with _tabs_lock:
        for t in [t for t, ts in _tabs.items() if now - ts > TAB_STALE]:
            del _tabs[t]
            _tab_state["last_change"] = now
        return (_tab_state["seen"] and not _tabs
                and now - _tab_state["last_change"] > CLOSE_GRACE)


def _tab_watcher():
    while True:
        time.sleep(2)
        if should_quit_now():
            print("Last browser tab closed - quitting (Close on Exit is on).")
            request_shutdown()
            return


def request_shutdown():
    srv = SERVER[0]
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    try:
        import evewatch_tray
        evewatch_tray.stop()                  # remove the tray icon too
    except Exception:  # noqa: BLE001
        pass


def _setup_logging():
    """The installed Windows app has no console, so keep a log file instead."""
    if not FROZEN:
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        log = open(os.path.join(DATA_DIR, "evewatch.log"), "a",
                   encoding="utf-8", buffering=1)
        if sys.stdout is None or not sys.stdout.isatty():
            sys.stdout = log
        if sys.stderr is None or not sys.stderr.isatty():
            sys.stderr = log
    except OSError:
        pass


def _run_tray(url):
    """Notification-area icon (Open / Check for updates / Quit). Returns False
    if the system has no tray support, so the app just keeps running."""
    try:
        import evewatch_tray
    except ImportError:
        return False
    return evewatch_tray.run(
        APP_NAME, resource_path(os.path.join("assets", "icon.png")),
        on_open=lambda: webbrowser.open(url),
        on_update=lambda: webbrowser.open(url + "/#update"),
        on_quit=request_shutdown)


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print(f"{APP_NAME} {APP_VERSION}")
        return
    _setup_logging()
    os.makedirs(DATA_DIR, exist_ok=True)
    url = f"http://localhost:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # already running: just show the existing instance
        print(f"{APP_NAME} is already running at {url}")
        if "--no-browser" not in args:
            webbrowser.open(url)
        return
    SERVER[0] = server
    print(f"{APP_NAME} {APP_VERSION} running at {url}")
    print("Your data lives in:", DATA_DIR)
    threading.Thread(target=archiver_loop, daemon=True).start()
    threading.Thread(target=seed_hubs, daemon=True).start()
    threading.Thread(target=notify_loop, daemon=True).start()
    threading.Thread(target=_tab_watcher, daemon=True).start()
    if "--no-browser" not in args:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    use_tray = FROZEN or "--tray" in args
    try:
        if use_tray:
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            if _run_tray(url):          # blocks until Quit
                server.shutdown()
            else:
                t.join()                # no tray available: serve until Quit
        else:
            print("Press Ctrl+C to stop.")
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
