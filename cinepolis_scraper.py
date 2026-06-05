#!/usr/bin/env python3
"""Scraper simple para la cartelera publica de Cinepolis Guatemala."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "https://cinepolis.com.gt/"
API_URL = "https://cinepolis.com.gt/wp-json/mapi/v1/sites-data"
MIN_MATCH_SCORE = 70
TITLE_STOPWORDS = {
    "a",
    "al",
    "and",
    "de",
    "del",
    "el",
    "en",
    "la",
    "las",
    "los",
    "of",
    "the",
    "to",
    "un",
    "una",
    "y",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
CAMOFOX_DEFAULT_URL = "http://localhost:9377"


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html"})
    try:
        with urlopen(request, timeout=20) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"No se pudo descargar {url}: {exc}") from exc


def request_json(url: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=45) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="replace"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"No se pudo llamar {url}: {exc}") from exc


def extract_js_object(html: str, variable_name: str) -> dict[str, Any]:
    marker = f"var {variable_name} = "
    start = html.find(marker)
    if start == -1:
        raise RuntimeError(f"No encontre la variable JavaScript {variable_name}")

    index = html.find("{", start + len(marker))
    if index == -1:
        raise RuntimeError(f"No encontre el inicio JSON de {variable_name}")

    depth = 0
    in_string = False
    escaped = False
    for pos in range(index, len(html)):
        char = html[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[index : pos + 1])

    raise RuntimeError(f"No encontre el cierre JSON de {variable_name}")


def read_cache(cache_path: Path, cache_ttl_minutes: int, backend: str) -> dict[str, Any] | None:
    if cache_ttl_minutes <= 0 or not cache_path.exists():
        return None

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(cached["cached_at"])
    except (KeyError, ValueError, json.JSONDecodeError, OSError):
        return None
    if cached.get("backend") and cached.get("backend") != backend:
        return None

    if datetime.now(timezone.utc) - cached_at > timedelta(minutes=cache_ttl_minutes):
        return None

    return cached.get("data")


def write_cache(cache_path: Path, data: dict[str, Any], backend: str) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cached_at": datetime.now(timezone.utc).isoformat(), "backend": backend, "data": data}
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_raw_data_direct() -> dict[str, Any]:
    try:
        return json.loads(fetch_text(API_URL))
    except Exception:
        html = fetch_text(BASE_URL)
        plugin_data = extract_js_object(html, "rpReactPlugin")
        return plugin_data.get("initialData") or {}


def load_raw_data_camofox(camofox_url: str = CAMOFOX_DEFAULT_URL) -> dict[str, Any]:
    base = camofox_url.rstrip("/")
    user_id = "cinepolis-scraper"
    tab: dict[str, Any] | None = None

    expression = """
    (async () => {
      if (window.rpReactPlugin && window.rpReactPlugin.initialData) {
        return window.rpReactPlugin.initialData;
      }

      const response = await fetch('/wp-json/mapi/v1/sites-data', {
        headers: {
          'Accept': 'application/json,text/plain,*/*',
          'Referer': 'https://cinepolis.com.gt/'
        }
      });
      if (!response.ok) {
        return { "__error": `Cinepolis returned HTTP ${response.status}` };
      }
      return await response.json();
    })()
    """

    try:
        tab = request_json(
            f"{base}/tabs",
            method="POST",
            body={"userId": user_id, "sessionKey": "cinepolis", "url": BASE_URL},
        )
        tab_id = tab.get("tabId")
        if not tab_id:
            raise RuntimeError(f"Camofox no devolvio tabId: {tab}")

        request_json(
            f"{base}/tabs/{tab_id}/wait",
            method="POST",
            body={"userId": user_id, "timeout": 3000},
        )
        evaluated = request_json(
            f"{base}/tabs/{tab_id}/evaluate",
            method="POST",
            body={"userId": user_id, "expression": expression},
        )
        data = evaluated.get("result")
        if isinstance(data, dict) and data.get("__error"):
            raise RuntimeError(data["__error"])
        if not isinstance(data, dict) or "movies" not in data:
            raise RuntimeError(f"Camofox no encontro datos de cartelera: {evaluated}")

        return data
    finally:
        try:
            request_json(f"{base}/sessions/{quote(user_id)}", method="DELETE")
        except RuntimeError:
            pass


def load_raw_data(
    cache_path: Path | None = None,
    cache_ttl_minutes: int = 0,
    backend: str = "direct",
    camofox_url: str = CAMOFOX_DEFAULT_URL,
) -> dict[str, Any]:
    if cache_path:
        cached = read_cache(cache_path, cache_ttl_minutes, backend)
        if cached:
            return cached

    if backend == "direct":
        data = load_raw_data_direct()
    elif backend == "camofox":
        data = load_raw_data_camofox(camofox_url)
    else:
        raise RuntimeError(f"Backend desconocido: {backend}")

    if cache_path:
        write_cache(cache_path, data, backend)

    return data


def compact_movie(movie: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "titulo": movie.get("webName"),
        "slug": movie.get("slug"),
        "codigo": movie.get("code"),
        "fecha_estreno": movie.get("releaseDate"),
        "duracion_min": movie.get("length"),
        "clasificacion": movie.get("ratingName"),
        "generos": movie.get("cats", []),
        "formatos": movie.get("attrsList", []),
        "doblada": movie.get("isDubbed") == "1",
        "subtitulada": movie.get("isSubtitled") == "1",
        "preventa": bool(movie.get("isPreSale")),
        "estreno": bool(movie.get("isPremiere")),
        "estado": status,
    }


def compact_site(site: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": site.get("id"),
        "nombre": site.get("name"),
        "slug": site.get("slug"),
        "ciudad": site.get("city"),
        "direccion": site.get("address"),
        "formatos": site.get("availAttrs", []),
    }


def normalize(raw_data: dict[str, Any]) -> dict[str, Any]:
    cinemas = [compact_site(site) for site in raw_data.get("sites", []) if site.get("id")]
    now_showing = [compact_movie(movie, "cartelera") for movie in raw_data.get("movies", [])]
    coming_soon = [compact_movie(movie, "proximamente") for movie in raw_data.get("comingSoonMovies", [])]

    return {
        "fuente": API_URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "resumen": {
            "cines": len(cinemas),
            "peliculas_cartelera": len(now_showing),
            "peliculas_proximamente": len(coming_soon),
        },
        "cines": cinemas,
        "peliculas_cartelera": now_showing,
        "peliculas_proximamente": coming_soon,
    }


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_accents.lower()).strip()


def title_words(value: str, *, keep_stopwords: bool = False) -> set[str]:
    words = set(normalize_text(value).split())
    if keep_stopwords:
        return words
    content_words = words - TITLE_STOPWORDS
    return content_words or words


def title_score(query: str, title: str) -> int:
    normalized_query = normalize_text(query)
    normalized_title = normalize_text(title)

    if not normalized_query or not normalized_title:
        return 0
    if normalized_query == normalized_title:
        return 100
    if normalized_query in normalized_title:
        return 90

    query_terms = title_words(query)
    title_terms = title_words(title)
    if not query_terms or not title_terms:
        return 0

    shared_word_score = int(len(query_terms & title_terms) / len(query_terms) * 80)
    whole_title_score = int(SequenceMatcher(None, normalized_query, normalized_title).ratio() * 85)

    fuzzy_word_scores = []
    for query_word in query_terms:
        best_word_score = max(
            SequenceMatcher(None, query_word, title_word).ratio()
            for title_word in title_terms
        )
        fuzzy_word_scores.append(best_word_score)
    fuzzy_word_score = int(sum(fuzzy_word_scores) / len(fuzzy_word_scores) * 88)

    return max(shared_word_score, whole_title_score, fuzzy_word_score)


def find_best_movie(query: str, movies: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored_movies = [
        (title_score(query, movie.get("titulo") or ""), movie)
        for movie in movies
    ]
    scored_movies = [(score, movie) for score, movie in scored_movies if score >= MIN_MATCH_SCORE]
    if not scored_movies:
        return None

    scored_movies.sort(key=lambda item: item[0], reverse=True)
    match = dict(scored_movies[0][1])
    match["match_score"] = scored_movies[0][0]
    return match


def check_movie_availability(query: str, data: dict[str, Any]) -> dict[str, Any]:
    now_showing = data.get("peliculas_cartelera", [])
    coming_soon = data.get("peliculas_proximamente", [])
    available_match = find_best_movie(query, now_showing)
    coming_soon_match = find_best_movie(query, coming_soon)

    if available_match:
        title = available_match.get("titulo") or query
        return {
            "query": query,
            "available": True,
            "notify": True,
            "message": f"sir, {title} is available",
            "match": available_match,
        }

    if coming_soon_match:
        title = coming_soon_match.get("titulo") or query
        return {
            "query": query,
            "available": False,
            "notify": False,
            "message": f"{title} is not in theaters yet",
            "match": coming_soon_match,
        }

    return {
        "query": query,
        "available": False,
        "notify": False,
        "message": f"{query} was not found in Cinepolis Guatemala",
        "match": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrapea datos publicos de Cinepolis Guatemala.")
    parser.add_argument("--output", "-o", type=Path, help="Ruta donde guardar el JSON normalizado.")
    parser.add_argument("--pretty", action="store_true", help="Imprime JSON indentado.")
    parser.add_argument("--watch", help="Titulo de pelicula que Alfred debe vigilar.")
    parser.add_argument(
        "--cache-file",
        type=Path,
        help="Archivo de cache opcional para cron, por ejemplo /tmp/cinepolis_cache.json.",
    )
    parser.add_argument(
        "--cache-ttl-minutes",
        type=int,
        default=0,
        help="Minutos de validez del cache. 0 desactiva cache.",
    )
    parser.add_argument(
        "--backend",
        choices=["direct", "camofox"],
        default="direct",
        help="Fuente de datos: HTTP directo o servidor Camofox local.",
    )
    parser.add_argument(
        "--camofox-url",
        default=CAMOFOX_DEFAULT_URL,
        help="URL del servidor Camofox cuando --backend camofox esta activo.",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Con --watch, retorna 0 si esta disponible y 1 si no esta disponible.",
    )
    parser.add_argument(
        "--message-only",
        action="store_true",
        help="Con --watch, imprime solo el mensaje para Telegram.",
    )
    parser.add_argument(
        "--notify-only",
        action="store_true",
        help="Con --watch, no imprime nada si la pelicula todavia no esta disponible.",
    )
    args = parser.parse_args()

    result = normalize(
        load_raw_data(
            args.cache_file,
            args.cache_ttl_minutes,
            backend=args.backend,
            camofox_url=args.camofox_url,
        )
    )
    if args.watch:
        result = check_movie_availability(args.watch, result)

    if args.watch and args.notify_only and not result["notify"]:
        payload = ""
    elif args.watch and args.message_only:
        payload = result["message"]
    else:
        payload = json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None)

    if args.output and payload:
        args.output.write_text(payload + "\n", encoding="utf-8")

    if payload:
        print(payload)

    if args.watch and args.exit_code:
        return 0 if result["available"] else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
