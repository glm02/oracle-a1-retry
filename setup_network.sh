#!/bin/bash
# Crée le réseau minimal nécessaire à une instance publique : VCN, passerelle
# internet, route par défaut, règles de sécurité (22/80/443 + ICMP) et un
# sous-réseau public. À lancer une seule fois, avant retry_instance.sh.
#
# N'est PAS limité par la capacité (contrairement au compute A1.Flex), donc
# ça passe toujours du premier coup.

set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-$(dirname "$0")/config.env}"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

: "${OCI_BIN:=oci}"
: "${COMPARTMENT_ID:?Renseigne COMPARTMENT_ID}"
: "${VCN_DISPLAY_NAME:=vcn-perso}"
: "${VCN_DNS_LABEL:=vcnperso}"
: "${VCN_CIDR:=10.0.0.0/16}"
: "${SUBNET_CIDR:=10.0.0.0/24}"
: "${SUBNET_DISPLAY_NAME:=subnet-public}"
: "${SUBNET_DNS_LABEL:=public}"

echo "== Création du VCN =="
VCN_JSON=$("$OCI_BIN" network vcn create -c "$COMPARTMENT_ID" \
  --cidr-block "$VCN_CIDR" --display-name "$VCN_DISPLAY_NAME" \
  --dns-label "$VCN_DNS_LABEL" --no-retry)

VCN_ID=$(echo "$VCN_JSON" | grep '"id"' | head -1 | sed -E 's/.*"id": "([^"]+)".*/\1/')
RT_ID=$(echo "$VCN_JSON" | grep '"default-route-table-id"' | sed -E 's/.*: "([^"]+)".*/\1/')
SL_ID=$(echo "$VCN_JSON" | grep '"default-security-list-id"' | sed -E 's/.*: "([^"]+)".*/\1/')

echo "VCN_ID=$VCN_ID"

echo "== Passerelle internet =="
IGW_JSON=$("$OCI_BIN" network internet-gateway create -c "$COMPARTMENT_ID" \
  --vcn-id "$VCN_ID" --is-enabled true --display-name "igw-perso" --no-retry)
IGW_ID=$(echo "$IGW_JSON" | grep '"id"' | head -1 | sed -E 's/.*"id": "([^"]+)".*/\1/')

echo "== Route par défaut vers la passerelle =="
"$OCI_BIN" network route-table update --rt-id "$RT_ID" \
  --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW_ID\"}]" \
  --force --no-retry >/dev/null

echo "== Règles de sécurité (22, 80, 443, ICMP en entrée ; tout en sortie) =="
"$OCI_BIN" network security-list update --security-list-id "$SL_ID" --ingress-security-rules '[
  {"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},
  {"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":80,"max":80}}},
  {"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":443,"max":443}}},
  {"protocol":"1","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","isStateless":false}
]' --egress-security-rules '[{"protocol":"all","destination":"0.0.0.0/0","destinationType":"CIDR_BLOCK","isStateless":false}]' \
  --force --no-retry >/dev/null

echo "== Sous-réseau public =="
SUBNET_JSON=$("$OCI_BIN" network subnet create -c "$COMPARTMENT_ID" --vcn-id "$VCN_ID" \
  --cidr-block "$SUBNET_CIDR" --display-name "$SUBNET_DISPLAY_NAME" \
  --dns-label "$SUBNET_DNS_LABEL" --prohibit-public-ip-on-vnic false --no-retry)
SUBNET_ID=$(echo "$SUBNET_JSON" | grep '"id"' | head -1 | sed -E 's/.*"id": "([^"]+)".*/\1/')

echo ""
echo "Terminé. Ajoute cette ligne à ton config.env :"
echo "SUBNET_ID=$SUBNET_ID"
