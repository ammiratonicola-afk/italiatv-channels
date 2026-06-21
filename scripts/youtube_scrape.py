"""
youtube_scrape.py - scraping completo della tab /videos di un canale YouTube.

Risolve il limite di 30 video della prima pagina seguendo i continuationToken
via l'endpoint interno youtubei/v1/browse.

API pubblica:
    fetch_all_videos(channel_id, verbose=False) -> [(video_id, title), ...]
"""

import json, re, time, sys
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

YT_BROWSE = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"

# Client info minimo richiesto dall'endpoint interno YouTube
CONTEXT = {
    "client": {
        "clientName":    "WEB",
        "clientVersion": "2.20240101.00.00",
        "hl":            "it",
        "gl":            "IT",
        "platform":      "DESKTOP",
    }
}


# ── Estrazione ricorsiva di video da node JSON ────────────────────────────────

def _walk_videos(node, out, seen):
    """Cerca videoId+title in qualsiasi struttura della response YouTube.
    Supporta sia il vecchio lockupViewModel sia altri renderer (gridVideoRenderer,
    richItemRenderer, videoRenderer)."""
    if isinstance(node, dict):
        # Pattern 1: lockupViewModel (Polymer nuovo)
        cid = node.get("contentId")
        title = None
        mtd = node.get("metadata")
        if isinstance(mtd, dict):
            ltv = mtd.get("lockupMetadataViewModel")
            if isinstance(ltv, dict):
                t = ltv.get("title")
                if isinstance(t, dict):
                    title = t.get("content")
        if cid and title and cid not in seen:
            seen.add(cid)
            out.append((cid, title.strip()))

        # Pattern 2: videoRenderer / gridVideoRenderer / richItemRenderer
        if "videoId" in node and "title" in node:
            vid = node["videoId"]
            ttl = node["title"]
            if isinstance(ttl, dict):
                # 'runs': [{'text': '...'}] oppure 'simpleText'
                if "simpleText" in ttl:
                    ttext = ttl["simpleText"]
                elif "runs" in ttl and ttl["runs"]:
                    ttext = "".join(r.get("text", "") for r in ttl["runs"])
                else:
                    ttext = None
                if vid and ttext and vid not in seen:
                    seen.add(vid)
                    out.append((vid, ttext.strip()))

        for v in node.values():
            _walk_videos(v, out, seen)
    elif isinstance(node, list):
        for v in node:
            _walk_videos(v, out, seen)


# ── Estrazione continuation token ──────────────────────────────────────────────

def _find_continuation(node):
    """Trova il prossimo continuationToken nelle response. Pattern:
    'continuationItemRenderer': {'continuationEndpoint': {'continuationCommand': {'token': '...'}}}
    """
    if isinstance(node, dict):
        cir = node.get("continuationItemRenderer")
        if isinstance(cir, dict):
            ep = cir.get("continuationEndpoint", {})
            cc = ep.get("continuationCommand", {})
            tok = cc.get("token")
            if tok: return tok
        # Alternative location
        cc = node.get("continuationCommand")
        if isinstance(cc, dict) and cc.get("token"):
            return cc["token"]
        for v in node.values():
            t = _find_continuation(v)
            if t: return t
    elif isinstance(node, list):
        for v in node:
            t = _find_continuation(v)
            if t: return t
    return None


# ── Fetch ──────────────────────────────────────────────────────────────────────

# Cookie di consenso: senza, YouTube serve il muro del consenso EU (specie agli IP
# datacenter come quelli di GitHub Actions) e ytInitialData arriva vuoto → 0 video.
CONSENT_COOKIE = "CONSENT=YES+1; SOCS=CAI"


def _http_get(url, headers=None, timeout=15):
    req = Request(url, headers={**(headers or {}), "User-Agent": UA, "Cookie": CONSENT_COOKIE})
    return urlopen(req, timeout=timeout).read()


def _http_post_json(url, body, headers=None, timeout=20):
    data = json.dumps(body).encode("utf-8")
    h = {
        "User-Agent":   UA,
        "Content-Type": "application/json",
        "Accept":       "*/*",
        "Origin":       "https://www.youtube.com",
        "Referer":      "https://www.youtube.com/",
        "Cookie":       CONSENT_COOKIE,
    }
    if headers: h.update(headers)
    req = Request(url, data=data, headers=h, method="POST")
    return urlopen(req, timeout=timeout).read()


def fetch_all_videos_api(channel_id, api_key, max_pages=60):
    """Lista TUTTI i (video_id, title) di un canale via YouTube Data API v3.
    Affidabile da QUALSIASI IP (anche datacenter/GitHub Actions), niente scraping.
    Usa la playlist 'uploads' del canale (UU + id) + playlistItems.list paginato.
    Quota: 1 unità per pagina da 50 video (~10/canale) → trascurabile sui 10.000/giorno."""
    uploads = "UU" + channel_id[2:]
    out, seen, token, page = [], set(), None, 0
    while page < max_pages:
        page += 1
        url = ("https://www.googleapis.com/youtube/v3/playlistItems"
               f"?part=snippet&maxResults=50&playlistId={uploads}&key={api_key}")
        if token:
            url += f"&pageToken={token}"
        try:
            data = json.loads(urlopen(Request(url, headers={"User-Agent": UA}), timeout=20).read().decode("utf-8"))
        except Exception as e:
            print(f"  ! YT Data API errore: {e}", file=sys.stderr)
            break
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            vid = (sn.get("resourceId") or {}).get("videoId")
            title = (sn.get("title") or "").strip()
            if vid and title and title not in ("Private video", "Deleted video") and vid not in seen:
                seen.add(vid)
                out.append((vid, title))
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def fetch_all_videos(channel_id, verbose=False, max_pages=50):
    """Restituisce TUTTI i (video_id, title) del canale seguendo le continuation.

    max_pages: limite di sicurezza (50 pagine × 30 video ≈ 1500 video).
    """
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        raw = _http_get(url, headers={"Accept-Language": "it-IT,it;q=0.9"})
    except (URLError, HTTPError, TimeoutError) as e:
        print(f"  ! fetch /videos errore: {e}", file=sys.stderr)
        return []
    html = raw.decode("utf-8", errors="replace")

    # Estrai ytInitialData
    m = re.search(r"ytInitialData\s*=\s*({.+?});\s*</script>", html)
    if not m:
        m = re.search(r"var ytInitialData\s*=\s*({.+?});", html)
    if not m:
        print(f"  ! ytInitialData non trovato", file=sys.stderr)
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  ! JSON parse: {e}", file=sys.stderr)
        return []

    out, seen = [], set()
    _walk_videos(data, out, seen)
    if verbose:
        print(f"      pagina 1: {len(out)} video", flush=True)

    # Estrai anche INNERTUBE_API_KEY per autenticare le richieste browse
    api_key = None
    m_k = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    if m_k:
        api_key = m_k.group(1)

    # Itera continuation
    continuation = _find_continuation(data)
    page = 1
    while continuation and page < max_pages:
        page += 1
        body = {"context": CONTEXT, "continuation": continuation}
        url = YT_BROWSE
        if api_key:
            url = f"{YT_BROWSE}&key={api_key}"
        try:
            resp_raw = _http_post_json(url, body)
        except (URLError, HTTPError, TimeoutError) as e:
            print(f"  ! continuation pagina {page} errore: {e}", file=sys.stderr)
            break
        try:
            resp = json.loads(resp_raw.decode("utf-8"))
        except json.JSONDecodeError:
            break

        before = len(out)
        _walk_videos(resp, out, seen)
        added = len(out) - before
        if verbose:
            print(f"      pagina {page}: +{added} (totale {len(out)})", flush=True)
        if added == 0:
            break  # nessun nuovo video → stop
        continuation = _find_continuation(resp)
        time.sleep(1.0)  # piu' gentile: evita rate limit YouTube su run lunghi

    return out


if __name__ == "__main__":
    # Test rapido
    test_id = sys.argv[1] if len(sys.argv) > 1 else "UC4rMaEpZKmKwQJHtMUi3IaA"
    videos = fetch_all_videos(test_id, verbose=True)
    print(f"\nTotale video: {len(videos)}")
    for v, t in videos[:5]:
        print(f"  {v}  {t[:70]}")
