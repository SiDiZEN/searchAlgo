"""Database rules for notes, their fields, tags and the reindex outbox."""

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import rejected


def add_field(conn, note_id, key=None, *, options=None, num=None, flag=None, text=None,
              custom_label=None, show=None):
    return conn.execute(
        "INSERT INTO note_fields (note_id, field_key, custom_label, value_options, value_num,"
        " value_bool, value_text, show_on_note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING show_on_note",
        (note_id, key, custom_label, options, num, flag, text, show)).fetchone()[0]


def pending(conn, note_id):
    return conn.execute(
        "SELECT version FROM outbox WHERE aggregate = 'note' AND aggregate_id = %s AND processed_at IS NULL",
        (note_id,)).fetchall()


# --- Publishing rules --------------------------------------------------------

def test_publish_fills_area_and_published_at(conn, make_note):
    nid = make_note("beauty.sunscreen", status="published", promo="non_promotion")
    area, published_at = conn.execute("SELECT area_id, published_at FROM notes WHERE id = %s", (nid,)).fetchone()
    assert area == "beauty" and published_at is not None


def test_review_cannot_publish_without_promotion_answer(conn, make_note):
    with rejected(conn):
        make_note("beauty.sunscreen", status="published")


def test_question_can_publish_without_promotion_answer(conn, make_note):
    make_note("beauty.sunscreen", status="published", note_type="question")


def test_publish_needs_a_kind_and_title(conn, make_note):
    with rejected(conn):
        make_note(None, status="published", promo="non_promotion")
    with rejected(conn):
        make_note("beauty.sunscreen", status="published", promo="non_promotion", title=None)


def test_promotion_needs_a_kind_of_promotion(conn, make_note):
    with rejected(conn):
        make_note(promo="promotion")
    with rejected(conn):
        make_note(promo="non_promotion", promo_kind="gifted")
    make_note(promo="promotion", promo_kind="gifted")


def test_promotion_kind_must_fit_template(conn, make_note):
    with rejected(conn, match="for places"):
        make_note("beauty.sunscreen", promo="promotion", promo_kind="free_visit")
    with rejected(conn, match="for products"):
        make_note("food.cafe", promo="promotion", promo_kind="gifted")
    make_note("food.cafe", promo="promotion", promo_kind="free_visit")


def test_kind_must_belong_to_area(conn, make_note):
    with rejected(conn):
        make_note("beauty.sunscreen", area_id="food")


# --- Prompts -----------------------------------------------------------------

def test_valid_prompts(conn, make_note):
    make_note(prompts=Jsonb([{"key": "worth_it_because", "answer": "No white cast"},
                             {"key": "how_i_use_it", "answer": "Two fingers, every morning"}]))


@pytest.mark.parametrize("prompts, match", [
    ([{"key": "nope", "answer": "x"}], "unknown"),
    ([{"key": "must_order", "answer": "x"}], "unknown or not available"),   # place prompt on a product
    ([{"key": "skip_it_if", "answer": "a"}, {"key": "skip_it_if", "answer": "b"}], "only once"),
    ([{"key": "skip_it_if"}], "must look like"),
    ([{"key": "skip_it_if", "answer": "   "}], "1-300"),
    ([{"key": k, "answer": "x"} for k in
      ("worth_it_because", "skip_it_if", "best_for", "wish_i_knew", "how_i_use_it", "compared_to")], "at most 5"),
])
def test_bad_prompts(conn, make_note, prompts, match):
    with rejected(conn, match=match):
        make_note(prompts=Jsonb(prompts))


# --- Ticked fields -----------------------------------------------------------

def test_valid_fields(conn, make_note):
    nid = make_note("beauty.sunscreen")
    add_field(conn, nid, "skin_type", options=["oily", "combination"])
    add_field(conn, nid, "price_paid", num=499)
    add_field(conn, nid, "pack_size", text="50 g")
    add_field(conn, nid, "would_buy_again", flag=True)
    add_field(conn, nid, "used_for_months", num=6)


@pytest.mark.parametrize("key, kwargs, match", [
    ("skin_type", {"options": ["shiny"]}, "not an option"),
    ("hair_type", {"options": ["wavy"]}, "not offered"),            # hair field on a sunscreen
    ("white_cast", {"options": ["no_cast", "heavy_cast"]}, "exactly one"),
    ("skin_type", {"options": ["oily", "oily"]}, "repeated"),
    ("skin_type", {"options": ["oily", None]}, "no empty items"),
    ("price_paid", {"num": -1}, "between"),
    ("rating", {"num": 6}, "between"),
    ("price_paid", {"text": "499"}, "needs a number"),
    ("ram_gb", {"num": 8}, "not offered"),                         # gadget field on beauty
])
def test_bad_fields(conn, make_note, key, kwargs, match):
    nid = make_note("beauty.sunscreen")
    with rejected(conn, match=match):
        add_field(conn, nid, key, **kwargs)


def test_text_list_limits(conn, make_note):
    nid = make_note("food.cafe")
    add_field(conn, nid, "dishes_tried", options=["Cold coffee", "Masala maggi"])
    with rejected(conn, match="1-60"):
        add_field(conn, make_note("food.cafe"), "dishes_tried", options=["x" * 61])


def test_one_value_per_field_per_note(conn, make_note):
    nid = make_note()
    add_field(conn, nid, "price_paid", num=499)
    with rejected(conn, psycopg.errors.UniqueViolation):
        add_field(conn, nid, "price_paid", num=599)


def test_personal_fields_hidden_by_default(conn, make_note):
    nid = make_note("beauty.sunscreen")
    assert add_field(conn, nid, "skin_tone", options=["medium_deep"]) is False
    assert add_field(conn, nid, "skin_type", options=["oily"]) is True
    nid2 = make_note("beauty.makeup")
    assert add_field(conn, nid2, "skin_tone", options=["deep"], show=True) is True


def test_custom_fields(conn, make_note):
    nid = make_note()
    assert add_field(conn, nid, custom_label="Fragrance", text="None, unscented") is True
    with rejected(conn, psycopg.errors.UniqueViolation):
        add_field(conn, nid, custom_label="  fragrance ", text="Mild")
    with rejected(conn):
        add_field(conn, nid, custom_label="Texture", num=3)            # custom fields are text


def test_retired_field_cannot_be_used(conn, make_note):
    conn.execute("UPDATE field_definitions SET status = 'retired' WHERE key = 'pack_size'")
    nid = make_note()
    with rejected(conn, match="retired"):
        add_field(conn, nid, "pack_size", text="50 g")


def test_custom_field_usage_view(conn, make_note):
    for _ in range(2):
        nid = make_note(status="published", promo="non_promotion")
        add_field(conn, nid, custom_label="Texture", text="Gel")
    row = conn.execute(
        "SELECT notes FROM custom_field_usage WHERE label_norm = 'texture' AND kind_id = 'beauty.sunscreen'"
    ).fetchone()
    assert row[0] == 2


# --- Tags --------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("#Oily Skin!", "oilyskin"),
    ("oily-skin", "oilyskin"),
    ("##SPF50", "spf50"),
    ("#", None),
    ("सनस्क्रीन", None),
])
def test_normalize_tag(conn, raw, expected):
    assert conn.execute("SELECT normalize_tag(%s)", (raw,)).fetchone()[0] == expected


def test_max_ten_tags_and_use_count(conn, make_note):
    nid = make_note()
    tag_ids = [conn.execute("INSERT INTO tags (name, display) VALUES (%s, %s) RETURNING id",
                            (f"tag{i}", f"#tag{i}")).fetchone()[0] for i in range(11)]
    for pos, tid in enumerate(tag_ids[:10], start=1):
        conn.execute("INSERT INTO note_tags (note_id, tag_id, position) VALUES (%s, %s, %s)", (nid, tid, pos))
    with rejected(conn):
        conn.execute("INSERT INTO note_tags (note_id, tag_id, position) VALUES (%s, %s, 11)", (nid, tag_ids[10]))
    assert conn.execute("SELECT use_count FROM tags WHERE id = %s", (tag_ids[0],)).fetchone()[0] == 1
    conn.execute("DELETE FROM note_tags WHERE note_id = %s AND tag_id = %s", (nid, tag_ids[0]))
    assert conn.execute("SELECT use_count FROM tags WHERE id = %s", (tag_ids[0],)).fetchone()[0] == 0


# --- Outbox and searchability -----------------------------------------------

def test_changes_coalesce_into_one_pending_row(conn, make_note):
    nid = make_note()
    assert pending(conn, nid) == [(1,)]
    conn.execute("UPDATE notes SET title = 'Better title' WHERE id = %s", (nid,))
    add_field(conn, nid, "price_paid", num=499)
    rows = pending(conn, nid)
    assert len(rows) == 1 and rows[0][0] == 3


def test_counter_updates_do_not_reindex(conn, make_note):
    nid = make_note()
    conn.execute("UPDATE notes SET save_count = save_count + 5, like_count = 2 WHERE id = %s", (nid,))
    assert pending(conn, nid) == [(1,)]


def test_new_change_after_processing_creates_new_row(conn, make_note):
    nid = make_note()
    conn.execute("UPDATE outbox SET processed_at = now() WHERE aggregate_id = %s", (nid,))
    assert pending(conn, nid) == []
    conn.execute("UPDATE notes SET visibility = 'only_me' WHERE id = %s", (nid,))
    assert pending(conn, nid) == [(1,)]


def test_deleting_a_note_leaves_a_pending_row(conn, make_note):
    nid = make_note()
    add_field(conn, nid, "price_paid", num=499)
    conn.execute("UPDATE outbox SET processed_at = now() WHERE aggregate_id = %s", (nid,))
    conn.execute("DELETE FROM notes WHERE id = %s", (nid,))
    assert len(pending(conn, nid)) == 1


def test_entity_changes_are_queued(conn):
    eid = conn.execute(
        "INSERT INTO entities (type, name, area_id, kind_id) VALUES ('product', 'Test SPF 50', 'beauty',"
        " 'beauty.sunscreen') RETURNING id").fetchone()[0]
    assert conn.execute("SELECT count(*) FROM outbox WHERE aggregate = 'entity' AND aggregate_id = %s",
                        (eid,)).fetchone()[0] == 1


def test_active_place_needs_a_city(conn):
    with rejected(conn):
        conn.execute("INSERT INTO entities (type, name, area_id, kind_id, status)"
                     " VALUES ('place', 'Test Café', 'food', 'food.cafe', 'active')")


def test_only_published_public_notes_are_searchable(conn, make_note):
    public = make_note(status="published", promo="non_promotion")
    private = make_note(status="published", promo="non_promotion", visibility="only_me")
    draft = make_note()
    ids = {r[0] for r in conn.execute("SELECT id FROM searchable_notes WHERE id = ANY (%s)",
                                      ([public, private, draft],))}
    assert ids == {public}
