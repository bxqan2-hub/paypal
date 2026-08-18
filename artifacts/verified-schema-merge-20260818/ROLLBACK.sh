#!/usr/bin/env bash
set -euo pipefail

CATALOG_TARGET="${1:?pass a catalog target file path}"
SUPPORTED_TARGET="${2:?pass a supported-country target file path}"
REPO_ROOT="$(git rev-parse --show-toplevel)"

git -C "$REPO_ROOT" show a1a76fbd5f85df6840824194181e00ba9bb0885d:paypal_agreement_protocol/data/country_discovery/country_field_catalog.json > "$CATALOG_TARGET"
git -C "$REPO_ROOT" show a1a76fbd5f85df6840824194181e00ba9bb0885d:paypal_agreement_protocol/data/paypal_supported_countries.json > "$SUPPORTED_TARGET"
printf 'ROLLBACK_CATALOG_RESTORED=%s\n' "$CATALOG_TARGET"
printf 'ROLLBACK_SUPPORTED_RESTORED=%s\n' "$SUPPORTED_TARGET"
