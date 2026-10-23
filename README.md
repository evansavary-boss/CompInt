# Veille Géospatial & New Space

Tableau de bord de veille quotidienne sur le **géospatial** et le **New Space** (focus France / Europe / défense).
Une page statique, mise à jour chaque matin par GitHub Actions, lisible en 30 secondes :

- **Politique** : défense et espace en France, UE / ESA / OTAN, budgets, programmes, réglementation.
- **Entreprises** : contrats, lancements, levées de fonds, rachats, nouveaux produits.
- **Marché** : cours et variations 1 jour / 1 semaine / 1 mois d'un panier de valeurs, et prochaines publications de résultats.

Filtre **Géospatial** (par défaut) / **New Space** / **Tout**, recherche plein texte, période 14 ou 30 jours,
thème clair / sombre automatique. Seuls le titre, un extrait de 200 caractères au plus et le lien vers l'article
source sont affichés : aucun article n'est reproduit.

## Installation

Prérequis : Python 3.12 et Git.

```bash
python -m venv .venv
```

Activer l'environnement (Windows : `.venv\Scripts\activate`, macOS / Linux : `source .venv/bin/activate`), puis :

```bash
pip install -r requirements.txt
```

## Utilisation locale

| Commande | Effet |
|---|---|
| `python -m src.build` | collecte les flux, met à jour le marché et génère le site dans `docs/` |
| `python -m src.build --serve` | idem, puis aperçu sur http://localhost:8000 |
| `python -m src.build --skip-collect --skip-market --serve` | régénère la page à partir des données existantes (rapide) |
| `python -m src.collect` | collecte et filtrage seuls, avec un rapport par source dans le terminal |
| `python -m src.collect --dry-run` | idem sans écrire `data/items.json` |
| `python -m src.market` | mise à jour des cours seule |
| `python -m pytest` | tests (dates, déduplication, mots-clés, marché) |

Un flux ou un ticker en erreur ne fait jamais échouer le build : l'erreur est journalisée et la liste des
sources en échec apparaît en pied de page.

## Organisation

```
config/       sources.yaml, keywords.yaml, tickers.yaml, settings.yaml
src/          collect.py, filter.py, enrich.py, market.py, build.py (+ config, models, store)
templates/    index.html.j2, market.html.j2
static/       style.css, app.js
data/         items.json (30 jours d'historique), market.json
docs/         site généré, servi par GitHub Pages
tests/
.github/workflows/update.yml
```

Tous les réglages (timeouts, User-Agent, durée de conservation, seuil de déduplication, fenêtres de calcul
boursier…) sont dans `config/settings.yaml`.

## Ajouter une source

1. Vérifier que l'URL renvoie bien un flux RSS ou Atom récent (l'ouvrir dans un navigateur, ou lancer
   `python -m src.collect --dry-run` après l'ajout).
2. Ajouter une entrée dans `config/sources.yaml` :

   ```yaml
   - name: Nom affiché
     url: https://exemple.com/feed/
     section: entreprises        # politique | entreprises
     domain: geospatial          # tag par défaut : geospatial | newspace
     require_keywords: false     # true pour une source généraliste (seuls les items avec mot-clé sont gardés)
     lang: fr
     exclude_url_patterns:       # optionnel : ignore les URL contenant ces fragments
       - /offres-emploi/
   ```

3. Lancer `python -m src.collect --dry-run` et regarder la ligne de la source dans le rapport
   (nombre d'items lus / gardés).

## Ajouter un mot-clé

Dans `config/keywords.yaml`, sous le domaine voulu (`geospatial` ou `newspace`) :

- `include` : mots ou expressions, insensibles à la casse et aux accents, sur mots entiers ;
  les pluriels simples (s, x, es) sont reconnus automatiquement.
- `acronyms` : sigles sensibles à la casse (`SAR` ne trouvera pas « sar »).
- `exclude` : retire le tag du domaine si l'expression est présente (ex. « search and rescue » pour SAR).
- `global_exclude` (en bas du fichier) : écarte l'item quel que soit le domaine.

Les mots-clés s'appliquent aux nouveaux items ; pour retraiter tout l'historique, supprimer `data/items.json`
puis relancer `python -m src.build`.

## Ajouter un ticker

1. Trouver le symbole Yahoo Finance (ex. sur finance.yahoo.com : `AIR.PA`, `RHM.DE`, `RKLB`).
2. L'ajouter dans `config/tickers.yaml` :

   ```yaml
   - { symbol: HAG.DE, name: Hensoldt, group: defense, currency: EUR }
   ```

   `group` doit être une clé de la section `groups` (geospatial, spatial, defense). Des suggestions déjà
   vérifiées sont en commentaire en bas du fichier.
3. Vérifier avec `python -m src.market --dry-run`.

## Mise en ligne sur GitHub Pages

1. Créer un dépôt sur GitHub (public : GitHub Pages n'est gratuit que pour les dépôts publics).
2. Pousser le projet :

   ```bash
   git remote add origin https://github.com/<utilisateur>/<depot>.git
   ```

   ```bash
   git push -u origin main
   ```

3. Sur GitHub : **Settings → Pages → Build and deployment** : Source **Deploy from a branch**,
   branche **main**, dossier **/docs**, puis **Save**.
4. **Settings → Actions → General → Workflow permissions** : cocher **Read and write permissions**
   (le workflow commit `data/` et `docs/`).
5. Onglet **Actions → Mise à jour quotidienne → Run workflow** pour un premier lancement manuel.

Le site est ensuite publié sur `https://<utilisateur>.github.io/<depot>/` et mis à jour chaque jour à
06:00 UTC (08:00 à Paris en été, 07:00 en hiver). GitHub peut retarder les exécutions planifiées de quelques
minutes, et suspend les workflows planifiés d'un dépôt sans activité pendant 60 jours (le commit quotidien
suffit normalement à l'éviter).

La page porte une balise `noindex` pour ne pas être référencée par les moteurs de recherche, mais elle reste
accessible à toute personne qui a le lien.

## Évolution prévue (V1)

`src/enrich.py` définit l'interface `Enricher`. La V0 utilise `NoopEnricher` (extrait du flux tel quel).
Un futur `ClaudeEnricher` pourra trier, résumer en deux lignes et attribuer les tags via l'API Anthropic :
il suffira de l'enregistrer dans `ENRICHERS`, de passer `enricher: claude` dans `config/settings.yaml` et
d'ajouter la clé API dans **Settings → Secrets and variables → Actions** (jamais dans le dépôt).

## Sources non retenues

Testées le 2026-10-22 et écartées : Air & Cosmos (serveur en erreur 521), OTAN, Ministère des Armées, CNES,
EDA (aucun flux fonctionnel trouvé), Euractiv, Les Échos, GPS World, Esri, defense-aerospace.com
(accès refusé aux robots, HTTP 403), Meta-defense, Directions Magazine, Parabolic Arc (flux supprimés).
