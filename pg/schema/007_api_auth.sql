-- 007_api_auth.sql — API keys, a per-key request limit, and a log of every access decision.
--
-- The reader role still cannot write a single table. It gets EXECUTE on one function,
-- auth.check_key, which runs as the owner (SECURITY DEFINER), and that function is the only
-- thing that ever writes here: it looks up the key, counts the key's requests in the last
-- minute, appends one row to auth.request_log, and answers ok / unknown / revoked / rate_limited.
--
-- Keys are 32 random bytes shown once at creation; only their SHA-256 is stored. A slow hash
-- (bcrypt, argon2) protects guessable passwords; a random 256-bit key does not need one.

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE auth.api_key (
  key_id          serial PRIMARY KEY,
  name            text NOT NULL UNIQUE,
  key_hash        text NOT NULL UNIQUE CHECK (key_hash ~ '^[0-9a-f]{64}$'),
  per_minute      int NOT NULL DEFAULT 60 CHECK (per_minute > 0),
  created_at      timestamptz NOT NULL DEFAULT now(),
  revoked_at      timestamptz
);

CREATE TABLE auth.request_log (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  key_id     int REFERENCES auth.api_key,
  surface    text NOT NULL,              -- 'api' or 'mcp'
  path       text NOT NULL,              -- '/accounts', 'mcp:search_accounts', ...
  decision   text NOT NULL CHECK (decision IN ('ok', 'missing', 'unknown', 'revoked', 'rate_limited'))
);
CREATE INDEX request_log_key_at ON auth.request_log (key_id, at);

CREATE FUNCTION auth.check_key(p_hash text, p_surface text, p_path text)
RETURNS TABLE (key_id int, decision text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = auth, pg_temp AS $$
DECLARE
  k auth.api_key;
  d text;
BEGIN
  IF p_hash IS NULL THEN
    d := 'missing';
  ELSE
    SELECT * INTO k FROM auth.api_key WHERE key_hash = p_hash;
    IF NOT FOUND THEN
      d := 'unknown';
    ELSIF k.revoked_at IS NOT NULL THEN
      d := 'revoked';
    ELSE
      PERFORM pg_advisory_xact_lock(k.key_id);   -- concurrent requests of one key count in turn
      IF (SELECT count(*) FROM auth.request_log l
          WHERE l.key_id = k.key_id AND l.decision = 'ok' AND l.at > now() - interval '1 minute') >= k.per_minute THEN
        d := 'rate_limited';
      ELSE
        d := 'ok';
      END IF;
    END IF;
  END IF;
  INSERT INTO auth.request_log (key_id, surface, path, decision) VALUES (k.key_id, p_surface, left(p_path, 200), d);
  RETURN QUERY SELECT k.key_id, d;
END $$;

REVOKE ALL ON FUNCTION auth.check_key(text, text, text) FROM PUBLIC;
GRANT USAGE ON SCHEMA auth TO xmetrics_reader;
GRANT EXECUTE ON FUNCTION auth.check_key(text, text, text) TO xmetrics_reader;
-- No SELECT, INSERT or anything else on auth.* for the reader: it cannot list keys or edit the log.
