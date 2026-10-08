"""Extract text and regression fixtures from the supplied research HTML."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip = True
        if tag in {"h2", "p", "tr", "pre", "li"}:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


if __name__ == "__main__":
    source = Path("0_Research/Korean_Stock_Strategy_Reconstruction.html").read_text(encoding="utf-8")
    parser = TextParser()
    parser.feed(source)
    out = Path("data/reference")
    out.mkdir(parents=True, exist_ok=True)
    (out / "research.txt").write_text("".join(parser.parts), encoding="utf-8")
    section = source.split('id="s23"')[1].split('id="s24"')[0]
    rows = []
    for row in re.findall(r"<tr>(.*?)</tr>", section, re.S):
        cells = re.findall(r"<td>(.*?)</td>", row, re.S)
        if len(cells) != 5:
            continue
        match = re.fullmatch(r"(\d\d:\d\d:\d\d) ([\d.]+)점 @([\d,]+)원", cells[0])
        if match:
            rows.append(
                {
                    "time": match[1],
                    "total": match[2],
                    "price": int(match[3].replace(",", "")),
                    "components": [v.strip() for v in cells[1].split("/")],
                }
            )
    fixtures = Path("tests/fixtures")
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "evidence_scores.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    trades = [
        {
            "entry_time": "09:00:28",
            "exit_time": "09:09:16",
            "buy": 142500,
            "sell": 145400,
            "net": 2566,
            "reason": "profit_trailing",
        },
        {
            "entry_time": "09:09:36",
            "exit_time": "09:44:18",
            "buy": 145100,
            "sell": 146500,
            "net": 1063,
            "reason": "trend_imbalance",
        },
        {
            "entry_time": "11:34:45",
            "exit_time": "15:20:00",
            "buy": 149300,
            "sell": 153500,
            "net": 3848,
            "reason": "session_exit",
        },
    ]
    (fixtures / "evidence_trades.json").write_text(json.dumps(trades, indent=2), encoding="utf-8")
    print(f"Extracted {len(rows)} score rows and {len(trades)} trades")
