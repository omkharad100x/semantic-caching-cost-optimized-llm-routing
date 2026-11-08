$ErrorActionPreference = 'Stop'

$required = @('PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGSSLMODE', 'PGSSLROOTCERT')
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw 'Required PostgreSQL connection configuration is missing.'
    }
}
if ($env:PGPORT -ne '5432' -or
    $env:PGDATABASE -ne 'postgres' -or
    $env:PGSSLMODE -cne 'verify-full' -or
    -not $env:PGHOST.EndsWith('.pooler.supabase.com',
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Expected the verified Supabase Session Pooler and verify-full TLS.'
}
if ($env:PGSSLROOTCERT -cne 'system' -and
    -not (Test-Path -LiteralPath $env:PGSSLROOTCERT -PathType Leaf)) {
    throw 'Trusted PostgreSQL CA certificate is unavailable.'
}

$psql = 'C:\Program Files\PostgreSQL\17\bin\psql.exe'
if (-not (Test-Path -LiteralPath $psql -PathType Leaf)) {
    throw 'PostgreSQL 17 psql.exe was not found.'
}
$migration1 = 'D:\Projects\llm-cost-router\migrations\postgresql\001_cache.sql'
$migration2 = 'D:\Projects\llm-cost-router\migrations\postgresql\002_digest.sql'
foreach ($file in @($migration1, $migration2)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'A required migration file is missing.'
    }
}

$preflight = @'
SET TRANSACTION ISOLATION LEVEL READ COMMITTED;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';
SELECT pg_advisory_xact_lock(1684027551);

DO $check$
BEGIN
    IF current_database() <> 'postgres'
       OR session_user <> 'postgres'
       OR current_user <> 'postgres'
       OR current_setting('server_encoding') <> 'UTF8'
       OR to_regnamespace('lcr_cache') IS NOT NULL
       OR NOT pg_has_role(session_user, 'lcr_migrator', 'SET')
       OR NOT has_database_privilege('lcr_migrator', 'postgres', 'CREATE')
       OR has_database_privilege('lcr_runtime', 'postgres', 'CREATE')
       OR to_regprocedure('pg_catalog.sha256(bytea)') IS NULL
       OR (
           SELECT count(*) FROM pg_roles
           WHERE rolname IN ('lcr_migrator', 'lcr_runtime')
             AND NOT rolcanlogin AND NOT rolinherit AND NOT rolsuper
             AND NOT rolcreatedb AND NOT rolcreaterole
             AND NOT rolreplication AND NOT rolbypassrls
       ) <> 2
    THEN
        RAISE EXCEPTION 'Task 8C migration preflight failed';
    END IF;
END
$check$;

SET LOCAL ROLE lcr_migrator;

DO $check$
BEGIN
    IF current_user <> 'lcr_migrator' THEN
        RAISE EXCEPTION 'Task 8C migration role switch failed';
    END IF;
END
$check$;
'@

$postflight = @'
DO $check$
BEGIN
    IF current_user <> 'lcr_migrator'
       OR (SELECT count(*) FROM lcr_cache.schema_version) <> 1
       OR (SELECT count(*) FROM lcr_cache.schema_version WHERE version = 2) <> 1
       OR (SELECT count(*) FROM lcr_cache.cache_entries) <> 0
       OR (
           SELECT pg_get_userbyid(nspowner)
           FROM pg_namespace WHERE nspname = 'lcr_cache'
       ) IS DISTINCT FROM 'lcr_migrator'
       OR (
           SELECT count(*)
           FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'lcr_cache'
             AND c.relname IN ('schema_version', 'cache_entries')
             AND c.relkind = 'r'
             AND pg_get_userbyid(c.relowner) = 'lcr_migrator'
       ) <> 2
       OR NOT EXISTS (
           SELECT 1
           FROM pg_constraint c
           JOIN pg_index i ON i.indexrelid = c.conindid
           WHERE c.conrelid = 'lcr_cache.cache_entries'::regclass
             AND c.conname = 'cache_scope_digest_unique'
             AND c.contype = 'u'
             AND NOT c.condeferrable
             AND i.indisunique AND i.indisvalid
             AND i.indisready AND i.indimmediate
             AND ARRAY(
                 SELECT a.attname::text
                 FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, pos)
                 JOIN pg_attribute a
                   ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                 ORDER BY k.pos
             ) = ARRAY[
                 'namespace', 'query_digest', 'embedding_model',
                 'embedding_version', 'embedding_task', 'embedding_dimensions',
                 'embedding_dtype', 'eligibility_version', 'cache_version',
                 'embedding_config'
             ]
       )
       OR NOT EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conrelid = 'lcr_cache.cache_entries'::regclass
             AND conname = 'digest_matches_key' AND convalidated
       )
       OR to_regclass('lcr_cache.cache_expiration') IS NULL
    THEN
        RAISE EXCEPTION 'Task 8C migration postflight failed';
    END IF;
END
$check$;
'@

$preFile = $null
$postFile = $null
try {
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $preFile = [System.IO.Path]::GetTempFileName()
    $postFile = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($preFile, $preflight, $encoding)
    [System.IO.File]::WriteAllText($postFile, $postflight, $encoding)

    & $psql -X -W -v 'ON_ERROR_STOP=1' --single-transaction -f $preFile -f $migration1 -f $migration2 -f $postFile

    if ($LASTEXITCODE -ne 0) {
        throw 'Migration was not confirmed. Inspect schema state before retrying.'
    }
}
finally {
    foreach ($file in @($preFile, $postFile)) {
        if ($null -ne $file) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        }
    }
}
