#!/usr/bin/env python3
"""Recherche Elasticsearch sync/async. Python 3.9+, dépendance : httpx.

Voir elk_search.md pour les mappings, filtres et exemples.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
import re
import ssl
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo


# Renseigner ici les URL, ou définir ELASTIC_<REGION>_URL.
ELK_REGIONS = {"EMEA": "", "APAC": "", "AMER": ""}


class SearchError(Exception):
    """Erreur présentable sans exposer les credentials ni la réponse brute."""


@dataclass
class SearchOptions:
    index: str
    fields: List[str] = field(default_factory=lambda: ['@timestamp', 'label', 'content'])
    date_field: str = '@timestamp'
    label_field: str = 'label.keyword'
    content_field: str = 'content'
    start: Optional[str] = None
    end: Optional[str] = None
    timezone: str = 'UTC'
    timestamp_unit: str = 'seconds'
    labels: List[str] = field(default_factory=list)
    texts: List[str] = field(default_factory=list)
    text_mode: str = 'phrase'
    text_operator: str = 'all'
    limit: int = 100


def date_bound(value: str, tz_name: str, unit: str, upper: bool = False) -> Tuple[str, int]:
    """Journée locale entière pour jj-mm-yyyy ; instant inclusif pour timestamp."""
    if re.fullmatch(r'\d{2}-\d{2}-\d{4}', value):
        dt = datetime.strptime(value, '%d-%m-%Y').replace(tzinfo=ZoneInfo(tz_name))
        if upper:
            dt += timedelta(days=1)
        return ('lt' if upper else 'gte'), int(dt.timestamp() * 1000)
    try:
        number = Decimal(value)
        if unit == 'seconds':
            number *= 1000
        elif unit != 'milliseconds':
            raise ValueError('Unité timestamp invalide')
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError('Timestamp non fini ou précision inférieure à la milliseconde')
        milliseconds = int(number)
        # Refuse les valeurs hors plage calendaire exploitable.
        datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        return ('lte' if upper else 'gte'), milliseconds
    except (InvalidOperation, OverflowError, OSError) as exc:
        raise ValueError('Date attendue : jj-mm-yyyy ou timestamp Unix') from exc


def build_query(options: SearchOptions) -> Dict[str, Any]:
    if not re.fullmatch(r'[A-Za-z0-9_.*?,+\-]+', options.index) or options.index in ('.', '..'):
        raise ValueError('Nom ou motif index invalide')
    if not 1 <= options.limit <= 10000:
        raise ValueError('limit doit être compris entre 1 et 10000')
    if options.text_mode not in ('phrase', 'words', 'exact') or options.text_operator not in ('all', 'any'):
        raise ValueError('Mode texte invalide')
    if options.timestamp_unit not in ('seconds', 'milliseconds'):
        raise ValueError('Unité timestamp invalide')
    ZoneInfo(options.timezone)
    if not options.fields or any(not x.strip() for x in options.fields + [options.date_field, options.label_field, options.content_field]):
        raise ValueError('Les noms de champs ne peuvent pas être vides')
    if any(not x.strip() for x in options.labels + options.texts):
        raise ValueError('Les filtres label et texte ne peuvent pas être vides')
    filters: List[Dict[str, Any]] = []
    bounds: Dict[str, Any] = {}
    for value, upper in ((options.start, False), (options.end, True)):
        if value is not None:
            key, milliseconds = date_bound(value, options.timezone, options.timestamp_unit, upper)
            bounds[key] = milliseconds
    upper = bounds.get('lt', bounds.get('lte'))
    if 'gte' in bounds and upper is not None:
        if bounds['gte'] > upper or ('lt' in bounds and bounds['gte'] == upper):
            raise ValueError('La date de début doit précéder la fin')
    if bounds:
        filters.append({'range': {options.date_field: {**bounds, 'format': 'epoch_millis'}}})
    if options.labels:
        filters.append({'terms': {options.label_field: options.labels}})
    text_queries = []
    for text in options.texts:
        if options.text_mode == 'exact':
            clause = {'term': {options.content_field: text}}
        elif options.text_mode == 'words':
            clause = {'match': {options.content_field: {'query': text, 'operator': 'and'}}}
        else:
            clause = {'match_phrase': {options.content_field: text}}
        text_queries.append(clause)
    if text_queries:
        if options.text_operator == 'all':
            filters.extend(text_queries)
        else:
            filters.append({'bool': {'should': text_queries, 'minimum_should_match': 1}})
    return {'size': options.limit, 'track_total_hits': True,
            '_source': options.fields, 'query': {'bool': {'filter': filters}},
            'sort': [{options.date_field: {'order': 'desc', 'missing': '_last'}}]}


def client_options(url: str, ca_file: Optional[str] = None,
                   timeout: float = 30.0, region: Optional[str] = None) -> Dict[str, Any]:
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('URL Elasticsearch attendue : https://serveur:9200 (sans credentials ni chemin)')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout positif et fini requis')
    # Les credentials régionaux remplacent le groupe global entier, sans mélange.
    prefix = f'ELASTIC_{region}_' if region else 'ELASTIC_'
    suffixes = ('USER', 'PASSWORD', 'API_KEY')
    if region and not any(prefix + k in os.environ for k in suffixes):
        prefix = 'ELASTIC_'
    user, password, key = (os.getenv(prefix + k) for k in suffixes)
    if key and (user or password):
        raise ValueError('Choisir ELASTIC_API_KEY ou ELASTIC_USER/ELASTIC_PASSWORD')
    if bool(user) != bool(password):
        raise ValueError('ELASTIC_USER et ELASTIC_PASSWORD doivent être définis ensemble')
    headers = {'Accept': 'application/json'}
    if key:
        headers['Authorization'] = 'ApiKey ' + key
    return {'base_url': url.rstrip('/') + '/', 'timeout': timeout,
            'verify': ssl.create_default_context(cafile=ca_file),
            'headers': headers, 'auth': (user, password) if user else None,
            'follow_redirects': False, 'trust_env': False}


def search_path(options: SearchOptions) -> str:
    return quote(options.index, safe='*,-_') + '/_search'


def parse_response(response: Any) -> Dict[str, Any]:
    if response.status_code != 200:
        reasons = {400: 'requête ou mapping incompatible', 401: 'authentification refusée',
                   403: 'permissions insuffisantes', 404: 'index ou route introuvable',
                   429: 'cluster surchargé'}
        raise SearchError(f"Elasticsearch HTTP {response.status_code}: " + reasons.get(response.status_code, 'échec de recherche'))
    try:
        data = response.json()
        if not isinstance(data, dict) or type(data['timed_out']) is not bool:
            raise ValueError()
        if data['timed_out'] or data['_shards']['failed'] != 0:
            raise SearchError('Recherche incomplète : timeout serveur ou shards en échec')
        hits = data['hits']['hits']
        total = data['hits']['total']
        if not isinstance(hits, list) or not isinstance(total, dict) or type(total['value']) is not int:
            raise ValueError()
        if total['relation'] != 'eq':
            raise SearchError('Nombre total de résultats non exact')
        documents = []
        for hit in hits:
            if not isinstance(hit['_source'], dict):
                raise ValueError()
            documents.append({'index': hit['_index'], 'id': hit['_id'], 'attributes': hit['_source']})
        return {'total': total['value'], 'returned': len(documents),
                'truncated': total['value'] > len(documents), 'documents': documents}
    except (ValueError, TypeError, KeyError):
        raise SearchError('Réponse Elasticsearch invalide ou _source indisponible') from None


def load_httpx() -> Any:
    try:
        import httpx
        return httpx
    except ImportError:
        raise SearchError('Dépendance absente : python3 -m pip install httpx') from None


def search_sync(options: SearchOptions, url: str, ca_file: Optional[str] = None,
                timeout: float = 30.0, region: Optional[str] = None) -> Dict[str, Any]:
    body = build_query(options)
    httpx = load_httpx()
    try:
        with httpx.Client(**client_options(url, ca_file, timeout, region)) as client:
            response = client.post(search_path(options), json=body,
                                   params={'allow_partial_search_results': 'false'})
        return parse_response(response)
    except httpx.RequestError:
        raise SearchError('Connexion Elasticsearch impossible (réseau, TLS ou timeout)') from None


async def search_async(options: SearchOptions, url: str, ca_file: Optional[str] = None,
                       timeout: float = 30.0, region: Optional[str] = None) -> Dict[str, Any]:
    body = build_query(options)
    httpx = load_httpx()
    try:
        async with httpx.AsyncClient(**client_options(url, ca_file, timeout, region)) as client:
            response = await client.post(search_path(options), json=body,
                                         params={'allow_partial_search_results': 'false'})
        return parse_response(response)
    except httpx.RequestError:
        raise SearchError('Connexion Elasticsearch impossible (réseau, TLS ou timeout)') from None


def selected_targets(region: Optional[str], url: Optional[str] = None) -> Dict[str, str]:
    if region is not None and region not in ELK_REGIONS:
        raise ValueError('Région invalide')
    if url and region is None:
        raise ValueError('--url nécessite --region pour identifier le cluster')
    names = [region] if region else list(ELK_REGIONS)
    return {name: url or os.getenv(f'ELASTIC_{name}_URL', ELK_REGIONS[name]) for name in names}


def region_error(exc: Exception) -> Dict[str, Any]:
    return {'status': 'ERROR', 'error': str(exc) if isinstance(exc, SearchError)
            else 'Configuration régionale invalide (URL, credentials ou CA)'}


def regional_ca(region: str, ca_file: Optional[str]) -> Optional[str]:
    return ca_file or os.getenv(f'ELASTIC_{region}_CA_FILE') or os.getenv('ELASTIC_CA_FILE')


def aggregate_results(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    successful = [result for result in results.values() if result['status'] == 'OK']
    complete = len(successful) == len(results)
    total = sum(result['total'] for result in successful)
    documents = [dict(document, region=region)
                 for region, result in results.items() if result['status'] == 'OK'
                 for document in result['documents']]
    return {'status': 'OK' if complete else ('PARTIAL' if successful else 'ERROR'),
            'complete': complete, 'total': total if complete else None,
            'total_available': total, 'returned': len(documents),
            'truncated': any(result['truncated'] for result in successful),
            'regions': results, 'documents': documents}


def search_regions_sync(options: SearchOptions, targets: Dict[str, str],
                        ca_file: Optional[str] = None, timeout: float = 30.0) -> Dict[str, Any]:
    """Interface bloquante ; requêtes régionales concurrentes via trois threads max."""
    build_query(options)
    if not targets:
        raise ValueError('Aucune région sélectionnée')
    def worker(item: Tuple[str, str]) -> Dict[str, Any]:
        region, url = item
        try:
            if not url:
                raise SearchError(f'URL absente : définir ELASTIC_{region}_URL')
            return dict(search_sync(options, url, regional_ca(region, ca_file), timeout, region), status='OK')
        except (SearchError, ValueError, KeyError, OSError) as exc:
            return region_error(exc)
    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
        results = dict(zip(targets, pool.map(worker, targets.items())))
    return aggregate_results(results)


async def search_regions_async(options: SearchOptions, targets: Dict[str, str],
                               ca_file: Optional[str] = None, timeout: float = 30.0) -> Dict[str, Any]:
    """Les requêtes régionales démarrent ensemble avec asyncio.gather."""
    build_query(options)
    if not targets:
        raise ValueError('Aucune région sélectionnée')
    async def worker(region: str, url: str) -> Dict[str, Any]:
        try:
            if not url:
                raise SearchError(f'URL absente : définir ELASTIC_{region}_URL')
            return dict(await search_async(options, url, regional_ca(region, ca_file), timeout, region), status='OK')
        except (SearchError, ValueError, KeyError, OSError) as exc:
            return region_error(exc)
    values = await asyncio.gather(*(worker(region, url) for region, url in targets.items()))
    return aggregate_results(dict(zip(targets, values)))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region', type=str.upper, choices=list(ELK_REGIONS), help='Sans région : les trois ELK')
    parser.add_argument('--url', help='URL alternative pour --region uniquement')
    parser.add_argument('--index', required=True)
    parser.add_argument('--mode', choices=['sync', 'async'], default='sync')
    parser.add_argument('--fields', nargs='+', default=['@timestamp', 'label', 'content'])
    parser.add_argument('--date-field', default='@timestamp')
    parser.add_argument('--label-field', default='label.keyword')
    parser.add_argument('--content-field', default='content')
    parser.add_argument('--from', dest='start', help='jj-mm-yyyy ou timestamp Unix')
    parser.add_argument('--to', dest='end', help='jj-mm-yyyy (journée incluse) ou timestamp inclusif')
    parser.add_argument('--timezone', default='UTC')
    parser.add_argument('--timestamp-unit', choices=['seconds', 'milliseconds'], default='seconds')
    parser.add_argument('--label', dest='labels', action='append', default=[])
    parser.add_argument('--text', dest='texts', action='append', default=[])
    parser.add_argument('--text-mode', choices=['phrase', 'words', 'exact'], default='phrase')
    parser.add_argument('--text-operator', choices=['all', 'any'], default='all')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--ca-file')
    parser.add_argument('--dry-run', action='store_true', help='Afficher la requête sans contacter ELK')
    args = parser.parse_args(argv)
    try:
        options = SearchOptions(**{k: getattr(args, k) for k in SearchOptions.__dataclass_fields__})
        targets = selected_targets(args.region, args.url)
        if args.dry_run:
            result = {'regions': list(targets), 'query': build_query(options)}
        elif args.mode == 'async':
            result = asyncio.run(search_regions_async(options, targets, args.ca_file, args.timeout))
        else:
            result = search_regions_sync(options, targets, args.ca_file, args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if args.dry_run or result['complete'] else 1
    except (SearchError, ValueError, KeyError, OSError):
        # Pas de réponse serveur, URL ou exception brute : peut contenir des secrets.
        exc = sys.exc_info()[1]
        message = str(exc) if isinstance(exc, SearchError) else 'Configuration invalide : vérifier dates, champs, URL, fuseau et certificat'
        print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
