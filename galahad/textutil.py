"""Text helpers shared by plugins: HTML extraction, SSRF-safe URL checks, local summarising."""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
from collections import Counter
from typing import Optional

STOP = set("""a an the and or but if then of to in on for with at by from as is are was were be been being it its this that these those
i you he she we they them his her our your their not no so than too very can could should would will just about into over after before
also more most other some such only own same up out off do does did has have had which who whom what when where why how all any both each
few many much""".split())


def check_url(url: str, allow_private: bool = False) -> str:
    """Return a normalised http(s) URL or raise ValueError. Blocks local/private targets (SSRF)."""
    url = (url or "").strip()
    if url.startswith("www."):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("That isn't a web address I can open (need http:// or https://).")
    if allow_private:
        return url
    host = p.hostname
    try:
        infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"I can't resolve '{host}'.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("I won't fetch addresses on your own network or machine.")
    return url


def html_to_text(html: str, base_url: str = "") -> tuple[str, str, list[dict]]:
    """(title, readable text, links)"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header", "aside"]):
        t.decompose()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "td", "blockquote"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(txt) > (3 if el.name.startswith("h") else 25):
            blocks.append(txt)
    text = "\n".join(blocks) if blocks else re.sub(r"\s+", " ", root.get_text(" ", strip=True))
    links = []
    for a in root.find_all("a", href=True)[:60]:
        href = urllib.parse.urljoin(base_url, a["href"])
        if href.startswith("http"):
            links.append({"text": a.get_text(" ", strip=True)[:80], "url": href})
    return title, text, links


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])", re.sub(r"\s+", " ", text))
    return [p.strip() for p in parts if 30 <= len(p.strip()) <= 400]


def summarise_extractive(text: str, n: int = 4) -> str:
    """Frequency-ranked sentences in original order. Used when no AI provider is available."""
    sents = sentences(text)
    if len(sents) <= n:
        return " ".join(sents) or text[:400]
    words = [w for w in re.findall(r"[a-z']{3,}", text.lower()) if w not in STOP]
    freq = Counter(words)
    top = max(freq.values()) if freq else 1
    scored = []
    for i, s in enumerate(sents):
        ws = [w for w in re.findall(r"[a-z']{3,}", s.lower()) if w not in STOP]
        if not ws:
            continue
        score = sum(freq[w] / top for w in ws) / (len(ws) ** 0.5) * (1.15 if i < 3 else 1.0)
        scored.append((score, i, s))
    best = sorted(scored, reverse=True)[:n]
    return " ".join(s for _sc, _i, s in sorted(best, key=lambda x: x[1]))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def keywords(text: str, n: int = 8) -> list[str]:
    words = [w for w in re.findall(r"[a-z']{4,}", text.lower()) if w not in STOP]
    return [w for w, _ in Counter(words).most_common(n)]


def similar(a: str, b: str) -> float:
    """Jaccard similarity of significant words — used to drop duplicate headlines."""
    sa = {w for w in re.findall(r"[a-z0-9']{3,}", a.lower()) if w not in STOP}
    sb = {w for w in re.findall(r"[a-z0-9']{3,}", b.lower()) if w not in STOP}
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0
