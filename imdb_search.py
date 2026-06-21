"""
imdb_search.py — Self-contained IMDb/TMDB/TVMaze/OMDB search module for StreamVault.

Ported from the Telegram bot's search logic (main_patched.py) and adapted to return
structured dicts instead of formatted card strings, so StreamVault can use them
directly in its ``_imdb_cache_mem`` entries.

Usage
-----
    from imdb_search import search_imdb_for_album, fill_imdb_gaps

    # Full search
    result = search_imdb_for_album("Money Heist")
    # result = {"poster": "...", "year": "2017", "rating": "8.2",
    #           "type": "Series", "plot": "...", "source": "tmdb", ...}

    # Gap-fill only
    existing = {"poster": "", "year": "2017", "rating": "", "type": "", "plot": "", "source": ""}
    filled = fill_imdb_gaps("Money Heist", existing)

Dependencies: ``requests``, ``re`` (both standard / already available).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_TMDB_API_KEY = "10822052cd7c36868f387e3f713ad713"
_DEFAULT_OMDB_API_KEY = "trilogy"
_TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_TMDB_API_BASE = "https://api.themoviedb.org/3"
_IMDB_GQL_URL = "https://api.graphql.imdb.com/"
_IMDB_SUGGEST_BASE = "https://v3.sg.media-imdb.com/suggestion/x"
_TVMAZE_SEARCH_URL = "https://api.tvmaze.com/search/shows"
_OMDB_URL = "https://www.omdbapi.com/"

# Articles / filler words stripped for exact-match comparison
_ARTICLES: Set[str] = {"the", "a", "an", "by", "of", "in", "on", "at", "to"}

# Leet-speak substitution table: digit/symbol → letter
_LEET_TABLE = str.maketrans(
    {"3": "e", "0": "o", "1": "i", "4": "a", "@": "a", "!": "i", "5": "s"}
)

# Regex to detect S/E codes in raw queries
_RE_HAS_SE = re.compile(r"\b[Ss]\d{1,2}([Ee]\d{1,3})?\b|\b[Ee]\d{1,3}\b")

# Filler prefix phrases stripped before TVMaze search
_FILLER_LEAD_TVMAZE = re.compile(
    r"^\s*(?:"
    r"what\s+about\s+"
    r"|how\s+about\s+"
    r"|any\s+chance\s+(?:you\s+have\s+)?"
    r"|got\s+any\s+"
    r"|i\s+(?:wanna?|want|need|crave)\s+"
    r"|i\s+would\s+(?:like|love)\s+to\s+(?:have|get|watch|see)?\s*"
    r"|(?:give|send|gimme|gimmie)\s+(?:me\s+)?"
    r"|(?:can|could)\s+i\s+(?:get|have)\s+"
    r"|(?:that|the)\s+(?:series|show|movie|film)\s+(?:called|titled|named|where|about|with|that)?\s*"
    r"|(?:looking|searching|hunting)\s+for\s+"
    r"|(?:i\s+(?:am|'m)\s+)?(?:looking|searching)\s+for\s+"
    r"|(?:do\s+you\s+have|have\s+you\s+got)\s+"
    r")+",
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────────────────────
# Utility / title-matching functions
# ──────────────────────────────────────────────────────────────────────────────


def _deleet(s: str) -> str:
    """Convert leet-speak digits back to letters: ``numb3rs`` → ``numbers``."""
    return s.lower().translate(_LEET_TABLE)


def _tokenize_title(s: str) -> Set[str]:
    """Tokenize a title with leet normalization.

    Punctuation like ``!`` and ``?`` is stripped first so ``Baymax!`` and
    ``Baymax`` match.  The leet table runs *after* punctuation removal so
    ``!`` is never mapped to ``i``.
    """
    s_clean = re.sub(r"[!?.:;,'\"]", "", s.lower())
    return set(_deleet(re.sub(r"[^a-z0-9@]", " ", s_clean)).split())


def _title_similarity(a: str, b: str) -> float:
    """Jaccard token overlap (leet-aware). Returns 0.0 – 1.0."""
    ta = _tokenize_title(a)
    tb = _tokenize_title(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _title_precision(query: str, title: str) -> float:
    """What fraction of the *result* title's words appear in the *query*?

    Penalises spin-offs/remakes with extra words::

        "money heist" vs "Money Heist Korea..." → 2/6 ≈ 0.33
        "money heist" vs "Money Heist"          → 1.0
    """
    tq = _tokenize_title(query)
    tt = _tokenize_title(title)
    if not tt:
        return 0.0
    return len(tq & tt) / len(tt)


def _leet_variants(query: str) -> List[str]:
    """Generate alternative search queries using leet-speak substitutions.

    ``"numbers"`` → ``["numb3rs"]``, ``"house"`` → ``["h0use"]``.
    Only generates variants that differ from the original.
    """
    _TO_LEET = {"e": "3", "o": "0", "i": "1", "a": "4", "s": "5"}
    variants: Set[str] = set()
    words = query.split()
    for i, word in enumerate(words):
        for char, digit in _TO_LEET.items():
            if char in word:
                new_word = word.replace(char, digit, 1)
                new_query = " ".join(words[:i] + [new_word] + words[i + 1 :])
                if new_query != query:
                    variants.add(new_query)
    return list(variants)


# ──────────────────────────────────────────────────────────────────────────────
# Query cleaning
# ──────────────────────────────────────────────────────────────────────────────


def _clean_query(raw_query: str) -> Tuple[str, Optional[str], bool]:
    """Pre-process a raw album/show query.

    Returns ``(cleaned_query, user_year, has_se_code)``.
    """
    # Extract year
    year_match = re.search(r"\b(19|20)\d{2}\b", raw_query)
    user_year = year_match.group(0) if year_match else None
    has_se_code = bool(_RE_HAS_SE.search(raw_query))

    # Replace dots-as-separators in filenames
    q = raw_query.rstrip(".")
    if re.search(r"(?<=[a-zA-Z])\.(?=[a-zA-Z])", q):
        q = re.sub(r"\.", " ", q)
        q = re.sub(r"\s+", " ", q).strip()

    # Strip release/quality tags and SE codes
    q = re.sub(
        r"\b(s\d{1,2}(e\d{1,3})?|e\d{1,3}|\d{3,4}p|x26[45]|hevc|webrip|bluray|"
        r"psa|vyndros|kontrast|yts|yify|rarbg|sparks|ion10|fgt|cmrg|ntb|evo|vtv|"
        r"remarkable|deflate|inflate|proper|repack|extended|unrated|theatrical|"
        r"directors\.?cut|(19|20)\d{2})\b",
        "",
        q,
        flags=re.IGNORECASE,
    )

    # Strip dangling connector words left after SE code removal
    q = re.sub(r"\b(and|or|also)\b", " ", q, flags=re.IGNORECASE)
    q = re.sub(r"(?<!\w)[-–](?!\w)", " ", q)
    q = re.sub(r"\s+", " ", q).strip().lower()
    q = re.sub(
        r"\s+\b(?:series|show|movie|film|seasons?|complete)\b\s*$",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip()
    return q, user_year, has_se_code


def _extract_title_for_tvmaze(cleaned_query: str) -> str:
    """Strip filler prefix phrases and trailing episode-title junk."""
    q = cleaned_query.strip()
    q = _FILLER_LEAD_TVMAZE.sub("", q).strip()
    q = re.sub(
        r"\s+\b(?:series|show|movie|film|seasons?|complete)\b\s*$",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip()
    return q


# ──────────────────────────────────────────────────────────────────────────────
# Empty result helper
# ──────────────────────────────────────────────────────────────────────────────


def _empty_result() -> Dict[str, str]:
    """Return a dict with all keys set to empty string."""
    return {
        "poster": "",
        "year": "",
        "rating": "",
        "type": "",
        "plot": "",
        "source": "",
        "stars": "",
        "director": "",
        "imdb_id": "",
        "genre": "",
        "runtime": "",
        "country": "",
        "network": "",
    }


# ──────────────────────────────────────────────────────────────────────────────
# TMDB search
# ──────────────────────────────────────────────────────────────────────────────


def _tmdb_search_best(
    cleaned_query: str,
    headers: Dict[str, str],
    tmdb_api_key: str,
    user_year: Optional[str] = None,
    force_type: Optional[str] = None,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Search TMDB for a movie or TV series.

    Returns ``(result_dict, kind)`` where *kind* is ``"movie"`` or ``"series"``.
    Returns ``(None, None)`` on failure.
    """
    if not tmdb_api_key:
        return None, None
    try:
        _params: Dict[str, Any] = {
            "api_key": tmdb_api_key,
            "query": cleaned_query,
            "include_adult": False,
        }
        if user_year:
            _params["year"] = user_year

        # Determine search endpoint
        if force_type == "series":
            _endpoints = [("tv", "series")]
        elif force_type == "movie":
            _endpoints = [("movie", "movie")]
        else:
            _endpoints = [("multi", None)]

        _today = datetime.now().strftime("%Y-%m-%d")
        _today_yr = int(datetime.now().strftime("%Y"))

        # Language variants — English first, then CJK fallbacks
        _lang_fallbacks = [None, "zh-CN", "zh-TW", "ja", "ko"]

        _results: List[Tuple[dict, str]] = []
        _seen_tmdb_ids: Set[int] = set()

        for _ep, _kind_hint in _endpoints:
            _english_results: List[Tuple[dict, str]] = []
            for _lang in _lang_fallbacks:
                try:
                    _lp = dict(_params)
                    if _lang:
                        _lp["language"] = _lang
                    _r = requests.get(
                        f"{_TMDB_API_BASE}/search/{_ep}",
                        params=_lp,
                        headers=headers,
                        timeout=8,
                    )
                    if _r.status_code == 200:
                        _data = _r.json().get("results") or []
                        for _item in _data:
                            _tid = _item.get("id")
                            if _tid in _seen_tmdb_ids:
                                continue
                            # Filter unreleased
                            _item_date = (
                                _item.get("release_date")
                                or _item.get("first_air_date")
                                or ""
                            )
                            if _item_date and _item_date > _today:
                                continue
                            _mt = _item.get("media_type") or (
                                _kind_hint
                                or ("tv" if _item.get("first_air_date") else "movie")
                            )
                            _results.append((_item, _mt))
                            _seen_tmdb_ids.add(_tid)
                            if _lang is None:
                                _english_results.append((_item, _mt))
                except Exception:
                    pass

                # Perf: if English found good results, skip CJK fallbacks
                if _lang is None and _english_results:
                    _best_eng_prec = 0.0
                    _ql = {tk for tk in _tokenize_title(cleaned_query) if len(tk) >= 3}
                    if _ql:
                        for _ei, _em in _english_results:
                            _et = (_ei.get("title") or _ei.get("name") or "").lower()
                            _eo = (
                                _ei.get("original_title")
                                or _ei.get("original_name")
                                or ""
                            ).lower()
                            _hits = sum(1 for _qt in _ql if _qt in _et or _qt in _eo)
                            _prec = _hits / len(_ql)
                            if _prec > _best_eng_prec:
                                _best_eng_prec = _prec
                    else:
                        for _ei, _em in _english_results:
                            _et = _ei.get("title") or _ei.get("name") or ""
                            _eo = (
                                _ei.get("original_title")
                                or _ei.get("original_name")
                                or ""
                            )
                            _es = max(
                                _title_similarity(cleaned_query, _et),
                                _title_similarity(cleaned_query, _eo),
                            )
                            if _es > _best_eng_prec:
                                _best_eng_prec = _es
                    if _best_eng_prec >= 0.50:
                        break

        if not _results:
            return None, None

        # ── Score candidates ─────────────────────────────────────────────────

        def _tmdb_score(item_mt: Tuple[dict, str]) -> float:
            _item, _mt = item_mt
            _t = _item.get("title") or _item.get("name") or ""
            _orig = _item.get("original_title") or _item.get("original_name") or ""
            _sim = max(
                _title_similarity(cleaned_query, _t),
                _title_similarity(cleaned_query, _orig),
            )
            # Substring similarity (long tokens ≥3 chars only)
            _q_tokens = _tokenize_title(cleaned_query)
            _q_long = {tk for tk in _q_tokens if len(tk) >= 3}
            if _q_long:
                for _candidate in [_t.lower(), _orig.lower()]:
                    _sub_hits = sum(1 for _qt in _q_long if _qt in _candidate)
                    _sub_sim = _sub_hits / len(_q_long)
                    _sim = max(_sim, _sub_sim)
            _pop = min(float(_item.get("popularity") or 0) / 200.0, 1.0)
            _yr_str = (_item.get("release_date") or _item.get("first_air_date") or "")[
                :4
            ]
            # Year bonus / penalty
            _yr_bonus = 0.0
            if user_year:
                if _yr_str == user_year:
                    _yr_bonus = 0.10
                elif _yr_str:
                    _yr_bonus = -0.20
            # Unreleased penalty
            _unreleased_pen = 0.0
            _item_date = _item.get("release_date") or _item.get("first_air_date") or ""
            _item_status = _item.get("status") or ""
            if _item_date and _item_date > _today:
                _unreleased_pen = -0.30
            elif _item_status in ("Planned", "In Production", "Canceled"):
                _unreleased_pen = -0.30
            _exact = (
                0.50
                if (
                    (_tokenize_title(cleaned_query) - _ARTICLES)
                    == (_tokenize_title(_t) - _ARTICLES)
                )
                or (
                    (_tokenize_title(cleaned_query) - _ARTICLES)
                    == (_tokenize_title(_orig) - _ARTICLES)
                )
                else 0.0
            )
            return _exact + 0.30 * _sim + 0.10 * _pop + _yr_bonus + _unreleased_pen

        _results.sort(key=_tmdb_score, reverse=True)
        _best_item, _best_mt = _results[0]

        if _tmdb_score((_best_item, _best_mt)) < 0.10:
            return None, None

        # ── Long-token precision gate ────────────────────────────────────────
        _q_long = {tk for tk in _tokenize_title(cleaned_query) if len(tk) >= 3}
        if _q_long:
            _best_t = _best_item.get("title") or _best_item.get("name") or ""
            _best_o = (
                _best_item.get("original_title")
                or _best_item.get("original_name")
                or ""
            )
            _long_hits = sum(
                1 for _qt in _q_long if _qt in _best_t.lower() or _qt in _best_o.lower()
            )
            _long_precision = _long_hits / len(_q_long)
            if _long_precision < 0.60:
                return None, None

        _tmdb_id = _best_item.get("id")
        _is_tv = _best_mt in ("tv",) or (
            _best_item.get("first_air_date") is not None
            and not _best_item.get("release_date")
        )

        # ── Fetch full details ──────────────────────────────────────────────
        _detail_ep = "tv" if _is_tv else "movie"
        _detail_r = requests.get(
            f"{_TMDB_API_BASE}/{_detail_ep}/{_tmdb_id}",
            params={
                "api_key": tmdb_api_key,
                "append_to_response": "credits,content_ratings,release_dates,translations,external_ids",
            },
            headers=headers,
            timeout=8,
        )
        if _detail_r.status_code != 200:
            return None, None
        _d = _detail_r.json()

        # ── Extract fields ──────────────────────────────────────────────────
        _title = _d.get("title") or _d.get("name") or ""
        _raw_date = _d.get("release_date") or _d.get("first_air_date") or ""
        _year = _raw_date[:4] if _raw_date else ""
        _year_end = None
        if _is_tv and _d.get("last_air_date"):
            _last_yr = _d["last_air_date"][:4]
            if _last_yr != _year:
                _year_end = _last_yr
        _rating = round(_d.get("vote_average") or 0, 1)
        _rating_str = str(_rating) if _rating else ""
        _genres = [g["name"] for g in (_d.get("genres") or [])[:4]]
        _overview = _d.get("overview") or ""
        # TMDB often returns empty overview for non-English titles
        if not _overview.strip():
            for _tr in (_d.get("translations") or {}).get("translations", []):
                if _tr.get("iso_639_1") == "en" and (
                    (_tr.get("data") or {}).get("overview", "").strip()
                ):
                    _overview = _tr["data"]["overview"]
                    break
        if not _overview.strip():
            _overview = ""
        _poster_url = (
            _TMDB_IMG_BASE + _d["poster_path"] if _d.get("poster_path") else ""
        )
        _networks = ", ".join(n.get("name", "") for n in (_d.get("networks") or [])[:2])
        _countries = [
            c.get("name", "") for c in (_d.get("production_countries") or [])[:2]
        ]

        # Runtime
        _rt_raw = _d.get("runtime") or (_d.get("episode_run_time") or [None])[0]
        _runtime_s = ""
        if _rt_raw:
            _runtime_s = (
                f"{_rt_raw // 60}h {_rt_raw % 60}m" if _rt_raw >= 60 else f"{_rt_raw}m"
            )

        # Credits
        _credits = _d.get("credits") or {}
        _cast_list = [c["name"] for c in (_credits.get("cast") or [])[:4]]
        _crew_list = _credits.get("crew") or []
        _directors = [c["name"] for c in _crew_list if c.get("job") == "Director"][:2]

        # External IDs
        _ext_ids = _d.get("external_ids") or {}
        _imdb_id_from_tmdb = _ext_ids.get("imdb_id") or ""
        if not _imdb_id_from_tmdb.startswith("tt"):
            _imdb_id_from_tmdb = ""

        # ── Gap-fill stars / plot / directors via IMDb GraphQL ───────────────
        if _imdb_id_from_tmdb and (not _cast_list or not _overview or not _directors):
            try:
                _gql_fallback = _gql_fetch_imdb(_imdb_id_from_tmdb)
                if _gql_fallback:
                    if not _cast_list:
                        _gql_stars = _gql_fallback.get("stars") or []
                        if _gql_stars:
                            _cast_list = _gql_stars
                    if not _overview:
                        _gql_plot = _gql_fallback.get("plot") or ""
                        if _gql_plot:
                            _overview = _gql_plot
                    if not _directors:
                        _gql_dirs = _gql_fallback.get("directors") or []
                        if _gql_dirs:
                            _directors = _gql_dirs
            except Exception:
                pass

        # Build result dict
        _kind = "series" if _is_tv else "movie"
        _year_disp = f"{_year}–{_year_end}" if _year_end else _year

        result = _empty_result()
        result.update(
            {
                "poster": _poster_url,
                "year": _year_disp,
                "rating": _rating_str,
                "type": "Series" if _is_tv else "Movie",
                "plot": _overview,
                "source": "tmdb",
                "stars": ", ".join(_cast_list) if _cast_list else "",
                "director": ", ".join(_directors) if _directors else "",
                "imdb_id": _imdb_id_from_tmdb,
                "genre": ", ".join(_genres) if _genres else "",
                "runtime": _runtime_s,
                "country": ", ".join(_countries) if _countries else "",
                "network": _networks,
            }
        )
        return result, _kind

    except Exception:
        return None, None


# ──────────────────────────────────────────────────────────────────────────────
# IMDb GraphQL helpers
# ──────────────────────────────────────────────────────────────────────────────


def _gql_fetch_imdb(imdb_id: str) -> Optional[Dict[str, Any]]:
    """Fetch full title details from IMDb's GraphQL API by ID.

    Returns a dict with keys: ``title``, ``year``, ``year_end``, ``rating``,
    ``votes``, ``runtime``, ``genres``, ``plot``, ``directors``, ``stars``,
    ``mpaa``, ``countries``, ``metacritic``, ``kind``, or ``None`` on failure.
    """
    _headers = {
        "Content-Type": "application/json",
        "x-imdb-user-country": "US",
        "User-Agent": "Mozilla/5.0",
    }
    query = """
        query {
          title(id: "%s") {
            id
            titleText { text }
            titleType { id }
            releaseYear { year endYear }
            ratingsSummary { aggregateRating voteCount }
            runtime { seconds }
            plot { plotText { plainText } }
            genres { genres { text } }
            certificate { rating }
            principalCredits {
              category { id }
              credits { name { nameText { text } } }
            }
            countriesOfOrigin { countries { text } }
            metacritic { metascore { score } }
          }
        }
    """ % imdb_id
    try:
        resp = requests.post(
            _IMDB_GQL_URL,
            headers=_headers,
            json={"query": query},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("errors"):
            return None
        t = (data.get("data") or {}).get("title")
        if not t:
            return None

        title = (t.get("titleText") or {}).get("text") or ""
        kind_raw = (t.get("titleType") or {}).get("id") or ""
        _ry = t.get("releaseYear") or {}
        year = str(_ry.get("year")) if _ry.get("year") else None
        year_end = str(_ry.get("endYear")) if _ry.get("endYear") else None
        _rs = t.get("ratingsSummary") or {}
        rating = _rs.get("aggregateRating")
        votes = _rs.get("voteCount")
        _rt = t.get("runtime") or {}
        _secs = _rt.get("seconds") or 0
        runtime = (
            f"{_secs // 3600}h {(_secs % 3600) // 60}m"
            if _secs >= 3600
            else (f"{_secs // 60}m" if _secs else "")
        )
        plot = ((t.get("plot") or {}).get("plotText") or {}).get("plainText") or ""
        genres = [
            g.get("text", "")
            for g in (t.get("genres") or {}).get("genres", [])
            if g.get("text")
        ]
        mpaa = (t.get("certificate") or {}).get("rating")
        countries = [
            c.get("text", "")
            for c in (t.get("countriesOfOrigin") or {}).get("countries", [])
            if c.get("text")
        ]
        _mc = (t.get("metacritic") or {}).get("metascore") or {}
        metacritic = int(_mc["score"]) if _mc.get("score") is not None else None

        directors: List[str] = []
        stars: List[str] = []
        for pc in t.get("principalCredits") or []:
            cat = (pc.get("category") or {}).get("id", "")
            names = [
                c.get("name", {}).get("nameText", {}).get("text", "")
                for c in (pc.get("credits") or [])
            ]
            names = [n for n in names if n]
            if cat == "director":
                directors = names[:2]
            elif cat == "cast":
                stars = names[:4]

        return {
            "title": title,
            "year": year,
            "year_end": year_end,
            "rating": rating,
            "votes": votes,
            "runtime": runtime,
            "genres": genres,
            "plot": plot,
            "directors": directors,
            "stars": stars,
            "mpaa": mpaa,
            "countries": countries,
            "metacritic": metacritic,
            "kind": kind_raw,
        }
    except Exception:
        return None


def _gql_search_imdb(
    query: str,
    tmdb_api_key: Optional[str] = None,
    omdb_api_key: Optional[str] = None,
    year: Optional[str] = None,
    force_type: Optional[str] = None,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Search IMDb via the Suggest/Legacy API, then fetch details via GraphQL.

    Returns ``(result_dict, kind)`` where *kind* is ``"movie"`` or ``"series"``.
    Returns ``(None, None)`` on failure.
    """
    try:
        _today_yr = int(datetime.now().strftime("%Y"))
        _sq = re.sub(r"[^a-zA-Z0-9 ]", " ", query).strip()
        _sq = re.sub(r"\s+", "_", _sq)
        _suggest_url = f"{_IMDB_SUGGEST_BASE}/{_sq}.json"
        _r = requests.get(
            _suggest_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
            timeout=6,
        )
        if _r.status_code != 200:
            return None, None
        _data = _r.json()
        _items = _data.get("d") or []
        if not _items:
            return None, None

        # Filter by type
        _VALID_TYPES = {
            "movie",
            "tvSeries",
            "tvMiniSeries",
            "tvMovie",
            "video",
        }
        if force_type == "series":
            _VALID_TYPES = {"tvSeries", "tvMiniSeries"}
        elif force_type == "movie":
            _VALID_TYPES = {"movie", "tvMovie"}

        # Score candidates
        _scored: List[Tuple[float, dict]] = []
        for _it in _items[:20]:
            _kind = _it.get("qid", "") or ""
            if _kind not in _VALID_TYPES and _kind not in {
                "movie",
                "tvSeries",
                "tvMiniSeries",
                "tvMovie",
                "video",
            }:
                continue
            _t = _it.get("l", "") or ""
            _sim = _title_similarity(query, _t)
            if _sim < 0.10:
                continue
            _yr = str(_it.get("y", ""))[:4] if _it.get("y") else ""
            if year and _yr and abs(int(_yr) - int(year)) > 1:
                continue
            # Filter unreleased
            if _yr and _yr.isdigit() and int(_yr) > _today_yr:
                continue
            # Long-token precision check (50 % threshold)
            _q_long = {tk for tk in _tokenize_title(query) if len(tk) >= 3}
            if _q_long:
                _lt_hits = sum(1 for _qt in _q_long if _qt in _t.lower())
                _lt_prec = _lt_hits / len(_q_long)
                if _lt_prec < 0.50:
                    continue
            _exact = (
                0.50
                if (_tokenize_title(query) - _ARTICLES)
                == (_tokenize_title(_t) - _ARTICLES)
                else 0.0
            )
            # Year bonus / penalty
            _yr_score = (
                0.10
                if _yr == (year or "")
                else (-0.15 if (year and _yr and _yr != year) else 0.0)
            )
            _score = _exact + 0.40 * _sim + _yr_score
            _scored.append((_score, _it))

        if not _scored:
            return None, None

        _scored.sort(key=lambda x: x[0], reverse=True)
        _best = _scored[0][1]
        _imdb_id = _best.get("id", "")
        if not _imdb_id:
            return None, None

        # Fetch full details via GraphQL
        _gql = _gql_fetch_imdb(_imdb_id)
        if not _gql:
            return None, None

        _kind_raw = _gql.get("kind") or ""
        _is_series = any(k in _kind_raw for k in ("series", "tvseries", "miniseries"))
        _kind = "series" if _is_series else "movie"

        _year = _gql.get("year") or str(_best.get("y", ""))
        _year_end = _gql.get("year_end")

        # Filter unreleased
        try:
            if _year and _year.isdigit() and int(_year) > _today_yr:
                return None, None
        except (ValueError, TypeError):
            pass

        _plot = _gql.get("plot") or ""
        _rating_val = _gql.get("rating")
        _rating_str = str(_rating_val) if _rating_val else ""
        _poster = ""
        # IMDb suggestion results include a poster URL in "i" → "imageUrl"
        _img_info = _best.get("i") or {}
        if isinstance(_img_info, dict) and _img_info.get("imageUrl"):
            _poster = _img_info["imageUrl"]

        _gql_stars = _gql.get("stars") or []
        _gql_directors = _gql.get("directors") or []

        # ── Gap-fill via TMDB find-by-IMDb-ID ──────────────────────────────
        if (not _plot.strip()) or (not _gql_stars) or (not _gql_directors):
            if tmdb_api_key and _imdb_id:
                try:
                    _find_r = requests.get(
                        f"{_TMDB_API_BASE}/find/{_imdb_id}",
                        params={
                            "api_key": tmdb_api_key,
                            "external_source": "imdb_id",
                        },
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=6,
                    )
                    if _find_r.status_code == 200:
                        _fd = _find_r.json()
                        _tmdb_hit = None
                        _tmdb_hit_is_tv = False
                        _tv_res = (
                            _fd.get("tv_results") or _fd.get("tv_episode_results") or []
                        )
                        _mv_res = _fd.get("movie_results") or []
                        if _tv_res:
                            _tmdb_hit = _tv_res[0]
                            _tmdb_hit_is_tv = True
                        elif _mv_res:
                            _tmdb_hit = _mv_res[0]
                            _tmdb_hit_is_tv = False
                        if _tmdb_hit and _tmdb_hit.get("id"):
                            _tmdb_fid = _tmdb_hit["id"]
                            _detail_ep = "tv" if _tmdb_hit_is_tv else "movie"
                            _det_r = requests.get(
                                f"{_TMDB_API_BASE}/{_detail_ep}/{_tmdb_fid}",
                                params={
                                    "api_key": tmdb_api_key,
                                    "append_to_response": "credits,translations",
                                },
                                headers={"User-Agent": "Mozilla/5.0"},
                                timeout=6,
                            )
                            if _det_r.status_code == 200:
                                _td = _det_r.json()
                                # Plot gap-fill
                                if not _plot.strip():
                                    _t_overview = _td.get("overview") or ""
                                    if not _t_overview.strip():
                                        for _tr in (_td.get("translations") or {}).get(
                                            "translations", []
                                        ):
                                            if _tr.get("iso_639_1") == "en" and (
                                                (_tr.get("data") or {})
                                                .get("overview", "")
                                                .strip()
                                            ):
                                                _t_overview = _tr["data"]["overview"]
                                                break
                                    if _t_overview.strip():
                                        _plot = _t_overview.strip()
                                # Stars gap-fill
                                if not _gql_stars:
                                    _t_credits = _td.get("credits") or {}
                                    _t_cast = [
                                        c["name"]
                                        for c in (_t_credits.get("cast") or [])[:4]
                                    ]
                                    if _t_cast:
                                        _gql_stars = _t_cast
                                # Directors gap-fill
                                if not _gql_directors:
                                    _t_crew = (_td.get("credits") or {}).get(
                                        "crew"
                                    ) or []
                                    _t_dirs = [
                                        c["name"]
                                        for c in _t_crew
                                        if c.get("job") == "Director"
                                    ][:2]
                                    if _t_dirs:
                                        _gql_directors = _t_dirs
                                # Poster gap-fill from TMDB
                                if not _poster and _td.get("poster_path"):
                                    _poster = _TMDB_IMG_BASE + _td["poster_path"]
                                # Fix content type if TMDB says it's actually TV
                                if _tmdb_hit_is_tv and not _is_series:
                                    _is_series = True
                                    _kind = "series"
                except Exception:
                    pass

        # ── OMDB gap-fill as last resort ────────────────────────────────────
        if (not _plot.strip()) and _imdb_id and omdb_api_key:
            try:
                _omdb_data = _omdb_fetch_by_imdbid(_imdb_id, omdb_api_key)
                if _omdb_data:
                    if not _plot.strip() and _omdb_data.get("Plot", "N/A") not in (
                        "N/A",
                        "",
                    ):
                        _plot = _omdb_data["Plot"]
                    if not _gql_stars and _omdb_data.get("Actors", "N/A") not in (
                        "N/A",
                        "",
                    ):
                        _gql_stars = [
                            a.strip() for a in _omdb_data["Actors"].split(",")
                        ][:4]
                    if not _gql_directors and _omdb_data.get("Director", "N/A") not in (
                        "N/A",
                        "",
                    ):
                        _gql_directors = [
                            d.strip() for d in _omdb_data["Director"].split(",")
                        ][:2]
            except Exception:
                pass

        _year_disp = f"{_year}–{_year_end}" if _year_end else _year
        _genres = _gql.get("genres") or []
        _runtime = _gql.get("runtime") or ""
        _countries = _gql.get("countries") or []

        result = _empty_result()
        result.update(
            {
                "poster": _poster,
                "year": _year_disp,
                "rating": _rating_str,
                "type": "Series" if _is_series else "Movie",
                "plot": _plot,
                "source": "imdbgql",
                "stars": ", ".join(_gql_stars) if _gql_stars else "",
                "director": (", ".join(_gql_directors) if _gql_directors else ""),
                "imdb_id": _imdb_id,
                "genre": ", ".join(_genres) if _genres else "",
                "runtime": _runtime,
                "country": ", ".join(_countries) if _countries else "",
            }
        )
        return result, _kind

    except Exception:
        return None, None


# ──────────────────────────────────────────────────────────────────────────────
# TVMaze search / gap-fill
# ──────────────────────────────────────────────────────────────────────────────


def _score_show(query: str, show: dict, tvmaze_score: float) -> float:
    """Score a single TVMaze show against a query.  Higher = better match."""
    title = show.get("name", "")
    sim = _title_similarity(query, title)
    if sim < 0.20:
        return -1.0

    precision = _title_precision(query, title)
    weight = float(show.get("weight", 0))
    norm_weight = min(weight / 100.0, 1.0)
    norm_score = min(tvmaze_score / 30.0, 1.0)
    lang_bonus = 0.05 if (show.get("language") or "").lower() == "english" else 0.0
    rating_val = show.get("rating", {}).get("average") or 0
    rated_bonus = 0.05 if rating_val else 0.0

    tq_core = _tokenize_title(query) - _ARTICLES
    tt_core = _tokenize_title(title) - _ARTICLES
    exact_bonus = 0.60 if tq_core and tq_core == tt_core else 0.0

    return (
        exact_bonus
        + 0.30 * precision
        + 0.20 * norm_weight
        + 0.10 * sim
        + 0.05 * norm_score
        + lang_bonus
        + rated_bonus
    )


def _tvmaze_fetch_results(query: str, headers: Dict[str, str]) -> List[dict]:
    """Fetch TVMaze search results for a query.  Returns raw list or ``[]``."""
    try:
        r = requests.get(
            _TVMAZE_SEARCH_URL,
            params={"q": query},
            headers=headers,
            timeout=8,
        )
        if r.status_code == 200:
            return r.json() or []
    except Exception:
        pass
    return []


def _tvmaze_search(
    cleaned_query: str,
    headers: Dict[str, str],
) -> Optional[Dict[str, str]]:
    """Search TVMaze and return a result dict, or ``None`` on failure.

    This is the "full fallback" mode — used when TMDB and IMDb GraphQL both
    fail.  It only returns TV series (TVMaze is TV-only).
    """
    try:
        all_candidates: List[Tuple[float, dict]] = []
        tv_query = _extract_title_for_tvmaze(cleaned_query)

        results = _tvmaze_fetch_results(tv_query, headers)
        for item in results:
            show = item.get("show", {})
            s = _score_show(tv_query, show, float(item.get("score", 0)))
            if s > 0:
                all_candidates.append((s, show))

        best_so_far = max((s for s, _ in all_candidates), default=0.0)
        if best_so_far < 0.50:
            for variant in _leet_variants(tv_query):
                variant_results = _tvmaze_fetch_results(variant, headers)
                for item in variant_results:
                    show = item.get("show", {})
                    s = _score_show(tv_query, show, float(item.get("score", 0)))
                    if s > 0:
                        all_candidates.append((s, show))

        if not all_candidates:
            return None

        seen: Dict[int, Tuple[float, dict]] = {}
        for s, show in all_candidates:
            sid = show.get("id")
            if sid not in seen or s > seen[sid][0]:
                seen[sid] = (s, show)

        ranked = sorted(seen.values(), key=lambda x: x[0], reverse=True)
        best_score, show = ranked[0]

        if best_score < 0.20:
            return None

        # Extract fields
        title = show.get("name", "")
        year_raw = (show.get("premiered") or "")[:4]
        year = year_raw if year_raw else ""
        _ended_raw = (show.get("ended") or "")[:4]
        year_end = _ended_raw if _ended_raw and _ended_raw != year else None
        rating = show.get("rating", {}).get("average") or ""
        rating_str = str(rating) if rating else ""
        runtime_s = (
            f"{show.get('averageRuntime', '')} min"
            if show.get("averageRuntime")
            else ""
        )
        network_obj = show.get("network") or show.get("webChannel") or {}
        network = network_obj.get("name", "")
        genres = show.get("genres") or []
        raw_summary = show.get("summary") or ""
        summary = re.sub(r"<[^>]+>", "", raw_summary).strip() if raw_summary else ""
        show_id = show.get("id")
        imdb_id = (show.get("externals") or {}).get("imdb") or ""

        # Fetch cast
        stars: List[str] = []
        try:
            if show_id:
                cast_r = requests.get(
                    f"https://api.tvmaze.com/shows/{show_id}/cast",
                    headers=headers,
                    timeout=5,
                )
                if cast_r.status_code == 200:
                    stars = [
                        c["person"]["name"]
                        for c in cast_r.json()[:4]
                        if c.get("person", {}).get("name")
                    ]
        except Exception:
            pass

        _year_disp = f"{year}–{year_end}" if year_end else year
        result = _empty_result()
        result.update(
            {
                "poster": (show.get("image") or {}).get("original", "")
                or (show.get("image") or {}).get("medium", ""),
                "year": _year_disp,
                "rating": rating_str,
                "type": "Series",
                "plot": summary,
                "source": "tvmaze",
                "stars": ", ".join(stars) if stars else "",
                "imdb_id": imdb_id,
                "genre": ", ".join(genres) if genres else "",
                "runtime": runtime_s,
                "network": network,
            }
        )
        return result

    except Exception:
        return None


def _tvmaze_gap_fill_dict(
    cleaned_query: str,
    headers: Dict[str, str],
    existing: Dict[str, str],
) -> Dict[str, str]:
    """Gap-fill an existing result dict using TVMaze data.

    Cross-validates title similarity ≥ 0.55 before filling opinionated fields
    (plot, rating, genre, stars) to avoid injecting data from a different show.
    Network and status are always filled if missing regardless of title match.
    """
    try:
        all_candidates: List[Tuple[float, dict]] = []
        tv_query = _extract_title_for_tvmaze(cleaned_query)

        results = _tvmaze_fetch_results(tv_query, headers)
        for item in results:
            show = item.get("show", {})
            s = _score_show(tv_query, show, float(item.get("score", 0)))
            if s > 0:
                all_candidates.append((s, show))

        best_so_far = max((s for s, _ in all_candidates), default=0.0)
        if best_so_far < 0.50:
            for variant in _leet_variants(tv_query):
                variant_results = _tvmaze_fetch_results(variant, headers)
                for item in variant_results:
                    show = item.get("show", {})
                    s = _score_show(tv_query, show, float(item.get("score", 0)))
                    if s > 0:
                        all_candidates.append((s, show))

        if not all_candidates:
            return existing

        seen: Dict[int, Tuple[float, dict]] = {}
        for s, show in all_candidates:
            sid = show.get("id")
            if sid not in seen or s > seen[sid][0]:
                seen[sid] = (s, show)

        ranked = sorted(seen.values(), key=lambda x: x[0], reverse=True)
        best_score, show = ranked[0]

        if best_score < 0.20:
            return existing

        # Always fill network if missing
        network_obj = show.get("network") or show.get("webChannel") or {}
        network = network_obj.get("name", "")
        if network and not existing.get("network"):
            existing["network"] = network

        # Cross-validate for opinionated fields (series only)
        is_series = existing.get("type") == "Series"
        if is_series:
            _tvmaze_title = show.get("name", "")
            _title_match = (
                _title_similarity(cleaned_query, _tvmaze_title)
                if _tvmaze_title
                else 0.0
            )

            if _title_match >= 0.55 or best_score >= 0.60:
                show_id = show.get("id")

                # Plot gap-fill
                _raw_summary = show.get("summary") or ""
                _clean_summary = (
                    re.sub(r"<[^>]+>", "", _raw_summary).strip() if _raw_summary else ""
                )
                if _clean_summary and not existing.get("plot"):
                    existing["plot"] = _clean_summary

                # Rating gap-fill
                _tvmaze_rating = show.get("rating", {}).get("average")
                if _tvmaze_rating and not existing.get("rating"):
                    existing["rating"] = str(_tvmaze_rating)

                # Genre gap-fill
                _tvmaze_genres = show.get("genres") or []
                if _tvmaze_genres and not existing.get("genre"):
                    existing["genre"] = ", ".join(_tvmaze_genres)

                # Stars gap-fill
                if not existing.get("stars") and show_id:
                    try:
                        _cast_r = requests.get(
                            f"https://api.tvmaze.com/shows/{show_id}/cast",
                            headers=headers,
                            timeout=5,
                        )
                        if _cast_r.status_code == 200:
                            _cast_names = [
                                c["person"]["name"]
                                for c in _cast_r.json()[:4]
                                if c.get("person", {}).get("name")
                            ]
                            if _cast_names:
                                existing["stars"] = ", ".join(_cast_names)
                    except Exception:
                        pass

                # Poster gap-fill
                if not existing.get("poster"):
                    _img = show.get("image") or {}
                    existing["poster"] = _img.get("original", "") or _img.get(
                        "medium", ""
                    )

                # IMDb ID gap-fill
                if not existing.get("imdb_id"):
                    _imdb = (show.get("externals") or {}).get("imdb") or ""
                    if _imdb:
                        existing["imdb_id"] = _imdb

        return existing

    except Exception:
        return existing


# ──────────────────────────────────────────────────────────────────────────────
# OMDB fallback
# ──────────────────────────────────────────────────────────────────────────────


def _omdb_fetch_by_imdbid(
    imdb_id: str, omdb_api_key: str = _DEFAULT_OMDB_API_KEY
) -> Optional[Dict[str, Any]]:
    """Fetch full movie/series details from OMDB by IMDb ID.

    Returns the raw OMDB dict or ``None``.
    """
    try:
        _r = requests.get(
            _OMDB_URL,
            params={"apikey": omdb_api_key, "i": imdb_id, "plot": "full"},
            timeout=6,
        )
        _d = _r.json()
        if _d.get("Response") == "True":
            return _d
    except Exception:
        pass
    return None


def _omdb_gap_fill_dict(
    existing: Dict[str, str],
    omdb_api_key: str = _DEFAULT_OMDB_API_KEY,
) -> Dict[str, str]:
    """Gap-fill an existing result dict using OMDB data (by IMDb ID).

    Only fills fields that are currently empty.
    """
    imdb_id = existing.get("imdb_id", "")
    if not imdb_id:
        return existing
    try:
        omdb = _omdb_fetch_by_imdbid(imdb_id, omdb_api_key)
        if not omdb:
            return existing

        if not existing.get("plot") and omdb.get("Plot", "N/A") not in (
            "N/A",
            "",
        ):
            existing["plot"] = omdb["Plot"]

        if not existing.get("stars") and omdb.get("Actors", "N/A") not in (
            "N/A",
            "",
        ):
            _actors = [a.strip() for a in omdb["Actors"].split(",")][:4]
            existing["stars"] = ", ".join(_actors)

        if not existing.get("director") and omdb.get("Director", "N/A") not in (
            "N/A",
            "",
        ):
            existing["director"] = omdb["Director"]

        if not existing.get("rating") and omdb.get("imdbRating", "N/A") not in (
            "N/A",
            "",
        ):
            existing["rating"] = omdb["imdbRating"]

        if not existing.get("genre") and omdb.get("Genre", "N/A") not in (
            "N/A",
            "",
        ):
            existing["genre"] = omdb["Genre"]

        if not existing.get("runtime") and omdb.get("Runtime", "N/A") not in (
            "N/A",
            "",
        ):
            existing["runtime"] = omdb["Runtime"]

        if not existing.get("country") and omdb.get("Country", "N/A") not in (
            "N/A",
            "",
        ):
            existing["country"] = omdb["Country"]

        if not existing.get("year") and omdb.get("Year", "N/A") not in (
            "N/A",
            "",
        ):
            existing["year"] = omdb["Year"]

        # Type correction from OMDB
        if not existing.get("type") and omdb.get("Type", "") in (
            "movie",
            "series",
        ):
            existing["type"] = "Movie" if omdb["Type"] == "movie" else "Series"

        # Poster from OMDB
        if not existing.get("poster") and omdb.get("Poster", "N/A") not in (
            "N/A",
            "",
        ):
            existing["poster"] = omdb["Poster"]

    except Exception:
        pass
    return existing


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def search_imdb_for_album(
    album_name: str,
    tmdb_api_key: Optional[str] = None,
    omdb_api_key: str = _DEFAULT_OMDB_API_KEY,
) -> Dict[str, str]:
    """Search for a movie/series by name and return structured metadata.

    Uses TMDB as PRIMARY source, falls back to IMDb GraphQL, then TVMaze,
    then OMDB for gap-filling.

    Parameters
    ----------
    album_name :
        The title to search for (e.g. ``"Money Heist"``,
        ``"Qing Chuan Ru Meng"``).
    tmdb_api_key :
        TMDB v3 API key.  Defaults to the shared key.
    omdb_api_key :
        OMDB API key.  Defaults to ``"trilogy"``.

    Returns
    -------
    dict
        A dict with keys: ``poster``, ``year``, ``rating``, ``type``, ``plot``,
        ``source``, ``stars``, ``director``, ``imdb_id``, ``genre``,
        ``runtime``, ``country``, ``network``.  Empty strings for unfound
        fields.
    """
    _tmdb_key = tmdb_api_key or _DEFAULT_TMDB_API_KEY
    headers = {"User-Agent": "Mozilla/5.0"}

    # ── Pre-process ───────────────────────────────────────────────────────
    cleaned_query, user_year, has_se_code = _clean_query(album_name)
    if not cleaned_query:
        return _empty_result()

    # ── Route: TMDB primary → IMDb GraphQL fallback → TVMaze gap-fill ────
    result: Optional[Dict[str, str]] = None
    kind: Optional[str] = None

    result, kind = _tmdb_search_best(
        cleaned_query,
        headers,
        _tmdb_key,
        user_year=user_year,
        force_type="series" if has_se_code else None,
    )

    if not result:
        result, kind = _gql_search_imdb(
            cleaned_query,
            tmdb_api_key=_tmdb_key,
            omdb_api_key=omdb_api_key,
            year=user_year,
            force_type="series" if has_se_code else None,
        )

    # ── Movie / series disambiguation ────────────────────────────────────
    # If primary returned a movie but TVMaze finds a series whose title is
    # an exact (or near-exact) match, prefer the series.
    if result and kind == "movie" and not has_se_code:
        _tv_candidate = _tvmaze_search(cleaned_query, headers)
        if _tv_candidate:
            _tv_title = _tv_candidate.get("plot", "")  # not used
            _tv_name = ""
            # We need the actual title from TVMaze for comparison.
            # Re-fetch to get the title via TVMaze search.
            _tv_query = _extract_title_for_tvmaze(cleaned_query)
            _tv_results = _tvmaze_fetch_results(_tv_query, headers)
            if _tv_results:
                _tv_show = _tv_results[0].get("show", {})
                _tv_name = _tv_show.get("name", "")
            if _tv_name:
                _tv_sim = _title_similarity(cleaned_query, _tv_name)
                if _tv_sim >= 0.80:
                    result = _tv_candidate
                    kind = "series"

    # ── TVMaze gap-fill ──────────────────────────────────────────────────
    if result:
        result = _tvmaze_gap_fill_dict(cleaned_query, headers, result)
    else:
        # TMDB + IMDb GraphQL found nothing → pure TVMaze fallback
        result = _tvmaze_search(cleaned_query, headers)
        if not result:
            result = _empty_result()

    # ── Last-resort OMDB gap-fill ────────────────────────────────────────
    if result and (not result.get("plot") or not result.get("stars")):
        result = _omdb_gap_fill_dict(result, omdb_api_key)

    return result


def fill_imdb_gaps(
    album_name: str,
    existing_meta: Dict[str, str],
    tmdb_api_key: Optional[str] = None,
    omdb_api_key: str = _DEFAULT_OMDB_API_KEY,
) -> Dict[str, str]:
    """Fill missing fields in an existing metadata dict.

    Checks which fields are missing / empty / ``"N/A"`` among the core keys
    (``poster``, ``year``, ``rating``, ``type``, ``plot``), then calls
    :func:`search_imdb_for_album` and fills **only** the missing fields.

    Parameters
    ----------
    album_name :
        Title to search for.
    existing_meta :
        Current metadata dict (from ``_imdb_cache_mem``).
    tmdb_api_key :
        TMDB v3 API key.  Defaults to the shared key.
    omdb_api_key :
        OMDB API key.  Defaults to ``"trilogy"``.

    Returns
    -------
    dict
        The updated metadata dict with gaps filled.
    """
    _CORE_KEYS = ("poster", "year", "rating", "type", "plot")
    _BONUS_KEYS = (
        "stars",
        "director",
        "imdb_id",
        "genre",
        "runtime",
        "country",
        "network",
    )
    _ALL_KEYS = _CORE_KEYS + _BONUS_KEYS

    # Check if any field (core or bonus) is missing
    needs_fill = any(
        not existing_meta.get(k, "") or existing_meta.get(k, "") == "N/A"
        for k in _ALL_KEYS
    )

    if not needs_fill:
        return existing_meta

    # Fetch fresh data
    fresh = search_imdb_for_album(album_name, tmdb_api_key, omdb_api_key)

    # Fill only missing / empty / N/A fields
    for key in _ALL_KEYS:
        current = existing_meta.get(key, "")
        if not current or current == "N/A":
            new_val = fresh.get(key, "")
            if new_val and new_val != "N/A":
                existing_meta[key] = new_val

    return existing_meta
