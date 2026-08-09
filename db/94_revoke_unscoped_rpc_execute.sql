-- 94_revoke_unscoped_rpc_execute.sql
-- Cross-tenant leak fix (Aug 9 audit): get_context_for has NO owner_id param
-- and is PUBLIC-executable — any role (rhodey_app, anon, authenticated) could
-- call it and read graph context across ALL tenants' graph_nodes. There are
-- no Python callers (the retrieval layer uses search_phrase_nodes /
-- match_memories_hybrid, which take owner_id). Close the hole: only
-- service_role/postgres may execute it.
--
-- If a tenant-scoped replacement is ever needed, add an owner_id param with a
-- WHERE gn.owner_id = p_owner_id filter and grant back per-role.

do $$
begin
    revoke execute on function public.get_context_for(text, text, timestamptz, int)
        from public, anon, authenticated, rhodey_app;
    -- Grant explicitly to service_role (idempotent — the default PUBLIC grant
    -- is removed by the revoke above, service_role keeps access via explicit grant).
    grant execute on function public.get_context_for(text, text, timestamptz, int)
        to service_role;
end $$;

-- Belt-and-suspenders: revoke the same for any overload created with fewer args.
do $$
begin
    revoke execute on function public.get_context_for(text, text)
        from public, anon, authenticated, rhodey_app;
    grant execute on function public.get_context_for(text, text)
        to service_role;
exception when others then null;  -- overload absent — fine
end $$;
