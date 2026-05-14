# web2epub

Turn any documentation website into a clean, readable EPUB — for your e-reader, offline use, or archiving.

```
python web2epub.py https://jwiegley.github.io/git-from-the-bottom-up -o git.epub
```

## Features

- **Smart nav detection** — reads sidebar navs, `<nav>` tags, and TOC lists to preserve chapter order
- **Fallback strategies** — if no nav found, follows `next →` links; falls back to BFS crawl
- **Image embedding** — downloads and bundles images directly into the EPUB file
- **Noise removal** — strips headers, footers, sidebars, ads, and scripts; keeps only the content
- **Scoped crawling** — stays within the URL subtree of the start page, never drifts to unrelated parts of the domain
- **Clean output** — readable typography, styled code blocks, proper EPUB3 structure with TOC

## Install

```bash
pip install requests beautifulsoup4 ebooklib lxml
```

Python 3.10+ required.

## Usage

```bash
python web2epub.py <URL> [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-o`, `--output` | auto-generated | Output `.epub` filename |
| `-t`, `--title` | from page `<title>` | Override book title |
| `-a`, `--author` | `web2epub` | Set author metadata |
| `--max-pages` | `200` | Max pages to crawl |
| `--delay` | `0.5` | Seconds between requests |
| `--no-images` | off | Skip image downloading |
| `--debug` | off | Verbose logging |

### Examples

```bash
# Basic
python web2epub.py https://jwiegley.github.io/git-from-the-bottom-up

# Custom output and metadata
python web2epub.py https://learnwebgl.brown37.net/ \
  -t "Learn WebGL" -a "C. Wayne Brown" \
  -o webgl.epub

# Fast, text-only, high page limit
python web2epub.py https://docs.example.com \
  --no-images --max-pages 500 --delay 0.2

# Debug nav detection
python web2epub.py https://some-site.com --debug
```

## How it works

### Page ordering

Getting chapter order right is the hard part. The script tries three strategies in sequence:

1. **Nav extraction** — scans for `<nav>` tags and elements with nav-like `id`/`class` attributes (`sidebar`, `toc`, `menu`, `contents`, etc.). Picks the candidate with the most in-scope links.
2. **Next-link traversal** — follows `rel="next"` or links with text matching "next", chaining pages sequentially.
3. **BFS crawl** — breadth-first crawl as a last resort, staying within the root URL's path subtree.

### Content extraction

Each page goes through:
- Noise removal (navbars, footers, ads, scripts, comments)
- Main content detection via semantic selectors (`main`, `article`, `[role=main]`, common CMS class names) with a text-length heuristic fallback
- HTML sanitization (allowlist of tags and attributes)
- Image downloading and src rewriting to epub-internal paths

### Scoped crawling

The crawler stays strictly within the URL subtree of the starting page. Given `https://example.com/docs/guide`, only pages under `/docs/guide/` are followed — not sibling paths or other parts of the domain.

## Limitations

- **JS-rendered sites (SPAs)** — pages that require JavaScript to load content won't work. Use Playwright or Selenium for those.
- **Auth-gated content** — no login support; only works on publicly accessible pages.
- **Bot-blocking CDNs** — images served from CDNs with bot protection will be silently skipped; text content is unaffected.
