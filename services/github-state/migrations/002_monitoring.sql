-- Additive diagnostics only. No repository ownership, routing or semantic rows change.
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS observation JSONB;
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS tables_prepared_at BIGINT;
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS tables_attempt_at BIGINT;
ALTER TABLE __SCHEMA__.capacity_statistics ADD COLUMN IF NOT EXISTS tables JSONB;

-- The application may measure capacity, never choose quotas or restore receipts.
-- Fixed schema/relations and a locked search_path keep this privilege narrow.
DROP FUNCTION IF EXISTS __SCHEMA__.prepare_monitoring(TEXT,BOOLEAN);
CREATE OR REPLACE FUNCTION __SCHEMA__.prepare_monitoring(expected TEXT, analyze_tables BOOLEAN DEFAULT FALSE, attempt_token BIGINT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET lock_timeout='500ms' AS $$
DECLARE
  stamp BIGINT := floor(extract(epoch FROM clock_timestamp())*1000)::bigint;
  previous __SCHEMA__.capacity_statistics%ROWTYPE;
  detail JSONB;
  table_values JSONB;
  analyzed BOOLEAN := FALSE;
  attempted BOOLEAN := FALSE;
  token BIGINT;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM __SCHEMA__.backend_control WHERE singleton=1 AND backend='postgres' AND generation=expected AND schema_version=1) THEN
    RAISE EXCEPTION 'Repository backend generation fenced' USING ERRCODE='P0308';
  END IF;
  token:=COALESCE(attempt_token,stamp);
  IF abs(token-stamp)>60000 THEN RAISE EXCEPTION 'Invalid monitoring attempt'; END IF;
  IF NOT pg_try_advisory_xact_lock(hashtextextended('__SCHEMA__:monitoring',0)) THEN RETURN jsonb_build_object('sampled',false,'analysis_attempted',false,'reason','busy'); END IF;
  INSERT INTO __SCHEMA__.capacity_statistics(singleton) VALUES(1) ON CONFLICT DO NOTHING;
  SELECT * INTO previous FROM __SCHEMA__.capacity_statistics WHERE singleton=1 FOR UPDATE;
  IF NOT analyze_tables AND previous.prepared_at>stamp-50000 AND previous.observation IS NOT NULL THEN RETURN jsonb_build_object('sampled',false,'analysis_attempted',false,'reason','recent_sample'); END IF;
  SELECT jsonb_build_object(
    'connections',jsonb_build_object('used',count(*) FILTER(WHERE backend_type='client backend'),'active',count(*) FILTER(WHERE backend_type='client backend' AND state='active'),
      'waiting',count(*) FILTER(WHERE backend_type='client backend' AND state='active' AND wait_event_type IS NOT NULL),
      'max',current_setting('max_connections')::int,
      'visibility',CASE WHEN pg_has_role(current_user,'pg_read_all_stats','USAGE') THEN 'all_session_details' ELSE 'definer_role_limited_session_details' END,
      'reserved',current_setting('superuser_reserved_connections')::int+COALESCE(current_setting('reserved_connections',true),'0')::int),
    'compute',jsonb_build_object('cpu_utilization',NULL,'reason','use_neon_compute_metrics',
      'active_queries',count(*) FILTER(WHERE state='active' AND datname=current_database()),
      'lock_waits',count(*) FILTER(WHERE wait_event_type='Lock' AND datname=current_database())))
  INTO detail FROM pg_catalog.pg_stat_activity;
  SELECT detail || jsonb_build_object('table_row_estimates',COALESCE(jsonb_object_agg(relname,jsonb_build_object(
    'live',n_live_tup,'dead',n_dead_tup,'updated',n_tup_upd,'inserted',n_tup_ins,'deleted',n_tup_del)),'{}'::jsonb))
    INTO detail FROM pg_catalog.pg_stat_user_tables WHERE schemaname='__SCHEMA__';
  IF analyze_tables AND COALESCE(previous.tables_attempt_at,0)<=token-3600000 THEN
    UPDATE __SCHEMA__.capacity_statistics SET tables_attempt_at=token WHERE singleton=1;
    attempted:=TRUE;
    BEGIN
      SELECT COALESCE(jsonb_object_agg(c.relname,jsonb_build_object(
        'rows_estimate',CASE WHEN c.reltuples<0 THEN NULL ELSE c.reltuples END,
        'table_bytes',pg_table_size(c.oid),'index_bytes',pg_indexes_size(c.oid),
        'total_bytes',pg_total_relation_size(c.oid),'dead_tuples_estimate',s.n_dead_tup,
        'growth_bytes_per_second',CASE WHEN previous.tables_prepared_at<stamp AND previous.tables ? c.relname
          THEN (pg_total_relation_size(c.oid)-(previous.tables->c.relname->>'total_bytes')::bigint)::numeric/((stamp-previous.tables_prepared_at)::numeric/1000) ELSE NULL END
      )),'{}'::jsonb) INTO table_values
      FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid=c.oid
      WHERE n.nspname='__SCHEMA__' AND c.relkind='r';
      UPDATE __SCHEMA__.capacity_statistics SET tables=table_values,tables_prepared_at=stamp WHERE singleton=1;
      analyzed:=TRUE;
    EXCEPTION WHEN OTHERS THEN detail:=detail || jsonb_build_object('table_error',SQLSTATE); END;
  END IF;
  UPDATE __SCHEMA__.capacity_statistics SET prepared_at=stamp,database_bytes=pg_database_size(current_database()),observation=detail WHERE singleton=1;
  RETURN jsonb_build_object('sampled',true,'analysis_attempted',attempted,'analyzed',analyzed,'sampled_at',stamp,'table_attempt_token',token,'table_retry_at',COALESCE(previous.tables_attempt_at,0)+3600000);
END;
$$;
REVOKE ALL ON FUNCTION __SCHEMA__.prepare_monitoring(TEXT,BOOLEAN,BIGINT) FROM PUBLIC;
