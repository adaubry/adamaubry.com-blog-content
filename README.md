# adamaubry.com content

Blog posts for https://adamaubry.com. Every push to `main` that touches `posts/` syncs them to Odoo.

- New post: add `posts/<slug>.md` (the filename becomes the URL slug), push.
- Update: edit the file, push. The Markdown wins over edits made in Odoo's editor.
- Publish / unpublish: `published: true` / `false` in the front matter.
- Remove: archive the post in Odoo (deleting the file does not delete the post).

All front matter keys are documented at the top of `scripts/md_to_odoo_blog.py`.
