"""Service de recherche : sync ou async, un cluster ou trois en parallèle."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import math
import re
import ssl
from urllib.parse import quote, urlsplit

import httpx

from .models import Cluster, Filters
from .parser import build_query, parse_response


class SearchService:
    def __init__(self, clusters: dict[str, Cluster], index: str, *,
                 date_field='@timestamp', policy_field='policy.keyword',
                 timezone='UTC', timestamp_unit='seconds', timeout=30):
        if not re.fullmatch(r'[A-Za-z0-9_.*,-]+', index) or index in ('.', '..'):
            raise ValueError('Index invalide')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Timeout positif requis')
        if set(clusters) != {'EMEA', 'APAC', 'AMER'}:
            raise ValueError('Configurer les trois régions EMEA, APAC et AMER')
        self.clusters, self.index, self.timeout = clusters, index, timeout
        self.query_options = dict(date_field=date_field, policy_field=policy_field,
                                  timezone=timezone, timestamp_unit=timestamp_unit)

    def _prepare(self, filters, fields, limit):
        filters = filters or Filters()
        region = filters.region.upper() if filters.region else None
        if region and region not in self.clusters:
            raise ValueError('region : EMEA, APAC ou AMER')
        return ([region] if region else list(self.clusters),
                build_query(filters, fields, limit, **self.query_options))

    def _connection(self, region):
        cluster = self.clusters[region]
        url = urlsplit(cluster.url)
        if (url.scheme not in ('https', 'http') or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('URL invalide')
        if cluster.api_key and (cluster.user or cluster.password):
            raise ValueError('Choisir API key ou user/password')
        if bool(cluster.user) != bool(cluster.password):
            raise ValueError('User et password requis ensemble')
        return dict(base_url=cluster.url.rstrip('/') + '/', timeout=self.timeout,
                    verify=ssl.create_default_context(cafile=cluster.ca_file), trust_env=False,
                    follow_redirects=False, auth=(cluster.user, cluster.password) if cluster.user else None,
                    headers={'Authorization': 'ApiKey ' + cluster.api_key} if cluster.api_key else {})

    def _sync(self, region, query):
        try:
            with httpx.Client(**self._connection(region)) as client:
                response = client.post(quote(self.index, safe='*,-_') + '/_search', json=query,
                                       params={'allow_partial_search_results': 'false'})
            return parse_response(response, region)
        except (httpx.RequestError, ValueError, KeyError, TypeError, OSError):
            return {'error': 'Échec recherche : vérifier URL, accès, TLS et mapping'}

    async def _async(self, region, query):
        try:
            async with httpx.AsyncClient(**self._connection(region)) as client:
                response = await client.post(quote(self.index, safe='*,-_') + '/_search', json=query,
                                             params={'allow_partial_search_results': 'false'})
            return parse_response(response, region)
        except (httpx.RequestError, ValueError, KeyError, TypeError, OSError):
            return {'error': 'Échec recherche : vérifier URL, accès, TLS et mapping'}

    @staticmethod
    def _merge(regions, results):
        successes = [r for r in results if 'error' not in r]
        complete = len(successes) == len(results)
        return {'complete': complete, 'total': sum(r['total'] for r in successes) if complete else None,
                'regions': dict(zip(regions, results)),
                'documents': [d for r in successes for d in r['documents']]}

    def search(self, filters: Filters = None, fields: list[str] = None, limit=100):
        regions, query = self._prepare(filters, fields, limit)
        with ThreadPoolExecutor(max_workers=len(regions)) as pool:
            results = list(pool.map(lambda r: self._sync(r, query), regions))
        return self._merge(regions, results)

    async def search_async(self, filters: Filters = None, fields: list[str] = None, limit=100):
        regions, query = self._prepare(filters, fields, limit)
        results = await asyncio.gather(*(self._async(r, query) for r in regions))
        return self._merge(regions, results)
