"""Construction de la requête et lecture de la réponse Elasticsearch."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .models import Filters


def date_bound(value, upper=False, timezone='UTC', timestamp_unit='seconds'):
    try:
        date = datetime.strptime(str(value), '%d-%m-%Y').replace(tzinfo=ZoneInfo(timezone))
    except ValueError:
        number = Decimal(str(value)) * (1000 if timestamp_unit == 'seconds' else 1)
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError('Timestamp invalide : précision maximale milliseconde')
        return ('lte' if upper else 'gte'), int(number)
    if upper:
        date += timedelta(days=1)
    return ('lt' if upper else 'gte'), int(date.timestamp() * 1000)


def build_query(filters: Filters, fields=None, limit=100, date_field='@timestamp',
                policy_field='policy.keyword', timezone='UTC', timestamp_unit='seconds'):
    if not 1 <= limit <= 10000:
        raise ValueError('limit doit être entre 1 et 10000')
    if timestamp_unit not in ('seconds', 'milliseconds'):
        raise ValueError('timestamp_unit : seconds ou milliseconds')
    ZoneInfo(timezone)
    clauses, bounds = [], {}
    for value, upper in ((filters.starttime, False), (filters.endtime, True)):
        if value is not None:
            key, date = date_bound(value, upper, timezone, timestamp_unit)
            bounds[key] = date
    end = bounds.get('lt', bounds.get('lte'))
    if 'gte' in bounds and end is not None:
        if bounds['gte'] > end or ('lt' in bounds and bounds['gte'] == end):
            raise ValueError('starttime doit précéder endtime')
    if bounds:
        clauses.append({'range': {date_field: {**bounds, 'format': 'epoch_millis'}}})
    if filters.policy is not None:
        clauses.append({'term': {policy_field: filters.policy}})
    if filters.text is not None:
        clauses.append({'match_phrase': {'content': filters.text}})
    return {'query': {'bool': {'filter': clauses}}, '_source': True if fields is None else fields,
            'size': limit, 'track_total_hits': True, 'sort': [{date_field: 'desc'}]}


def parse_response(response, region):
    if response.status_code != 200:
        raise ValueError(f'Elasticsearch HTTP {response.status_code}')
    data = response.json()
    if data['timed_out'] or data['_shards']['failed']:
        raise ValueError('Résultat incomplet : timeout ou shard en échec')
    total = data['hits']['total']
    if total['relation'] != 'eq':
        raise ValueError('Nombre de résultats non exact')
    documents = []
    for hit in data['hits']['hits']:
        if not isinstance(hit.get('_source'), dict):
            raise ValueError('_source absent ou invalide')
        documents.append({'region': region, 'index': hit['_index'], 'id': hit['_id'],
                          'attributes': hit['_source']})
    return {'total': total['value'], 'truncated': total['value'] > len(documents),
            'documents': documents}
