---
title: Bonjour tout le monde
subtitle: Le premier article publié à partir d'un fichier Markdown
slug: bonjour-tout-le-monde
teaser: "Comment fonctionne ce blog : on écrit en Markdown, on pousse, c’est en ligne."
meta_description: Le premier article d'adamaubry.com, publié directement depuis un dépôt git.
---

Cet article a été écrit en Markdown et publié en le poussant dans un dépôt git.

## Comment ça marche

1. Un fichier `.md` arrive dans `posts/`.
2. GitHub Actions le convertit en HTML.
3. Le script crée ou met à jour l'article dans Odoo.

Modifier le fichier et le pousser à nouveau met l'article à jour — rien d'autre à faire.
