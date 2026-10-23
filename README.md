# Veille Géospatial & New Space

Tableau de bord statique, mis à jour chaque matin par GitHub Actions, pour suivre l'actualité
**politique** et **économique** mondiale du géospatial et du New Space (focus France / Europe / défense).

- **Politique** : défense et espace, institutions, budgets, réglementation, coopération. Filtres par
  pays, région, institution et type d'événement.
- **Économie** : contrats gagnés ou perdus, lancements, nouveaux satellites et produits, levées de fonds,
  rachats, partenariats, résultats. Filtres par entreprise, segment, pays, région et type d'événement.
- **Marché** : synthèse tirée des actualités classées (indicateurs, activité par semaine, régions,
  segments × événements, entreprises les plus actives, principaux donneurs d'ordre, derniers contrats,
  levées et rachats).
- **Fiches** : une page par entreprise ou institution (description, faits Wikidata, produits ou
  programmes clés, activité, acteurs souvent cités avec elle, fil d'actualités).

Filtres communs : domaine **Géospatial** / **New Space** / **Tout**, période (7 à 90 jours), recherche
plein texte. Les filtres sont gardés dans l'adresse de la page (lien partageable). Thème clair / sombre
automatique. Seuls le titre, un extrait de 200 caractères au plus et le lien vers l'article source sont
affichés : aucun article n'est reproduit.

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
| `python -m src.build` | fiches Wikidata (si plus d'une semaine), collecte, classement, génération de `docs/` |
| `python -m src.build --serve` | idem, puis aperçu sur http://localhost:8000 |
| `python -m src.build --skip-collect --serve` | régénère les pages à partir des données existantes, sans réseau |
| `python -m src.collect` | collecte et classement seuls, avec un rapport détaillé dans le terminal |
| `python -m src.collect --backfill` | recherches Google News sur 30 jours (premier remplissage) |
| `python -m src.collect --reclassify` | reclasse tout l'historique sans rien télécharger (après un changement de mots-clés) |
| `python -m src.collect --no-gnews` / `--dry-run` | flux éditeurs seuls / sans écrire `data/items.json` |
| `python -m src.entities` | vérifie le référentiel et rafraîchit les fiches Wikidata (`--force`, `--offline`) |
| `python -m pytest` | tests (hors ligne) |

Un flux, une recherche ou Wikidata en erreur ne fait jamais échouer le build : l'erreur est journalisée,
les données précédentes sont conservées, et les flux en échec apparaissent en pied de page.

## Comment ça marche

```
flux RSS éditeurs ─┐
                   ├─> filtre (mots-clés, exclusions) ─> dédoublonnage ─> extrait ─> classement ─> data/items.json ─> docs/
Google News /      ┘                                                                  │
entité suivie                                              entités, pays, région, type d'événement, onglet
Wikidata ─────────────────────────────────────────────> data/entities.json (fiches) ─┘
```

- **Sources** : 36 flux RSS d'éditeurs vérifiés, plus une recherche Google News par entité suivie (édition
  française en plus pour les entités françaises). Un article Google News n'est gardé que si l'entité
  recherchée est citée dans son titre ; pour les groupes diversifiés (défense, ministères…), il faut en
  plus un mot-clé spatial ou géospatial.
- **Classement par règles**, entièrement piloté par les fichiers YAML et refait sur tout l'historique à
  chaque passage : un changement de mot-clé s'applique donc aussi aux anciens articles.
- **Historique** : 90 jours dans `data/items.json`.

Limites connues : le classement par règles détecte bien les contrats, lancements, levées et rachats
annoncés dans le titre, beaucoup moins les événements politiques ; les liens Google News passent par
news.google.com ; les faits Wikidata peuvent être périmés (voir « Corriger une fiche »).

## Organisation

```
config/
  settings.yaml          réglages (réseau, historique, Google News, Marché, Wikidata, chemins)
  sources.yaml           flux RSS des éditeurs
  keywords.yaml          mots-clés de domaine (géospatial / New Space) et exclusions
  events.yaml            types d'événements et expressions qui les signalent
  geo.yaml               pays (noms, gentilés) et régions
  entities/companies.yaml, entities/institutions.yaml   entreprises et institutions suivies
src/
  collect.py   collecte (RSS + Google News), filtre, dédoublonnage, stockage
  classify.py  entités, pays, régions, événements, onglets      textmatch.py  détection rapide de termes
  entities.py  référentiel + Wikidata                            filter.py     mots-clés, dédoublonnage
  market.py    données de l'onglet Marché                        enrich.py     extrait (V0) / Claude (V1)
  build.py     génération des pages                              store.py, models.py, config.py
templates/     base, onglets, fiche, marché (Jinja2)
static/        style.css, app.js (filtres), market.css, market.js (graphiques)
data/          items.json, entities.json (générés)
docs/          site généré, servi par GitHub Pages
tests/
```

## Ajouter une source RSS

1. Vérifier que l'URL renvoie bien un flux RSS ou Atom récent.
2. Ajouter une entrée dans `config/sources.yaml` :

   ```yaml
   - name: Nom affiché
     url: https://exemple.com/feed/
     section: economie           # onglet par défaut : politique | economie
     domain: geospatial          # tag par défaut : geospatial | newspace
     require_keywords: false     # true pour une source généraliste (seuls les articles avec mot-clé sont gardés)
     lang: fr
     exclude_url_patterns:       # optionnel : ignore les URL contenant ces fragments
       - /offres-emploi/
   ```

3. Lancer `python -m src.collect --dry-run --no-gnews` et regarder la ligne de la source dans le rapport.

## Ajouter une entreprise ou une institution

1. Trouver son identifiant Wikidata (page Wikipédia → « Élément Wikidata », de la forme `Q123456`).
2. Ajouter une entrée dans `config/entities/companies.yaml` (ou `institutions.yaml`) :

   ```yaml
   - id: ma-societe                 # sert d'adresse à la fiche : entite/ma-societe.html
     name: Ma Société
     wikidata: Q123456
     countries: [FR]
     segments: [eo_optique]         # companies.yaml ; pour une institution : type + countries ou region
     domains: [geospatial]
     aliases: [Ma Société, MaSociete]   # termes cherchés dans les articles (casse et accents ignorés)
     aliases_cs: [MSC]                  # sigles, casse respectée
     description: >-
       Deux lignes sur l'activité.
     highlights: [Produit A, Satellite B]
     news_query: '"Ma Société" satellite'   # optionnel : requête Google News si le nom est ambigu
   ```

3. `python -m src.entities` vérifie la configuration (message clair en cas d'erreur) et télécharge la
   fiche Wikidata, puis `python -m src.collect --reclassify` rattache les anciens articles.

### Corriger une fiche

Wikidata a parfois des dirigeants périmés. Ajouter `facts` à l'entité ; la valeur passe avant Wikidata
(`null` masque le champ) :

```yaml
facts: {ceo: Nouveau Nom, director: null}
```

Clés possibles : `founded`, `headquarters`, `country`, `ceo`, `director`, `parent`, `employees`,
`website`, `logo`, `wikipedia`.

## Ajouter un mot-clé ou un type d'événement

- **Domaines** (`config/keywords.yaml`) : `include` (casse et accents ignorés, mots entiers, pluriels
  simples acceptés), `acronyms` (casse respectée), `exclude` par domaine, et `global_exclude` qui écarte
  l'article (y compris dans l'historique).
- **Événements** (`config/events.yaml`) : `patterns`, `patterns_cs` (sigles), `regex` (texte en
  minuscules sans accents), `overrides` (un type en masque un autre), `requires: company` (retenu
  seulement si une entreprise suivie est citée), `section: auto` (onglet selon l'acteur cité).
- **Pays** (`config/geo.yaml`) : noms et gentilés en français et en anglais ; mettre entre guillemets les
  codes que YAML lirait comme un booléen (`"NO"` pour la Norvège).

Après une modification : `python -m src.collect --reclassify`, puis `python -m src.build --skip-collect --serve`.

## Mise en ligne sur GitHub Pages

1. Créer un dépôt sur GitHub (public : GitHub Pages n'est gratuit que pour les dépôts publics).
2. Pousser le projet :

   ```bash
   git remote add origin https://github.com/<utilisateur>/<depot>.git
   ```

   ```bash
   git push -u origin main
   ```

3. **Settings → Pages → Build and deployment** : Source **Deploy from a branch**, branche **main**,
   dossier **/docs**, puis **Save**.
4. **Settings → Actions → General → Workflow permissions** : cocher **Read and write permissions**
   (le workflow commit `data/` et `docs/`).
5. **Actions → Mise à jour quotidienne → Run workflow** pour un premier lancement manuel (cocher
   « Rattrapage Google News » si `data/items.json` est vide).
6. Recommandé : ajouter l'adresse du dépôt dans `http.user_agent` (`config/settings.yaml`) ; Wikimedia
   demande qu'un robot puisse être identifié.

Le site est ensuite publié sur `https://<utilisateur>.github.io/<depot>/` et mis à jour chaque jour à
06:00 UTC (08:00 à Paris en été, 07:00 en hiver), en 3 à 5 minutes. GitHub peut retarder les exécutions
planifiées de quelques minutes, et suspend les workflows planifiés d'un dépôt sans activité pendant
60 jours (le commit quotidien suffit normalement à l'éviter).

La page porte une balise `noindex` pour ne pas être référencée par les moteurs de recherche, mais elle reste
accessible à toute personne qui a le lien.

## Évolution prévue (V1)

`src/enrich.py` définit l'interface `Enricher` ; la V0 utilise `NoopEnricher` (extrait du flux tel quel).
Un futur `ClaudeEnricher` pourra résumer en deux lignes et affiner le classement (contrats perdus,
événements politiques, pays concernés) via l'API Anthropic : il suffira de l'enregistrer dans
`ENRICHERS`, de passer `enricher: claude` dans `config/settings.yaml` et d'ajouter la clé API dans
**Settings → Secrets and variables → Actions** (jamais dans le dépôt).

## Sources non retenues

Testées le 2026-10-22 et écartées : Air & Cosmos (serveur en erreur 521), OTAN, Ministère des Armées, CNES,
EDA (aucun flux fonctionnel trouvé), Euractiv, Les Échos, GPS World, Esri, defense-aerospace.com
(accès refusé aux robots, HTTP 403), Meta-defense, Directions Magazine, Parabolic Arc (flux supprimés).
Via Satellite répond par intermittence (HTTP 403). Ces acteurs restent couverts via Google News.
