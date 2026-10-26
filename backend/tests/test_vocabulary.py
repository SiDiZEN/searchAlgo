"""The seeded vocabulary must support the search rules we agreed on."""

from seed_fields import apply_seed, load_seed

LAUNCH = ("beauty", "fashion", "gadgets", "food")


def fields_for(conn, kind_id):
    return {r[0]: r for r in conn.execute(
        "SELECT field_key, step::text, default_ticked, config FROM fields_for_kind(%s)", (kind_id,))}


def launch_kinds(conn, template=None):
    q = "SELECT id FROM kinds WHERE area_id = ANY (%s) AND is_active"
    params = [list(LAUNCH)]
    if template:
        q += " AND template = %s::template_kind"
        params.append(template)
    return [r[0] for r in conn.execute(q, params)]


def test_launch_areas(conn):
    rows = conn.execute("SELECT id FROM areas WHERE is_launch ORDER BY sort_order").fetchall()
    assert [r[0] for r in rows] == ["beauty", "fashion", "gadgets", "food"]


def test_every_launch_kind_has_details_to_tick(conn):
    for kind in launch_kinds(conn):
        steps = {f[1] for f in fields_for(conn, kind).values()}
        assert "details" in steps, kind


def test_every_launch_kind_has_a_price_field(conn):
    # Price was made pre-ticked because it matters so much for search
    price_fields = {"price_paid", "cost_for_two", "price_level"}
    for kind in launch_kinds(conn):
        fields = fields_for(conn, kind)
        present = price_fields & set(fields)
        assert present, f"{kind} has no price field"
        assert any(fields[p][2] for p in present), f"{kind} price field is not pre-ticked"


def test_verdict_fields_follow_template(conn):
    for kind in launch_kinds(conn, "product"):
        assert {"rating", "used_for_months"} <= set(fields_for(conn, kind)), kind
    for kind in launch_kinds(conn, "place"):
        f = fields_for(conn, kind)
        assert {"rating", "visits"} <= set(f), kind
        assert "used_for_months" not in f, kind


def test_most_specific_assignment_wins(conn):
    phone = fields_for(conn, "gadgets.phone")["price_paid"][3]
    speaker = fields_for(conn, "gadgets.speaker")["price_paid"][3]
    assert phone["price_bands"][0] == 10000
    assert speaker["price_bands"][0] == 1000


def test_kind_scoped_fields_stay_scoped(conn):
    assert "skin_type" not in fields_for(conn, "beauty.fragrance")
    assert "longevity" in fields_for(conn, "beauty.fragrance")
    assert "white_cast" in fields_for(conn, "beauty.sunscreen")
    assert "white_cast" not in fields_for(conn, "beauty.serum")
    assert "ram_gb" not in fields_for(conn, "gadgets.earbuds")


def test_hard_and_soft_fields_are_facetable(conn):
    bad = conn.execute("SELECT key FROM field_definitions WHERE search_role IN ('hard','soft') AND NOT facetable").fetchall()
    assert bad == []


def test_index_names_use_prefix(conn):
    assert conn.execute("SELECT index_name FROM field_definitions WHERE key = 'skin_type'").fetchone()[0] == "f_skin_type"


def test_audience_options_scoped_to_real_areas(conn):
    options = conn.execute("SELECT options FROM field_definitions WHERE key = 'audience'").fetchone()[0]
    areas = {r[0] for r in conn.execute("SELECT id FROM areas")}
    for opt in options:
        assert set(opt.get("areas", [])) <= areas


def test_reseeding_is_idempotent(conn):
    before = conn.execute("SELECT count(*) FROM field_assignments").fetchone()[0]
    apply_seed(conn, load_seed())
    apply_seed(conn, load_seed())
    after = conn.execute("SELECT count(*) FROM field_assignments").fetchone()[0]
    assert before == after > 0
    assert conn.execute("SELECT count(*) FROM field_definitions WHERE status <> 'active'").fetchone()[0] == 0
