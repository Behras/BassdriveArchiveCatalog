#!/usr/bin/env python3
"""Build the public Bassdrive archive index and suggest tags for new shows."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import difflib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


ARCHIVE_ROOT = "http://bassdrivearchive.com/"
RADIO_ROOT = "https://www.bassdrive.com/radio"
MINIMUM_PLAYABLE_BYTES = 1024 * 1024
MAX_WORKERS = 4
USER_AGENT = "BassdriveTerminal-CatalogSync/1.0"
TAG_RULES_VERSION = 2

DAY_FOLDERS = [
    ("Monday", "1%20-%20Monday/"),
    ("Tuesday", "2%20-%20Tuesday/"),
    ("Wednesday", "3%20-%20Wednesday/"),
    ("Thursday", "4%20-%20Thursday/"),
    ("Friday", "5%20-%20Friday/"),
    ("Saturday", "6%20-%20Saturday/"),
    ("Sunday", "7%20-%20Sunday/"),
]

TAG_KEYWORDS = {
    "LIQUID_SOULFUL": (
        "liquid", "liquid funk", "soulful", "jazzy", "jazz", "vocal dnb",
        "vocal drum", "melodic dnb", "melodic drum",
    ),
    "DEEP_ATMOSPHERIC": (
        "atmospheric", "ambient", "autonomic", "minimal", "deep dnb",
        "deep drum", "dubwise", "dub", "rollers", "rolling grooves",
    ),
    "JUNGLE_BREAKS": (
        "jungle", "ragga", "amen", "breakbeat", "breaks", "old school",
        "old-school", "classic jungle",
    ),
    "TECH_DARK": (
        "neuro", "techstep", "darkstep", "dark dnb", "dark drum",
        "industrial", "technical", "heavy dnb", "heavy drum",
    ),
    "DANCEFLOOR_JUMP_UP": (
        "dancefloor dnb", "dancefloor drum", "dancefloor forward",
        "dancefloor-forward", "jump up", "jump-up", "high energy dnb",
        "high energy drum",
    ),
    "FULL_SPECTRUM": (
        "full spectrum", "across the spectrum", "all styles", "all aspects",
        "the gamut", "eclectic", "no limits", "varies from",
    ),
}


class DirectoryListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._row: dict[str, str] | None = None
        self._cell_class = ""
        self._cell_text: list[str] = []
        self._link: dict[str, str] | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self._row = {}
        elif tag == "td" and self._row is not None:
            self._cell_class = attributes.get("class", "")
            self._cell_text = []
        elif tag == "a" and self._row is not None:
            self._link = {
                "href": attributes.get("href", ""),
                "title": attributes.get("title", ""),
            }
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._row is not None and self._cell_class:
            self._cell_text.append(data)
        if self._link is not None:
            self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None and self._row is not None:
            href = self._link["href"]
            if href and "href" not in self._row:
                self._row["href"] = href
                self._row["name"] = (
                    self._link["title"] or "".join(self._link_text)
                ).strip().rstrip("/")
            self._link = None
            self._link_text = []
        elif tag == "td" and self._row is not None:
            value = " ".join("".join(self._cell_text).split())
            if "size" in self._cell_class:
                self._row["size"] = value
            elif "date" in self._cell_class:
                self._row["date"] = value
            self._cell_class = ""
            self._cell_text = []
        elif tag == "tr" and self._row is not None:
            if self._row.get("href"):
                self.rows.append(self._row)
            self._row = None


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attributes = {key: value or "" for key, value in attrs}
            self._href = attributes.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


class ArticleSummaryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._summary_depth = 0
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "div" and "article-summary" in classes and self._summary_depth == 0:
            self._summary_depth = 1
        elif tag == "div" and self._summary_depth:
            self._summary_depth += 1
        if self._summary_depth and tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "div" and self._summary_depth:
            self._summary_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._summary_depth and not self._ignored_depth:
            value = " ".join(data.split())
            if value:
                self.parts.append(value)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def fetch_text(url: str, attempts: int = 2) -> str:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    raise ValueError(f"Response too large: {url}")
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def parse_listing(html: str) -> list[dict[str, str]]:
    parser = DirectoryListingParser()
    parser.feed(html)
    return parser.rows


def parse_size(value: str) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)", value.strip(), re.I)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
    }
    return int(amount * multipliers[unit])


def parse_listing_date(value: str) -> str | None:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%b-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def normalize_name(value: str) -> str:
    decoded = urllib.parse.unquote(value).strip().rstrip("/")
    ascii_value = unicodedata.normalize("NFKD", decoded).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def slugify(value: str) -> str:
    return normalize_name(value).replace(" ", "-") or "unknown-show"


def title_for_matching(value: str) -> str:
    title = value.split(" - ", 1)[0]
    normalized = normalize_name(title)
    stop_words = {"the", "show", "radio", "live", "bassdrive", "records", "recordings"}
    words = [word for word in normalized.split() if word not in stop_words]
    return " ".join(words) or normalized


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_if_changed(path: Path, payload: dict[str, Any], volatile_keys: set[str]) -> bool:
    previous = load_json(path, {})
    comparable_previous = {key: value for key, value in previous.items() if key not in volatile_keys}
    comparable_new = {key: value for key, value in payload.items() if key not in volatile_keys}
    if comparable_previous == comparable_new:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
    return True


def discover_sources() -> list[dict[str, Any]]:
    def fetch_day(day: str, suffix: str) -> list[dict[str, Any]]:
        day_url = urllib.parse.urljoin(ARCHIVE_ROOT, suffix)
        rows = parse_listing(fetch_text(day_url))
        sources: list[dict[str, Any]] = []
        for row in rows:
            href = row.get("href", "")
            if href.startswith("../") or href.startswith("?") or not href.endswith("/"):
                continue
            url = urllib.parse.urljoin(day_url, href)
            if not url.startswith(ARCHIVE_ROOT):
                continue
            sources.append(
                {
                    "name": row.get("name") or urllib.parse.unquote(href).rstrip("/"),
                    "day": day,
                    "url": url,
                    "folderModified": parse_listing_date(row.get("date", "")),
                }
            )
        return sources

    all_sources: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_day, day, suffix) for day, suffix in DAY_FOLDERS]
        for future in concurrent.futures.as_completed(futures):
            all_sources.extend(future.result())
    return sorted(all_sources, key=lambda source: (normalize_name(source["name"]), source["url"]))


def inspect_source(source: dict[str, Any]) -> dict[str, Any]:
    rows = parse_listing(fetch_text(source["url"]))
    playable: list[tuple[str, str | None]] = []
    broken = 0
    for row in rows:
        href = row.get("href", "")
        if not href.lower().split("?", 1)[0].endswith(".mp3"):
            continue
        size = parse_size(row.get("size", ""))
        if size is not None and size < MINIMUM_PLAYABLE_BYTES:
            broken += 1
            continue
        playable.append((row.get("name", href), parse_listing_date(row.get("date", ""))))

    dated = [(name, date) for name, date in playable if date]
    newest = max(dated, key=lambda item: item[1]) if dated else None
    return {
        **source,
        "playableEpisodes": len(playable),
        "brokenEpisodes": broken,
        "newestEpisode": newest[1] if newest else None,
        "newestEpisodeName": newest[0] if newest else None,
        "scanStatus": "OK",
    }


def build_index(
    sources: list[dict[str, Any]],
    previous: dict[str, Any],
    force_full_scan: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    previous_by_url = {
        source.get("url"): source
        for source in previous.get("sources", [])
        if isinstance(source, dict) and source.get("url")
    }
    resolved: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for source in sources:
        old = previous_by_url.get(source["url"])
        if (
            not force_full_scan
            and
            old
            and source.get("folderModified")
            and old.get("folderModified") == source.get("folderModified")
            and isinstance(old.get("playableEpisodes"), int)
        ):
            resolved.append({**old, "name": source["name"], "day": source["day"]})
        else:
            changed.append(source)

    if changed:
        print(f"Inspecting {len(changed)} new or changed show folders...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_sources = {executor.submit(inspect_source, source): source for source in changed}
        for position, future in enumerate(concurrent.futures.as_completed(future_sources), start=1):
            source = future_sources[future]
            try:
                resolved.append(future.result())
            except Exception as error:  # Keep a previous usable result when a single folder fails.
                old = previous_by_url.get(source["url"])
                if old:
                    resolved.append({**old, "scanStatus": "STALE"})
                else:
                    resolved.append(
                        {
                            **source,
                            "playableEpisodes": -1,
                            "brokenEpisodes": 0,
                            "newestEpisode": None,
                            "newestEpisodeName": None,
                            "scanStatus": "ERROR",
                        }
                    )
                print(f"WARNING: {source['name']}: {error}", file=sys.stderr, flush=True)
            if position % 10 == 0 or position == len(changed):
                print(f"Inspected {position}/{len(changed)} folders", flush=True)

    return sorted(resolved, key=lambda source: (normalize_name(source["name"]), source["url"])), len(changed)


def discover_profiles() -> list[dict[str, str]]:
    parser = LinkCollector()
    parser.feed(fetch_text(RADIO_ROOT))
    profiles: dict[str, dict[str, str]] = {}
    for href, text in parser.links:
        url = urllib.parse.urljoin(RADIO_ROOT, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc not in {"bassdrive.com", "www.bassdrive.com"}:
            continue
        path = parsed.path.rstrip("/")
        if not path.startswith("/radio/") or path == "/radio":
            continue
        encoded_path = urllib.parse.quote(urllib.parse.unquote(path), safe="/-._~&")
        url = urllib.parse.urlunparse(parsed._replace(path=encoded_path, query="", fragment=""))
        slug = path.rsplit("/", 1)[-1]
        profiles[url] = {
            "url": url,
            "name": text.strip() or urllib.parse.unquote(slug).replace("-", " ").title(),
            "matchName": title_for_matching(text or slug.replace("-", " ")),
        }
    return list(profiles.values())


def best_profile(show_name: str, profiles: list[dict[str, str]]) -> tuple[dict[str, str] | None, float]:
    candidate = title_for_matching(show_name)
    best: dict[str, str] | None = None
    best_score = 0.0
    for profile in profiles:
        profile_name = profile["matchName"]
        candidate_compact = candidate.replace(" ", "")
        profile_compact = profile_name.replace(" ", "")
        if candidate_compact and candidate_compact == profile_compact:
            score = 1.0
        else:
            candidate_words = set(candidate.split())
            profile_words = set(profile_name.split())
            minimum_words = min(len(candidate_words), len(profile_words))
            overlap = (
                len(candidate_words & profile_words) / minimum_words
                if minimum_words
                else 0.0
            )
            sequence = difflib.SequenceMatcher(None, candidate, profile_name).ratio()
            score = sequence * 0.55 + overlap * 0.45
        if score > best_score:
            best = profile
            best_score = score
    return best, best_score


def suggest_tags(text: str) -> list[str]:
    normalized = normalize_name(text)
    tags = [
        tag
        for tag, keywords in TAG_KEYWORDS.items()
        if any(normalize_name(keyword) in normalized for keyword in keywords)
    ]
    return tags or ["UNCLASSIFIED"]


def update_show_catalog(
    catalog: dict[str, Any],
    sources: list[dict[str, Any]],
    use_profiles: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    shows = [
        entry
        for entry in catalog.get("shows", [])
        if isinstance(entry, dict)
        and (
            entry.get("tagSource") == "CURATED"
            or int(entry.get("tagRulesVersion", 0)) >= TAG_RULES_VERSION
        )
    ]
    alias_map: dict[str, dict[str, Any]] = {}
    for entry in shows:
        for alias in entry.get("aliases", []):
            alias_map[normalize_name(str(alias))] = entry
        if entry.get("displayName"):
            alias_map.setdefault(normalize_name(str(entry["displayName"])), entry)

    unique_names = sorted({source["name"] for source in sources}, key=normalize_name)
    unknown_names = [name for name in unique_names if normalize_name(name) not in alias_map]
    if not unknown_names:
        return catalog, []

    print(f"Discovering tags for {len(unknown_names)} uncatalogued shows...", flush=True)
    profiles: list[dict[str, str]] = []
    if use_profiles:
        try:
            profiles = discover_profiles()
        except Exception as error:
            print(f"WARNING: Could not read Bassdrive show profiles: {error}", file=sys.stderr)

    additions: list[dict[str, Any]] = []

    def build_entry(name: str) -> dict[str, Any]:
        profile, score = best_profile(name, profiles)
        profile_url: str | None = None
        tags = suggest_tags(name)
        if profile is not None and score >= 0.78:
            profile_url = profile["url"]
            try:
                text_parser = ArticleSummaryParser()
                text_parser.feed(fetch_text(profile_url))
                tags = suggest_tags(f"{name} {text_parser.text}")
            except Exception as error:
                print(f"WARNING: Could not inspect {profile_url}: {error}", file=sys.stderr)
        return {
            "id": slugify(name),
            "displayName": profile["name"] if profile_url and profile else name,
            "aliases": [name],
            "tags": tags,
            "tagSource": "AUTO" if tags != ["UNCLASSIFIED"] else "UNCLASSIFIED",
            "tagRulesVersion": TAG_RULES_VERSION if use_profiles else 0,
            "profileUrl": profile_url,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        additions.extend(executor.map(build_entry, unknown_names))

    existing_by_profile = {
        str(entry.get("profileUrl")): entry
        for entry in shows
        if entry.get("profileUrl")
    }
    grouped_additions: list[dict[str, Any]] = []
    grouped_by_profile: dict[str, dict[str, Any]] = {}
    for entry in additions:
        profile_url = entry.get("profileUrl")
        existing = existing_by_profile.get(str(profile_url)) if profile_url else None
        if existing is not None:
            aliases = list(existing.get("aliases", []))
            for alias in entry["aliases"]:
                if alias not in aliases:
                    aliases.append(alias)
            existing["aliases"] = sorted(aliases, key=normalize_name)
            continue
        if profile_url and str(profile_url) in grouped_by_profile:
            grouped = grouped_by_profile[str(profile_url)]
            grouped["aliases"].extend(entry["aliases"])
            grouped["aliases"] = sorted(set(grouped["aliases"]), key=normalize_name)
            continue
        grouped_additions.append(entry)
        if profile_url:
            grouped_by_profile[str(profile_url)] = entry

    used_ids = {str(entry.get("id")) for entry in shows}
    for entry in grouped_additions:
        base_id = entry["id"]
        suffix = 2
        while entry["id"] in used_ids:
            entry["id"] = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(entry["id"])

    shows.extend(grouped_additions)
    shows.sort(key=lambda entry: normalize_name(str(entry.get("displayName", ""))))
    next_version = int(catalog.get("catalogVersion", 0)) + 1
    return {
        "schemaVersion": 1,
        "catalogVersion": next_version,
        "updatedAt": utc_now(),
        "shows": shows,
    }, grouped_additions


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog-dir", type=Path, default=Path("catalog/data"))
    parser.add_argument(
        "--reset-auto",
        action="store_true",
        help="Drop generated entries before rebuilding suggestions; keep CURATED entries.",
    )
    parser.add_argument(
        "--offline-seed",
        action="store_true",
        help="Rebuild generated entries from the existing index without network access.",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Inspect every show folder even when its modification time is unchanged.",
    )
    args = parser.parse_args()

    catalog_path = args.catalog_dir / "shows-v1.json"
    index_path = args.catalog_dir / "archive-index-v1.json"
    catalog = load_json(
        catalog_path,
        {"schemaVersion": 1, "catalogVersion": 0, "updatedAt": utc_now(), "shows": []},
    )
    if args.reset_auto:
        catalog = {
            **catalog,
            "shows": [
                entry
                for entry in catalog.get("shows", [])
                if isinstance(entry, dict) and entry.get("tagSource") == "CURATED"
            ],
        }
    previous_index = load_json(index_path, {"sources": []})

    if args.offline_seed:
        indexed_sources = [
            source for source in previous_index.get("sources", []) if isinstance(source, dict)
        ]
        inspected_count = 0
        print(f"Using {len(indexed_sources)} cached show folders", flush=True)
    else:
        print("Reading Bassdrive day folders...", flush=True)
        sources = discover_sources()
        print(f"Found {len(sources)} show folders", flush=True)
        force_full_scan = args.full_scan or dt.datetime.now(dt.timezone.utc).day == 1
        indexed_sources, inspected_count = build_index(
            sources,
            previous_index,
            force_full_scan=force_full_scan,
        )

    updated_catalog, additions = update_show_catalog(
        catalog,
        indexed_sources,
        use_profiles=not args.offline_seed,
    )

    index_payload = {
        "schemaVersion": 1,
        "indexVersion": int(previous_index.get("indexVersion", 0)) + 1,
        "updatedAt": utc_now(),
        "minimumPlayableBytes": MINIMUM_PLAYABLE_BYTES,
        "sources": indexed_sources,
    }

    catalog_changed = write_json_if_changed(catalog_path, updated_catalog, {"updatedAt"})
    index_changed = False if args.offline_seed else write_json_if_changed(
        index_path,
        index_payload,
        {"updatedAt", "indexVersion"},
    )

    print(
        f"Done: {len(indexed_sources)} sources, {inspected_count} inspected, "
        f"{len(additions)} new catalog entries",
        flush=True,
    )
    if catalog_changed:
        print("Show catalog changed", flush=True)
    if index_changed:
        print("Archive index changed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
