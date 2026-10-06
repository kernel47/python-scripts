# Recherche Elasticsearch simplifiée

Python 3.9+ ; dépendances : `python3 -m pip install "httpx>=0.27,<1" "pydantic>=2,<3"`.
L'ancien script CLI est remplacé par le dossier `elasticsearch` :

- `models.py` : `Cluster` et `Filters`.
- `parser.py` : filtres et lecture des réponses.
- `service.py` : accès asynchrone aux trois clusters.
- `test.py` : exemples d'utilisation exécutables avec `python3 -m elasticsearch.test`.
- `__init__.py` : exports pour les imports Python.

```python
from elasticsearch import Cluster, Filters, elasticsearch_service

elasticsearch_service.init(regions={
    'EMEA': Cluster(url='https://emea:9200'),
    'APAC': Cluster(url='https://apac:9200'),
    'AMER': Cluster(url='https://amer:9200'),
}, index='logs-*')

result = await elasticsearch_service.search(
    Filters(policy='backup', region='EMEA', starttime='01-10-2026', endtime='06-10-2026'),
    fields=['@timestamp', 'hostname', 'policy', 'content'],
)
# Sans région :
# result = await elasticsearch_service.search(Filters(policy="backup"))
```

Tous les filtres sont optionnels. Sans région, les trois ELK sont interrogés en
parallèle avec asyncio.gather. Sans `fields`, tous les attributs
_source sont retournés ; `fields=[]` retourne des attributs vides.
`limit=100` est une limite **par région**, pas un export paginé.
`policy` filtre par égalité sur `policy.keyword` ; adapter `policy_field='policy'`
si le champ est déjà keyword. La région sélectionne le serveur, pas un champ du document.
`text` recherche une phrase analysée dans le tableau de strings `content`.
Les filtres se combinent avec ET.

Dates : jj-mm-yyyy ou timestamps Unix en secondes ; pour des millisecondes,
configurer `timestamp_unit='milliseconds'` dans init(). Le fuseau par défaut
est UTC (`timezone='Europe/Paris'` possible). La date de fin civile inclut toute la
journée ; un timestamp de fin est inclusif. Le champ date par défaut `@timestamp`
doit être mappé date ; adapter `date_field` dans init() si nécessaire.

Chaque Cluster accepte `user`, `password`, `api_key` ou `ca_file`. Ne pas combiner
API key et user/password. TLS est vérifié ; l'URL cible Elasticsearch, pas Kibana.
HTTP explicite est accepté mais ne chiffre pas les credentials.
Le timeout réseau par phase vaut 30 secondes, configurable dans init().

Le résultat contient `complete`, `total`, `regions` et `documents`. Les documents
conservent `region`, `index`, `id` et `attributes`. Les régions en erreur ne font
pas perdre les autres résultats : `complete=False`, `total=None`, détail dans
`regions`. Chaque région réussie expose `total` et `truncated`. Le tri est par date
décroissante dans chaque région, sans tri global ni déduplication inter-clusters.
Les fonctions lèvent une exception pour un filtre global invalide.

Le dossier local porte le nom demandé `elasticsearch` : il utilise HTTPX, pas le
SDK officiel homonyme. Exécuter depuis la racine de ce dépôt dans un environnement
dédié pour éviter une collision avec un SDK Elasticsearch déjà installé.

Les modèles `Cluster` et `Filters` utilisent `pydantic.BaseModel` (version 2).
Les paramètres sont nommés, par exemple `Cluster(url="https://emea:9200")`.
Les champs inconnus sont refusés ; password et api_key sont exclus du repr et
de la sérialisation `model_dump()`.

`elasticsearch_service` est une instance exportée de `ElasticSaerchService`.
Appeler `init(regions, index)` une fois au démarrage, avant les recherches.
`init` configure uniquement le service et ne nécessite pas `await`.
`search` est asynchrone : utiliser `await` dans une coroutine, ou `asyncio.run`
depuis un programme synchrone (voir `test.py`). Ne pas reconfigurer l’instance
pendant des recherches actives. L’ancienne API SearchService/search_async est remplacée.
