-- 0001_core.sql
-- Core schema for Blabzu notes and the shared posting/search vocabulary.
--
-- The one idea to keep in mind: field_definitions + field_assignments decide BOTH
-- which tick-boxes a creator sees while posting AND which filters/boosts search
-- uses. Posting and search read the same rows, so they cannot drift apart.
--
-- Search index rule: only notes in the searchable_notes view (published + public)
-- go into Typesense. Every change that could affect a note's search document drops
-- a row into `outbox`; the index worker (step 4) rebuilds the document from the
-- current state, or deletes it if the note is no longer searchable.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Enums (stable sets; add values with ALTER TYPE ... ADD VALUE)
-- ---------------------------------------------------------------------------

CREATE TYPE template_kind     AS ENUM ('product', 'place', 'stay', 'experience', 'recipe', 'other');
CREATE TYPE field_data_type   AS ENUM ('single', 'multi', 'int', 'money', 'bool', 'text', 'text_list');
-- hard = filter, soft = ranking boost, rank = quality/ranking signal,
-- text = indexed as searchable text, display = shown on the note only
CREATE TYPE field_search_role AS ENUM ('hard', 'soft', 'rank', 'text', 'display');
CREATE TYPE field_status      AS ENUM ('active', 'proposed', 'retired');
CREATE TYPE posting_step      AS ENUM ('details', 'verdict', 'settings');

CREATE TYPE note_status       AS ENUM ('draft', 'published', 'removed');
CREATE TYPE note_visibility   AS ENUM ('public', 'followers', 'only_me');
CREATE TYPE note_format       AS ENUM ('photo', 'video', 'voice', 'text');
CREATE TYPE note_type         AS ENUM ('review', 'top_picks', 'comparison', 'how_to', 'question', 'tips');
CREATE TYPE promo_status      AS ENUM ('non_promotion', 'promotion');
CREATE TYPE promo_kind        AS ENUM ('gifted', 'free_visit', 'paid_partnership',
                                       'discount_commission', 'own_brand', 'owner_or_staff');
CREATE TYPE media_type        AS ENUM ('image', 'video', 'audio');

CREATE TYPE entity_type       AS ENUM ('product', 'place');
CREATE TYPE entity_status     AS ENUM ('active', 'pending', 'merged');
CREATE TYPE note_entity_role  AS ENUM ('primary', 'compared', 'mentioned');

-- ---------------------------------------------------------------------------
-- Shared helpers
-- ---------------------------------------------------------------------------

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- "#Oily Skin!" -> "oilyskin". English-only for now; other scripts normalise to NULL.
CREATE FUNCTION normalize_tag(raw text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT NULLIF(left(regexp_replace(lower(coalesce(raw, '')), '[^a-z0-9]+', '', 'g'), 40), '')
$$;

-- ---------------------------------------------------------------------------
-- Taxonomy: area tiles -> kinds ("What's this about?" -> "What kind?")
-- ---------------------------------------------------------------------------

CREATE TABLE areas (
  id          text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_]*$'),
  label       text NOT NULL,
  sort_order  int  NOT NULL DEFAULT 0,
  is_launch   boolean NOT NULL DEFAULT false,
  is_active   boolean NOT NULL DEFAULT true
);

CREATE TABLE kinds (
  id          text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'),  -- "beauty.sunscreen"
  area_id     text NOT NULL REFERENCES areas (id) ON UPDATE CASCADE,
  label       text NOT NULL,
  template    template_kind NOT NULL,
  synonyms    text[] NOT NULL DEFAULT '{}',
  sort_order  int  NOT NULL DEFAULT 0,
  is_active   boolean NOT NULL DEFAULT true,
  CHECK (split_part(id, '.', 1) = area_id)
);
CREATE INDEX kinds_area_idx ON kinds (area_id);

-- ---------------------------------------------------------------------------
-- Posting/search vocabulary
-- ---------------------------------------------------------------------------

CREATE TABLE field_definitions (
  key          text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]{1,40}$'),
  index_name   text GENERATED ALWAYS AS ('f_' || key) STORED,   -- Typesense field name
  label        text NOT NULL,
  help         text,
  data_type    field_data_type NOT NULL,
  -- [{"value": "oily", "label": "Oily", "synonyms": ["oily skin"]}, ...]
  -- For int fields these are optional presets (chips), e.g. {"value": 6, "label": "6 months"}.
  options      jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(options) = 'array'),
  unit         text,
  min_value    bigint,
  max_value    bigint,
  search_role  field_search_role NOT NULL,
  facetable    boolean NOT NULL DEFAULT false,
  personal     boolean NOT NULL DEFAULT false,   -- Hinge-style show/hide; hidden by default
  status       field_status NOT NULL DEFAULT 'active',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (data_type NOT IN ('single', 'multi') OR jsonb_array_length(options) > 0),
  CHECK (min_value IS NULL OR max_value IS NULL OR min_value <= max_value),
  CHECK (search_role NOT IN ('hard', 'soft') OR facetable),
  CHECK (NOT personal OR search_role IN ('soft', 'display'))
);
CREATE TRIGGER field_definitions_updated_at BEFORE UPDATE ON field_definitions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Which fields show for which area / template / kind. NULL = "any".
-- The most specific matching row wins (kind > area > template), see fields_for_kind().
CREATE TABLE field_assignments (
  id              bigserial PRIMARY KEY,
  field_key       text NOT NULL REFERENCES field_definitions (key) ON UPDATE CASCADE,
  area_id         text REFERENCES areas (id) ON UPDATE CASCADE,
  template        template_kind,
  kind_id         text REFERENCES kinds (id) ON UPDATE CASCADE,
  step            posting_step NOT NULL,
  sort_order      int NOT NULL DEFAULT 0,
  default_ticked  boolean NOT NULL DEFAULT false,
  config          jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object'),  -- e.g. price bands
  CHECK (kind_id IS NULL OR (area_id IS NOT NULL AND split_part(kind_id, '.', 1) = area_id)),
  UNIQUE NULLS NOT DISTINCT (field_key, area_id, template, kind_id)
);

CREATE FUNCTION fields_for_kind(p_kind_id text)
RETURNS TABLE (field_key text, step posting_step, sort_order int, default_ticked boolean, config jsonb)
LANGUAGE sql STABLE AS $$
  SELECT r.field_key, r.step, r.sort_order, r.default_ticked, r.config
  FROM (
    SELECT DISTINCT ON (fa.field_key)
           fa.field_key, fa.step, fa.sort_order, fa.default_ticked, fa.config
    FROM kinds k
    JOIN field_assignments fa
      ON  (fa.area_id  IS NULL OR fa.area_id  = k.area_id)
      AND (fa.template IS NULL OR fa.template = k.template)
      AND (fa.kind_id  IS NULL OR fa.kind_id  = k.id)
    JOIN field_definitions fd ON fd.key = fa.field_key AND fd.status = 'active'
    WHERE k.id = p_kind_id
    ORDER BY fa.field_key,
             (fa.kind_id IS NOT NULL)::int * 4
           + (fa.area_id IS NOT NULL)::int * 2
           + (fa.template IS NOT NULL)::int DESC
  ) r
  ORDER BY r.step, r.sort_order, r.field_key
$$;

-- Prompt library ("Worth it because…", "Skip it if…"). Weighted above body text in search.
CREATE TABLE prompt_definitions (
  key         text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]{1,40}$'),
  label       text NOT NULL,
  template    template_kind,            -- NULL = every template
  sort_order  int NOT NULL DEFAULT 0,
  is_active   boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------------
-- People (stub until real auth lands)
-- ---------------------------------------------------------------------------

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  handle        text NOT NULL CHECK (handle ~ '^[A-Za-z0-9_.]{3,30}$'),
  display_name  text NOT NULL,
  is_verified   boolean NOT NULL DEFAULT false,
  trust_score   real NOT NULL DEFAULT 0.5 CHECK (trust_score BETWEEN 0 AND 1),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_handle_key ON users (lower(handle));

-- ---------------------------------------------------------------------------
-- Catalog: products and places (no purchase links; future product/place card)
-- ---------------------------------------------------------------------------

CREATE TABLE entities (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  type               entity_type NOT NULL,
  name               text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
  brand              text,
  area_id            text NOT NULL REFERENCES areas (id) ON UPDATE CASCADE,
  kind_id            text REFERENCES kinds (id) ON UPDATE CASCADE,
  aliases            text[] NOT NULL DEFAULT '{}',
  attributes         jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),
  city               text,
  locality           text,
  lat                double precision CHECK (lat BETWEEN -90 AND 90),
  lng                double precision CHECK (lng BETWEEN -180 AND 180),
  external_place_id  text,              -- maps provider id for places
  status             entity_status NOT NULL DEFAULT 'pending',
  merged_into        uuid REFERENCES entities (id),
  created_by         uuid REFERENCES users (id),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ((lat IS NULL) = (lng IS NULL)),
  CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
  CHECK (merged_into IS DISTINCT FROM id),
  CHECK (kind_id IS NULL OR split_part(kind_id, '.', 1) = area_id),
  CHECK (type <> 'place' OR status <> 'active' OR city IS NOT NULL)
);
CREATE UNIQUE INDEX entities_external_place_key ON entities (external_place_id) WHERE external_place_id IS NOT NULL;
CREATE INDEX entities_area_kind_idx ON entities (area_id, kind_id);
CREATE INDEX entities_name_trgm_idx ON entities USING gin (lower(name) gin_trgm_ops);  -- dedup job
CREATE TRIGGER entities_updated_at BEFORE UPDATE ON entities
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Notes
-- ---------------------------------------------------------------------------

CREATE TABLE notes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  author_id     uuid NOT NULL REFERENCES users (id),
  status        note_status NOT NULL DEFAULT 'draft',
  visibility    note_visibility NOT NULL DEFAULT 'public',   -- Note settings > Who sees it
  format        note_format NOT NULL,
  note_type     note_type NOT NULL DEFAULT 'review',
  area_id       text REFERENCES areas (id) ON UPDATE CASCADE,
  kind_id       text REFERENCES kinds (id) ON UPDATE CASCADE,
  title         text CHECK (char_length(title) <= 100),
  body          text CHECK (char_length(body) <= 5000),
  cover_text    text CHECK (char_length(cover_text) <= 80),
  prompts       jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(prompts) = 'array'),  -- [{"key","answer"}]
  promo         promo_status,
  promo_kind    promo_kind,
  city          text,
  locality      text,
  lat           double precision CHECK (lat BETWEEN -90 AND 90),
  lng           double precision CHECK (lng BETWEEN -180 AND 180),
  lang          text NOT NULL DEFAULT 'en',
  save_count    int NOT NULL DEFAULT 0 CHECK (save_count >= 0),
  like_count    int NOT NULL DEFAULT 0 CHECK (like_count >= 0),
  view_count    bigint NOT NULL DEFAULT 0 CHECK (view_count >= 0),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  CHECK (kind_id IS NULL OR (area_id IS NOT NULL AND split_part(kind_id, '.', 1) = area_id)),
  CHECK ((lat IS NULL) = (lng IS NULL)),
  -- Promotion answer: "Promotion" always needs a kind; a kind only goes with "Promotion"
  CHECK (promo_kind IS NULL OR promo = 'promotion'),
  CHECK (promo IS DISTINCT FROM 'promotion' OR promo_kind IS NOT NULL),
  -- Minimum note bar on publish (cover/media checks live in the API)
  CHECK (status <> 'published' OR (area_id IS NOT NULL AND kind_id IS NOT NULL
                                   AND title IS NOT NULL AND published_at IS NOT NULL)),
  CHECK (status <> 'published' OR note_type = 'question' OR promo IS NOT NULL)
);
CREATE INDEX notes_author_idx ON notes (author_id, created_at DESC);
CREATE INDEX notes_area_kind_idx ON notes (area_id, kind_id) WHERE status = 'published';

CREATE FUNCTION notes_before_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_template template_kind;
  v_area     text;
  v_prompt   jsonb;
BEGIN
  IF NEW.kind_id IS NOT NULL THEN
    SELECT k.template, k.area_id INTO v_template, v_area FROM kinds k WHERE k.id = NEW.kind_id;
    IF NEW.area_id IS NULL THEN
      NEW.area_id := v_area;
    END IF;
  END IF;

  -- Promotion kinds must fit the template (products vs places)
  IF NEW.promo_kind IS NOT NULL AND v_template IS NOT NULL THEN
    IF v_template IN ('product', 'recipe') AND NEW.promo_kind IN ('free_visit', 'owner_or_staff') THEN
      RAISE EXCEPTION 'promotion kind "%" is for places, not % notes', NEW.promo_kind, v_template
        USING ERRCODE = 'check_violation';
    ELSIF v_template IN ('place', 'stay', 'experience') AND NEW.promo_kind IN ('gifted', 'own_brand') THEN
      RAISE EXCEPTION 'promotion kind "%" is for products, not % notes', NEW.promo_kind, v_template
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  -- Prompts: [{"key": "...", "answer": "..."}], known keys, no repeats, max 5
  IF jsonb_array_length(NEW.prompts) > 5 THEN
    RAISE EXCEPTION 'a note can have at most 5 prompts' USING ERRCODE = 'check_violation';
  END IF;
  FOR v_prompt IN SELECT * FROM jsonb_array_elements(NEW.prompts) LOOP
    IF jsonb_typeof(v_prompt) IS DISTINCT FROM 'object'
       OR jsonb_typeof(v_prompt -> 'key') IS DISTINCT FROM 'string'
       OR jsonb_typeof(v_prompt -> 'answer') IS DISTINCT FROM 'string' THEN
      RAISE EXCEPTION 'each prompt must look like {"key": text, "answer": text}'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM prompt_definitions p
      WHERE p.key = v_prompt ->> 'key' AND p.is_active
        AND (p.template IS NULL OR v_template IS NULL OR p.template = v_template)
    ) THEN
      RAISE EXCEPTION 'prompt "%" is unknown or not available for this kind of note', v_prompt ->> 'key'
        USING ERRCODE = 'check_violation';
    END IF;
    IF char_length(btrim(v_prompt ->> 'answer')) NOT BETWEEN 1 AND 300 THEN
      RAISE EXCEPTION 'prompt answers must be 1-300 characters' USING ERRCODE = 'check_violation';
    END IF;
  END LOOP;
  IF (SELECT count(DISTINCT e ->> 'key') FROM jsonb_array_elements(NEW.prompts) e)
     <> jsonb_array_length(NEW.prompts) THEN
    RAISE EXCEPTION 'each prompt can be answered only once per note' USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status = 'published' AND NEW.published_at IS NULL THEN
    NEW.published_at := now();
  END IF;
  IF TG_OP = 'UPDATE' THEN
    NEW.updated_at := now();
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER notes_before_write BEFORE INSERT OR UPDATE ON notes
  FOR EACH ROW EXECUTE FUNCTION notes_before_write();

-- Products/places a note is about. One primary; "compared" powers X vs Y.
CREATE TABLE note_entities (
  note_id    uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
  entity_id  uuid NOT NULL REFERENCES entities (id),
  role       note_entity_role NOT NULL DEFAULT 'primary',
  PRIMARY KEY (note_id, entity_id)
);
CREATE UNIQUE INDEX note_entities_one_primary ON note_entities (note_id) WHERE role = 'primary';
CREATE INDEX note_entities_entity_idx ON note_entities (entity_id);

CREATE TABLE note_media (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note_id         uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
  position        smallint NOT NULL CHECK (position BETWEEN 1 AND 20),
  media_type      media_type NOT NULL,
  storage_key     text NOT NULL,
  width           int,
  height          int,
  duration_ms     int,
  is_cover        boolean NOT NULL DEFAULT false,
  alt_text        text,
  extracted_text  text,   -- OCR / transcript from the media pipeline; low search weight
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (note_id, position)
);
CREATE UNIQUE INDEX note_media_one_cover ON note_media (note_id) WHERE is_cover;

-- Ticked details, verdict fields, settings fields and creators' own fields.
-- Value column by data type:
--   single, multi, text_list -> value_options     int, money -> value_num
--   bool -> value_bool                            text, custom fields -> value_text
CREATE TABLE note_fields (
  id             bigserial PRIMARY KEY,
  note_id        uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
  field_key      text REFERENCES field_definitions (key) ON UPDATE CASCADE,
  custom_label   text,
  value_options  text[],
  value_num      bigint,
  value_bool     boolean,
  value_text     text,
  show_on_note   boolean NOT NULL,   -- filled by trigger: personal fields default to hidden
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK ((field_key IS NULL) <> (custom_label IS NULL)),
  CHECK (custom_label IS NULL OR char_length(btrim(custom_label)) BETWEEN 1 AND 40),
  CHECK (custom_label IS NULL OR value_text IS NOT NULL),
  CHECK (value_text IS NULL OR char_length(btrim(value_text)) BETWEEN 1 AND 200),
  CHECK (num_nonnulls(value_options, value_num, value_bool, value_text) = 1)
);
CREATE UNIQUE INDEX note_fields_defined_key ON note_fields (note_id, field_key) WHERE field_key IS NOT NULL;
CREATE UNIQUE INDEX note_fields_custom_key ON note_fields (note_id, lower(btrim(custom_label))) WHERE custom_label IS NOT NULL;
CREATE INDEX note_fields_field_idx ON note_fields (field_key);
CREATE INDEX note_fields_custom_label_idx ON note_fields (lower(btrim(custom_label))) WHERE custom_label IS NOT NULL;

CREATE FUNCTION note_fields_validate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_fd       field_definitions%ROWTYPE;
  v_allowed  text[];
  v_bad      text;
  v_kind     text;
BEGIN
  IF NEW.field_key IS NULL THEN
    NEW.show_on_note := coalesce(NEW.show_on_note, true);
    RETURN NEW;   -- creator's own field; table CHECKs cover it
  END IF;

  SELECT * INTO v_fd FROM field_definitions WHERE key = NEW.field_key;
  IF v_fd.status <> 'active' THEN
    RAISE EXCEPTION 'field "%" is %, it cannot be used on notes', NEW.field_key, v_fd.status
      USING ERRCODE = 'check_violation';
  END IF;
  NEW.show_on_note := coalesce(NEW.show_on_note, NOT v_fd.personal);

  CASE v_fd.data_type
    WHEN 'single', 'multi', 'text_list' THEN
      IF NEW.value_options IS NULL OR cardinality(NEW.value_options) = 0
         OR array_position(NEW.value_options, NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'field "%" needs its value(s) in value_options, with no empty items', NEW.field_key
          USING ERRCODE = 'check_violation';
      END IF;
      IF (SELECT count(DISTINCT v) FROM unnest(NEW.value_options) v) <> cardinality(NEW.value_options) THEN
        RAISE EXCEPTION 'field "%" has repeated values', NEW.field_key USING ERRCODE = 'check_violation';
      END IF;
      IF v_fd.data_type = 'single' AND cardinality(NEW.value_options) <> 1 THEN
        RAISE EXCEPTION 'field "%" takes exactly one option', NEW.field_key USING ERRCODE = 'check_violation';
      END IF;
      IF v_fd.data_type = 'text_list' THEN
        IF cardinality(NEW.value_options) > 20 THEN
          RAISE EXCEPTION 'field "%" takes at most 20 items', NEW.field_key USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM unnest(NEW.value_options) v
                   WHERE char_length(btrim(coalesce(v, ''))) NOT BETWEEN 1 AND 60) THEN
          RAISE EXCEPTION 'items in field "%" must be 1-60 characters', NEW.field_key
            USING ERRCODE = 'check_violation';
        END IF;
      ELSE
        SELECT array_agg(o ->> 'value') INTO v_allowed FROM jsonb_array_elements(v_fd.options) o;
        SELECT v INTO v_bad FROM unnest(NEW.value_options) v
         WHERE v IS NULL OR NOT (v = ANY (v_allowed)) LIMIT 1;
        IF FOUND THEN
          RAISE EXCEPTION '"%" is not an option for field "%"', coalesce(v_bad, 'NULL'), NEW.field_key
            USING ERRCODE = 'check_violation',
                  HINT = 'allowed: ' || array_to_string(v_allowed, ', ');
        END IF;
      END IF;

    WHEN 'int', 'money' THEN
      IF NEW.value_num IS NULL THEN
        RAISE EXCEPTION 'field "%" needs a number in value_num', NEW.field_key USING ERRCODE = 'check_violation';
      END IF;
      IF (v_fd.min_value IS NOT NULL AND NEW.value_num < v_fd.min_value)
         OR (v_fd.max_value IS NOT NULL AND NEW.value_num > v_fd.max_value) THEN
        RAISE EXCEPTION 'field "%" must be between % and %', NEW.field_key,
          coalesce(v_fd.min_value::text, '-inf'), coalesce(v_fd.max_value::text, 'inf')
          USING ERRCODE = 'check_violation';
      END IF;

    WHEN 'bool' THEN
      IF NEW.value_bool IS NULL THEN
        RAISE EXCEPTION 'field "%" needs true/false in value_bool', NEW.field_key USING ERRCODE = 'check_violation';
      END IF;

    WHEN 'text' THEN
      IF NEW.value_text IS NULL THEN
        RAISE EXCEPTION 'field "%" needs text in value_text', NEW.field_key USING ERRCODE = 'check_violation';
      END IF;
  END CASE;

  -- The field must be offered for the note's kind (drafts without a kind skip this)
  SELECT n.kind_id INTO v_kind FROM notes n WHERE n.id = NEW.note_id;
  IF v_kind IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM fields_for_kind(v_kind) f WHERE f.field_key = NEW.field_key) THEN
    RAISE EXCEPTION 'field "%" is not offered for kind "%"', NEW.field_key, v_kind
      USING ERRCODE = 'check_violation';
  END IF;

  RETURN NEW;
END $$;

CREATE TRIGGER note_fields_validate BEFORE INSERT OR UPDATE ON note_fields
  FOR EACH ROW EXECUTE FUNCTION note_fields_validate();

-- Tags: max 10 per note (positions 1-10). Low ranking weight in search.
CREATE TABLE tags (
  id              bigserial PRIMARY KEY,
  name            text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9]{1,40}$'),   -- normalize_tag() output
  display         text NOT NULL,
  maps_to_field   text REFERENCES field_definitions (key) ON UPDATE CASCADE,  -- #oilyskin -> skin_type
  maps_to_value   text,                                                       --            -> oily
  use_count       int NOT NULL DEFAULT 0 CHECK (use_count >= 0),
  created_at      timestamptz NOT NULL DEFAULT now(),
  CHECK ((maps_to_field IS NULL) = (maps_to_value IS NULL))
);

CREATE TABLE note_tags (
  note_id   uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
  tag_id    bigint NOT NULL REFERENCES tags (id),
  position  smallint NOT NULL CHECK (position BETWEEN 1 AND 10),
  PRIMARY KEY (note_id, tag_id),
  UNIQUE (note_id, position)
);
CREATE INDEX note_tags_tag_idx ON note_tags (tag_id);

CREATE FUNCTION note_tags_count() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    UPDATE tags SET use_count = use_count + 1 WHERE id = NEW.tag_id;
  ELSIF TG_OP = 'DELETE' THEN
    UPDATE tags SET use_count = greatest(use_count - 1, 0) WHERE id = OLD.tag_id;
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER note_tags_count AFTER INSERT OR DELETE ON note_tags
  FOR EACH ROW EXECUTE FUNCTION note_tags_count();

-- ---------------------------------------------------------------------------
-- Outbox: one pending row per note/entity that needs reindexing.
-- Rows are state-based (no event payload): the worker loads the current state
-- and upserts or deletes the search document. Repeated changes bump `version`;
-- the worker marks a row done only if the version it read is still current,
-- so a change made mid-indexing is never lost.
-- ---------------------------------------------------------------------------

CREATE TABLE outbox (
  id            bigserial PRIMARY KEY,
  aggregate     text NOT NULL CHECK (aggregate IN ('note', 'entity')),
  aggregate_id  uuid NOT NULL,
  version       int NOT NULL DEFAULT 1,
  created_at    timestamptz NOT NULL DEFAULT now(),
  available_at  timestamptz NOT NULL DEFAULT now(),   -- retry backoff
  attempts      int NOT NULL DEFAULT 0,
  last_error    text,
  processed_at  timestamptz
);
CREATE UNIQUE INDEX outbox_pending_key ON outbox (aggregate, aggregate_id) WHERE processed_at IS NULL;
CREATE INDEX outbox_pending_idx ON outbox (available_at) WHERE processed_at IS NULL;

CREATE FUNCTION outbox_enqueue(p_aggregate text, p_id uuid) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO outbox (aggregate, aggregate_id) VALUES (p_aggregate, p_id)
  ON CONFLICT (aggregate, aggregate_id) WHERE processed_at IS NULL
  DO UPDATE SET version = outbox.version + 1,
                available_at = least(outbox.available_at, now())
$$;

CREATE FUNCTION notes_enqueue() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  -- Engagement counters are pushed to the index by their own batch job
  IF TG_OP = 'UPDATE'
     AND (to_jsonb(NEW) - ARRAY['save_count', 'like_count', 'view_count', 'updated_at'])
       = (to_jsonb(OLD) - ARRAY['save_count', 'like_count', 'view_count', 'updated_at']) THEN
    RETURN NULL;
  END IF;
  PERFORM outbox_enqueue('note', coalesce(NEW.id, OLD.id));
  RETURN NULL;
END $$;

CREATE TRIGGER notes_enqueue AFTER INSERT OR UPDATE OR DELETE ON notes
  FOR EACH ROW EXECUTE FUNCTION notes_enqueue();

CREATE FUNCTION note_child_enqueue() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    PERFORM outbox_enqueue('note', NEW.note_id);
  END IF;
  IF TG_OP = 'DELETE' THEN
    PERFORM outbox_enqueue('note', OLD.note_id);
  ELSIF TG_OP = 'UPDATE' AND OLD.note_id IS DISTINCT FROM NEW.note_id THEN
    PERFORM outbox_enqueue('note', OLD.note_id);
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER note_fields_enqueue AFTER INSERT OR UPDATE OR DELETE ON note_fields
  FOR EACH ROW EXECUTE FUNCTION note_child_enqueue();
CREATE TRIGGER note_tags_enqueue AFTER INSERT OR UPDATE OR DELETE ON note_tags
  FOR EACH ROW EXECUTE FUNCTION note_child_enqueue();
CREATE TRIGGER note_entities_enqueue AFTER INSERT OR UPDATE OR DELETE ON note_entities
  FOR EACH ROW EXECUTE FUNCTION note_child_enqueue();
CREATE TRIGGER note_media_enqueue AFTER INSERT OR UPDATE OR DELETE ON note_media
  FOR EACH ROW EXECUTE FUNCTION note_child_enqueue();

-- Entity changes: the worker refreshes the entity document and its notes' entity_name.
CREATE FUNCTION entities_enqueue() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM outbox_enqueue('entity', coalesce(NEW.id, OLD.id));
  RETURN NULL;
END $$;

CREATE TRIGGER entities_enqueue AFTER INSERT OR UPDATE OR DELETE ON entities
  FOR EACH ROW EXECUTE FUNCTION entities_enqueue();

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

-- The only notes that may enter the public search index.
CREATE VIEW searchable_notes AS
  SELECT * FROM notes WHERE status = 'published' AND visibility = 'public';

-- Creators' own fields, grouped. Labels many creators add become suggested fields.
CREATE VIEW custom_field_usage AS
  SELECT lower(btrim(nf.custom_label))  AS label_norm,
         min(nf.custom_label)           AS example_label,
         n.area_id,
         n.kind_id,
         count(DISTINCT nf.note_id)     AS notes,
         count(DISTINCT n.author_id)    AS creators
  FROM note_fields nf
  JOIN notes n ON n.id = nf.note_id
  WHERE nf.custom_label IS NOT NULL AND n.status = 'published'
  GROUP BY lower(btrim(nf.custom_label)), n.area_id, n.kind_id;
