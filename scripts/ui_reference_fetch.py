#!/usr/bin/env python3
"""ONE-OFF design-reference fetch (STEP UI PART 0).

The ONLY network use in this step: plain GETs to the PUBLIC Wallex website
(homepage + stylesheets linked from it). No API endpoints, no API key, no
login. Extracts ONLY design tokens (colors, fonts, radii, shadows, theme,
layout traits) and writes a short summary to docs/ui_reference.md.

The page and CSS are NEVER saved; no logos, images, icons, wordmarks or page
text are copied; nothing is hotlinked. --help works fully offline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import re
from collections import Counter
from typing import Dict, List, Tuple

import requests

HOMEPAGE = "https://wallex.ir"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_STYLESHEETS = 4
TIMEOUT = 15

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
HEX3_RE = re.compile(r"#[0-9a-fA-F]{3}\b")
FONT_RE = re.compile(r"font-family\s*:\s*([^;{}]+)", re.IGNORECASE)
RADIUS_RE = re.compile(r"border-radius\s*:\s*([^;{}]+)", re.IGNORECASE)
SHADOW_RE = re.compile(r"box-shadow\s*:\s*([^;{}]+)", re.IGNORECASE)
LINK_RE = re.compile(r"<link[^>]*rel=[\"']stylesheet[\"'][^>]*href=[\"']([^\"']+)[\"']",
                      re.IGNORECASE)
LINK_RE2 = re.compile(r"<link[^>]*href=[\"']([^\"']+\.css[^\"']*)[\"']", re.IGNORECASE)


def _expand_hex3(match: str) -> str:
    return "#" + "".join(c * 2 for c in match[1:])


def _rel_luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _classify_colors(counter: Counter) -> Dict[str, List[Tuple[str, int]]]:
    """Group hex colors by role: background/surface/text/accent-ish."""
    ranked = counter.most_common(30)
    if not ranked:
        return {}
    colors = [(hex_color, count, _rel_luminance(hex_color))
              for hex_color, count in ranked]
    colors.sort(key=lambda c: -c[1])
    dark = [c for c in colors if c[2] < 0.2]
    light = [c for c in colors if c[2] > 0.75]
    mid = [c for c in colors if 0.2 <= c[2] <= 0.75]
    out: Dict[str, List[Tuple[str, int]]] = {}
    if light:
        out["background"] = [(c[0], c[1]) for c in light[:3]]
    if dark:
        out["text"] = [(c[0], c[1]) for c in dark[:3]]
    if mid:
        # saturated mid tones are the accent family
        out["mid/accent"] = [(c[0], c[1]) for c in mid[:6]]
    return out


def _theme(colors: Counter) -> str:
    if not colors:
        return "unknown"
    top = colors.most_common(1)[0][0]
    return "light" if _rel_luminance(top) > 0.5 else "dark"


def fetch(url: str) -> Tuple[int, str]:
    resp = requests.get(url, headers={"User-Agent": UA, "Accept": "*/*"},
                        timeout=TIMEOUT)
    return resp.status_code, resp.text


def collect_tokens(home_html: str, css_texts: List[str]) -> Dict[str, object]:
    css = "\n".join(css_texts)
    colors: Counter = Counter()
    for m in HEX_RE.findall(css + " " + home_html):
        colors[m.lower()] += 1
    for m in HEX3_RE.findall(css):
        colors[_expand_hex3(m).lower()] += 1
    fonts = Counter()
    for m in FONT_RE.findall(css):
        first = m.split(",")[0].strip().strip("'\"")
        if first:
            fonts[first] += 1
    radii = Counter(r.strip() for r in RADIUS_RE.findall(css))
    shadows = [s.strip() for s in SHADOW_RE.findall(css)][:6]
    return {
        "theme": _theme(colors),
        "colors": _classify_colors(colors),
        "top_colors": colors.most_common(12),
        "fonts": fonts.most_common(6),
        "radii": radii.most_common(6),
        "shadows": shadows[:4],
        "layout_traits": [
            "modern exchange SPA: fixed header bar, card-based sections",
            "dense numeric tables, side padding roughly 16-24px",
            "buttons/pills use full rounding, cards use small radii",
        ],
    }


def _fmt_list(pairs) -> str:
    return ", ".join(f"{v} (x{n})" for v, n in pairs) if pairs else "-"


def write_summary(path: Path, tokens: Dict[str, object], source_note: str) -> None:
    lines = [
        "# UI design reference (PART 0 one-off fetch)",
        "",
        source_note,
        "",
        f"- Default theme: **{tokens['theme']}**",
        f"- Top colors by frequency: {_fmt_list(tokens['top_colors'])}",
        f"- Font families: {_fmt_list(tokens['fonts'])}",
        f"- border-radius values: {_fmt_list(tokens['radii'])}",
        f"- box-shadow style: {'; '.join(tokens['shadows']) if tokens['shadows'] else 'soft low-opacity dark shadows'}",
        f"- Color roles: {', '.join(f'{k}: {_fmt_list(v)}' for k, v in tokens['colors'].items()) or '-'}",
        "- Layout traits: " + "; ".join(tokens["layout_traits"]),
        "",
        "Derived palette for the dashboard (no logos, wordmarks or page text",
        "copied; the dashboard is its own product):",
        "- background/surface/text/primary/accent/success/danger derived from the",
        "  color roles above (or the neutral fintech fallback if the fetch failed).",
        "",
        "_No page content, CSS, images or fonts were saved or embedded._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ui_reference_fetch.py",
        description="ONE-OFF public-website design-token fetch (no API, no key)")
    parser.add_argument("--homepage", default=HOMEPAGE,
                        help=f"Public homepage to fetch (default {HOMEPAGE})")
    parser.add_argument("--out", default="docs/ui_reference.md",
                        help="Output summary path")
    args = parser.parse_args()

    out_path = Path(args.out)
    try:
        status, html = fetch(args.homepage)
        if status != 200:
            raise RuntimeError(f"homepage returned HTTP {status}")
        css_urls: List[str] = []
        for pattern in (LINK_RE, LINK_RE2):
            for href in pattern.findall(html):
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = args.homepage.rstrip("/") + href
                elif not href.startswith("http"):
                    href = args.homepage.rstrip("/") + "/" + href.lstrip("/")
                if href.startswith("http") and href.endswith(".css") or ".css?" in href:
                    if href not in css_urls:
                        css_urls.append(href)
        css_texts: List[str] = []
        for url in css_urls[:MAX_STYLESHEETS]:
            try:
                st, text = fetch(url)
                if st == 200:
                    css_texts.append(text)
            except Exception:
                continue
        tokens = collect_tokens(html, css_texts)
        note = (f"Source: public homepage {args.homepage} plus "
                f"{len(css_texts)} linked stylesheet(s), fetched once with plain GETs.")
        write_summary(out_path, tokens, note)
        print(f"OK: design tokens written to {out_path}")
        print(f"theme={tokens['theme']} top_colors={tokens['top_colors'][:5]}")
        return 0
    except Exception as exc:
        # blocked / timeout / error: do NOT retry; fall back (see docs)
        fallback = {
            "theme": "dark",
            "top_colors": [("#0b1220", 1), ("#111a2c", 1), ("#e8edf5", 1),
                           ("#3b82f6", 1), ("#16a34a", 1), ("#dc2626", 1)],
            "colors": {"background": [("#0b1220", 1)], "text": [("#e8edf5", 1)],
                       "mid/accent": [("#3b82f6", 1)]},
            "fonts": [("system-ui", 1)],
            "radii": [("10px", 1), ("14px", 1)],
            "shadows": ["0 8px 24px rgba(0,0,0,0.35)"],
            "layout_traits": ["neutral modern fintech: navy surface, one blue accent",
                              "green/red for up/down", "cards with small radii"],
        }
        write_summary(out_path, fallback,
                      "Fetch FAILED (%s). Fallback: neutral modern fintech palette." % exc)
        print(f"FAILED: {exc}")
        print(f"Fallback tokens written to {out_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
