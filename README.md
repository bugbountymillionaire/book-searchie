# Book-Searchie

**Turn a folder of PDFs and EPUBs into a searchable knowledge base, then open each result at the page where it was found.**

> **Status: early development.** Phase 1 works: indexing and command-line search. The web UI is next. This README also serves as the development plan; sections marked *(planned)* do not exist yet.

---

## What it does

- **Search your whole library instantly.** Book-Searchie reads your books once and builds a full-text index. Searches then take milliseconds, not minutes.
- **Organise by topic.** Each top-level folder in your library becomes a topic you can pick from a dropdown, plus an *All topics* option.
- **See the source.** Click a result to open the actual page:
  - **PDF:** the page is shown as an image with your search terms highlighted.
  - **EPUB:** the chapter is shown, scrolled to the match, with the terms highlighted.
- **Use proper search syntax.** It supports phrases (`"black cohosh"`), `AND` / `OR` / `NOT`, prefix matching (`echinac*`), and results ranked by relevance.
- **Run it your way.** Use it privately on your own computer, or host it on a home server so every device in the house can use it.
- **Cross-platform.** It runs natively on Windows, macOS and Linux. No WSL needed.

## How your library is organised

Point Book-Searchie at one or more library folders. Each top-level subfolder becomes a topic:

```
E:\Knowledge\              <- library root (set in config)
├── Herbalism\             <- topic: "Herbalism"
│   ├── Culpeper - Complete Herbal.pdf
│   └── Modern Herbal Medicine.epub
├── Woodwork\              <- topic: "Woodwork"
│   ├── ...
│   └── _unsorted\         <- skipped (starts with "_")
├── Gardening\             <- topic: "Gardening"
│   └── Seasonal\          <- subfolders are included in their parent topic
│       └── ...
└── _inbox\                <- skipped (starts with "_")
```

To add a topic, create a folder and re-index. No code changes are needed. Books placed directly in a library root, outside any topic folder, go in a topic called *General*.

To leave a folder out, either start its name with an underscore (`_inbox`, `_to-sort`) or add it to `exclude` in the config. Underscore folders are skipped at any depth, along with everything inside them.

---

## Quick start

Book-Searchie isn't on PyPI yet, so install it from a copy of this repo (see [Development](#development)). Then:

```bash
book-searchie init            # creates book-searchie.toml and asks where your library is
book-searchie index           # builds the search index (first run can take a while)
book-searchie search '"black cohosh" AND root*'
book-searchie serve           # (planned) opens http://localhost:8000 in your browser
```

### Commands

| Command | What it does |
|---|---|
| `init [--root FOLDER]... [--force]` | Create `book-searchie.toml` in the current folder |
| `index [--full] [-q]` | Add new and changed books, and drop deleted ones. `--full` re-reads everything |
| `search QUERY [-t TOPIC] [-n LIMIT]` | Search from the terminal |
| `topics` | List topics and how many books each has |
| `report` | List books that can't be searched (scanned, damaged, password-protected or DRM) |

Every command accepts `--config FILE` to use a config file other than `./book-searchie.toml`. You can also set `BOOK_SEARCHIE_CONFIG`.

Indexing can be interrupted with Ctrl+C at any time. Books finished so far are kept, and the next run carries on.

## Configuration

The config file is `book-searchie.toml` (`book-searchie init` creates it). Relative paths in it are relative to the config file:

```toml
[library]
roots = ["E:/Knowledge"]          # one or more library folders
extensions = ["pdf", "epub"]
exclude = []                      # extra folders or glob patterns to skip, e.g. ["Archive", "**/drafts"]
                                  # (folders starting with "_" are always skipped)

[index]
path = "book-searchie.db"         # SQLite index file

[server]
mode = "local"                    # "local" or "network" (see below)
host = "127.0.0.1"
port = 8000
```

Environment variables override the config file, which is handy for Docker:

| Variable | Setting |
|---|---|
| `BOOK_SEARCHIE_ROOTS` | `library.roots`, separated like `PATH` (`;` on Windows, `:` elsewhere) |
| `BOOK_SEARCHIE_EXTENSIONS` | `library.extensions`, comma-separated |
| `BOOK_SEARCHIE_EXCLUDE` | `library.exclude`, comma-separated |
| `BOOK_SEARCHIE_INDEX` | `index.path` |
| `BOOK_SEARCHIE_MODE`, `_HOST`, `_PORT` | `server.mode`, `server.host`, `server.port` |

If `BOOK_SEARCHIE_ROOTS` is set, no config file is needed at all.

---

## Deployment modes

### 1. Local (default)

Book-Searchie listens only on `127.0.0.1`, so only your own computer can reach it. No login is required. This is the safe default.

```bash
book-searchie serve
```

### 2. Home server / network *(planned)*

Run Book-Searchie on a home server, NAS or always-on PC, and use it from phones, tablets and laptops on your network.

```bash
book-searchie serve --mode network        # listens on 0.0.0.0:8000
```

In network mode:

- **Login is required.** Book-Searchie refuses to start in network mode until at least one user exists:
  ```bash
  book-searchie user add alice
  book-searchie user add bob --admin
  ```
  Passwords are stored hashed, never in plain text.
- **Users have two roles:** *readers* can search and view; *admins* can also trigger re-indexing and manage users from the web UI.
- **Topics can be restricted per user** *(optional)*, for example to keep a private topic hidden from other household members.
- **The library is read-only.** Book-Searchie never modifies, moves or deletes your books.
- **It works on phones.** The interface is responsive, and the page viewer supports pinch-zoom.

#### Docker *(planned)*

This is the easiest option for a NAS or home server:

```yaml
# docker-compose.yml
services:
  book-searchie:
    image: ghcr.io/<your-github-user>/book-searchie:latest
    ports:
      - "8000:8000"
    volumes:
      - /path/to/your/library:/library:ro    # mounted read-only
      - ./data:/data                          # index + config + users
    environment:
      BOOK_SEARCHIE_MODE: network
    restart: unless-stopped
```

#### HTTPS and access from outside your home

Book-Searchie serves plain HTTP. On a home network that is usually fine. **Don't expose it directly to the internet.** For remote access, use one of these:

- a VPN into your home network (WireGuard, Tailscale, etc.), **recommended**, or
- a reverse proxy with HTTPS (Caddy, Nginx, Traefik) in front of Book-Searchie.

Example configs for both will be included in `docs/`.

---

## Architecture

```
┌──────────────┐     ┌───────────────────┐     ┌──────────────────┐
│ Library      │────>│ Indexer           │────>│ SQLite FTS5      │
│ PDFs / EPUBs │     │ PyMuPDF / zipfile │     │ 1 row per PDF    │
└──────────────┘     └───────────────────┘     │ page or EPUB     │
                                               │ chapter          │
                                               └────────┬─────────┘
                                                        │
┌──────────────┐     ┌───────────────────┐              │
│ Browser      │<───>│ Web server        │<─────────────┘
│ (any device) │     │ FastAPI           │
└──────────────┘     │ search · render   │
                     │ auth (network)    │
                     └───────────────────┘
```

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Cross-platform, easy to install |
| Index | SQLite FTS5 (included in Python's standard builds) | No separate database server; fast; ranks results |
| PDF text and rendering | PyMuPDF | Finds the exact position of each match; renders pages with highlights; has Windows/macOS/Linux wheels |
| EPUB | `zipfile` + HTML parsing | EPUBs are zip files of HTML chapters |
| Web | FastAPI + server-rendered HTML | Small and simple; no frontend build step |
| Auth | Session cookies, Argon2 password hashes | Standard and safe |

### Indexing

- **One-time and incremental:** each file's size and modified time are recorded, so re-indexing only processes new or changed files, and removes entries for deleted ones.
- **Text normalisation:** ligatures (`ﬁ` becomes `fi`), odd whitespace and hyphenated line breaks are cleaned up so searches match.
- **Scanned PDFs:** files with no text layer are flagged in an *Unsearchable files* report rather than skipped silently. Running [`ocrmypdf`](https://github.com/ocrmypdf/OCRmyPDF) on them adds a text layer.
- **FTS5 check:** some Linux distributions build Python's SQLite without FTS5. Book-Searchie checks for it at startup and explains how to fix it if it's missing.

### Locations in results

- **PDF:** the page number, using the book's printed page labels where the PDF has them (e.g. `xii`, `147`), otherwise the physical page number.
- **EPUB:** EPUBs have no fixed pages, so a result shows the chapter title and how far through the chapter the match is (e.g. *Chapter 4: Roots · 35%*).

### Security notes

- **No path traversal:** every file path served is checked to be inside a configured library root.
- **Escaped text:** text extracted from books is HTML-escaped before display.
- **No shell commands:** Book-Searchie never runs shell commands built from user input.

---

## Roadmap

### Phase 1: Core
- [x] Project skeleton (`pyproject.toml`, CLI entry point, config loading)
- [x] Indexer: PDF pages + EPUB chapters into SQLite FTS5
- [x] Folder exclusions (`_`-prefixed folders plus `exclude` patterns)
- [x] Incremental re-indexing
- [x] CLI search (`book-searchie search "term"`) for testing
- [x] Unsearchable files report (`book-searchie report`)

### Phase 2: Web UI (local mode)
- [ ] Search page with topic dropdown
- [ ] Results grouped by book, with highlighted snippets
- [ ] PDF page viewer with highlighted matches
- [ ] EPUB chapter viewer, scrolled to the match
- [ ] Previous/next match navigation within a book

### Phase 3: Network mode
- [ ] `--mode network` with user accounts and login
- [ ] Reader/admin roles; re-index from the web UI
- [ ] Optional per-user topic restrictions
- [ ] Mobile-friendly layout
- [ ] Docker image + compose example
- [ ] Reverse proxy and VPN docs

### Phase 4: Nice to have
- [ ] Watch folders and re-index automatically (`watch = true` in `[index]`)
- [ ] Book cover thumbnails
- [ ] Search history and saved searches
- [ ] Filters by file type or book
- [ ] Windows `.exe` via PyInstaller

### Release checklist
- [ ] Demo library of public-domain books (e.g. from Project Gutenberg) for trying it out and for tests
- [ ] Screenshots in this README
- [ ] Tests running on Windows, macOS and Linux (GitHub Actions)
- [ ] `LICENSE` file (MIT)
- [ ] Publish to PyPI and GitHub Container Registry

---

## Development

Keep this repo **outside your book library**, not in a subfolder of a library root. That way the indexer won't pick up the source code, and you can't accidentally commit copyrighted books.

```bash
git clone https://github.com/<your-github-user>/book-searchie.git
cd book-searchie
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
pytest                          # run the tests
```

The tests generate their own small PDFs and EPUBs, so they don't need a real library.

The `.gitignore` should exclude `*.db`, `book-searchie.toml`, `.venv/` and any local library folders.

## License

MIT (planned)
