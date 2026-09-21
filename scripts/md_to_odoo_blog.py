#!/usr/bin/env python3
"""
md_to_odoo_blog.py — publish Markdown files as Odoo 19 blog posts (JSON-2 API).

    ODOO_URL=https://odoo.example.com ODOO_API_KEY=... \
        python md_to_odoo_blog.py posts/*.md

Optional env: ODOO_DB (multi-db servers), ODOO_LANG: the language plain
<slug>.md files are written in (default: the website's default language).
Files named <slug>.<lang>.md always use their own language, see below.

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

One post, several languages: name the files <slug>.<lang>.md, with <lang> one
of the website's language codes as they appear in its URLs (en, fr, ...):

    posts/why-hand-picking.en.md   base version, in the website's default language
    posts/why-hand-picking.fr.md   French version of the same post

The base file (or a plain <slug>.md) creates/updates the post exactly as a
single file would; it alone controls shared settings (published, dates,
tags, author, cover, blog). Every other file becomes a translation of that
same post: its title, subtitle, slug (-> /fr/blog/.../<its slug>), teaser,
meta_* and body. Odoo translates a body piece by piece -- each paragraph,
heading, list item, table cell, link text and image alt -- so the files must
line up one-to-one; the sync stops with the first block that doesn't.
Images and link targets are shared by all languages.

Requires: pip install requests markdown python-frontmatter lxml
(and odoo_terms.py next to this file)
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

import odoo_terms  # Odoo's own HTML term splitting, vendored

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
                fields=["default_lang_id", "domain", "language_ids"], limit=1)
    if not site:
        return None, ODOO_URL, {}
    langs = {l["url_code"]: l["code"] for l in
             call("res.lang", "read", ids=site[0]["language_ids"], fields=["code", "url_code"])}
    lang = None
    if site[0]["default_lang_id"]:
        lang = call("res.lang", "read", ids=[site[0]["default_lang_id"][0]], fields=["code"])[0]["code"]
    base = (site[0]["domain"] or ODOO_URL).rstrip("/")
    return lang, base if base.startswith("http") else "https://" + base, langs


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


def split_title(meta, body, fallback_name):
    """Title from front matter, else a leading '# H1' (which is then removed
    from the body since Odoo already shows the title on the cover), else the
    file name. Returns (title, body, explicit)."""
    m = re.match(r"\s*#[ \t]+(.+?)[ \t]*#*[ \t]*(\n|$)", body)
    h1 = m.group(1).strip() if m else None
    explicit = bool(meta.get("title") or h1)
    title = meta.get("title") or h1 or fallback_name.replace("-", " ").replace("_", " ").capitalize()
    if h1 and h1 == title:
        body = body[m.end():]
    return str(title), body, explicit


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
def sync(md_path: Path, base_name=None, lang=None, blog_name=None):
    """Create or update the post from its base file. Returns
    (post_id, body html as stored, content language, public site URL)."""
    post = load(md_path)
    meta = post.metadata
    base_name = base_name or md_path.stem
    title, body, _ = split_title(meta, post.content, base_name)
    slug = slugify(meta.get("slug") or base_name)
    global _lang
    b_id, website_id = blog_info(meta.get("blog") or blog_name)
    site_lang, site_url, langs = website_info(website_id)
    _lang = lang or ODOO_LANG or site_lang
    prefix = "" if _lang == site_lang else "/" + next((u for u, c in langs.items() if c == _lang), "")
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
    print(f"{md_path}: {action} [{state}] {site_url}{prefix}{info['website_url']}")
    return post_id, vals["content"], _lang, site_url


# ------------------------------------------------------------------ translations
TRANSLATED_FIELDS = {  # front matter key -> blog.post field (all translatable)
    "subtitle": "subtitle", "teaser": "teaser_manual",
    "meta_title": "website_meta_title", "meta_description": "website_meta_description",
}


def _pieces_preview(terms, n=3):
    text = " / ".join(re.sub(r"<[^>]+>", "", t) for t in terms[:n])
    return f"({text[:90]}{'...' if len(text) > 90 else ''})" if terms else "(nothing)"


def explain_mismatch(src_html, tr_html, src_lang, lang):
    """Point at the first top-level block whose pieces don't line up."""
    def blocks(value):
        root = odoo_terms.parse_html(f"<div>{value}</div>")
        return [(el.tag, odoo_terms.html_terms(odoo_terms.serialize_html(el)))
                for el in root if isinstance(el.tag, str)]
    a, b = blocks(src_html), blocks(tr_html)
    for i, ((ta, pa), (tb, pb)) in enumerate(zip(a, b), 1):
        if ta != tb or len(pa) != len(pb):
            return (f"block {i}: {src_lang} <{ta}> in {len(pa)} piece(s) {_pieces_preview(pa)}, "
                    f"{lang} <{tb}> in {len(pb)} piece(s) {_pieces_preview(pb)}")
    return f"{src_lang} has {len(a)} blocks, {lang} has {len(b)}"


def check_alignment(base_file, base_name, translations, src_lang, langs):
    """Before writing anything: every translation must line up with the base."""
    post = load(base_file)
    _, body, _ = split_title(post.metadata, post.content, base_name)
    src_html = to_html(body, base_file.parent, {})
    for code, path in translations:
        tr = load(path)
        _, tr_body, _ = split_title(tr.metadata, tr.content, path.stem)
        tr_html = to_html(tr_body, path.parent, {})
        n_src, n_tr = len(odoo_terms.html_terms(src_html)), len(odoo_terms.html_terms(tr_html))
        if n_src != n_tr:
            raise RuntimeError(
                f"{path}: doesn't line up with the {src_lang} version ({n_src} pieces vs {n_tr}), "
                f"so nothing was published. First difference at "
                f"{explain_mismatch(src_html, tr_html, src_lang, langs[code])}. "
                "Each paragraph, heading, list item and table cell must match one-to-one; "
                "a link or image splits its paragraph into pieces, so keep them in the same place.")


def translate(post_id, md_path, src_html, src_lang, lang, url_code, site_url):
    """Store md_path as the `lang` translation of the post."""
    post = load(md_path)
    meta = post.metadata
    title, body, explicit_title = split_title(meta, post.content, md_path.stem)
    tr_html = to_html(body, md_path.parent, {})

    # Body: pair Odoo's pieces one-to-one, as Odoo itself does.
    src_terms, tr_terms = odoo_terms.html_terms(src_html), odoo_terms.html_terms(tr_html)
    if len(src_terms) != len(tr_terms):
        raise RuntimeError(
            f"doesn't line up with the {src_lang} version ({len(src_terms)} pieces vs {len(tr_terms)}). "
            f"First difference at {explain_mismatch(src_html, tr_html, src_lang, lang)}. "
            "Each paragraph, heading, list item and table cell must match one-to-one; "
            "a link or image splits its paragraph into pieces, so keep them in the same place.")
    mapping = {}
    for s, t in zip(src_terms, tr_terms):
        if s in mapping and mapping[s] != t:
            print(f"{md_path}: warning: '{re.sub('<[^>]+>', '', s)[:50]}' appears more than once "
                  f"in {src_lang} with different translations; Odoo keeps one (the first).", file=sys.stderr)
            continue
        mapping[s] = t

    fields = ["name", "seo_name", "content"] + list(TRANSLATED_FIELDS.values())
    current = call("blog.post", "read", ids=[post_id], fields=fields, context={"lang": lang})[0]
    changed = []

    wanted = {TRANSLATED_FIELDS[k]: str(v) for k, v in meta.items() if k in TRANSLATED_FIELDS and v}
    if explicit_title:
        wanted["name"] = title
    if meta.get("slug"):
        wanted["seo_name"] = slugify(meta["slug"])
    for field, value in wanted.items():
        if value != (current.get(field) or ""):
            call("blog.post", "update_field_translations", ids=[post_id],
                 field_name=field, translations={lang: value})
            changed.append(field)

    expected = odoo_terms.html_translate(lambda term: mapping.get(term), src_html)
    if expected != current["content"]:
        if src_lang == "en_US":  # Odoo keys translations on the en_US text: check ours match
            known = {t["source"] for t in call("blog.post", "get_field_translations", ids=[post_id],
                                               field_name="content", langs=[lang])[0]}
            unknown = [t for t in mapping if t not in known]
            if unknown:
                raise RuntimeError(f"{len(unknown)} piece(s) not recognised by Odoo, e.g. "
                                   f"{unknown[0][:60]!r}: the local HTML splitting differs from Odoo's")
        call("blog.post", "update_field_translations", ids=[post_id], field_name="content",
             translations={lang: mapping}, source_lang=src_lang)
        changed.append("content")

    url = call("blog.post", "read", ids=[post_id], fields=["website_url"], context={"lang": lang})[0]["website_url"]
    action = f"translated {lang} ({', '.join(sorted(changed))})" if changed else f"unchanged {lang}"
    print(f"{md_path}: {action} {site_url}/{url_code}{url}")


def load(path):
    """frontmatter.load, with the file named in YAML errors."""
    try:
        return frontmatter.load(path)
    except Exception as e:
        hint = (" (a French ' : ' inside a value needs the value in quotes)"
                if "mapping values are not allowed" in str(e) else "")
        raise RuntimeError(f"{path}: front matter: {str(e).splitlines()[0]}{hint}") from None


# ------------------------------------------------------------------ grouping
LANG_SUFFIX = re.compile(r"^(?P<base>.+)\.(?P<code>[a-z]{2}(?:[_-][a-zA-Z]{2,4})?)$")


def group_files(paths):
    """{(folder, base name): {url code or None: path}}"""
    groups = {}
    for p in paths:
        m = LANG_SUFFIX.match(p.stem)
        base, code = (m["base"], m["code"]) if m else (p.stem, None)
        group = groups.setdefault((p.parent, base), {})
        if code in group:
            raise RuntimeError(f"{p}: two files for the same post and language")
        group[code] = p
    return groups


def sync_group(base, files):
    """One post: the base file first, then each translation."""
    metas = [load(p).metadata for p in sorted(files.values(), key=lambda p: p.name)]
    blog_name = next((str(m["blog"]) for m in metas if m.get("blog")), None)
    _, website_id = blog_info(blog_name)
    site_lang, site_url, langs = website_info(website_id)
    codes = {code: url for url, code in langs.items()}

    unknown = [c for c in files if c is not None and c not in langs]
    if unknown:
        raise RuntimeError(f"{files[unknown[0]]}: '{unknown[0]}' isn't a language of this website "
                           f"(available: {', '.join(sorted(langs))})")
    default_code = codes.get(site_lang)
    base_file = files.get(None) or files.get(default_code)
    if base_file is None:
        if len(files) == 1:  # a single file in one language: store it as that language
            code, path = next(iter(files.items()))
            sync(path, base, langs[code], blog_name)
            return
        raise RuntimeError(f"{base}: add {base}.{default_code}.md (or {base}.md), the "
                           f"{site_lang} version the translations hang on")
    if None in files and default_code in files:
        raise RuntimeError(f"{base}: {base}.md and {base}.{default_code}.md are both the {site_lang} version")

    base_lang = site_lang if (None not in files or len(files) > 1) else None  # plain single file: ODOO_LANG applies
    translations = sorted((c, p) for c, p in files.items() if p != base_file)
    check_alignment(base_file, base, translations, site_lang, langs)
    post_id, src_html, src_lang, site_url = sync(base_file, base, base_lang, blog_name)
    for code, path in translations:
        try:
            translate(post_id, path, src_html, src_lang, langs[code], code, site_url)
        except Exception as e:
            raise RuntimeError(f"{path}: {e}") from None


if __name__ == "__main__":
    paths = [Path(a) for a in sys.argv[1:]]
    if not paths:
        sys.exit(__doc__)
    failures = 0
    try:
        groups = group_files(paths)
    except Exception as e:
        sys.exit(f"FAILED — {e}")
    for (folder, base), files in groups.items():
        try:
            sync_group(base, files)
        except Exception as e:  # keep going; report at the end
            failures += 1
            name = files.get(None) or folder / f"{base}.*.md"
            print(f"{name}: FAILED — {e}", file=sys.stderr)
    sys.exit(1 if failures else 0)
