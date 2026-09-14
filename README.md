# Cambium Networks Grabber

Mirrors your Cambium Networks support-portal downloads locally: firmware,
software tools, release notes/PDFs, and the MIB package zips bundled with
each release, across every product category (PMP, ePMP, PTP, cnMaestro,
Enterprise Wi-Fi, cnPilot, cnWave, cnMatrix, and the rest). Sibling project
to the [MikroTik CDN grabber](../MIKROTIK-CDN-MASTER) - same shape (headless
CLI, systemd timer, resumable manifest) - but the sites are structurally
very different, see **Read this first** below.

## Read this first: this is not the MikroTik tool's situation

MikroTik's CDN is open and anonymous - no login, no ToS restriction found.
Cambium's support portal (`support.cambiumnetworks.com`) is not:

- **Every download requires a real account login.** There's no anonymous
  catalog to crawl - this tool logs in as you and pulls whatever your
  account/warranty/SMA entitles you to see.
- **Cambium's website Terms & Conditions explicitly prohibit automated
  access** ("To use any data mining, robots, or similar data gathering or
  extraction methods in connection with this Website" - under "Unauthorized
  Activities"). This tool exists anyway, at the account owner's explicit,
  informed request and risk - not because that clause doesn't apply.
- **The login endpoint has an active technical check, not just a ToS
  clause.** A plain, honestly-self-identifying User-Agent gets the login
  silently rejected (200 OK, but the session never authenticates); a
  standard browser UA string doesn't. `engine.py` uses a browser UA for
  exactly this reason - again, a deliberate, informed choice, not an
  oversight.
- **The practical risk is your Cambium account getting flagged or
  suspended**, which would hurt real work that depends on that portal. This
  isn't a legal-jeopardy risk to worry about - it's an account-relationship
  risk you're accepting knowingly.

If any of that changes your mind, don't run this - there's no other way to
get this data short of manually clicking through the portal yourself.

## What actually gets pulled

Confirmed live (2026-09-14) by logging in and inspecting the real pages:

```
GET /files                          -> tree of product groups/categories
GET /files/<category-slug>/         -> "Current" releases for that product
GET /files/<category-slug>/archive  -> "Archive" (older) releases
```

Each release panel contains one or more files - firmware images, `.tar.gz`/
`.pkg3`/`.zip` software bundles, PDF release notes and planning guides, and
**MIB package zips** (e.g. `PMP450 Series 25.1 MIBS.zip`) - every file type
is pulled, nothing is filtered by extension. (Cambium's separate public MIB
browser at `/framed/onlinetools/` is *not* used here - it's just rendered
HTML documentation of MIB object/trap definitions, not the actual `.mib`
files; the real ones ship inside these per-release zips instead.)

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Verify credentials and cache a session (do this first)
python run.py login --output ./cambium_archive

# Dry run: discover everything, report file counts/sizes, download nothing.
# Do this before your first full scan - a complete crawl across every
# product line can plausibly run into the hundreds of GB (see the MikroTik
# tool's archive for a sense of scale on a similarly old product catalog).
# Also useful after a partial run to see how much is left.
python run.py estimate --output ./cambium_archive

# Full crawl - current releases everywhere first, then archives everywhere
python run.py scan --output ./cambium_archive

# Prioritize specific product groups - fully finished (current+archive)
# before anything else even starts. Matched case-insensitively against
# group name, category name, and slug.
python run.py scan --output ./cambium_archive --priority ePMP,PTP

# Restrict to specific category slugs (see them via a `scan -v` run, or the
# /files page's links)
python run.py scan --output ./cambium_archive --categories pmp450,ptp820

# Skip Archive tabs - current releases only, much faster
python run.py scan --output ./cambium_archive --no-archive

# Single daily-style check (cron/systemd friendly)
python run.py watch --once --output ./cambium_archive
```

Credentials: `CAMBIUM_EMAIL` / `CAMBIUM_PASSWORD` environment variables
(preferred - `--email`/`--password` are visible to anyone on the box via
`ps`). A successful login's session is cached to `<output>/.session.json`
and reused until it actually expires, so a scheduled run isn't logging in
fresh every time.

Run `python run.py --help` / `scan --help` / `watch --help` for every flag.

## Download order: newest first, your priorities first

Two things shape the order files land in, in this priority:

1. **Priority groups/categories** (`--priority`) are fully finished -
   current *and* archive - before any other category is even started.
2. Within everything else, every category's **Current** releases are
   downloaded across the whole catalog before **any** category's Archive
   tab is fetched.

So `--priority ePMP,PTP` (which the shipped systemd units use by default)
means: ePMP+PTP current, then ePMP+PTP archive, then everything else
current, then everything else archive. An interrupted run has always
grabbed the stuff that matters most first.

## Deployment

### CentOS - native systemd service, no Docker required

```bash
sudo ./deploy/centos/install.sh
```

Installs to `/opt/cambium-grabber` under a dedicated `cambium-grabber`
system user. **Unlike the MikroTik tool, this needs credentials before the
timer can run anything** - `install.sh` creates
`/opt/cambium-grabber/credentials.env` (mode 600) with empty
`CAMBIUM_EMAIL=` / `CAMBIUM_PASSWORD=` for you to fill in, then prints the
exact commands to verify login and enable the timer. It does not enable the
timer automatically the way the MikroTik installer does, on purpose - there
are real credentials to set first.

Default priority baked into the shipped systemd units: `--priority
ePMP,PTP` (edit `/etc/systemd/system/cambium-grabber-*.service` to change).

### Docker

No Dockerfile/compose file yet (see the MikroTik project's for the general
shape if you want one) - the CentOS systemd path was built and tested
first since that's the actual target server.

## Archive layout

```
cambium_archive/
├── manifest.json                 # discovered release groups, downloaded files (size/path/url), stats
├── .session.json                 # cached login session (gitignored, do not commit)
├── ptp820/
│   ├── r2743_System Release 12.9 - Non FIPS_2025-01-28/
│   │   ├── PTP 820C 820E 820S - 12.9.0.0.0.372.zip
│   │   ├── PTP 850C - 12.9.0.0.0.372.zip
│   │   └── cnMaestro-PTP8XX-v12.9.0.0.0.372.tar.gz
│   └── ...
├── epmp/...
├── pmp450/...
└── ...
```

`manifest.json` keys on `<category-slug>/<release-id>` ("groups", to match
the MikroTik tool's manifest shape) rather than a version string, since
Cambium organizes by product + release rather than one flat version number
the way RouterOS does.

## Known limitations / things to verify before relying on this

- **Login flow is a plain 2-step form POST** (email, then password) as of
  2026-09-14 - no MFA/SSO hit during testing with a real account. If
  Cambium adds MFA later, `auth.login()` raises `LoginError` with a clear
  message rather than hanging or silently failing - it won't guess.
- **Not yet tested at full scale.** The login/discovery/download mechanics
  were verified against real category pages (`pmp450`, `ptp820`) and a real
  file download, but a complete 87-category crawl hasn't been run start to
  finish. Expect to iterate on retry/backoff tuning once you see it run
  for real across everything.
- **Concurrency defaults are deliberately modest** (4 category workers, 4
  download workers) given the account-risk consideration above - raise
  `--category-workers`/`--dl-workers` at your own judgment.

## License

MIT
