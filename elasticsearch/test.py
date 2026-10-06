"""Exemples : depuis la racine du dépôt, lancer python3 -m elasticsearch.test.

Dépendance : python3 -m pip install "httpx>=0.27,<1" "pydantic>=2,<3"
Configurer ELASTIC_EMEA_URL, ELASTIC_APAC_URL, ELASTIC_AMER_URL et
ELASTIC_USER / ELASTIC_PASSWORD (ou adapter les Cluster ci-dessous).
"""
import asyncio
import json
import os

from .models import Cluster, Filters
from .service import elasticsearch_service


def configure():
    clusters = {
        region: Cluster(
            url=os.environ[f'ELASTIC_{region}_URL'],
            user=os.getenv('ELASTIC_USER'),
            password=os.getenv('ELASTIC_PASSWORD'),
            ca_file=os.getenv('ELASTIC_CA_FILE'),
        ) for region in ('EMEA', 'APAC', 'AMER')
    }
    elasticsearch_service.init(regions=clusters, index='logs-*')


async def example_region():
    # Une région, filtres optionnels et attributs choisis.
    return await elasticsearch_service.search(
        filters=Filters(policy='backup', region='EMEA',
                        starttime='01-10-2026', endtime='06-10-2026'),
        fields=['@timestamp', 'hostname', 'policy', 'content'],
        limit=100,
    )


async def example_all_regions():
    # Pas de région : les trois ELK en parallèle. Timestamps Unix en secondes.
    return await elasticsearch_service.search(
        filters=Filters(starttime=1790812800, endtime=1791244800, text='timeout'),
        fields=['@timestamp', 'content'],
    )


async def main():
    configure()
    print(json.dumps(await example_region(), indent=2, ensure_ascii=False))
    # Autres exemples (décommenter selon le besoin) :
    # result = await example_all_regions()
    # result = await elasticsearch_service.search()  # Sans filtres, tous les attributs.
    # result = await elasticsearch_service.search(Filters(region='APAC'), fields=['policy'])


if __name__ == '__main__':
    asyncio.run(main())
