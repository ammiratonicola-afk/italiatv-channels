"""
catalog_new_uploads.py — MANUTENZIONE RICORRENTE del catalogo film.

Fa il diff tra i video ATTUALI dei 6 canali curati e `movies_metadata.json`,
e per ogni NUOVO upload (video_id non ancora in catalogo) prova a identificarlo
su TMDB e ad aggiungere il record completo (poster/trama/cast/regista/trailer).

Politica doppioni automatica:
- se il nuovo upload corrisponde a un film gia' presente (stesso tmdb_id),
  NON viene scartato;
- confronta automaticamente nuovo e vecchio per qualita' video YouTube,
  durata video e completezza metadati;
- mantiene visibile il migliore;
- conserva l'altro come riserva in `related_video_ids`, cosi' il player lo usa
  automaticamente se il link principale non e' piu' riproducibile.

USO:
    python catalog_new_uploads.py            # DRY-RUN: stampa cosa farebbe, non scrive
    python catalog_new_uploads.py --apply    # scrive davvero (con backup automatico)

Dopo --apply ricordarsi di bumpare MovieLoader.POSTER_RESOLVER_VERSION e
ricompilare l'APK perche' i dispositivi gia' installati ricostruiscano il pool.

NB: i film "no-match" (titolo clickbait non identificabile) NON vengono aggiunti
in dry-run; con --apply vengono aggiunti come voce base (titolo grezzo, no tmdb)
cosi' restano tracciati e si possono sistemare a mano con fix_film.py.
"""
import sys, os, re, json, shutil, datetime
import urllib.request, urllib.parse

from youtube_scrape import fetch_all_videos, fetch_all_videos_api, video_info_api
from add_channel import parse_title, tmdb_match, full_entry, JUNK, UA


def title_from_description(desc):
    """Estrae candidati titolo dalla DESCRIZIONE YouTube: cerca 'Titolo originale: X'
    / 'Original title: X' (e la prima riga utile). Aiuta a identificare i film il cui
    titolo del video è clickbait ma la descrizione riporta il titolo vero."""
    out = []
    for m in re.finditer(r"(?:titolo\s+originale|original\s+title)\s*[:\-]\s*([^\n(|]+)", desc or "", re.I):
        t = m.group(1).strip(" .\"'“”")
        if 2 <= len(t) <= 80:
            out.append(t)
    return out

# Se presente la chiave YouTube Data API (env YT_API_KEY), usala: è affidabile da
# qualsiasi IP (anche GitHub Actions). Altrimenti ripiega sullo scraping InnerTube.
YT_API_KEY = os.environ.get("YT_API_KEY", "").strip()
# Tetto di film identificati aggiunti per esecuzione: evita che un canale enorme
# (es. centinaia di film) faccia girare l'Action all'infinito / sfori i rate-limit TMDB.
# I restanti vengono importati alle esecuzioni successive (schedule/manuale).
MAX_NEW = int(os.environ.get("MAX_NEW_PER_RUN", "60"))


def get_videos(channel_id):
    if YT_API_KEY:
        try:
            vids = fetch_all_videos_api(channel_id, YT_API_KEY)
            if vids:
                return vids
        except Exception as e:
            print(f"  ! YT API fallita ({e}), ripiego sullo scraping")
    return fetch_all_videos(channel_id)

HERE = os.path.dirname(os.path.abspath(__file__))
# Path override via env (per la GitHub Action che lavora sui file alla radice del repo)
META = os.environ.get("META_PATH") or os.path.join(HERE, "..", "app", "src", "main", "assets", "movies_metadata.json")
ARCHIVE = os.environ.get("ARCHIVE_DIR") or os.path.join(HERE, "..", "_archive")

# Fallback hardcoded (usato se channels.json non c'è)
CHANNELS = {
    "UCbHyTxV_6Xz9FUXkGagH4bw": "FFF Seconda Serata",
    "UCElISJ4xHN50JzFVL8OPSrA": "BlueSwan Entertainment",
    "UCyzV93S6FKWPNtbxvl2DmJQ": "Movies in Action IT",
    "UC4rMaEpZKmKwQJHtMUi3IaA": "Moviedome IT",
    "UCavdo7TCQrJRxYt2QMj2Zaw": "Screamtime IT",
    "UC2DWplnIpAtu9OWgH9-ZVrQ": "Film&More",
    "UCObhuo6BTx5RpQISZqgTCwg": "Winston Media LLC",
}


def resolve_handle(ref):
    """Risolve un @handle o URL canale -> UC id. Prima via YouTube Data API
    (forHandle, affidabile da qualsiasi IP); fallback allo scraping della pagina."""
    m = re.search(r"(UC[A-Za-z0-9_-]{22})", ref)
    if m:
        return m.group(1)
    h = re.search(r"@([A-Za-z0-9._-]+)", ref)
    handle = h.group(1) if h else ref.lstrip("@").strip()
    # 1) YouTube Data API (niente scraping → non si blocca dagli IP datacenter)
    if YT_API_KEY:
        try:
            data = json.loads(urllib.request.urlopen(
                f"https://www.googleapis.com/youtube/v3/channels?part=id&forHandle={urllib.parse.quote(handle)}&key={YT_API_KEY}",
                timeout=15).read().decode("utf-8"))
            items = data.get("items", [])
            if items:
                return items[0]["id"]
        except Exception as e:
            print(f"  ! forHandle API errore ({e}), provo lo scraping")
    # 2) fallback: scraping pagina canale (può fallire/lento dagli IP cloud)
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/@{handle}/videos",
            headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9", "Cookie": "CONSENT=YES+1; SOCS=CAI"},
        )
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        mm = re.search(r'"(?:channelId|browseId|externalId)":"(UC[\w-]{22})"', html)
        return mm.group(1) if mm else None
    except Exception as e:
        print(f"  ! resolve_handle({ref}) errore: {e}")
        return None


def load_channels():
    """Legge channels.json (gestito dall'editor web), risolvendo gli handle in ID.
    Fallback al dict CHANNELS hardcoded se il file non esiste/è vuoto."""
    candidates = [
        os.environ.get("CHANNELS_JSON", ""),
        os.path.join(HERE, "..", "channels.json"),
        os.path.join(HERE, "..", "..", "channels.json"),
    ]
    path = next((p for p in candidates if p and os.path.exists(p)), None)
    if not path:
        return dict(CHANNELS)
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return dict(CHANNELS)
    out = {}
    for c in data.get("channels", []):
        cid = c.get("id") or (resolve_handle(c["handle"]) if c.get("handle") else None) \
            or (resolve_handle(c["url"]) if c.get("url") else None)
        if cid:
            out[cid] = c.get("name") or cid
    return out or dict(CHANNELS)

QUALITY_CACHE = {}


def active(m):
    return m.get("is_movie") is not False


def deleted_by_editor(m):
    reason = str(m.get("reason_not_movie") or "").lower()
    return m.get("is_movie") is False and "eliminat" in reason and "editor" in reason


def deleted_tmdb_ids(d):
    return {
        m.get("tmdb_id")
        for m in d["movies"]
        if deleted_by_editor(m) and m.get("tmdb_id")
    }


def yt_watch_info(video_id):
    if video_id in QUALITY_CACHE:
        return QUALITY_CACHE[video_id]
    info = {"height": 0, "duration": 0}
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/watch?v={video_id}&hl=it",
            headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"},
        )
        html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
        labels = re.findall(r'"qualityLabel":"(\d+)p?(?:60)?"', html)
        info["height"] = max([int(x) for x in labels] or [0])
        m = re.search(r'"lengthSeconds":"(\d+)"', html)
        if m:
            info["duration"] = int(m.group(1))
    except Exception:
        pass
    QUALITY_CACHE[video_id] = info
    return info


def title_quality_hint(m):
    raw = " ".join(str(m.get(k) or "") for k in ("original_title", "quality_notes", "channel")).lower()
    if "2160" in raw or "4k" in raw:
        return 2160
    if "1080" in raw or "full hd" in raw or "fhd" in raw:
        return 1080
    if "720" in raw:
        return 720
    if re.search(r"\bhd\b", raw):
        return 720
    if re.search(r"\bsd\b", raw):
        return 480
    return 0


def metadata_score(m):
    v2 = m.get("strict_id_v2") or {}
    score = 0
    if m.get("tmdb_id"): score += 30
    if m.get("tmdb_poster_path") or v2.get("locandina_url"): score += 10
    if v2.get("trama") or m.get("enriched_plot"): score += 8
    if (v2.get("trailer") or {}).get("url"): score += 3
    score += min(len(v2.get("cast_principale") or m.get("enriched_cast") or []), 8)
    if m.get("tmdb_confidence") == "verified": score += 3
    return score


def rank_tuple(m):
    info = yt_watch_info(m["video_id"])
    # Ordine: qualita' reale YouTube, hint dal titolo, durata upload, completezza scheda.
    return (info["height"], title_quality_hint(m), info["duration"], metadata_score(m))


def film_title(m):
    v2 = m.get("strict_id_v2") or {}
    return (
        v2.get("titolo_italiano")
        or m.get("tmdb_title_official")
        or m.get("enriched_title")
        or m.get("tmdb_title")
        or m.get("original_title")
        or m.get("video_id")
    )


def reserve_label_count(labels):
    return sum(1 for x in labels if str(x).lower().startswith("riserva"))


def set_primary_with_reserves(primary, reserves, note_title):
    ids = []
    for m in [primary] + reserves:
        vid = m.get("video_id")
        if vid and vid not in ids:
            ids.append(vid)

    labels = ["Principale"] + [f"Riserva {i}" for i in range(1, len(ids))]
    primary["is_movie"] = True
    primary["related_video_ids"] = ids
    primary["related_video_labels"] = labels
    primary["duplicate_policy"] = "primary_with_reserves"
    primary["duplicate_reserve_note"] = (
        "Doppione gestito automaticamente da catalog_new_uploads.py: "
        "la versione migliore resta visibile, le altre sono fallback automatici."
    )

    for i, m in enumerate(reserves, start=1):
        m["is_movie"] = False
        m["related_video_ids"] = ids
        m["related_video_labels"] = labels
        m["duplicate_policy"] = "reserve_link"
        m["reason_not_movie"] = (
            f"doppione riserva di {note_title}: nascosto dalla lista principale, "
            f"resta fallback automatico come Riserva {i}"
        )


def duplicate_candidates(d, tmdb_id):
    return [
        m for m in d["movies"]
        if m.get("tmdb_id") == tmdb_id and active(m)
    ]


def handle_duplicate(d, new_entry, cname, apply):
    tid = new_entry.get("tmdb_id")
    candidates = duplicate_candidates(d, tid)
    if not candidates:
        return "new"

    # Rispetta il lock: i film identificati (locked) non vengono modificati
    # automaticamente (es. trasformati in riserve). Solo modifiche manuali dall'editor.
    if any(m.get("locked") for m in candidates):
        print(f"    [LOCK] TMDB {tid} gia' presente e bloccato: non modifico automaticamente")
        return "duplicate_skipped_parts"

    # Evita di mescolare riserve con miniserie/film in parti: quelle restano picker.
    if any(any("parte" in str(label).lower() for label in (m.get("related_video_labels") or [])) for m in candidates):
        print(f"    [DUP/PARTI] TMDB {tid} gia' in gruppo a parti: non modifico automaticamente")
        return "duplicate_skipped_parts"

    old_ids = []
    for m in candidates:
        for rid in m.get("related_video_ids") or [m.get("video_id")]:
            if rid and rid not in old_ids:
                old_ids.append(rid)
    by_id = {m.get("video_id"): m for m in d["movies"] if m.get("video_id")}
    group = [by_id[vid] for vid in old_ids if vid in by_id]
    group = [m for m in group if m.get("video_id") != new_entry.get("video_id")]
    if not group:
        group = candidates

    all_versions = group + [new_entry]
    ranked = sorted(all_versions, key=rank_tuple, reverse=True)
    primary = ranked[0]
    reserves = ranked[1:]
    old_primary = next((m for m in group if active(m)), group[0])
    decision = "nuovo migliore" if primary is new_entry else "esistente migliore"
    print(
        f"    [DUP] {film_title(new_entry)[:42]} = TMDB {tid}: {decision} | "
        f"new={rank_tuple(new_entry)} old={rank_tuple(old_primary)}"
    )

    if apply:
        if new_entry not in d["movies"]:
            new_entry["channel"] = cname
            d["movies"].append(new_entry)
        set_primary_with_reserves(primary, reserves, film_title(primary))
    return "duplicate_merged"


def base_entry(vid, raw, cname):
    return {
        "video_id": vid,
        "is_movie": True,
        "original_title": raw,
        "channel": cname,
        "tmdb_id": None,
        "tmdb_confidence": "no_match",
    }


def main():
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== catalog_new_uploads.py [{mode}] ===\n")

    d = json.load(open(META, encoding="utf-8"))
    existing_vids = {m["video_id"] for m in d["movies"]}
    blocked_tmdb = deleted_tmdb_ids(d)
    seen_tmdb_run = set()

    tot_new = tot_added = tot_dupfilm = tot_nomatch = tot_skipped_parts = tot_blocked_deleted = 0
    run_added = 0   # film aggiunti in QUESTO run (identificati + no-match) per il tetto

    for cid, cname in load_channels().items():
        try:
            vids = get_videos(cid)
        except Exception as e:
            print(f"[{cname}] scrape FALLITO: {e}")
            continue
        new = [(v, t) for v, t in vids
               if v not in existing_vids and not any(k in t.lower() for k in JUNK)]
        # Info video (durata + lingua + descrizione) per: scartare i non-film (durata),
        # tenere solo i film in ITALIANO (lingua), e cercare il titolo nella descrizione.
        info = {}
        if YT_API_KEY and new:
            info = video_info_api([v for v, _ in new], YT_API_KEY)
            min_film = int(os.environ.get("MIN_FILM_SEC", "2400"))   # 40 minuti
            kept, drop_short, drop_lang = [], 0, 0
            for v, t in new:
                nfo = info.get(v, {})
                if nfo.get("dur", min_film) < min_film:
                    drop_short += 1; continue
                # È in italiano? Il campo audio è inaffidabile (gli uploader mettono la
                # lingua ORIGINALE anche per i film DOPPIATI). Quindi consideriamo italiano
                # se: audio=it OPPURE titolo/descrizione contengono marcatori italiani
                # ("italiano"/"in italiano"). Scartiamo solo i film palesemente stranieri
                # (es. canale inglese tipo Cult Cinema Classics, senza marcatori italiani).
                lang = (nfo.get("lang") or "").lower()
                text = (t + " " + (nfo.get("desc") or "")).lower()
                is_it = lang.startswith("it") or "italian" in text  # "italian" copre "italiano"
                if not is_it:
                    drop_lang += 1; continue
                kept.append((v, t))
            if drop_short:
                print(f"[{cname}] scartati {drop_short} video non-film (durata < {min_film // 60} min)")
            if drop_lang:
                print(f"[{cname}] scartati {drop_lang} video NON in italiano")
            new = kept
        if not new:
            print(f"[{cname}] {len(vids)} video, 0 nuovi (film italiani).")
            continue
        print(f"[{cname}] {len(vids)} video, {len(new)} NUOVI da valutare:")
        tot_new += len(new)

        if apply and run_added >= MAX_NEW:
            print(f"[{cname}] tetto {MAX_NEW} film/esecuzione raggiunto: il resto al prossimo run.")
            break
        for vid, raw in new:
            if apply and run_added >= MAX_NEW:
                print(f"    ...tetto {MAX_NEW} raggiunto, mi fermo (continua al prossimo run).")
                break
            cands, actor, year = parse_title(raw)
            # #2: aggiungi i titoli trovati nella DESCRIZIONE YouTube (es. "Original title: X")
            desc_cands = title_from_description((info.get(vid) or {}).get("desc", ""))
            cands = cands + [c for c in desc_cands if c not in cands]
            best, score = tmdb_match(cands, actor, year) if cands else (None, 0)
            if best:
                tid = best["id"]
                if tid in blocked_tmdb:
                    tot_blocked_deleted += 1
                    print(f"    [BLOCK] TMDB {tid} eliminato dall'editor: non lo reinserisco")
                    continue
                entry = full_entry(vid, tid, raw)
                if not entry:
                    print(f"    [?] TMDB {tid} ma full_entry fallita: {raw[:50]}")
                    continue
                entry["channel"] = cname

                if tid in seen_tmdb_run:
                    # Doppione trovato nello stesso run: confronta con le entry gia' aggiunte.
                    status = handle_duplicate(d, entry, cname, apply)
                else:
                    status = handle_duplicate(d, entry, cname, apply)

                if status == "new":
                    title = best.get("title")
                    year = (best.get("release_date") or "")[:4]
                    print(f"    [+] {title} ({year})  <- {raw[:42]}  | https://youtu.be/{vid}")
                    if apply:
                        d["movies"].append(entry)
                        existing_vids.add(vid)
                        seen_tmdb_run.add(tid)
                        tot_added += 1; run_added += 1
                elif status == "duplicate_merged":
                    tot_dupfilm += 1
                    if apply:
                        existing_vids.add(vid)
                        seen_tmdb_run.add(tid)
                elif status == "duplicate_skipped_parts":
                    tot_skipped_parts += 1
                continue

            tot_nomatch += 1
            print(f"    [?] NO MATCH: {raw[:55]}  | https://youtu.be/{vid}")
            if apply:
                d["movies"].append(base_entry(vid, raw, cname))
                existing_vids.add(vid)
                run_added += 1

    print(f"\n=== RIEPILOGO [{mode}] ===")
    print(f" nuovi upload valutati : {tot_new}")
    print(f" aggiunti nuovi film   : {tot_added if apply else '(dry: vedi [+])'}")
    print(f" doppioni gestiti      : {tot_dupfilm}")
    print(f" gruppi a parti saltati: {tot_skipped_parts}")
    print(f" bloccati eliminati    : {tot_blocked_deleted}")
    print(f" senza match           : {tot_nomatch}")

    if apply and (tot_added or tot_dupfilm or tot_nomatch):
        os.makedirs(ARCHIVE, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy(META, os.path.join(ARCHIVE, f"movies_metadata.pre-catalog-newuploads-{ts}.bak"))
        d["generated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        json.dump(d, open(META, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("\n SCRITTO su movies_metadata.json (backup in _archive).")
        print(" >> Ora bumpa MovieLoader.POSTER_RESOLVER_VERSION e ricompila l'APK.")
    elif apply:
        print("\n Niente da scrivere.")
    else:
        print("\n DRY-RUN: nessuna modifica. Rilancia con --apply per scrivere.")


if __name__ == "__main__":
    main()
