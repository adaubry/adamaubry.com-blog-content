# adamaubry.com content

Blog posts for https://adamaubry.com. Every push to `main` that touches `posts/` syncs them to Odoo.

- New post: add `posts/<slug>.md` (English, the site's default language) and push.
- Same post in French: add `posts/<slug>.fr.md` next to it. One post, both languages.
  The two files must line up paragraph for paragraph (headings, list items, table cells,
  link texts and image alt texts included); the sync refuses to publish if they don't.
  Only the English file controls published / dates / tags / author / cover.
- French typography: a value containing " : " must be in quotes, e.g. `teaser: "Le blog : mode d'emploi"`.
- Update: edit the file(s), push. The Markdown wins over edits made in Odoo's editor.
- Publish / unpublish: `published: true` / `false` in the English file's front matter.
- Remove: archive the post in Odoo (deleting the file does not delete the post).

All front matter keys are documented at the top of `scripts/md_to_odoo_blog.py`.
