"""Exemples : depuis la racine du dépôt, lancer python3 -m elasticsearch.test.

Dépendance : python3 -m pip install "httpx>=0.27,<1" "pydantic>=2,<3"
Configurer ELASTIC_EMEA_URL, ELASTIC_APAC_URL, ELASTIC_AMER_URL et
ELASTIC_USER / ELASTIC_PASSWORD (ou adapter les Cluster ci-dessous).
"""
import asyncio
import json
import os

from .models import Cluster, Filters
from .service import SearchService


def create_service():
    clusters = {
        region: Cluster(
            url=os.environ[f'ELASTIC_{region}_URL'],
            user=os.getenv('ELASTIC_USER'),
            password=os.getenv('ELASTIC_PASSWORD'),
            ca_file=os.getenv('ELASTIC_CA_FILE'),
        ) for region in ('EMEA', 'APAC', 'AMER')
    }
    return SearchService(clusters, index='logs-*', policy_field='policy.keyword')


def example_sync(service):
    # Une région, filtres optionnels et attributs choisis.
    return service.search(
        filters=Filters(policy='backup', region='EMEA',
                        starttime='01-10-2026', endtime='06-10-2026'),
        fields=['@timestamp', 'hostname', 'policy', 'content'],
        limit=100,
    )


async def example_async(service):
    # Pas de région : les trois ELK en parallèle. Timestamps Unix en secondes.
    return await service.search_async(
        filters=Filters(starttime=1790812800, endtime=1791244800, text='timeout'),
        fields=['@timestamp', 'content'],
    )


if __name__ == '__main__':
    service = create_service()
    print(json.dumps(example_sync(service), indent=2, ensure_ascii=False))
    # Décommenter uniquement l'exemple souhaité :
    # print(json.dumps(asyncio.run(example_async(service)), indent=2, ensure_ascii=False))
    # result = service.search()  # Trois régions, aucun filtre, tous les attributs.
    # result = service.search(Filters(region='APAC'), fields=['hostname', 'policy'])
