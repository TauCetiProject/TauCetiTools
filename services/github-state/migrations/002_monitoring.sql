-- Additive diagnostics only. No repository ownership, routing or semantic rows change.
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS observation JSONB;
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS tables_prepared_at BIGINT;
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS tables_attempt_at BIGINT;
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS tables JSONB;

-- The application may measure capacity, never choose quotas or restore receipts.
-- Fixed schema/relations and a locked search_path keep this privilege narrow.
CREATE OR REPLACE FUNCTION __SCHEMA__.prepare_monitoring(expected TEXT, analyze_tables BOOLEAN DEFAULT FALSE)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog SET statement_timeout='5s' SET lock_timeout='500ms' AS $$
DECLARE
  stamp BIGINT := floor(extract(epoch FROM clock_timestamp())*1000)::bigint;
  previous __SCHEMA__.capacity_statistics%ROWTYPE;
  detail JSONB;
  table_values JSONB;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM __SCHEMA__.backend_control WHERE singleton=1 AND backend='postgres' AND generation=expected AND schema_version=1) THEN
    RAISE EXCEPTION 'Repository backend generation fenced' USING ERRCODE='P0308';
  END IF;
  IF NOT pg_try_advisory_xact_lock(hashtextextended('__SCHEMA__:monitoring',0)) THEN RETURN; END IF;
  INSERT INTO __SCHEMA__.capacity_statistics(singleton) VALUES(1) ON CONFLICT DO NOTHING;
  SELECT * INTO previous FROM __SCHEMA__.capacity_statistics WHERE singleton=1 FOR UPDATE;
  IF previous.prepared_at>stamp-60000 AND previous.observation IS NOT NULL THEN RETURN; END IF;
  SELECT jsonb_build_object(
    'connections',jsonb_build_object('used',count(*),'active',count(*) FILTER(WHERE state='active'),
      'waiting',count(*) FILTER(WHERE state='active' AND wait_event_type IS NOT NULL),
      'max',current_setting('max_connections')::int,
      'visibility','role_limited_session_details',
      'reserved',current_setting('superuser_reserved_connections')::int+COALESCE(current_setting('reserved_connections',true),'0')::int),
    'compute',jsonb_build_object('cpu_utilization',NULL,'reason','use_neon_compute_metrics',
      'active_queries',count(*) FILTER(WHERE state='active' AND datname=current_database()),
      'lock_waits',count(*) FILTER(WHERE wait_event_type='Lock' AND datname=current_database())))
  INTO detail FROM pg_stat_activity;
  SELECT detail || jsonb_build_object('table_row_estimates',COALESCE(jsonb_object_agg(relname,jsonb_build_object(
    'live',n_live_tup,'dead',n_dead_tup,'updated',n_tup_upd,'inserted',n_tup_ins,'deleted',n_tup_del)),'{}'::jsonb))
    INTO detail FROM pg_stat_user_tables WHERE schemaname='__SCHEMA__';
  IF analyze_tables AND COALESCE(previous.tables_attempt_at,0)<=stamp-3600000 THEN
    UPDATE __SCHEMA__.capacity_statistics SET tables_attempt_at=stamp WHERE singleton=1;
    BEGIN
      SELECT COALESCE(jsonb_object_agg(c.relname,jsonb_build_object(
        'rows_estimate',CASE WHEN c.reltuples<0 THEN NULL ELSE c.reltuples END,
        'table_bytes',pg_table_size(c.oid),'index_bytes',pg_indexes_size(c.oid),
        'total_bytes',pg_total_relation_size(c.oid),'dead_tuples_estimate',s.n_dead_tup,
        'growth_bytes_per_second',CASE WHEN previous.tables_prepared_at<stamp AND previous.tables ? c.relname
          THEN (pg_total_relation_size(c.oid)-(previous.tables->c.relname->>'total_bytes')::bigint)::numeric/((stamp-previous.tables_prepared_at)::numeric/1000) ELSE NULL END
      )),'{}'::jsonb) INTO table_values
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
      WHERE n.nspname='__SCHEMA__' AND c.relkind='r';
      UPDATE __SCHEMA__.capacity_statistics SET tables=table_values,tables_prepared_at=stamp WHERE singleton=1;
    EXCEPTION WHEN OTHERS THEN detail:=detail || jsonb_build_object('table_error',SQLSTATE); END;
  END IF;
  UPDATE __SCHEMA__.capacity_statistics SET prepared_at=stamp,database_bytes=pg_database_size(current_database()),observation=detail WHERE singleton=1;
END;
$$;
REVOKE ALL ON FUNCTION __SCHEMA__.prepare_monitoring(TEXT,BOOLEAN) FROM PUBLIC;
