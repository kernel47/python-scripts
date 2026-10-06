"""Service asynchrone : une région ou les trois en parallèle."""
import asyncio
import math
import re
import ssl
from urllib.parse import quote, urlsplit

import httpx

from .models import Cluster, Filters
from .parser import build_query, parse_response


class ElasticSaerchService:
    def __init__(self):
        self.clusters = None

    def init(self, regions: dict[str, Cluster], index: str, *,
                 date_field='@timestamp', policy_field='policy.keyword',
                 timezone='UTC', timestamp_unit='seconds', timeout=30):
        if not re.fullmatch(r'[A-Za-z0-9_.*,-]+', index) or index in ('.', '..'):
            raise ValueError('Index invalide')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Timeout positif requis')
        if set(regions) != {'EMEA', 'APAC', 'AMER'}:
            raise ValueError('Configurer les trois régions EMEA, APAC et AMER')
        self.clusters, self.index, self.timeout = dict(regions), index, timeout
        self.query_options = dict(date_field=date_field, policy_field=policy_field,
                                  timezone=timezone, timestamp_unit=timestamp_unit)

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

    async def _search_region(self, region, query):
        try:
            async with httpx.AsyncClient(**self._connection(region)) as client:
                response = await client.post(quote(self.index, safe='*,-_') + '/_search', json=query,
                                             params={'allow_partial_search_results': 'false'})
            return parse_response(response, region)
        except (httpx.RequestError, ValueError, KeyError, TypeError, OSError):
            return {'error': 'Échec recherche : vérifier URL, accès, TLS et mapping'}

    async def search(self, filters: Filters = None, fields: list[str] = None, limit=100):
        if self.clusters is None:
            raise RuntimeError('Appeler init(regions, index) avant search()')
        filters = filters or Filters()
        region = filters.region.upper() if filters.region else None
        if region and region not in self.clusters:
            raise ValueError('region : EMEA, APAC ou AMER')
        regions = [region] if region else list(self.clusters)
        query = build_query(filters, fields, limit, **self.query_options)
        results = await asyncio.gather(*(self._search_region(r, query) for r in regions))
        successes = [r for r in results if 'error' not in r]
        complete = len(successes) == len(results)
        return {'complete': complete, 'total': sum(r['total'] for r in successes) if complete else None,
                'regions': dict(zip(regions, results)),
                'documents': [d for r in successes for d in r['documents']]}


elasticsearch_service = ElasticSaerchService()
