#!/bin/bash
# Retente la création d'une instance Oracle Cloud "Always Free" (VM.Standard.A1.Flex,
# ARM Ampere) jusqu'à ce que la capacité soit disponible.
#
# Pourquoi : les shapes ARM A1.Flex du Always Free Tier sont très demandées et
# Oracle répond souvent "Out of host capacity" pendant des heures, voire des jours,
# selon la région. Ce script retente régulièrement jusqu'au succès, en respectant
# deux limites différentes renvoyées par l'API :
#   - capacité saturée (racine du problème)       -> pause courte (quelques minutes)
#   - 429 "Too Many Requests" (on a trop insisté)  -> pause longue (10-15 min)
# Toute autre erreur arrête le script (pas la peine de boucler sur une vraie erreur
# de configuration, ex: mauvais shape, sous-réseau invalide, etc.)
#
# Prérequis :
#   - OCI CLI installé et configuré (~/.oci/config + clé API), voir README.md
#   - Un VCN + sous-réseau public déjà créés (le script ne les crée pas)
#   - Les variables ci-dessous renseignées (ou via un fichier config.env, voir
#     config.example.env)

set -u

# --- Config : à copier dans config.env (jamais commité, voir .gitignore) ---
CONFIG_FILE="${CONFIG_FILE:-$(dirname "$0")/config.env}"
if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

: "${OCI_BIN:=oci}"                 # chemin vers l'exécutable oci (ou juste "oci" si dans le PATH)
: "${COMPARTMENT_ID:?Renseigne COMPARTMENT_ID (OCID du compartiment ou de la tenancy)}"
: "${AVAILABILITY_DOMAIN:?Renseigne AVAILABILITY_DOMAIN (ex: xxxx:EU-PARIS-1-AD-1)}"
: "${IMAGE_ID:?Renseigne IMAGE_ID (OCID de l'image, ex: Ubuntu 24.04 aarch64)}"
: "${SUBNET_ID:?Renseigne SUBNET_ID (OCID du sous-réseau public)}"
: "${SSH_PUBLIC_KEY:?Renseigne SSH_PUBLIC_KEY (contenu de ta clé publique ssh-ed25519/rsa)}"
: "${DISPLAY_NAME:=mon-instance-a1}"
: "${OCPUS:=4}"                     # max gratuit : 4 OCPU au total (toutes instances A1 confondues)
: "${MEMORY_GB:=24}"                # max gratuit : 24 Go au total
: "${BOOT_GB:=100}"                 # max gratuit : 200 Go au total (boot + block volumes)
: "${LOG:=$(dirname "$0")/retry_instance.log}"
: "${MIN_SLEEP_CAPACITY:=240}"      # secondes, borne basse de la pause après "out of capacity"
: "${MAX_SLEEP_CAPACITY:=420}"      # borne haute
: "${MIN_SLEEP_RATE_LIMIT:=600}"    # pause après un 429 (10 min)
: "${MAX_SLEEP_RATE_LIMIT:=900}"    # (15 min)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== Démarrage du script de retry ==="

attempt=0
while true; do
  attempt=$((attempt + 1))
  log "Tentative #$attempt..."

  OUT=$("$OCI_BIN" compute instance launch \
    --compartment-id "$COMPARTMENT_ID" \
    --availability-domain "$AVAILABILITY_DOMAIN" \
    --display-name "$DISPLAY_NAME" \
    --image-id "$IMAGE_ID" \
    --shape "VM.Standard.A1.Flex" \
    --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}" \
    --subnet-id "$SUBNET_ID" \
    --assign-public-ip true \
    --boot-volume-size-in-gbs "$BOOT_GB" \
    --metadata "{\"ssh_authorized_keys\":\"$SSH_PUBLIC_KEY\"}" \
    --no-retry \
    --wait-for-state RUNNING \
    --max-wait-seconds 300 2>&1)

  if echo "$OUT" | grep -q '"lifecycle-state": "RUNNING"'; then
    log "SUCCÈS ! Instance créée et RUNNING."
    echo "$OUT" >>"$LOG"
    INSTANCE_ID=$(echo "$OUT" | grep '"id"' | head -1 | sed -E 's/.*"id": "([^"]+)".*/\1/')
    IP=$("$OCI_BIN" compute instance list-vnics --instance-id "$INSTANCE_ID" \
      --query "data[0].\"public-ip\"" --raw-output --no-retry 2>&1)
    log "IP PUBLIQUE : $IP"
    break
  fi

  if echo "$OUT" | grep -qiE "out of (host )?capacity"; then
    log "Pas de capacité disponible, nouvelle tentative dans quelques minutes."
    SLEEP=$((MIN_SLEEP_CAPACITY + RANDOM % (MAX_SLEEP_CAPACITY - MIN_SLEEP_CAPACITY + 1)))
  elif echo "$OUT" | grep -qiE "toomanyrequests|too many requests|\"status\": 429"; then
    log "Limitation de débit (429) côté Oracle : on ralentit nettement pour ne pas insister."
    SLEEP=$((MIN_SLEEP_RATE_LIMIT + RANDOM % (MAX_SLEEP_RATE_LIMIT - MIN_SLEEP_RATE_LIMIT + 1)))
  else
    log "Erreur inattendue (pas liée à la capacité ni au débit) :"
    echo "$OUT" | tee -a "$LOG"
    log "Arrêt du script pour éviter de boucler sur une vraie erreur de configuration."
    break
  fi

  log "Pause de ${SLEEP}s avant la prochaine tentative."
  sleep "$SLEEP"
done

log "=== Script terminé ==="
