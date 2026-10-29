# oracle-a1-retry

Obtenir une instance **Always Free** `VM.Standard.A1.Flex` (ARM, jusqu'à 4
OCPU / 24 Go de RAM gratuits) sur Oracle Cloud, quand la console répond sans
arrêt **"Out of host capacity"**.

C'est un problème très courant : les shapes ARM gratuites sont tellement
demandées que la capacité peut rester saturée pendant des heures, voire des
jours, selon la région. Ce dépôt automatise le "essaie, échoue, réessaie plus
tard" avec un rythme qui ne se fait pas limiter par l'API Oracle, plus un
petit tableau de bord pour suivre ça sans avoir à lire les journaux.

## Ce qu'il y a dedans

- **`setup_network.sh`** — crée le réseau minimal (VCN, passerelle internet,
  règles de sécurité 22/80/443, sous-réseau public). À lancer une seule fois :
  ce n'est pas limité par la capacité, ça passe toujours.
- **`retry_instance.sh`** — boucle de création d'instance. Distingue deux cas
  d'échec et adapte la pause :
  - `Out of host capacity` → nouvelle tentative dans 4 à 7 minutes
  - `429 Too Many Requests` (on a insisté trop vite) → pause de 10 à 15 minutes
  - toute autre erreur → arrêt immédiat (pas la peine de boucler sur une
    vraie erreur de config)
- **`dashboard/server.py`** — petit serveur local (stdlib Python seulement,
  aucune dépendance) qui lit le journal et sert une page HTML qui se
  rafraîchit toute seule : statut en cours, prochaine tentative, historique,
  IP publique dès que c'est bon.
- **`windows/fix_antivirus_tls.ps1`** — corrige une erreur très spécifique
  rencontrée sous Windows avec un antivirus qui inspecte le trafic HTTPS
  (Avast, AVG, Kaspersky, Bitdefender...), voir plus bas.

## Prérequis

1. [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm)
   installé et configuré (`oci setup config`, ou clé API créée depuis la
   console : Profil → My profile → API keys → Add API key).
2. Un compte Oracle Cloud avec le Always Free Tier actif.

## Utilisation

```bash
cp config.example.env config.env
# remplis config.env avec tes OCID (compartiment, AD, image, clé SSH...)

./setup_network.sh      # une seule fois
./retry_instance.sh      # tourne jusqu'au succès, laisse-le en arrière-plan

python dashboard/server.py   # puis ouvre http://127.0.0.1:8787
```

Récupérer les OCID nécessaires :

```bash
oci iam availability-domain list -c <tenancy_ocid>
oci compute image list -c <tenancy_ocid> \
  --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
  --shape "VM.Standard.A1.Flex"
```

(`SUBNET_ID` est affiché à la fin de `setup_network.sh`.)

## Le piège Windows + antivirus (Avast, AVG, Kaspersky, Bitdefender...)

Si le CLI OCI reste bloqué de longues secondes puis finit par afficher :

```
SSLException: "It looks like you are missing some additional certificates..."
```

ce n'est **pas** un problème réseau ni d'identifiants. Ces antivirus
installent un certificat racine local et interceptent le trafic HTTPS pour le
scanner (TLS inspection). Le CLI OCI (comme beaucoup d'outils Python) utilise
son propre magasin de certificats de confiance ("certifi") et ne connaît pas
ce certificat local, donc chaque requête échoue — en boucle, car le CLI
retente plusieurs fois avant d'afficher l'erreur (ajoute `--no-retry` à une
commande pour la voir immédiatement et confirmer ce diagnostic).

Correctif (sans désactiver la protection antivirus) :

```powershell
.\windows\fix_antivirus_tls.ps1
# puis, comme indiqué par le script :
[Environment]::SetEnvironmentVariable('REQUESTS_CA_BUNDLE', '<chemin affiché>', 'User')
```

Rouvre un terminal, et le CLI OCI fonctionne normalement.

## Pourquoi pas juste "réessayer toutes les minutes" ?

Parce qu'Oracle répond alors avec un `429 Too Many Requests`, ce qui n'aide
personne : ça gaspille des appels API pour rien et ça retarde encore plus le
succès. Le rythme utilisé ici (quelques minutes entre les tentatives,
ralenti fortement après un 429) est un compromis qui a bien fonctionné en
pratique, sans jamais déclencher de blocage de compte.

## Limites connues

- Testé sur la région `eu-paris-1` (un seul availability domain) ; dans une
  région à plusieurs AD, adapte le script pour les essayer tour à tour.
- Le script ne gère qu'un seul shape (`A1.Flex`) ; adapte `--shape` et
  `--shape-config` pour `E2.1.Micro` (l'autre shape Always Free, x86).
- Fonctionne avec Git Bash / WSL / Linux / macOS. Sous Windows, lance-le
  depuis Git Bash (pas PowerShell directement).

## Sécurité

Ne commite jamais `config.env`, tes clés `.pem`, ni le journal
`retry_instance.log` (il peut contenir des identifiants de ressources) —
tout est déjà dans `.gitignore`.

## Licence

MIT — fais-en ce que tu veux.
