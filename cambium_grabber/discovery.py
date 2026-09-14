"""Crawl the authenticated Cambium downloads tree: product groups -> product
categories -> releases -> individual files. Unlike MikroTik's predictable
"probe every version string" CDN, everything here is a real, fully-linked
catalog once you're logged in - so this is a straightforward crawl of what's
actually on the page, not a brute-force guesser.

Structure (verified live, 2026-09-14):
  GET /files                    -> tree of product groups/categories (<details>/<summary>)
  GET /files/<category-slug>/           -> "Current" releases for that product
  GET /files/<category-slug>/archive    -> "Archive" (older) releases
  each release panel (id="r<n>") contains one or more files, each with a
  filename, a human-readable size, and an opaque download URL:
      https://support.cambiumnetworks.com/file/<hash>
  (the real filename only comes from the page text / Content-Disposition on
  download - the URL itself gives no hint.)
"""

from urllib.parse import urljoin

from bs4 import BeautifulSoup

FILES_URL = "https://support.cambiumnetworks.com/files"


class SessionExpired(Exception):
    """The session got redirected to /login instead of the page we asked
    for. A crawl across ~90 categories can genuinely outlast a session's
    lifetime - without this check, a expired-mid-crawl session would just
    silently parse the login page (0 releases found, no error) instead of
    telling anyone what happened, which looks exactly like "the tool got
    stuck" from the outside.
    """


class Category:
    def __init__(self, group, name, url):
        self.group = group          # e.g. "PMP"
        self.name = name            # e.g. "PMP 450"
        self.url = url              # e.g. https://support.cambiumnetworks.com/files/pmp450/
        self.slug = url.rstrip("/").rsplit("/", 1)[-1]

    def __repr__(self):
        return f"Category({self.group!r}, {self.name!r}, {self.slug!r})"


class ReleaseFile:
    def __init__(self, filename, size_text, url):
        self.filename = filename
        self.size_text = size_text
        self.url = url


class Release:
    def __init__(self, release_id, title, files):
        self.release_id = release_id   # e.g. "r2743"
        self.title = title             # e.g. "System Software v25.1 / 2026-07-23"
        self.files = files             # list[ReleaseFile]


def discover_categories(session):
    """Parse the /files tree page into every (group, category) pair."""
    r = session.get(FILES_URL, timeout=20)
    r.raise_for_status()
    if "/login" in r.url:
        raise SessionExpired()
    soup = BeautifulSoup(r.text, "html.parser")

    categories = []
    for group_li in soup.select("li.cs-file-group"):
        summary = group_li.find("summary")
        group_name = summary.get_text(strip=True) if summary else "Unknown"
        for cat_li in group_li.select("li.cs-file-category"):
            a = cat_li.find("a", href=True)
            if not a:
                continue
            name = a.get_text(strip=True)
            url = urljoin(FILES_URL, a["href"])
            categories.append(Category(group_name, name, url))
    return categories


def _parse_releases(html: str):
    soup = BeautifulSoup(html, "html.parser")
    releases = []
    for panel in soup.select("div.cs-release"):
        release_id = panel.get("id", "")
        title_el = panel.find("h4", class_="panel-title")
        title = title_el.get_text(strip=True) if title_el else release_id

        files = []
        for file_div in panel.select("div.cs-file"):
            bold = file_div.find("b")
            filename = bold.get_text(strip=True) if bold else None
            details = file_div.select("div.cs-file-details")
            size_text = details[0].get_text(strip=True) if details else None
            link = file_div.find("a", href=True)
            url = link["href"] if link else None
            if filename and url:
                files.append(ReleaseFile(filename, size_text, url))

        if files:
            releases.append(Release(release_id, title, files))
    return releases


def discover_current_releases(session, category: Category):
    r = session.get(category.url, timeout=20)
    r.raise_for_status()
    if "/login" in r.url:
        raise SessionExpired()
    return _parse_releases(r.text)


def discover_archive_releases(session, category: Category):
    archive_url = urljoin(category.url, "archive")
    r = session.get(archive_url, timeout=20)
    if "/login" in r.url:
        raise SessionExpired()
    if r.status_code != 200:
        # Not every product line has an Archive tab - not an error.
        return []
    return _parse_releases(r.text)


def discover_releases(session, category: Category, include_archive: bool = True):
    """Current releases, plus archived ones unless include_archive=False
    (archives can be large for decades-old product lines like PMP - useful
    to be able to skip them for a fast "just get current" pass).
    """
    releases = discover_current_releases(session, category)
    if include_archive:
        releases.extend(discover_archive_releases(session, category))
    return releases
