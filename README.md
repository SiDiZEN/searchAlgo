# Blabzu search backend

Search for Blabzu: notes in Postgres, a search index in Typesense, caches in Redis.

**Done so far:** step 0 (local stack) and step 1 (schema + posting/search vocabulary).

## One idea to keep in mind

`field_definitions` and `field_assignments` decide **both** which tick-boxes a creator sees while posting **and** which filters and boosts search uses. Posting and search read the same rows, so they can't drift apart. The rows come from YAML files in `backend/seeds/field_definitions/`.

## Setup

You need Docker Desktop and Python 3.11+.

```bash
# 1. config
cp .env.example .env                      # Windows: copy .env.example .env

# 2. start Postgres, Typesense, Redis
docker compose -f infra/docker-compose.yml --env-file .env up -d

# 3. Python env
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt

# 4. schema + vocabulary
python backend/scripts/migrate.py
python backend/scripts/seed_fields.py

# 5. tests (builds a throwaway blabzu_test database)
python -m pytest backend/tests -q
```

Check Typesense is up with `curl http://localhost:8108/health`.

## Everyday commands

| What | Command |
|---|---|
| Apply new migrations | `python backend/scripts/migrate.py` |
| See migration status | `python backend/scripts/migrate.py --status` |
| Wipe and rebuild the dev DB | `python backend/scripts/migrate.py --reset --yes` then re-seed |
| Validate YAML only (no DB) | `python backend/scripts/seed_fields.py --check` |
| Sync YAML into Postgres | `python backend/scripts/seed_fields.py` |
| Run tests | `python -m pytest backend/tests -q` |
| Open a SQL shell | `docker compose -f infra/docker-compose.yml exec postgres psql -U blabzu` |

## Layout

```
infra/docker-compose.yml          Postgres 16, Typesense 30.1, Redis 7
backend/migrations/0001_core.sql  schema, triggers, views
backend/seeds/field_definitions/  the vocabulary (YAML)
  _common.yaml                    area tiles, prompt library, shared fields
  beauty.yaml fashion.yaml gadgets.yaml food.yaml other.yaml
backend/scripts/                  migrate.py, seed_fields.py
backend/tests/                    pytest suite (DB rules + seed rules)
```

## Schema map

| Table | Holds |
|---|---|
| `areas`, `kinds` | Area tiles and "What kind?" options. Kind ids look like `beauty.sunscreen`; each kind has a template (product, place, …) |
| `field_definitions` | Every tick-box field: type, options + synonyms, min/max, search role, facetable, personal |
| `field_assignments` | Which fields show for which area / template / kind, on which step, pre-ticked or not, plus config like price bands |
| `prompt_definitions` | Prompt library ("Worth it because…", "Skip it if…") |
| `users` | Stub until real auth |
| `entities` | Product/place catalog with aliases, location and dedup status |
| `notes` | The note: type, format, kind, title, prompts, promotion answer, visibility, location, counters |
| `note_entities` | What the note is about (primary / compared / mentioned) |
| `note_fields` | Ticked values and creators' own fields |
| `note_media` | Photos/video/audio, cover, OCR/transcript text |
| `tags`, `note_tags` | Normalised tags, max 10 per note |
| `outbox` | Notes/entities waiting to be reindexed |

Useful SQL:

- `SELECT * FROM fields_for_kind('beauty.sunscreen');` gives the fields a creator sees for that kind, most specific assignment winning.
- `searchable_notes` view: the only notes allowed in the public index (published + public).
- `custom_field_usage` view: creators' own fields grouped by label. Labels many creators add are candidates for new suggested fields.

## Rules the database enforces

- A field value must match its type and options, stay within min/max, and the field must be offered for the note's kind.
- Personal fields (skin tone, height) are hidden on the note unless the creator turns them on.
- "Promotion" needs a kind of promotion, and the kind must fit the template (no "free visit" on a product).
- Publishing needs area, kind and title, and a promotion answer for everything except questions.
- Prompts must come from the library, fit the template, appear once each, max 5.
- Max 10 tags per note.
- Every change that could affect a search document queues one `outbox` row per note. Repeated changes bump its `version` instead of adding rows. Changes to save/like/view counters are not queued; a batch job pushes those.

## Adding or changing a field

1. Edit the YAML (define the field once, then `assign` it where it should show).
2. `python backend/scripts/seed_fields.py --check`
3. `python backend/scripts/seed_fields.py`
4. `python -m pytest backend/tests -q`

Removing a field from YAML retires it (never deletes; old notes keep their values). Changing the type of a field that notes already use is refused; that needs a migration.

Search roles: `hard` = filter, `soft` = ranking boost, `rank` = quality signal, `text` = searchable text, `display` = shown only. Hard and soft fields must be facetable. Personal fields can only be soft or display.

## Next steps

2. Notes API (FastAPI): create, publish, edit, visibility, validated against the vocabulary
3. Seed notes (300–500) + golden query set
4. Index worker: `outbox` → Typesense `notes` and `entities`
5. Search endpoint: parser, hard/soft filters, multi-search, relax on zero results
6. Typeahead + nightly suggestions job
7. Wire the Flutter screens
