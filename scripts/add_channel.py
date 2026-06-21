"""
add_channel.py <channel_id> - aggiunge i FILM di un canale YouTube al metadata.
- scarica tutti i video (paginazione), filtra non-film (unboxing/trailer/news),
- per ogni film: parsing titolo -> match TMDB -> popola poster/cast/trama/trailer,
- SALTA i doppioni: video_id gia' presente, o stesso tmdb_id gia' in catalogo.
- i no-match vengono aggiunti come voce base (titolo pulito, niente tmdb).
"""
import sys, os, re, json, difflib, urllib.request
from youtube_scrape import fetch_all_videos

KEY = "b1d160958cc3d7fddbd7de3caf85b926"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
HERE = os.path.dirname(os.path.abspath(__file__))
META = os.path.join(HERE, "..", "app", "src", "main", "assets", "movies_metadata.json")

JUNK = ["unboxing", "steelbook", "the movie corner", "le uscite", "trailer", "trl ", " trl",
        "teaser", "recensione", "a cura di", "in digital", "blu-ray", "blu ray", "4k ultra",
        "intervista", "reaction", "clip "]
GENRE_WORDS = {"azione","action","guerra","war","commedia","comedy","thriller","horror",
               "drammatico","drama","fantascienza","sci-fi","fantasy","family","storico",
               "biografico","avventura","adventure","romantico","romance","crime","mystery",
               "western","musical","documentario","poliziesco","grottesco","storia vera","ita","hd","4k"}


def http(u):
    try:
        return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=15).read().decode("utf-8", "ignore")
    except Exception:
        return ""


def tjson(u):
    r = http(u)
    try: return json.loads(r)
    except: return None


def parse_title(raw):
    """Estrae il/i titolo/i candidati dal pattern 'ITA - HD - Titolo - genere...'.
    Ritorna (candidati, attore, anno). L'anno (da '(2012)') serve a tmdb_match per
    disambiguare i match (evita di prendere un film omonimo di un altro anno)."""
    ym = re.search(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]", raw)
    year = int(ym.group(1)) if ym else None
    parts = [p.strip() for p in re.split(r"\s+-\s+|\s+\|\s+", raw) if p.strip()]
    actor = None
    cand = []
    for p in parts:
        pl = p.lower().strip()
        m = re.match(r"con\s+(.+)", pl)
        if m:
            actor = p[p.lower().index("con") + 3:].strip()
            continue
        # scarta token che sono SOLO parole di genere / marker
        words = [w for w in re.split(r"[\s,]+", pl) if w]
        if words and all(w in GENRE_WORDS for w in words):
            continue
        if pl in ("ita", "hd", "4k", "hd ita", "ita hd"):
            continue
        cand.append(p)
    # rimuovi prefissi ITA/HD residui dal primo token
    cand = [re.sub(r"^(ITA\s*-?\s*HD|HD\s*-?\s*ITA|ITA|HD)\s*[-:]?\s*", "", c, flags=re.I).strip() for c in cand]
    # ── Pulizia formato titolo (causa di molti mismatch) ──────────────────────
    # Per ogni candidato genera varianti più "pulite" che hanno più chance di
    # matchare TMDB: senza l'anno "(2012)", senza virgolette/puntini, e con il
    # solo titolo principale prima dei due punti ("BLOOD DEEP: ..." -> "BLOOD DEEP").
    extra = []
    for c in list(cand):
        c2 = re.sub(r"[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]", "", c)        # togli (anno)/[anno]
        c2 = c2.strip(" \"'“”«»").rstrip(".·-–").strip()
        if c2 and c2.lower() != c.lower():
            extra.append(c2)
        base = c2 if c2 else c
        if ":" in base:
            main = base.split(":")[0].strip(" \"'“”«»")
            if len(main) >= 3:
                extra.append(main)
    # dedup mantenendo l'ordine (i titoli "veri" restano davanti ai derivati)
    seen, out = set(), []
    for c in cand + extra:
        cl = c.lower()
        if len(c) >= 2 and cl not in seen:
            seen.add(cl); out.append(c)
    return out, actor, year


def sim(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def tmdb_match(cands, actor=None, year=None):
    best, best_s = None, 0.0
    for q in cands:
        for lang in ("it-IT", "en-US"):
            url = f"https://api.themoviedb.org/3/search/movie?api_key={KEY}&language={lang}&query={urllib.parse.quote(q)}"
            if year:
                url += f"&year={year}"
            r = tjson(url)
            for res in (r or {}).get("results", [])[:5]:
                s = max(sim(q, res.get("title", "")), sim(q, res.get("original_title", "")))
                score = s + (0.1 if res.get("poster_path") else -0.4)
                # disambigua per anno: bonus se combacia (±1), penalità se lontano (>2)
                if year:
                    ry = (res.get("release_date") or "")[:4]
                    if ry.isdigit():
                        diff = abs(int(ry) - year)
                        score += 0.15 if diff <= 1 else (-0.2 if diff > 2 else 0)
                if score > best_s:
                    best_s, best = score, res
    return (best, best_s) if best_s >= 0.72 else (None, best_s)


def full_entry(vid, tid, raw):
    it = tjson(f"https://api.themoviedb.org/3/movie/{tid}?api_key={KEY}&language=it-IT&append_to_response=credits,videos")
    en = tjson(f"https://api.themoviedb.org/3/movie/{tid}?api_key={KEY}&language=en-US")
    if not it: return None
    ov = it.get("overview") or (en or {}).get("overview") or ""
    cr = it.get("credits", {})
    cast = []
    for c in cr.get("cast", [])[:10]:
        pp = c.get("profile_path")
        cast.append({"nome": c.get("name"), "personaggio": c.get("character") or None,
                     "foto_url": (f"https://image.tmdb.org/t/p/w185{pp}" if pp else None),
                     "foto_tipo": "foto_attore" if pp else "nessuna",
                     "foto_fonte": {"nome": "TMDB", "url_pagina_origine": f"https://www.themoviedb.org/person/{c.get('id')}", "verificata": bool(pp)}})
    directors = [c["name"] for c in cr.get("crew", []) if c.get("job") == "Director"]
    yr = (it.get("release_date") or "")[:4]
    yt = [v for v in (it.get("videos") or {}).get("results", []) if v.get("site") == "YouTube"]
    yt.sort(key=lambda v: (v.get("type") == "Trailer", bool(v.get("official"))), reverse=True)
    trailer = {"url": f"https://www.youtube.com/watch?v={yt[0]['key']}", "lingua": "it", "fonte": "YouTube",
               "canale": None, "ufficiale": True, "verificato": True, "motivo_verifica": "TMDB videos"} if yt else \
              {"url": None, "lingua": None, "fonte": "YouTube", "canale": None, "ufficiale": False, "verificato": False, "motivo_verifica": ""}
    poster = it.get("poster_path")
    return {
        "video_id": vid, "is_movie": True,
        "original_title": raw,
        "channel": "Film&More",
        "tmdb_id": tid, "tmdb_title_official": it.get("title"),
        "tmdb_year_official": int(yr) if yr.isdigit() else None,
        "tmdb_poster_path": poster, "tmdb_confidence": "verified",
        "tmdb_reasoning": "Aggiunto da canale Film&More (add_channel.py)",
        "strict_id_v2": {
            "identificato": True, "livello_certezza": "alto",
            "titolo_youtube_originale": raw, "youtube_url": f"https://www.youtube.com/watch?v={vid}",
            "titolo_italiano": it.get("title"), "titolo_originale": it.get("original_title"),
            "anno_uscita": int(yr) if yr.isdigit() else None,
            "durata_minuti": it.get("runtime"),
            "genere": [g["name"] for g in it.get("genres", [])],
            "regista": directors, "cast_principale": cast, "trama": ov,
            "trama_tradotta_in_italiano": bool(it.get("overview")),
            "locandina_url": (f"https://image.tmdb.org/t/p/w500{poster}" if poster else None),
            "trailer": trailer, "tmdb_id_scelto": tid,
        },
    }


def main():
    ch = sys.argv[1] if len(sys.argv) > 1 else "UC2DWplnIpAtu9OWgH9-ZVrQ"
    d = json.load(open(META, encoding="utf-8"))
    existing_vids = {m["video_id"] for m in d["movies"]}
    existing_tmdb = {m.get("tmdb_id") for m in d["movies"] if m.get("is_movie") is not False and m.get("tmdb_id")}
    vids = fetch_all_videos(ch)
    films = [(v, t) for v, t in vids if not any(k in t.lower() for k in JUNK)]
    print(f"Video totali: {len(vids)} | film candidati: {len(films)}")

    added = dup_vid = dup_film = nomatch = 0
    seen_tmdb_run = set()
    for i, (vid, raw) in enumerate(films, 1):
        if vid in existing_vids:
            dup_vid += 1; continue
        cands, actor, year = parse_title(raw)
        if actor: cands = cands + [f"{c}".strip() for c in cands]
        best, score = tmdb_match(cands, actor, year) if cands else (None, 0)
        if best:
            tid = best["id"]
            if tid in existing_tmdb or tid in seen_tmdb_run:
                dup_film += 1
                print(f"  [DUP film] {raw[:45]} = TMDB {tid} gia' presente")
                continue
            e = full_entry(vid, tid, raw)
            if e:
                d["movies"].append(e); seen_tmdb_run.add(tid); added += 1
                print(f"  [+] {best.get('title')} ({(best.get('release_date') or '')[:4]})")
                continue
        # no match: voce base
        d["movies"].append({"video_id": vid, "is_movie": True, "original_title": raw,
                            "channel": "Film&More", "tmdb_id": None, "tmdb_confidence": "no_match"})
        nomatch += 1
        if i % 25 == 0:
            json.dump(d, open(META, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"  ...{i}/{len(films)} (aggiunti {added}, dup {dup_vid+dup_film}, nomatch {nomatch})")
    json.dump(d, open(META, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n=== REPORT ===\n aggiunti con TMDB: {added}\n doppioni video_id: {dup_vid}\n doppioni film (tmdb gia' in catalogo): {dup_film}\n senza match (voce base): {nomatch}")


if __name__ == "__main__":
    import urllib.parse
    main()
