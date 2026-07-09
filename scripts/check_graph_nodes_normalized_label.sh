#!/usr/bin/env bash
set -euo pipefail

# Fail if any graph_nodes insert/upsert is missing normalized_label.
# Excludes tests/, scripts/archive/, and node_modules.
root="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

broken=$(
    grep -rn --include='*.py' \
        -e "table('graph_nodes')\.insert(" \
        -e 'table("graph_nodes")\.insert(' \
        -e "table('graph_nodes')\.upsert(" \
        -e 'table("graph_nodes")\.upsert(' \
        "$root" \
        | grep -v '/tests/' \
        | grep -v '/scripts/archive/' \
        | grep -v '/node_modules/' \
        | grep -v 'normalized_label' \
    || true
)

if [ -n "$broken" ]; then
    echo "ERROR: graph_nodes write without normalized_label:"
    echo "$broken"
    exit 1
fi

echo "OK: all graph_nodes writes include normalized_label"
