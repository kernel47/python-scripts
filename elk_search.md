# Recherche ELK — Python 3.9+ (dont 3.12)

`elk_search.py` est indépendant des scripts Icinga. Il interroge directement
**Elasticsearch**, pas l'URL du dashboard Kibana, via `POST /<index>/_search`.
Cette requête est une recherche en lecture seule.

## Installation et connexion

```bash
python3.12 -m pip install httpx
export ELASTIC_EMEA_URL='https://elastic-emea.exemple.com:9200'
export ELASTIC_APAC_URL='https://elastic-apac.exemple.com:9200'
export ELASTIC_AMER_URL='https://elastic-amer.exemple.com:9200'
export ELASTIC_USER='lecture_logs'
read -r -s -p 'Mot de passe Elasticsearch : ' ELASTIC_PASSWORD
export ELASTIC_PASSWORD
```

Ou définir `ELASTIC_API_KEY` (valeur encodée fournie par Elasticsearch) à la place
du couple utilisateur/mot de passe. Ne pas définir les deux méthodes ensemble.
Sans variables d'authentification, une connexion anonyme est tentée.
TLS est vérifié ; utiliser `--ca-file /chemin/ca.pem` pour une CA interne.
HTTP est accepté si explicitement fourni, mais ne chiffre pas les credentials.
Aucune désactivation TLS implicite, aucun proxy d'environnement, aucune redirection.

## Exemple synchrone sur les trois régions

```bash
python3.12 elk_search.py \
  --index 'logs-*' \
  --mode sync \
  --fields '@timestamp' host.name label content \
  --from 01-10-2026 --to 06-10-2026 \
  --timezone Europe/Paris \
  --label EMEA \
  --text 'connection failed' \
  --limit 100
```

Même recherche asynchrone : remplacer `--mode sync` par `--mode async`.
Sans `--region`, les trois clusters sont interrogés en parallèle.
Le mode sync est bloquant pour son appelant et utilise trois threads au maximum.
Le mode async utilise réellement `httpx.AsyncClient` et `await` ; il n'est pas une
simulation par thread et n'accélère pas à lui seul une requête unique.

## Dates

- Champ date par défaut : `@timestamp`, modifiable par `--date-field date`.
- `jj-mm-yyyy` : début à minuit ; la date de fin inclut toute la journée, en
  utilisant une borne exclusive au début du jour suivant (changements d'heure inclus).
- Fuseau des dates civiles : UTC par défaut, configurable avec `--timezone Europe/Paris`.
- Timestamp Unix : secondes par défaut, millisecondes avec `--timestamp-unit milliseconds`.
  Il représente un instant UTC ; la borne de fin timestamp est inclusive.
- Les secondes décimales sont acceptées à précision milliseconde. Les bornes
  peuvent être omises séparément, ou toutes deux pour ne pas filtrer les dates.

```bash
python3.12 elk_search.py --index logs \
  --from 1700000000000 --to 1700086400000 \
  --timestamp-unit milliseconds --fields '@timestamp' content
```

Le champ Elasticsearch doit être mappé **date** (ou date_nanos), même si sa source
contient une date française ou un nombre. Un champ `keyword` ou `long` contenant
une date nécessite une adaptation du mapping ou du script. Les bornes sont
transmises avec `format: epoch_millis`.

## Labels et tableau content

Les filtres date, label et contenu se combinent avec **ET**.

- `--label EMEA --label APAC` : label égal à EMEA **OU** APAC.
- Champ label : `label.keyword` par défaut. Utiliser `--label-field label` si `label`
  est déjà de type keyword. Un sous-champ `.keyword` n'existe pas systématiquement.
- `content` est supposé être un tableau de strings, par exemple
  `["connection failed to server", "retry scheduled"]`. Pas besoin de requête nested.
- `--text 'connection failed'` : phrase analysée (`match_phrase`) par défaut,
  pas une recherche arbitraire de sous-chaîne. Le comportement dépend de l'analyseur.
- `--text-mode words` : tous les mots de chaque texte doivent correspondre ; ils
  peuvent provenir de plusieurs éléments du tableau.
- `--text-mode exact --content-field content.keyword` : égalité d'un élément
  entier sur un champ keyword. Respect de la casse selon le normalizer du mapping.
- Plusieurs `--text` sont combinés avec ET ; utiliser `--text-operator any` pour OU.
  Avec ET, deux textes distincts peuvent correspondre à deux éléments différents.
- Les éléments non correspondants du tableau restent dans le document retourné :
  le filtre sélectionne les documents, il ne découpe pas leur tableau `content`.

## Attributs, résultat et limites

`--fields` sélectionne les attributs de `_source` (chemins pointés et motifs pris
en charge par Elasticsearch). Les objets et tableaux restent structurés.
Le sous-champ indexé `label.keyword` n'est généralement pas présent dans `_source` :
retourner `label`, tout en filtrant sur `label.keyword`.

La sortie stdout est exclusivement JSON, avec `status`, `complete`, `total`,
`total_available`, `returned`, `truncated`, `regions` et `documents`.
Chaque région contient son résultat ou son erreur. Chaque document de la liste
agrégée contient un champ `region` (EMEA/APAC/AMER), ainsi que `index`, `id` et
`attributes`. Les documents identiques de deux clusters sont conservés séparément.

Les résultats sont triés par date décroissante. Sans valeur de tri unique, l'ordre
entre dates identiques n'est pas garanti. La limite est 100 **par région** par défaut, jusqu'à
10 000 (la fenêtre maximale configurée côté serveur peut être plus petite).
**Ce script est une recherche bornée, pas un export intégral paginé** : `truncated`
signale explicitement que d'autres résultats existent. `_source` doit être activé.
Un index vide renvoie une liste vide. Un mapping incompatible, un index absent,
un timeout serveur ou un échec de shard est une erreur, pas une recherche vide.
`--timeout 30` configure les délais réseau HTTPX par phase, pas une échéance globale.
Erreurs de configuration globales sur stderr. Erreurs régionales dans le JSON stdout,
avec conservation des régions réussies. Code retour 1 si une région échoue ;
réussite 0, interruption utilisateur 130.
Les erreurs d'usage argparse affichent l'aide standard et renvoient 2.

Pour inspecter la requête sans réseau ni credentials :

```bash
python3.12 elk_search.py --index logs --label EMEA --text timeout --dry-run
```

## Utilisation depuis Python

```python
from elk_search import SearchOptions, search_sync, search_async

options = SearchOptions(
    index="logs-*", fields=["@timestamp", "label", "content"],
    start="01-10-2026", end="06-10-2026",
    labels=["EMEA"], texts=["timeout"],
)
result = search_sync(options, "https://elastic.exemple.com:9200")

# Dans une coroutine existante :
# result = await search_async(options, "https://elastic.exemple.com:9200")
```

Les credentials proviennent des mêmes variables d'environnement dans les deux modes.
Les fonctions libèrent leurs connexions à la fin de chaque appel.

## Validation et références

```bash
python3.12 -m unittest -v test_elk_search.py
```

Tests avec transport HTTP simulé, sans accès à un cluster de production.
La version de votre cluster et son mapping restent à confirmer. Les API utilisées
sont les API REST classiques d'Elasticsearch 7/8/9 ; aucun client Python spécifique
à une version majeure n'est nécessaire.

- [Filtres de date](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-range-query)
- [Tableaux Elasticsearch](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/array)
- [Égalité exacte et champ keyword](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-term-query)
- [Sélection des attributs](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/retrieve-selected-fields)
- [Client asynchrone HTTPX](https://www.python-httpx.org/async/)


## Sélection régionale et exécution parallèle

```bash
# Les trois ELK en parallèle (async)
python3.12 elk_search.py --index 'logs-*' --mode async --text timeout

# Uniquement EMEA
python3.12 elk_search.py --index 'logs-*' --region EMEA --text timeout

# Uniquement APAC avec une URL explicite
python3.12 elk_search.py --index 'logs-*' --region APAC --url https://apac.exemple.com:9200
```

Les noms de régions sont insensibles à la casse en CLI. La structure `ELK_REGIONS`
en tête du script permet aussi de configurer les URL directement. Les variables
`ELASTIC_EMEA_URL`, `ELASTIC_APAC_URL`, `ELASTIC_AMER_URL` priment sur cette structure.
`--url` nécessite `--region` ; l'ancienne variable `ELASTIC_URL` n'est plus utilisée,
pour éviter d'interroger accidentellement trois fois le même serveur.
Une URL manquante produit une erreur pour sa région, sans ignorer celle-ci.

Les credentials globaux restent utilisables pour les trois clusters. Pour des
comptes différents, définir `ELASTIC_EMEA_USER` et `ELASTIC_EMEA_PASSWORD`, ou
`ELASTIC_EMEA_API_KEY` (mêmes noms pour APAC et AMER). Dès qu'une variable de
credentials régionale est présente, le groupe global entier est remplacé : aucun
mélange entre le mot de passe global et l'utilisateur régional.

CA : `--ca-file` prime, puis `ELASTIC_<REGION>_CA_FILE`, puis `ELASTIC_CA_FILE`,
puis les CA système. Les variables globales sont des paramètres explicitement
partagés ; les credentials régionaux ne sont pas transmis aux autres régions.

Le même index, les mêmes filtres et la même limite sont appliqués à chaque ELK.
`--limit 100` peut donc retourner jusqu'à 300 documents. La liste agrégée est
regroupée par EMEA, APAC, AMER ; le tri chronologique est propre à chaque cluster,
ce n'est pas un tri chronologique global.

Si un cluster échoue : `status: PARTIAL`, `complete: false`, `total: null` et
`total_available` contient la somme des régions réussies. Si tous échouent :
`status: ERROR`. Les résultats disponibles restent dans `documents`.
`truncated` désigne uniquement la limite sur les régions réussies ; consulter
`complete` pour savoir si tous les clusters ont répondu.

Depuis Python : `search_regions_sync(options, selected_targets(None))` ou
`await search_regions_async(options, selected_targets("EMEA"))` (fonctions à importer
avec `selected_targets`). Les fonctions bas niveau `search_sync` et `search_async`
restent disponibles pour une URL explicite.
