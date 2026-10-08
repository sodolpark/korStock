"""Cache public official API documentation; never downloads account data."""

import concurrent.futures
import urllib.request
from pathlib import Path

from read_research import TextParser

PAGES = {
    "00": "14",
    "04": "14",
    "0B": "14",
    "0D": "14",
    "0s": "14",
    "1h": "14",
    "au10001": "00",
    "au10002": "00",
    "kt10000": "13",
    "kt10001": "13",
    "kt10002": "13",
    "kt10003": "13",
    "ka10032": "05",
    "ka10173": "15",
    "ka10075": "08",
    "ka10076": "08",
    "kt00018": "08",
    "kt00001": "08",
    "ka10099": "01",
    "ka10100": "01",
    "ka10001": "01",
    "ka10054": "01",
}


def fetch(item):
    code, section = item
    url = f"https://openapi.kiwoom.com/guide/apiGuideContents/{section}/{code}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read().decode("utf-8")
        parser = TextParser()
        parser.feed(raw)
        target = Path("data/reference") / f"{code}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(url + "\n" + "".join(parser.parts), encoding="utf-8")
        return f"{code}: {target.stat().st_size} bytes"
    except Exception as exc:
        return f"{code}: {type(exc).__name__}"


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        print("\n".join(pool.map(fetch, PAGES.items())))
