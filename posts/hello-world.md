---
title: Hello, world
subtitle: The first post published from a Markdown file
tags: [meta]
teaser: How this blog works — write Markdown, push, done.
meta_description: The first post on adamaubry.com, published straight from a git repository.
published: false          # push once as a draft, check it, then set to true and push again
# author: Adam Aubry      # must match an existing contact's name exactly
# cover: images/cover.jpg # image next to this file, used as the header
# publish_date: 2026-10-01T09:00:00+02:00   # future date + published: true = scheduled
---

This post was written in Markdown and published by pushing it to a git repository.

## How it works

1. A `.md` file lands in `posts/`.
2. GitHub Actions converts it to HTML.
3. The script creates or updates the post in Odoo.

Editing the file and pushing again updates the post — nothing else to do.
