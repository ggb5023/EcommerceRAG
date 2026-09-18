\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DO $$
DECLARE v text;
BEGIN
  SELECT extversion INTO v FROM pg_extension WHERE extname='vector';
  IF string_to_array(v, '.')::int[] < ARRAY[0,8,0] THEN
    RAISE EXCEPTION 'pgvector >= 0.8.0 required, found %', v;
  END IF;
END $$;
