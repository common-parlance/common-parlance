#!/usr/bin/env bash
# Tag a date-based release snapshot on the HuggingFace dataset repo.
#
# Usage:
#   ./scripts/tag-release.sh v2026.03 "Initial public release"
#   ./scripts/tag-release.sh v2026.03.1 "PII removal patch"
#
# Requires:
#   HF_TOKEN env var (fine-grained write token scoped to the dataset repo)
#
# Versioning scheme:
#   vYYYY.MM     — quarterly/monthly stable snapshot (e.g. v2026.03)
#   vYYYY.MM.N   — patch to a snapshot (e.g. v2026.03.1 for PII removal)
#
# Consumers pin with:
#   load_dataset("common-parlance/conversations", revision="v2026.03")

set -euo pipefail

REPO="${HF_REPO:-common-parlance/conversations}"
TAG="${1:-}"
MESSAGE="${2:-}"

if [[ -z "$TAG" ]]; then
  echo "Usage: $0 <version-tag> [message]"
  echo ""
  echo "Formats:"
  echo "  $0 v2026.03        'Q1 2026 snapshot'"
  echo "  $0 v2026.03.1      'PII removal patch'"
  exit 1
fi

# Validate date-based format: vYYYY.MM or vYYYY.MM.N
if ! echo "$TAG" | grep -qE '^v[0-9]{4}\.(0[1-9]|1[0-2])(\.[0-9]+)?$'; then
  echo "Error: Tag must be date-based format"
  echo "  vYYYY.MM     — snapshot  (e.g. v2026.03)"
  echo "  vYYYY.MM.N   — patch     (e.g. v2026.03.1)"
  exit 1
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "Error: HF_TOKEN environment variable is required"
  echo "  Set it with: export HF_TOKEN=hf_..."
  exit 1
fi

if [[ -z "$MESSAGE" ]]; then
  MESSAGE="Snapshot $TAG"
fi

echo "Tagging $REPO with $TAG..."

# Create tag via HuggingFace API
RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST \
  "https://huggingface.co/api/datasets/${REPO}/tag" \
  -H "Authorization: Bearer ${HF_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"tag\": \"${TAG}\", \"message\": \"${MESSAGE}\"}")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]]; then
  echo "Tagged ${REPO} as ${TAG}"
  echo ""
  echo "Next steps:"
  echo "  1. Update the dataset card's Versions table on HuggingFace"
  echo "  2. Note the tag in any release announcements"
  echo ""
  echo "Consumers can pin with:"
  echo "  load_dataset(\"${REPO}\", revision=\"${TAG}\")"
  echo ""
  echo "View at: https://huggingface.co/datasets/${REPO}/tree/${TAG}"
else
  echo "Error (HTTP ${HTTP_CODE}): ${BODY}"
  exit 1
fi
