import os
import re
from pathlib import Path
import json
import time
import datetime
import logging
import requests
from bs4 import BeautifulSoup
from python_utils import converters

# ================== CONFIG ================== #

MAX_RUNTIME_SECONDS = 60 * 5
MAX_RESULTS_OFFSET = 2000

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "scrape_state.json"
RESULTS_FILE = ROOT / "results.json"
FAILED_URLS_FILE = ROOT / "failed_urls.json"

MAX_RETRIES = 3
RETRY_SLEEP_SECONDS = 2

FLARE_SOLVERR_URL = os.environ.get("FLARE_SOLVERR_URL", "http://localhost:8191/v1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

START_TIME = None

# ================== TIME GUARD ================== #

def time_exceeded():
    return (time.time() - START_TIME) >= MAX_RUNTIME_SECONDS

# ================== STATE ================== #

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "known_match_ids": {},
            "enriched_match_ids": {},
            "failed_match_ids": {}
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
        state.setdefault("known_match_ids", {})
        state.setdefault("enriched_match_ids", {})
        state.setdefault("failed_match_ids", {})
        return state

def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def save_state(state):
    write_json(STATE_FILE, state)


def deduplicate(matches):
    """Keep newest values, filling missing fields from older duplicate rows."""
    unique = {}
    for match in matches:
        mid = str(match["match-id"])
        if mid not in unique:
            unique[mid] = dict(match)
        else:
            for key, value in match.items():
                if unique[mid].get(key) in (None, "", []) and value not in (None, "", []):
                    unique[mid][key] = value
    return list(unique.values())

# ================== HTTP ================== #

def load_failed_urls():
    if not Path(FAILED_URLS_FILE).exists():
        return []
    text = Path(FAILED_URLS_FILE).read_text(encoding="utf-8")
    try:
        urls = json.loads(text)
    except json.JSONDecodeError:
        # Older versions wrote newline-separated URLs into this .json file.
        urls = [line.strip() for line in text.splitlines() if line.strip()]
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ValueError("failed_urls.json must contain a list of URLs")
    return list(dict.fromkeys(urls))


def get_parsed_page(url):
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}

    for attempt in range(1, MAX_RETRIES + 1):
        if START_TIME is not None and time_exceeded():
            break
        try:
            r = requests.post(FLARE_SOLVERR_URL, json=payload, timeout=70)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "ok":
                raise RuntimeError("FlareSolverr non-ok")
            if data["solution"].get("status", 200) != 200:
                raise RuntimeError(f"Upstream HTTP {data['solution']['status']}")
            return BeautifulSoup(data["solution"]["response"], "lxml")
        except Exception as e:
            logging.warning(f"{attempt}/{MAX_RETRIES} failed for {url}: {e}")
            time.sleep(RETRY_SLEEP_SECONDS)

    failures = load_failed_urls()
    if url not in failures:
        failures.append(url)
    write_json(FAILED_URLS_FILE, failures)
    return None

# ================== TEAM CACHE ================== #

TEAM_MAP = {}
TEAMS_LOADED = False

def load_teams():
    global TEAMS_LOADED
    if TEAMS_LOADED:
        return
    TEAMS_LOADED = True

    soup = get_parsed_page("https://www.hltv.org/stats/teams?minMapCount=0")
    if not soup:
        return

    for team in soup.find_all("td", class_="teamCol-teams-overview"):
        a = team.find("a")
        if not a:
            continue
        TEAM_MAP[a.text.strip()] = converters.to_int(a["href"].split("/")[-2])

def team_id(name):
    load_teams()
    return TEAM_MAP.get(name)

# ================== DATE ================== #

def parse_result_date(text):
    text = text.removeprefix("Results for ")
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text)
    return datetime.datetime.strptime(text, "%B %d %Y").date().isoformat()

# ================== SCRAPE RESULTS ================== #

def scrape_latest_results(state):
    results = []
    known_ids = set(state["known_match_ids"].keys())
    seen_ids = set()

    offset = 0
    while offset <= MAX_RESULTS_OFFSET and not time_exceeded():
        logging.info(f"Scraping offset {offset}")
        page = get_parsed_page(f"https://www.hltv.org/results?offset={offset}")
        if page is None:
            raise RuntimeError("Could not fetch results listing")
        if not page.select(".results-holder .result-con"):
            raise RuntimeError("No results found: blocked page or changed HTML")

        stop = False

        for section in page.find_all("div", class_="results-holder"):
            headline = section.find("span", class_="standard-headline")
            date_str = None
            if headline:
                date_str = parse_result_date(headline.get_text(strip=True))

            for res in section.find_all("div", class_="result-con"):
                a = res.find("a", class_="a-reset")
                if not a:
                    continue

                match_id = converters.to_int(a["href"].split("/")[-2])
                if str(match_id) in known_ids:
                    stop = True
                    continue
                if str(match_id) in seen_ids:
                    continue
                seen_ids.add(str(match_id))

                entry = {
                    "match-id": match_id,
                    "url": "https://hltv.org" + a["href"],
                    "date": date_str
                }

                event = res.find("td", class_="event") or res.find("td", class_="placeholder-text-cell")
                entry["event"] = event.text.strip() if event else None

                teams = res.find_all("td", class_="team-cell")
                if len(teams) == 2:
                    entry["team1"] = teams[0].text.strip()
                    entry["team2"] = teams[1].text.strip()
                    entry["team1-id"] = team_id(entry["team1"])
                    entry["team2-id"] = team_id(entry["team2"])

                score_td = res.find("td", class_="result-score")
                if score_td:
                    spans = score_td.find_all("span")
                    if len(spans) == 2:
                        entry["team1score"] = converters.to_int(spans[0].text)
                        entry["team2score"] = converters.to_int(spans[1].text)

                results.append(entry)
                state["known_match_ids"][str(match_id)] = True

        if stop:
            logging.info("Reached known match, stopping scrape")
            break

        offset += 100
        time.sleep(1)

    return results

# ================== MATCH DETAILS ================== #

def parse_match_details(soup):
    data = {"format": "", "stage": "", "veto": [], "maps": []}

    maps_section = soup.find("div", class_="col-6 col-7-small")
    if not maps_section:
        raise ValueError("Match details missing: blocked page or changed HTML")

    for box in maps_section.find_all("div", class_="standard-box veto-box"):
        text = box.get_text("\n", strip=True)
        lines = [l for l in text.split("\n") if l]
        is_veto = any(k in text.lower() for k in ["removed", "picked", "left over"])
        if lines and not is_veto:
            data["format"] = lines[0]
            if len(lines) > 1:
                data["stage"] = lines[1].lstrip("* ")

        if is_veto:
            data["veto"] = lines

    for holder in maps_section.find_all("div", class_="mapholder"):
        map_name = holder.find("div", class_="mapname")
        results = holder.find("div", class_="results")
        if not results:
            continue

        def parse_team(node):
            return {
                "name": node.find("div", class_="results-teamname").text.strip(),
                "score": node.find("div", class_="results-team-score").text.strip(),
                "status": "won" if "won" in node.get("class", []) else "lost"
            }

        half = results.find("div", class_="results-center-half-score")

        data["maps"].append({
            "map": map_name.text.strip() if map_name else "Unknown",
            "team1": parse_team(results.find("div", class_="results-left")),
            "team2": parse_team(results.find(class_="results-right")),
            "half_scores": half.text.strip() if half else "",
            "status": "played" if half else "not_played"
        })

    if not data["format"] and not data["maps"]:
        raise ValueError("Match page contains no recognizable details")
    return data

# ================== ENRICH ================== #

def enrich_matches(matches, state):
    for match in matches:
        if time_exceeded():
            break

        mid = str(match["match-id"])
        if state["enriched_match_ids"].get(mid):
            continue

        soup = get_parsed_page(match["url"])
        if not soup:
            state["failed_match_ids"][mid] = True
            continue

        try:
            match.update(parse_match_details(soup))
        except (ValueError, AttributeError) as error:
            logging.warning("Could not parse match %s: %s", mid, error)
            state["failed_match_ids"][mid] = True
            continue
        state["enriched_match_ids"][mid] = True
        state["failed_match_ids"].pop(mid, None)
        time.sleep(0.2)

# ================== MAIN ================== #

def main():
    global START_TIME
    START_TIME = time.time()

    state = load_state()
    write_json(FAILED_URLS_FILE, load_failed_urls())

    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    else:
        existing = []

    original_count = len(existing)
    existing = deduplicate(existing)
    logging.info("Removed %d duplicate rows", original_count - len(existing))
    # Persisted results are authoritative if an earlier run stopped between writes.
    state["known_match_ids"] = {str(m["match-id"]): True for m in existing}
    state["enriched_match_ids"] = {str(m["match-id"]): True for m in existing if "maps" in m}
    state["failed_match_ids"] = {
        mid: True for mid in state["failed_match_ids"]
        if mid in state["known_match_ids"] and mid not in state["enriched_match_ids"]
    }
    results = scrape_latest_results(state)
    all_results = deduplicate(results + existing)
    enrich_matches(all_results, state)

    write_json(RESULTS_FILE, all_results)

    save_state(state)
    logging.info("Done: %d new, %d total, %d failed details", len(results), len(all_results), len(state["failed_match_ids"]))
    pending = len(all_results) - len(state["enriched_match_ids"])
    summary = (
        f"New matches: {len(results)}\n\n"
        f"Total unique matches: {len(all_results)}\n\n"
        f"Duplicate rows removed: {original_count - len(existing)}\n\n"
        f"Details pending: {pending}\n\n"
        f"Failed details: {len(state['failed_match_ids'])}\n"
    )
    logging.info("Details pending: %d", pending)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(summary)
    if state["failed_match_ids"]:
        raise RuntimeError("Match detail failures remain; progress saved for retry")

if __name__ == "__main__":
    main()
