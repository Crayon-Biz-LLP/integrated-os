CREATE OR REPLACE FUNCTION archive_terminal_pending_edges()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    -- Insert terminal rows older than 24 hours into the archive table
    INSERT INTO pending_graph_edges_archive
    SELECT * FROM pending_graph_edges
    WHERE status IN ('approved', 'rejected', 'archived', 'skipped')
      AND created_at < NOW() - INTERVAL '24 hours'
    ON CONFLICT DO NOTHING;
    
    -- Delete the archived rows from the active table
    DELETE FROM pending_graph_edges
    WHERE status IN ('approved', 'rejected', 'archived', 'skipped')
      AND created_at < NOW() - INTERVAL '24 hours';
END;
$$;
