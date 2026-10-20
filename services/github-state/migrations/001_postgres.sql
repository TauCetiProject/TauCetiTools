-- Applied explicitly with migration credentials, never by the Worker.
-- __SCHEMA__ is substituted only after validating a numeric repository ID.
CREATE SCHEMA IF NOT EXISTS __SCHEMA__;
CREATE TABLE IF NOT EXISTS __SCHEMA__.meta(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,key TEXT COLLATE "C" PRIMARY KEY,value TEXT COLLATE "C");
CREATE TABLE IF NOT EXISTS __SCHEMA__.resources(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,kind TEXT COLLATE "C" NOT NULL,id TEXT COLLATE "C" NOT NULL,parent TEXT COLLATE "C",data TEXT COLLATE "C",updated TEXT COLLATE "C",observed BIGINT,revision BIGINT,tombstone BIGINT DEFAULT 0);
CREATE TABLE IF NOT EXISTS __SCHEMA__.activity_loc_cache(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,number TEXT COLLATE "C" PRIMARY KEY,fingerprint TEXT COLLATE "C",data TEXT COLLATE "C");
CREATE TABLE IF NOT EXISTS __SCHEMA__.activity_landings(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,number TEXT COLLATE "C",head TEXT COLLATE "C",at BIGINT,source TEXT COLLATE "C",PRIMARY KEY(number,head));
CREATE INDEX IF NOT EXISTS resource_parent ON __SCHEMA__.resources(kind,parent,tombstone);
CREATE TABLE IF NOT EXISTS __SCHEMA__.collections(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,key TEXT COLLATE "C" PRIMARY KEY,operation TEXT COLLATE "C",args TEXT COLLATE "C",complete BIGINT,verified BIGINT,revision BIGINT,etag TEXT COLLATE "C");
CREATE TABLE IF NOT EXISTS __SCHEMA__.members(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,collection TEXT COLLATE "C" NOT NULL,id TEXT COLLATE "C" NOT NULL);
CREATE TABLE IF NOT EXISTS __SCHEMA__.deliveries(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,id TEXT COLLATE "C" PRIMARY KEY,event TEXT COLLATE "C",received BIGINT);
CREATE TABLE IF NOT EXISTS __SCHEMA__.changes(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,revision BIGINT PRIMARY KEY,at BIGINT,kind TEXT COLLATE "C",id TEXT COLLATE "C",tombstone BIGINT);
CREATE TABLE IF NOT EXISTS __SCHEMA__.jobs(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,key TEXT COLLATE "C" PRIMARY KEY,query TEXT COLLATE "C",priority BIGINT,due BIGINT,started BIGINT,cursor TEXT COLLATE "C",page BIGINT DEFAULT 1,api TEXT COLLATE "C",etag TEXT COLLATE "C",attempt BIGINT DEFAULT 0,dirty BIGINT DEFAULT 0,generation BIGINT DEFAULT 0,start_generation BIGINT DEFAULT 0,lane TEXT COLLATE "C" DEFAULT 'verification',lease_id TEXT COLLATE "C",lease_until BIGINT DEFAULT 0,requested_at BIGINT,created_at BIGINT,wait_reason TEXT COLLATE "C",nested TEXT COLLATE "C");
CREATE INDEX IF NOT EXISTS jobs_due ON __SCHEMA__.jobs(due,priority);
CREATE TABLE IF NOT EXISTS __SCHEMA__.demand(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,key TEXT COLLATE "C" PRIMARY KEY,at BIGINT);
CREATE TABLE IF NOT EXISTS __SCHEMA__.cursors(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,id TEXT COLLATE "C" PRIMARY KEY,expires BIGINT,payload TEXT COLLATE "C");
CREATE TABLE IF NOT EXISTS __SCHEMA__.retired_objects(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,key TEXT COLLATE "C" PRIMARY KEY,at BIGINT);
CREATE TABLE IF NOT EXISTS __SCHEMA__.head_prs(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,sha TEXT COLLATE "C",number TEXT COLLATE "C",PRIMARY KEY(sha,number));
CREATE TABLE IF NOT EXISTS __SCHEMA__.survey_fragments(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,kind TEXT COLLATE "C",id TEXT COLLATE "C",parent TEXT COLLATE "C",data TEXT COLLATE "C",PRIMARY KEY(kind,id));
CREATE INDEX IF NOT EXISTS survey_fragment_parent ON __SCHEMA__.survey_fragments(parent,kind);
CREATE TABLE IF NOT EXISTS __SCHEMA__.survey_state(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,number TEXT COLLATE "C" PRIMARY KEY,data TEXT COLLATE "C",revision BIGINT);
CREATE TABLE IF NOT EXISTS __SCHEMA__.survey_inventory(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,number TEXT COLLATE "C" PRIMARY KEY,data TEXT COLLATE "C");
CREATE TABLE IF NOT EXISTS __SCHEMA__.survey_hydrated(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,number TEXT COLLATE "C" PRIMARY KEY,contract TEXT COLLATE "C");
CREATE TABLE IF NOT EXISTS __SCHEMA__.survey_dirty(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,number TEXT COLLATE "C" PRIMARY KEY);
CREATE TABLE IF NOT EXISTS __SCHEMA__.survey_hydration_failures(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,number TEXT COLLATE "C" PRIMARY KEY,retry_at BIGINT);
CREATE TABLE IF NOT EXISTS __SCHEMA__.repair_metrics(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,at BIGINT,event TEXT COLLATE "C",lane TEXT COLLATE "C",operation TEXT COLLATE "C",value BIGINT DEFAULT 1);
CREATE INDEX IF NOT EXISTS repair_metrics_at ON __SCHEMA__.repair_metrics(at);
CREATE TABLE IF NOT EXISTS __SCHEMA__.query_metrics(rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,operation TEXT COLLATE "C",coverage TEXT COLLATE "C",requests BIGINT,last_at BIGINT,PRIMARY KEY(operation,coverage));
CREATE INDEX IF NOT EXISTS jobs_lane_due ON __SCHEMA__.jobs(lane,due,lease_until);
CREATE TABLE IF NOT EXISTS __SCHEMA__.backend_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1),backend TEXT COLLATE "C" NOT NULL,generation TEXT COLLATE "C" NOT NULL,schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS __SCHEMA__.capacity_statistics(singleton INTEGER PRIMARY KEY CHECK(singleton=1),prepared_at BIGINT,database_bytes BIGINT,storage_limit_bytes BIGINT,restore_verified_at BIGINT);

-- Invoker privileges allow checking and locking ownership, never changing it.
CREATE OR REPLACE FUNCTION __SCHEMA__.assert_writer(expected TEXT) RETURNS void LANGUAGE plpgsql SET lock_timeout='5s' AS $$
BEGIN
  PERFORM singleton FROM __SCHEMA__.backend_control
    WHERE singleton=1 AND backend='postgres' AND generation=expected AND schema_version=1 FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Repository backend generation fenced' USING ERRCODE='P0308'; END IF;
END;
$$;
REVOKE ALL ON FUNCTION __SCHEMA__.assert_writer(TEXT) FROM PUBLIC;

-- Full file/ref keys can exceed the B-tree tuple limit. Hash exclusion indexes
-- compare the full expression for equality, so hash collisions remain distinct.
-- Length-prefix the first component to keep composite identities unambiguous.
ALTER TABLE __SCHEMA__.resources DROP CONSTRAINT IF EXISTS resources_pkey;
ALTER TABLE __SCHEMA__.members DROP CONSTRAINT IF EXISTS members_pkey;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='__SCHEMA__.resources'::regclass AND conname='resource_identity') THEN
  ALTER TABLE __SCHEMA__.resources ADD CONSTRAINT resource_identity EXCLUDE USING hash ((length(kind)::text || ':' || kind || id) WITH =);
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='__SCHEMA__.members'::regclass AND conname='member_identity') THEN
  ALTER TABLE __SCHEMA__.members ADD CONSTRAINT member_identity EXCLUDE USING hash ((length(collection)::text || ':' || collection || id) WITH =);
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS resource_id ON __SCHEMA__.resources USING hash(id);
CREATE INDEX IF NOT EXISTS member_collection ON __SCHEMA__.members(collection);
CREATE INDEX IF NOT EXISTS member_id ON __SCHEMA__.members USING hash(id);

-- S2 idempotent import ledger, owned exclusively by the migration role.
CREATE TABLE IF NOT EXISTS __SCHEMA__.migration_imports(generation TEXT NOT NULL,name TEXT NOT NULL,page INTEGER NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(generation,name,page));
