#!/usr/bin/env python3
"""
md_to_odoo_blog.py — publish Markdown files as Odoo 19 blog posts (JSON-2 API).

    ODOO_URL=https://odoo.example.com ODOO_API_KEY=... \
        python md_to_odoo_blog.py posts/*.md

Optional env: ODOO_DB (multi-db servers), ODOO_LANG (defaults to the
default language of the website the blog belongs to — what visitors read).

Each file is an idempotent upsert keyed on (blog, slug):
  * first run creates the post, later runs update it;
  * only fields that actually changed are written, so re-syncing a whole
    folder is cheap and leaves untouched posts alone;
  * Markdown is the source of truth: edits made in Odoo's website editor are
    overwritten the next time that file changes.

A bare Markdown file works — the first "# Heading" becomes the title and the
filename becomes the slug. Front matter (YAML) is optional:

    ---
    title: Pourquoi nous cueillons à la main   # default: first H1, else filename
    subtitle: Une question de goût
    slug: cueillette-a-la-main       # URL + idempotency key (default: filename)
    blog: Our blog                   # blog.blog name (default: first blog)
    tags: [récolte, saison]          # created if missing; [] clears
    author: Adam Aubry               # existing res.partner name (default: API user)
    cover: images/cover.jpg          # path relative to the .md file, or a URL
    teaser: Short text for the listing card   # default: first 200 chars
    meta_title: ...
    meta_description: ...
    published: true                  # default false = draft
    publish_date: 2026-10-01T10:00:00+02:00   # future + published = scheduled
    ---

Local images referenced in the body are uploaded once (deduplicated by
checksum) as public attachments and their src rewritten.

Requires: pip install requests markdown python-frontmatter
"""
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import unicodedata
from functools import lru_cache
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import frontmatter
import markdown
import requests

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
API_KEY = os.environ["ODOO_API_KEY"]
ODOO_DB = os.environ.get("ODOO_DB")
ODOO_LANG = os.environ.get("ODOO_LANG")
_lang = ODOO_LANG  # language of the current post's website, set per post

session = requests.Session()
session.headers.update({
    "Authorization": f"bearer {API_KEY}",
    "Content-Type": "application/json; charset=utf-8",
})
if ODOO_DB:
    session.headers["X-Odoo-Database"] = ODOO_DB


def call(model, method, **kwargs):
    ctx = dict(kwargs.pop("context", {}))
    if _lang:
        ctx.setdefault("lang", _lang)
    if ctx:
        kwargs["context"] = ctx
    r = session.post(f"{ODOO_URL}/json/2/{model}/{method}", json=kwargs, timeout=60)
    if not r.ok:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"{model}.{method} -> HTTP {r.status_code}: {msg[:400]}")
    return r.json()


# ------------------------------------------------------------------ helpers
def slugify(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_odoo_datetime(value):
    """YAML date/datetime/str -> 'YYYY-MM-DD HH:MM:SS' in UTC (naive = UTC)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    elif isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@lru_cache(maxsize=None)
def find_id(model, name, create=False):
    found = call(model, "search_read", domain=[["name", "=", name]], fields=["id"], limit=1)
    if found:
        return found[0]["id"]
    if create:
        return call(model, "create", vals_list=[{"name": name}])[0]
    raise RuntimeError(f"{model} named {name!r} not found")


@lru_cache(maxsize=None)
def blog_info(name):
    """(blog id, website id or False) — by name, else the first blog."""
    found = call("blog.blog", "search_read",
                 domain=[["name", "=", name]] if name else [], fields=["id", "website_id"], limit=1)
    if not found:
        raise RuntimeError(f"no blog{' named ' + repr(name) if name else ''} found")
    return found[0]["id"], (found[0]["website_id"] or [False])[0]


@lru_cache(maxsize=None)
def website_info(website_id):
    """(default language, public base URL) of the blog's website. Title,
    subtitle and SEO fields are translatable: writing them in another language
    than the one visitors read leaves the visible version untouched as soon as
    anyone has edited it on the site."""
    site = call("website", "search_read", domain=[["id", "=", website_id]] if website_id else [],
                fields=["default_lang_id", "domain"], limit=1)
    if not site:
        return None, ODOO_URL
    lang = None
    if site[0]["default_lang_id"]:
        lang = call("res.lang", "read", ids=[site[0]["default_lang_id"][0]], fields=["code"])[0]["code"]
    base = (site[0]["domain"] or ODOO_URL).rstrip("/")
    return lang, base if base.startswith("http") else "https://" + base


def upload_image(path: Path, cache: dict):
    """Upload a local image as a public attachment (once) and return its URL."""
    data = path.read_bytes()
    sha1 = hashlib.sha1(data).hexdigest()
    if sha1 not in cache:
        found = call("ir.attachment", "search_read",
                     domain=[["checksum", "=", sha1], ["public", "=", True]],
                     fields=["id"], limit=1)
        att_id = found[0]["id"] if found else call("ir.attachment", "create", vals_list=[{
            "name": path.name,
            "datas": base64.b64encode(data).decode(),
            "mimetype": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "public": True,
            "res_model": "blog.post",
        }])[0]
        cache[sha1] = f"/web/image/{att_id}/{quote(path.name)}"
    return cache[sha1]


def resolve_src(src, base_dir, cache):
    if re.match(r"^([a-z]+:)?//|^/web/|^data:", src, re.I):
        return src
    p = (base_dir / src).resolve()
    if not p.is_file():
        raise RuntimeError(f"image not found: {src}")
    return upload_image(p, cache)


# ------------------------------------------------------------------ markdown
SMARTY_ENTITIES = {"&lsquo;": "‘", "&rsquo;": "’", "&ldquo;": "“", "&rdquo;": "”",
                   "&ndash;": "–", "&mdash;": "—", "&hellip;": "…",
                   "&laquo;": "«", "&raquo;": "»"}


def to_html(md_text, base_dir, cache):
    html = markdown.markdown(md_text, extensions=["extra", "sane_lists", "smarty"])
    # Real characters instead of entities: Odoo's auto-teaser drops entities
    for ent, char in SMARTY_ENTITIES.items():
        html = html.replace(ent, char)
    html = re.sub(r'(<img\b[^>]*?\bsrc=")([^"]+)(")',
                  lambda m: m.group(1) + resolve_src(m.group(2), base_dir, cache) + m.group(3),
                  html)
    # Bootstrap classes so the Odoo theme styles them
    html = re.sub(r"<img\b", '<img class="img-fluid"', html)
    html = html.replace("<table>", '<table class="table table-bordered">')
    html = html.replace("<blockquote>", '<blockquote class="blockquote">')
    return html


def split_title(meta, body, md_path):
    """Title from front matter, else a leading '# H1' (which is then removed
    from the body since Odoo already shows the title on the cover)."""
    m = re.match(r"\s*#[ \t]+(.+?)[ \t]*#*[ \t]*(\n|$)", body)
    h1 = m.group(1).strip() if m else None
    title = meta.get("title") or h1 or md_path.stem.replace("-", " ").replace("_", " ").capitalize()
    if h1 and h1 == title:
        body = body[m.end():]
    return str(title), body


# ------------------------------------------------------------------ diffing
def differs(field, new, old):
    if field == "tag_ids":
        return sorted(new[0][2]) != sorted(old or [])
    if field in ("blog_id", "author_id"):
        return new != (old[0] if old else False)
    if field == "cover_properties":
        return json.loads(new) != json.loads(old or "{}")
    return (new or False) != (old or False)


# ------------------------------------------------------------------ sync
def sync(md_path: Path):
    post = frontmatter.load(md_path)
    meta = post.metadata
    title, body = split_title(meta, post.content, md_path)
    slug = slugify(meta.get("slug") or md_path.stem)
    global _lang
    b_id, website_id = blog_info(meta.get("blog"))
    site_lang, site_url = website_info(website_id)
    _lang = ODOO_LANG or site_lang
    cache = {}

    vals = {
        "name": title,
        "subtitle": meta.get("subtitle") or False,
        "blog_id": b_id,
        "seo_name": slug,
        "content": to_html(body, md_path.parent, cache),
        "teaser_manual": meta.get("teaser") or False,
        "website_meta_title": meta.get("meta_title") or False,
        "website_meta_description": meta.get("meta_description") or False,
    }
    if "tags" in meta:
        vals["tag_ids"] = [[6, 0, [find_id("blog.tag", str(t), create=True) for t in (meta["tags"] or [])]]]
    if meta.get("author"):
        vals["author_id"] = find_id("res.partner", str(meta["author"]))

    fields = list(vals) + ["is_published", "published_date", "cover_properties"]
    found = call("blog.post", "search_read",
                 domain=[["seo_name", "=", slug], ["blog_id", "=", b_id]],
                 fields=fields, limit=1, context={"active_test": False})
    existing = found[0] if found else None

    if meta.get("cover"):
        props = json.loads(existing["cover_properties"]) if existing else {
            "background_color_class": "o_cc3", "opacity": "0.2",
            "resize_class": "o_half_screen_height",
        }
        props["background-image"] = f"url('{resolve_src(str(meta['cover']), md_path.parent, cache)}')"
        if "o_record_has_cover" not in props.get("resize_class", ""):
            props["resize_class"] = (props.get("resize_class", "") + " o_record_has_cover").strip()
        vals["cover_properties"] = json.dumps(props)

    # Publishing. Two Odoo quirks drive this block:
    #  - every write containing is_published=True re-sends the "new post"
    #    notification to the blog's followers;
    #  - such a write also resets published_date to now unless one is given.
    # So is_published is only sent when the state actually changes.
    want_published = bool(meta.get("published", False))
    publish_date = to_odoo_datetime(meta["publish_date"]) if meta.get("publish_date") else None
    if existing:
        if want_published != existing["is_published"]:
            vals["is_published"] = want_published
        if publish_date and publish_date != existing["published_date"]:
            vals["published_date"] = publish_date
    else:
        vals["is_published"] = want_published
        if publish_date or want_published:
            vals["published_date"] = publish_date or now_utc()

    if existing:
        post_id = existing["id"]
        changes = {k: v for k, v in vals.items()
                   if k in ("is_published", "published_date") or differs(k, v, existing.get(k))}
        if changes:
            call("blog.post", "write", ids=[post_id], vals=changes)
        action = f"updated ({', '.join(sorted(changes))})" if changes else "unchanged"
    else:
        post_id = call("blog.post", "create", vals_list=[vals])[0]
        action = "created"

    info = call("blog.post", "read", ids=[post_id], fields=["website_url", "is_published", "post_date"])[0]
    state = "draft"
    if info["is_published"]:
        state = "scheduled " + info["post_date"] if info["post_date"] > now_utc() else "live"
    print(f"{md_path}: {action} [{state}] {site_url}{info['website_url']}")


if __name__ == "__main__":
    paths = [Path(a) for a in sys.argv[1:]]
    if not paths:
        sys.exit(__doc__)
    failures = 0
    for p in paths:
        try:
            sync(p)
        except Exception as e:  # keep going; report at the end
            failures += 1
            print(f"{p}: FAILED — {e}", file=sys.stderr)
    sys.exit(1 if failures else 0)
