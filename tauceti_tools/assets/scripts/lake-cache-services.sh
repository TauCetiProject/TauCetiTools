#!/usr/bin/env bash
# lake-cache-services.sh: the Lake cache services CI reads from, as one Lake system config.
#
# Sourced by lake-cache-get.sh and repair-cache-get.sh. Defines
#
# * the public cache service (`tauceti-public`), published by CI on each `main` push and by
#   the merge-group/staging finalizer, the base cache every build starts from;
# * the repair cache service (`tauceti-repair`), where the queue's repair worker publishes the
#   outputs of a green repair build under the chain tip's revision (TauCetiWorker
#   tauceti_queue/repair_publish.py). Staging CI fetches from it for the exact staging head only.
#
# Both are anonymous reads (no key is configured here). The repair service name and base URL are
# rendered from `[services.repair_cache]` in tauceti.toml when the scripts are materialized; an
# installation without that table renders an empty URL, and the service is then not defined.
# Both bases follow the public cache's layout: <base>/artifacts and <base>/revisions.

REPAIR_CACHE_SERVICE="tauceti-repair"
REPAIR_CACHE_URL="https://repair-cache.taucetiproject.org"

# write_lake_cache_config PATH PUBLIC_ARTIFACT_ENDPOINT PUBLIC_REVISION_ENDPOINT
write_lake_cache_config() {
  local cfg="$1" artifacts="$2" revisions="$3"
  {
    printf 'cache.defaultService = "tauceti-public"\n'
    printf '[[cache.service]]\nname = "tauceti-public"\nkind = "s3"\n'
    printf 'artifactEndpoint = "%s"\nrevisionEndpoint = "%s"\n' "$artifacts" "$revisions"
    if [ -n "$REPAIR_CACHE_URL" ]; then
      printf '[[cache.service]]\nname = "%s"\nkind = "s3"\n' "$REPAIR_CACHE_SERVICE"
      printf 'artifactEndpoint = "%s/artifacts"\nrevisionEndpoint = "%s/revisions"\n' \
        "${REPAIR_CACHE_URL%/}" "${REPAIR_CACHE_URL%/}"
    fi
  } > "$cfg"
}
