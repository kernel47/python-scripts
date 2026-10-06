# Supervision des satellites depuis le master

Le script complet est dans `icinga_satellite_health.py`. La configuration
`icinga_satellite_health.conf` contient le CheckCommand, le Service et l'ApiUser.
L'ancien script `check_icinga_satellites.py` n'est pas modifié.

## Architecture et API vérifiée

Deux requêtes HTTPS en lecture seule sont faites au master :
`GET /v1/objects/endpoints` et `GET /v1/objects/zones`.
Les objets sont lus dans `results[].name` et `results[].attrs`.
L'appartenance est vérifiée avec `Zone.endpoints` ; la connexion avec
`Endpoint.connected`, booléen runtime effectivement défini par Icinga.
Il reflète une connexion cluster dans les deux sens, y compris initiée par le satellite.
Aucun SSH ni appel à l'API des satellites n'est utilisé.

Le lag par satellite reprend la sémantique native de
`ApiListener::CalculateZoneLag` : si l'endpoint synchronise ou est déconnecté,
et que `remote_log_position` est non nul, prendre
`max(0, heure_du_master - remote_log_position)` ; sinon prendre zéro.
Le script doit donc tourner sur le master interrogé, qui partage l'horloge de ces
attributs. Si `syncing` ou `remote_log_position` est absent ou invalide, lag = null.
Il s'agit du retard du journal de réplication, pas d'une mesure de fraîcheur des
checks ni du temps écoulé depuis le dernier message. Un zéro natif ne garantit
pas que tous les services sont frais.

Le statut `/v1/status/ApiListener` expose aussi `status.api.conn_endpoints`,
`not_conn_endpoints` et `zones.<zone>.client_log_lag`. Cette dernière mesure est
agrégée par zone : le script ne l'attribue pas artificiellement à chacun des deux
satellites. Le check natif `cluster-zone` peut compléter cette supervision pour
l'état global des zones (sa perfdata `slave_lag` est également agrégée).

Sources officielles consultées :
- [REST API et permissions](https://icinga.com/docs/icinga-2/latest/doc/12-icinga2-api/)
- [Attributs Endpoint](https://github.com/Icinga/icinga2/blob/master/lib/remote/endpoint.ti)
- [Implémentation connected](https://github.com/Icinga/icinga2/blob/master/lib/remote/endpoint.cpp)
- [Calcul natif du lag et statistiques](https://github.com/Icinga/icinga2/blob/master/lib/remote/apilistener.cpp)
- [Check cluster-zone](https://github.com/Icinga/icinga2/blob/master/lib/methods/clusterzonechecktask.cpp)

Ces références pointent sur la branche courante ; les attributs sont validés à
l'exécution. Un champ de connectivité absent ne devient jamais implicitement false.
Le périmètre est celui demandé : un master et ses satellites directement reliés
au cluster. Les endpoints derrière un autre satellite nécessitent une supervision
sur leur parent ; une absence de lien direct au master ne mesure pas leur santé globale.

## Statuts

| Cluster | TCP | Lag | Statut |
|---|---|---|---|
| connecté | réussi, échoué ou désactivé | absent ou < 10s | OK |
| connecté | quelconque | >= 10s et < 30s | WARNING |
| connecté | quelconque | >= 30s | CRITICAL |
| déconnecté | réussi | quelconque | WARNING |
| déconnecté | échoué | quelconque | CRITICAL |
| déconnecté | désactivé | quelconque | CRITICAL |
| API inaccessible, objet/champ nécessaire absent ou erreur interne | quelconque | quelconque | UNKNOWN |

Sans TCP, la déconnexion cluster suffit pour CRITICAL ; la sortie précise que le
test TCP est désactivé. Les régions et le global utilisent CRITICAL > WARNING > OK
lorsque les observations sont complètes. En présence d'un UNKNOWN, UNKNOWN prime
pour signaler le caractère incomplet du contrôle ; les pannes connues restent
visibles dans les détails. Une panne API globale produit `regions: {}` et aucun
satellite fictivement CRITICAL.

## Installation sur le master Linux

Adapter d'abord les six entrées de `SATELLITES` dans le script, le Host
`icinga-master`, le FQDN et le secret dans le fichier `.conf`.
Le compte d'exécution est généralement `icinga:icinga` ; certaines distributions
utilisent `nagios`, à remplacer alors dans les commandes.

Depuis le dossier contenant les nouveaux fichiers, sur le master :

```bash
# Debian/Ubuntu ; utiliser le paquet équivalent de votre distribution.
sudo apt-get install python3-requests
sudo install -d -o root -g icinga -m 0750 /etc/icinga2/scripts
sudo install -o root -g icinga -m 0750 icinga_satellite_health.py /etc/icinga2/scripts/icinga_satellite_health.py
sudo install -o root -g icinga -m 0640 icinga_satellite_health.conf /etc/icinga2/conf.d/satellite-health.conf
sudo install -o root -g icinga -m 0640 /dev/null /etc/icinga2/satellite-health-credentials.json
sudoedit /etc/icinga2/satellite-health-credentials.json
```

Contenu du fichier de credentials (même secret que l'ApiUser) :

```json
{
  "user": "satellite-health",
  "password": "REMPLACER_PAR_UN_SECRET_LONG"
}
```

`root` possède les fichiers ; le groupe du daemon a uniquement lecture/exécution.
Ne pas rendre le script modifiable par le daemon. Garder les secrets en 0640.
Le fichier JSON est lu directement par le plugin : aucun mot de passe dans la
ligne de commande et aucun besoin de transmettre un environnement shell à Icinga.
Les variables `ICINGA_API_USER` et `ICINGA_API_PASSWORD`, si présentes, priment sur
le fichier ; `ICINGA_API_URL` est également accepté.

Placer la règle uniquement dans la configuration du master, sans
`command_endpoint` vers un satellite. Vérifier que `conf.d` est effectivement
inclus dans votre installation ; sinon utiliser le répertoire local inclus
approprié. Les objets Endpoint et Zone existants ne sont pas redéfinis.
L'API doit déjà être activée, comme dans l'architecture fournie.

TLS reste vérifié par défaut. Le FQDN de `--api-url` doit correspondre au certificat
et résoudre vers le master local. `https://localhost:5665` convient seulement si le
certificat couvre localhost. `--ca-file` permet de fournir la CA Icinga. La lecture
de cette CA doit être autorisée au compte icinga. `--insecure` ou `VERIFY_TLS=False`
désactive explicitement la vérification pour un environnement qui le nécessite.

## Validation et tests manuels

Après adaptation des fichiers :

```bash
sudo icinga2 daemon -C
# Recharger seulement si la validation réussit.
sudo systemctl reload icinga2

sudo -u icinga /usr/bin/python3 /etc/icinga2/scripts/icinga_satellite_health.py \
  --api-url https://icinga-master.domain:5665 \
  --ca-file /var/lib/icinga2/certs/ca.crt \
  --credentials-file /etc/icinga2/satellite-health-credentials.json
```

Ajouter `--json`, `--debug` ou `--no-tcp` à cette commande selon le besoin.
Le JSON reste seul sur stdout ; le debug va sur stderr, sans contenu des réponses,
identifiants, mots de passe ni valeurs d'exceptions non contrôlées.

Test par variables d'environnement, avec saisie masquée du secret (shell Bash) :

```bash
sudo -u icinga bash
export ICINGA_API_USER=satellite-health
read -r -s -p 'Mot de passe API : ' ICINGA_API_PASSWORD
export ICINGA_API_PASSWORD
python3 /etc/icinga2/scripts/icinga_satellite_health.py \
  --api-url https://icinga-master.domain:5665 \
  --ca-file /var/lib/icinga2/certs/ca.crt --json
# Le code de retour reste 0/1/2/3, même en JSON.
echo $?
unset ICINGA_API_PASSWORD
exit
```

Options supplémentaires : `--api-timeout 5`, `--timeout 50`,
`--lag-warning 10`, `--lag-critical 30` et `--tcp-timeout 3`.
Les appels requests ont un timeout connexion/lecture ; le délai global POSIX
borne aussi la résolution DNS et les essais successifs de plusieurs adresses.
Garder le timeout CheckCommand (60s) supérieur au délai global du plugin (50s).
Le TCP est séquentiel, mesure l'ouverture de socket avec `perf_counter`, sans
valider le protocole Icinga. Sa réussite ne prouve donc pas une connexion cluster.

Tests automatisés locaux, sans accès à la production :

```bash
python3 -m unittest -v test_icinga_satellite_health.py
```

## Exemples illustratifs

Extraits sans les métriques individuelles pour la lisibilité :

```text
OK - 6/6 satellites OK | ok=6 warning=0 critical=0 unknown=0
EMEA: OK
  emea-sat-01: OK - Cluster connected - tcp=12.000ms - lag=0.000s
  emea-sat-02: OK - Cluster connected, direct TCP unavailable - lag=0.000s
```

```text
WARNING - 5/6 satellites OK; apac-sat-01: Cluster connected, high cluster lag (12.000s) | ok=5 warning=1 critical=0 unknown=0
```

```text
CRITICAL - 5/6 satellites OK; apac-sat-02: Satellite disconnected from cluster and unreachable on TCP/5665 | ok=5 warning=0 critical=1 unknown=0
```

Les sorties réelles détaillent les trois régions et ajoutent les métriques disponibles.
Le label de perfdata utilise l'endpoint encodé en hexadécimal (`ep_<hex>_lag` et
`ep_<hex>_latency`) pour éviter les collisions entre noms et les caractères réservés.
Les valeurs inconnues ne sont pas inventées. Les seuils sont inclus dans les
perfdata lag ; le plugin applique les seuils inclusifs demandés (`>=`).

Aucun master Icinga n'est disponible dans l'environnement de développement : les
tests locaux couvrent les règles et les réponses simulées, pas une validation
réelle par `icinga2 daemon -C` ni un test TLS sur votre infrastructure.

## Affichage dans le dashboard

Les pastilles sont affichées par défaut, sans argument supplémentaire.
Les statuts global, régionaux et individuels affichent des pastilles Unicode :
🟢 OK, 🟡 WARNING, 🔴 CRITICAL, 🟣 UNKNOWN (erreur ou état indéterminé).
Le texte reste présent pour ne pas dépendre uniquement de la couleur.
Aucun code ANSI n'est injecté. Le rendu coloré des pastilles dépend du
navigateur et de ses polices emoji ; ce mode ne colore pas le texte lui-même.
Les codes de retour et les perfdata restent inchangés ; `--json` ignore ce mode.
La première ligne apparaît dans les listes ; les détails par région et satellite
apparaissent dans la fiche du service. Une seule exécution représente toujours
un seul service et un statut global.


### Tableaux régionaux

La sortie par défaut contient le résumé global en première ligne, suivi de trois
tableaux HTML (EMEA, APAC, AMER) dans la sortie longue. Colonnes : Hostname,
Statut, Cluster, TCP, Latence en ms et Lag en secondes. Le hostname provient du
champ `host` configuré ; il est aussi présent sous `hostname` en JSON.
Les diagnostics WARNING/CRITICAL/UNKNOWN apparaissent sous le tableau concerné.
`Non testé`, `Inconnu`, `—` et `N/D` distinguent les données absentes des valeurs zéro.
Les noms et diagnostics sont échappés HTML. Les perfdata restent uniquement sur
la première ligne. Aucun argument supplémentaire n'est nécessaire.

L'affichage en tableau nécessite que votre module Icinga Web rende le HTML de la
sortie des plugins ; s'il l'échappe, les balises seront visibles. Le rendu réel est
à vérifier dans la fiche du service sur votre dashboard. La liste des services
ne montre généralement que le résumé. Les anciennes sorties illustratives
ci-dessus décrivent les statuts ; les détails sont maintenant en tableaux.
