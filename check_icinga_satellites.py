#!/usr/bin/env python3
"""Contrôle séquentiel des satellites depuis le master Linux.

Remplacer l'import SshClient et les paramètres dans main().
Commandes distantes : systemctl, awk, cat, sleep et df (pas de Python requis).
"""
import math
import sys

from votre_module_ssh import SshClient  # À remplacer par votre module.

CODES = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "UNKNOWN": 3}
# Une panne confirmée prime sur un contrôle indisponible.
PRIORITE = {"OK": 0, "WARNING": 1, "UNKNOWN": 2, "CRITICAL": 3}
WARNING, CRITICAL = 80, 95  # Pourcentages, seuils inclusifs.

COMMANDES = {
    "ICINGA": "systemctl show icinga2.service --property=ActiveState --value",
    # Utilisation moyenne de tous les CPU sur 2 secondes, hors iowait.
    "CPU": """{ cat /proc/stat; sleep 2; cat /proc/stat; } | awk '
        /^cpu / {
            total=0; for(i=2;i<=9;i++) total+=$i;
            idle=$5+$6;
            if(n++==0) {t=total; d=idle}
            else if(total>t) {print 100*(1-(idle-d)/(total-t)); ok=1}
        }
        END {if(!ok) exit 1}'""",
    "RAM": """awk '
        /^MemTotal:/ {t=$2}
        /^MemAvailable:/ {a=$2; found=1}
        END {if(t>0 && found) print 100*(1-a/t); else exit 1}' /proc/meminfo""",
    "DISQUE": "LC_ALL=C df -P /var/lib/icinga2",
    "INODES": "LC_ALL=C df -Pi /var/lib/icinga2",
}


def statut_final(statuts):
    return max(statuts, key=PRIORITE.get, default="UNKNOWN")


def executer(client, commande):
    result = client.run(commande)
    # Selon votre client, result peut être un dictionnaire ou un objet.
    if isinstance(result, dict):
        stdout, rc, stderr = result["stdout"], result["rc"], result["stderr"]
    else:
        stdout, rc, stderr = result.stdout, result.rc, result.stderr
    if rc != 0:
        raise RuntimeError(stderr or f"commande échouée (rc={rc})")
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8")
    return stdout.strip()


def verifier_satellite(satellite, user, port, options):
    try:
        client = SshClient(satellite["hostname"], user, port, options)
    except Exception as exc:
        return "UNKNOWN", f"connexion SSH impossible : {exc}"

    statuts, details = [], []
    for nom, commande in COMMANDES.items():
        try:
            sortie = executer(client, commande)  # Une commande à la fois.
            if nom == "ICINGA":
                if not sortie:
                    raise ValueError("état du service absent")
                statut = "OK" if sortie == "active" else "CRITICAL"
                valeur = sortie
            else:
                # df : pourcentage dans l'avant-dernière colonne.
                valeur = (sortie.splitlines()[-1].split()[-2].rstrip("%")
                          if nom in ("DISQUE", "INODES") else sortie)
                pourcentage = float(valeur)
                if not math.isfinite(pourcentage) or not 0 <= pourcentage <= 100:
                    raise ValueError("pourcentage invalide")
                statut = ("CRITICAL" if pourcentage >= CRITICAL else
                          "WARNING" if pourcentage >= WARNING else "OK")
                valeur = f"{pourcentage:.1f}%"
            details.append(f"{nom}={valeur} ({statut})")
        except Exception as exc:
            statut = "UNKNOWN"
            details.append(f"{nom}=UNKNOWN ({exc})")
        statuts.append(statut)
    return statut_final(statuts), "; ".join(details)


def main():
    user, port = "monitoring", 22
    options = {}  # Vos options SshClient, avec timeouts connexion/commande.
    satellites = [
        {"name": "S1", "hostname": "satellite01.example.net", "region": "EU"},
        {"name": "S2", "hostname": "satellite02.example.net", "region": "EU"},
        {"name": "S3", "hostname": "satellite03.example.net", "region": "US"},
        {"name": "S4", "hostname": "satellite04.example.net", "region": "US"},
        {"name": "S5", "hostname": "satellite05.example.net", "region": "APAC"},
        {"name": "S6", "hostname": "satellite06.example.net", "region": "APAC"},
    ]
    statuts, rapports = [], []
    for satellite in satellites:
        statut, detail = verifier_satellite(satellite, user, port, options)
        statuts.append(statut)
        rapports.append(
            f"{satellite['name']} ({satellite['hostname']}, {satellite['region']})"
            f" - {statut} : {detail}"
        )

    final = statut_final(statuts)
    # Résumé en première ligne pour Icinga, détails des satellites ensuite.
    bilan = ", ".join(f"{statuts.count(s)} {s}" for s in CODES)
    print(f"ICINGA {final} - {bilan}")
    for rapport in rapports:
        print(" ".join(rapport.split()).replace("|", "/"))
    return CODES[final]


if __name__ == "__main__":
    sys.exit(main())
