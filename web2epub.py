#!/usr/bin/env python3
"""
web2epub.py — Crawl a website and compile it into a clean EPUB.

Usage:
    python web2epub.py <URL> [options]

Options:
    -o, --output    Output .epub filename (default: auto-generated)
    -t, --title     Override book title
    -a, --author    Set author name
    --max-pages     Max pages to crawl (default: 200)
    --delay         Delay between requests in seconds (default: 0.5)
    --no-images     Skip images
    --debug         Verbose logging
"""

import argparse
import base64
import hashlib
import logging
import mimetypes
import re
import time
import urllib.parse
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment
from ebooklib import epub

# ─── Config ────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

NOISE_SELECTORS = [
    "nav", "header", "footer", "aside",
    ".sidebar", ".nav", ".navbar", ".navigation", ".menu",
    ".breadcrumb", ".breadcrumbs", "#sidebar", "#nav", "#menu",
    ".toc", "#toc", ".site-header", ".site-footer",
    ".cookie-banner", ".ads", ".advertisement", ".social-share",
    "script", "style", "noscript", "iframe",
]

# Tags that are safe to keep in epub content
ALLOWED_TAGS = {
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "strong", "b", "em", "i", "u", "s", "code", "pre", "kbd", "samp",
    "blockquote", "q", "cite",
    "a", "img",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "figure", "figcaption",
    "hr", "div", "span", "section", "article", "main",
    "sup", "sub",
}

ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "*": ["id", "class"],
}


# ─── Helpers ───────────────────────────────────────────────────────────────────

def normalize_url(url: str, base: str) -> str | None:
    """Resolve relative URLs; return None for non-http or external."""
    url = url.strip()
    if url.startswith(("mailto:", "javascript:", "#", "data:")):
        return None
    resolved = urllib.parse.urljoin(base, url)
    # Strip fragment
    resolved = resolved.split("#")[0].rstrip("/")
    return resolved


def base_with_slash(url: str) -> str:
    """Ensure URL ends with / so urljoin treats it as a directory base."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return urllib.parse.urlunparse(parsed._replace(path=path))


def same_origin(url: str, base: str) -> bool:
    a = urllib.parse.urlparse(url)
    b = urllib.parse.urlparse(base)
    return a.scheme == b.scheme and a.netloc == b.netloc


def same_scope(url: str, root_url: str) -> bool:
    """URL must be same origin AND under the root path prefix."""
    a = urllib.parse.urlparse(url)
    b = urllib.parse.urlparse(root_url)
    if a.scheme != b.scheme or a.netloc != b.netloc:
        return False
    root_path = b.path.rstrip("/") + "/"
    return a.path.startswith(root_path) or a.path.rstrip("/") == b.path.rstrip("/")


def url_to_chapter_id(url: str, index: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", urllib.parse.urlparse(url).path.strip("/"))
    slug = slug[:40] or f"page_{index}"
    return f"ch_{index:04d}_{slug}"


def fetch(session: requests.Session, url: str, timeout=15) -> requests.Response | None:
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        logging.warning(f"Fetch failed: {url} — {e}")
        return None


# ─── Nav detection ─────────────────────────────────────────────────────────────

def extract_nav_links(soup: BeautifulSoup, base_url: str, root_url: str) -> list[str]:
    """
    Try to find an ordered navigation list (sidebar, toc, next-links).
    Returns ordered list of absolute URLs.
    """
    candidates = []

    # 1. Explicit <nav> elements
    for nav in soup.find_all("nav"):
        links = [a.get("href") for a in nav.find_all("a", href=True)]
        if len(links) >= 3:
            candidates.append(("nav_tag", links))

    # 2. Elements with nav-like IDs/classes
    nav_patterns = re.compile(
        r"(nav|sidebar|toc|menu|contents?|chapters?|index)", re.I
    )
    for tag in soup.find_all(["ul", "ol", "div"], id=nav_patterns):
        links = [a.get("href") for a in tag.find_all("a", href=True)]
        if len(links) >= 3:
            candidates.append((f"id={tag.get('id')}", links))

    for tag in soup.find_all(["ul", "ol", "div"], class_=True):
        cls = " ".join(tag.get("class", []))
        if nav_patterns.search(cls):
            links = [a.get("href") for a in tag.find_all("a", href=True)]
            if len(links) >= 3:
                candidates.append((f"class={cls}", links))

    # 3. Pick the candidate with the most same-origin links
    best_raw = []
    best_count = 0
    for name, raw_links in candidates:
        resolved = [normalize_url(l, base_url) for l in raw_links]
        resolved = [u for u in resolved if u and same_scope(u, root_url)]
        if len(resolved) > best_count:
            best_count = len(resolved)
            best_raw = resolved
            logging.debug(f"Nav candidate '{name}': {len(resolved)} links")

    # Deduplicate preserving order
    seen = set()
    ordered = []
    for u in best_raw:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return ordered


def discover_via_next_links(
    session: requests.Session,
    start_url: str,
    root_url: str,
    max_pages: int,
    delay: float,
) -> list[str]:
    """Follow 'next' links sequentially."""
    pages = [start_url]
    visited = {start_url}
    current = start_url
    next_patterns = re.compile(r"\bnext\b", re.I)

    for _ in range(max_pages):
        r = fetch(session, current)
        if not r:
            break
        soup = BeautifulSoup(r.text, "lxml")
        found = None
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            rel = a.get("rel", [])
            if "next" in rel or next_patterns.search(text):
                resolved = normalize_url(a["href"], current)
                if resolved and same_scope(resolved, root_url) and resolved not in visited:
                    found = resolved
                    break
        if not found:
            break
        visited.add(found)
        pages.append(found)
        current = found
        time.sleep(delay)

    return pages


# ─── Content extraction ────────────────────────────────────────────────────────

def remove_noise(soup: BeautifulSoup) -> None:
    """Remove nav/header/footer/scripts in-place."""
    # Remove HTML comments
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()


def find_main_content(soup: BeautifulSoup) -> BeautifulSoup:
    """Heuristically find the main content block."""
    for sel in ["main", "article", "[role='main']", "#content", "#main",
                ".content", ".main-content", ".post-content", ".entry-content",
                ".document", ".rst-content", ".book-content", ".page-content"]:
        el = soup.select_one(sel)
        if el:
            return el

    # Fallback: largest <div> by text length
    divs = soup.find_all("div")
    if divs:
        return max(divs, key=lambda d: len(d.get_text()))

    return soup.find("body") or soup


def clean_html_for_epub(
    content_tag,
    page_url: str,
    image_cache: dict,
    session: requests.Session,
    include_images: bool,
) -> str:
    """
    Clean content for epub:
    - Rewrite image src to epub item references
    - Remove disallowed tags (unwrap them, keep text)
    - Strip disallowed attributes
    """
    soup = BeautifulSoup(str(content_tag), "lxml")

    # Process images
    for img in soup.find_all("img"):
        if not include_images:
            img.decompose()
            continue
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        abs_src = urllib.parse.urljoin(page_url, src)
        epub_ref = download_image(abs_src, image_cache, session)
        if epub_ref:
            img["src"] = epub_ref
        else:
            img.decompose()

    # Rewrite internal <a> hrefs to strip them (epub cross-linking is complex)
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if href.startswith("#"):
            pass  # keep anchors
        else:
            del a["href"]  # strip external/internal links to avoid broken refs

    # Strip disallowed tags (unwrap = keep children)
    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()

    # Strip disallowed attributes
    for tag in soup.find_all(True):
        allowed = set(ALLOWED_ATTRS.get(tag.name, []) + ALLOWED_ATTRS.get("*", []))
        for attr in list(tag.attrs.keys()):
            if attr not in allowed:
                del tag[attr]

    # Get inner HTML of body
    body = soup.find("body")
    if body:
        return body.decode_contents()
    return str(soup)


# ─── Image handling ────────────────────────────────────────────────────────────

def download_image(
    url: str,
    cache: dict,
    session: requests.Session,
) -> str | None:
    """
    Download image, store in cache keyed by url.
    Returns the epub item filename reference, or None on failure.
    """
    if url in cache:
        return cache[url]["epub_filename"]

    r = fetch(session, url)
    if not r:
        return None

    ct = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    ext = mimetypes.guess_extension(ct) or ".jpg"
    ext = ext.replace(".jpe", ".jpg")

    # Deduplicate by content hash
    h = hashlib.md5(r.content).hexdigest()[:12]
    filename = f"images/{h}{ext}"

    cache[url] = {
        "epub_filename": filename,
        "content": r.content,
        "media_type": ct,
    }
    logging.debug(f"Downloaded image: {url} → {filename}")
    return filename


# ─── EPUB assembly ─────────────────────────────────────────────────────────────

CSS = """
body {
    font-family: Georgia, 'Times New Roman', serif;
    line-height: 1.7;
    margin: 1em 2em;
    color: #1a1a1a;
}
h1, h2, h3, h4, h5, h6 {
    font-family: 'Helvetica Neue', Arial, sans-serif;
    font-weight: bold;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
    line-height: 1.3;
}
h1 { font-size: 1.8em; border-bottom: 2px solid #ccc; padding-bottom: 0.3em; }
h2 { font-size: 1.4em; }
h3 { font-size: 1.2em; }
pre, code {
    font-family: 'Courier New', Courier, monospace;
    font-size: 0.88em;
    background: #f4f4f4;
    border-radius: 3px;
}
pre {
    padding: 1em;
    overflow-x: auto;
    border-left: 3px solid #aaa;
    margin: 1em 0;
    white-space: pre-wrap;
    word-break: break-word;
}
code { padding: 0.1em 0.3em; }
blockquote {
    border-left: 4px solid #ccc;
    margin: 1em 0;
    padding: 0.5em 1em;
    color: #555;
    font-style: italic;
}
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1em auto;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 1em 0;
    font-size: 0.9em;
}
th, td {
    border: 1px solid #ccc;
    padding: 0.5em 0.8em;
    text-align: left;
}
th { background: #f0f0f0; font-weight: bold; }
a { color: #2563eb; text-decoration: none; }
hr { border: none; border-top: 1px solid #ddd; margin: 2em 0; }
"""


def build_epub(
    pages: list[dict],
    image_cache: dict,
    title: str,
    author: str,
    output_path: str,
) -> None:
    book = epub.EpubBook()
    book.set_identifier(hashlib.md5(title.encode()).hexdigest())
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)

    # CSS
    css_item = epub.EpubItem(
        uid="style",
        file_name="styles/main.css",
        media_type="text/css",
        content=CSS.encode(),
    )
    book.add_item(css_item)

    # Images
    for img_data in image_cache.values():
        img_item = epub.EpubItem(
            file_name=img_data["epub_filename"],
            media_type=img_data["media_type"],
            content=img_data["content"],
        )
        book.add_item(img_item)

    # Chapters
    epub_chapters = []
    for page in pages:
        chapter = epub.EpubHtml(
            title=page["title"],
            file_name=f"{page['id']}.xhtml",
            lang="en",
        )
        body_html = page['html'] if page['html'].strip() else "<p>(empty page)</p>"
        chapter.content = (
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            "<head>"
            f"<title>{page['title']}</title>"
            '<link rel="stylesheet" href="../styles/main.css" type="text/css"/>'
            "</head>"
            f"<body>{body_html}</body>"
            "</html>"
        ).encode("utf-8")
        chapter.add_item(css_item)
        book.add_item(chapter)
        epub_chapters.append(chapter)

    # TOC + spine
    book.toc = [epub.Link(ch.file_name, ch.title, ch.id) for ch in epub_chapters]
    book.spine = ["nav"] + epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(output_path, book)
    logging.info(f"EPUB written: {output_path}")


# ─── Orchestrator ──────────────────────────────────────────────────────────────

def crawl_and_build(
    start_url: str,
    output: str | None,
    title: str | None,
    author: str,
    max_pages: int,
    delay: float,
    include_images: bool,
) -> str:
    start_url = start_url.rstrip("/")
    root_url = start_url
    nav_base_url = base_with_slash(start_url)

    session = requests.Session()
    session.headers.update(HEADERS)

    logging.info(f"Fetching start page: {start_url}")
    r = fetch(session, start_url)
    if not r:
        raise RuntimeError(f"Cannot fetch start URL: {start_url}")

    soup = BeautifulSoup(r.text, "lxml")

    # Title
    if not title:
        og_title = soup.find("meta", property="og:title")
        title = (
            (og_title and og_title.get("content"))
            or (soup.title and soup.title.get_text(strip=True))
            or urllib.parse.urlparse(start_url).netloc
        )

    # Output filename
    if not output:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:40].strip("_")
        output = f"{slug}.epub"

    logging.info(f"Book title: {title}")

    # Discover page order
    nav_links = extract_nav_links(soup, nav_base_url, root_url)
    logging.info(f"Nav detection found {len(nav_links)} links")

    if len(nav_links) >= 2:
        # Ensure start_url is included
        if start_url not in nav_links:
            nav_links = [start_url] + nav_links
        ordered_urls = nav_links[:max_pages]
        strategy = "nav"
    else:
        logging.info("No nav found, trying next-link traversal...")
        ordered_urls = discover_via_next_links(session, start_url, root_url, max_pages, delay)
        strategy = "next-links"

        if len(ordered_urls) <= 1:
            logging.info("Falling back to BFS crawl...")
            strategy = "bfs"
            ordered_urls = bfs_crawl(session, start_url, root_url, max_pages, delay, soup)

    logging.info(f"Strategy: {strategy} | Pages to process: {len(ordered_urls)}")

    # Process each page
    image_cache: dict = {}
    pages: list[dict] = []
    processed_urls: set = set()

    for i, url in enumerate(ordered_urls):
        if url in processed_urls:
            continue
        processed_urls.add(url)

        logging.info(f"[{i+1}/{len(ordered_urls)}] {url}")

        if url == start_url and r:
            page_soup = soup
        else:
            time.sleep(delay)
            pr = fetch(session, url)
            if not pr:
                continue
            page_soup = BeautifulSoup(pr.text, "lxml")

        # Page title
        page_title = (
            (page_soup.title and page_soup.title.get_text(strip=True))
            or f"Page {i+1}"
        )
        # Clean up title — remove site name suffix
        if " — " in page_title:
            page_title = page_title.split(" — ")[0].strip()
        elif " | " in page_title:
            page_title = page_title.split(" | ")[0].strip()
        elif " - " in page_title:
            page_title = page_title.split(" - ")[0].strip()

        remove_noise(page_soup)
        content = find_main_content(page_soup)
        html = clean_html_for_epub(content, url, image_cache, session, include_images)

        if len(html.strip()) < 100:
            logging.warning(f"Skipping near-empty page: {url}")
            continue

        pages.append({
            "id": url_to_chapter_id(url, i),
            "title": page_title,
            "html": html,
            "url": url,
        })

    if not pages:
        raise RuntimeError("No pages extracted.")

    logging.info(f"Building EPUB: {len(pages)} chapters, {len(image_cache)} images")
    build_epub(pages, image_cache, title, author, output)

    return output


def bfs_crawl(
    session: requests.Session,
    start_url: str,
    root_url: str,
    max_pages: int,
    delay: float,
    start_soup: BeautifulSoup,
) -> list[str]:
    """Simple BFS crawl staying on same origin."""
    visited = OrderedDict()
    queue = [start_url]
    visited[start_url] = True

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url == start_url:
            page_soup = start_soup
        else:
            time.sleep(delay)
            r = fetch(session, url)
            if not r:
                continue
            page_soup = BeautifulSoup(r.text, "lxml")

        for a in page_soup.find_all("a", href=True):
            resolved = normalize_url(a["href"], url)
            if resolved and same_scope(resolved, root_url) and resolved not in visited:
                visited[resolved] = True
                queue.append(resolved)

    return list(visited.keys())


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Crawl a website and compile it into an EPUB."
    )
    parser.add_argument("url", help="Starting URL")
    parser.add_argument("-o", "--output", default=None, help="Output .epub filename")
    parser.add_argument("-t", "--title", default=None, help="Book title")
    parser.add_argument("-a", "--author", default="web2epub", help="Author name")
    parser.add_argument("--max-pages", type=int, default=200, help="Max pages (default: 200)")
    parser.add_argument("--delay", type=float, default=0.5, help="Request delay in seconds")
    parser.add_argument("--no-images", action="store_true", help="Skip images")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    output = crawl_and_build(
        start_url=args.url,
        output=args.output,
        title=args.title,
        author=args.author,
        max_pages=args.max_pages,
        delay=args.delay,
        include_images=not args.no_images,
    )
    print(f"\n✓ Done! EPUB saved to: {output}")


if __name__ == "__main__":
    main()
