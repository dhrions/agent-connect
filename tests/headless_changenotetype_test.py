# Headless verification for Agent Connect changeNoteType (SPEC 34).
#
# Run with the Anki venv python:  <anki-venv>/bin/python tests/headless_changenotetype_test.py
#
# Loads core.py STANDALONE (no package __init__, no aqt) against a FRESH scratch
# collection; never touches a real Anki profile.

import importlib.util
import os
import shutil
import sys
import tempfile
import time
import traceback

SCRATCH = (os.environ.get("ANCP_TEST_SCRATCH")
           or tempfile.mkdtemp(prefix="ancp_cnt_"))
CORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "connect_plus", "core.py")

assert "Anki2" not in SCRATCH

if os.path.exists(SCRATCH):
    shutil.rmtree(SCRATCH)
os.makedirs(SCRATCH)

spec = importlib.util.spec_from_file_location("core", CORE_PATH)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
assert "aqt" not in sys.modules, "core.py (or its imports) pulled in aqt"

import anki.lang
anki.lang.set_lang("en_US")
from anki.collection import Collection

col = Collection(os.path.join(SCRATCH, "test.anki2"))

REVERSED = "Basic (and reversed card)"
RESULTS = []


def run(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print("PASS  %s" % name)
    except Exception:
        tb = traceback.format_exc()
        RESULTS.append((name, False, tb))
        print("FAIL  %s\n%s" % (name, tb))


def note_count():
    return col.db.scalar("select count() from notes") or 0


def mk(model_name, fields, deck="Default"):
    model = col.models.by_name(model_name)
    note = col.new_note(model)
    for key, value in fields.items():
        note[key] = value
    col.add_note(note, col.decks.id(deck))
    return note.id


def err_code(fn):
    try:
        fn()
    except core.PlusError as exc:
        return exc.code
    return None


def mid_of(name):
    return col.models.by_name(name)["id"]


# ---------------------------------------------------------------- the point:
# retained cards keep their id, so revlog / scheduling survive the conversion
def test_keeps_cards_and_revlog():
    nid = mk("Basic", {"Front": "k-front", "Back": "k-back"})
    old_cids = list(col.card_ids_of_note(nid))
    assert len(old_cids) == 1, old_cids
    cid = old_cids[0]
    col.db.execute(
        "insert into revlog (id,cid,usn,ease,ivl,lastIvl,factor,time,type) "
        "values (?,?,?,?,?,?,?,?,?)",
        int(time.time() * 1000), cid, -1, 3, 10, 1, 2500, 1500, 0)
    old_mid = col.get_note(nid).mid

    res = core.change_note_type(col, [nid], REVERSED)

    note = col.get_note(nid)
    assert note.mid != old_mid, "mid must change"
    assert note.mid == mid_of(REVERSED)
    assert cid in col.card_ids_of_note(nid), "original card id must be preserved"
    assert col.db.scalar("select count() from revlog where cid=?", cid) == 1, \
        "revlog of the retained card must survive"
    assert res["changed"] == 1 and res["changedIds"] == [nid], res
    assert res["oldNotetype"] == "Basic" and res["newNotetype"] == REVERSED, res
    assert res["schemaModified"] is True, res
    assert res["undoEntry"] == core.UNDO_CHANGE_NOTETYPE, res
    assert res["templatesKept"] == 1 and res["templatesAdded"] == 1 \
        and res["templatesDropped"] == 0, res
    assert res["fieldMapping"] == [{"new": "Front", "from": "Front"},
                                   {"new": "Back", "from": "Back"}], res


def test_undoable():
    nid = mk("Basic", {"Front": "u-front", "Back": "u-back"})
    old_mid = col.get_note(nid).mid
    core.change_note_type(col, [nid], REVERSED)
    assert col.undo_status().undo == core.UNDO_CHANGE_NOTETYPE
    col.undo()
    assert col.get_note(nid).mid == old_mid, "undo must restore the source notetype"


def test_dry_run_writes_nothing():
    nid = mk("Basic", {"Front": "d-front", "Back": "d-back"})
    old_mid = col.get_note(nid).mid
    before = note_count()
    res = core.change_note_type(col, [nid], REVERSED, dry_run=True)
    assert col.get_note(nid).mid == old_mid, "dry run must not change the note"
    assert note_count() == before
    assert res["wouldChange"] == 1 and res["undoEntry"] is None, res
    assert "changed" not in res and "schemaModified" not in res, res
    assert res["templatesKept"] == 1 and res["templatesAdded"] == 1, res


def test_field_map_swap():
    nid = mk("Basic", {"Front": "FF", "Back": "BB"})
    res = core.change_note_type(col, [nid], REVERSED,
                                field_map={"Front": "Back", "Back": "Front"})
    note = col.get_note(nid)
    assert note["Front"] == "BB" and note["Back"] == "FF", dict(note.items())
    assert res["fieldMapping"] == [{"new": "Front", "from": "Back"},
                                   {"new": "Back", "from": "Front"}], res


def test_field_map_null_empties_target():
    nid = mk("Basic", {"Front": "x", "Back": "y"})
    res = core.change_note_type(col, [nid], REVERSED, field_map={"Back": None})
    note = col.get_note(nid)
    assert note["Front"] == "x" and note["Back"] == "", dict(note.items())
    assert res["fieldMapping"][1] == {"new": "Back", "from": None}, res


def test_templates_dropped_dry():
    nid = mk(REVERSED, {"Front": "a", "Back": "b"})
    assert len(col.card_ids_of_note(nid)) == 2
    res = core.change_note_type(col, [nid], "Basic", dry_run=True)
    assert res["templatesKept"] == 1 and res["templatesDropped"] == 1 \
        and res["templatesAdded"] == 0, res


def test_templates_dropped_real_deletes_card():
    nid = mk(REVERSED, {"Front": "a", "Back": "b"})
    assert len(col.card_ids_of_note(nid)) == 2
    core.change_note_type(col, [nid], "Basic")
    assert len(col.card_ids_of_note(nid)) == 1, "dropped template's card must be removed"


def test_batch_same_source():
    ids = [mk("Basic", {"Front": "m%d" % i, "Back": "b"}) for i in range(5)]
    res = core.change_note_type(col, ids, REVERSED)
    assert res["changed"] == 5 and sorted(res["changedIds"]) == sorted(ids), res
    for nid in ids:
        assert col.get_note(nid).mid == mid_of(REVERSED)


def test_custom_undo_label():
    nid = mk("Basic", {"Front": "a", "Back": "b"})
    res = core.change_note_type(col, [nid], REVERSED, undo_label="my conv")
    assert res["undoEntry"] == "Agent Connect: my conv", res
    assert col.undo_status().undo == "Agent Connect: my conv"


def test_mixed_notetypes_refused():
    a = mk("Basic", {"Front": "a", "Back": "b"})
    b = mk(REVERSED, {"Front": "c", "Back": "d"})
    assert err_code(lambda: core.change_note_type(col, [a, b], "Cloze")) == "validation_error"


def test_unknown_target_not_found():
    nid = mk("Basic", {"Front": "a", "Back": "b"})
    assert err_code(lambda: core.change_note_type(col, [nid], "NoSuchModel")) == "not_found"


def test_same_type_refused():
    nid = mk("Basic", {"Front": "a", "Back": "b"})
    assert err_code(lambda: core.change_note_type(col, [nid], "Basic")) == "validation_error"


def test_missing_note_not_found():
    assert err_code(lambda: core.change_note_type(col, [999999999], REVERSED)) == "not_found"


def test_bad_fieldmap_key():
    nid = mk("Basic", {"Front": "a", "Back": "b"})
    assert err_code(lambda: core.change_note_type(
        col, [nid], REVERSED, field_map={"Nope": "Front"})) == "invalid_param"


def test_bad_fieldmap_value():
    nid = mk("Basic", {"Front": "a", "Back": "b"})
    assert err_code(lambda: core.change_note_type(
        col, [nid], REVERSED, field_map={"Front": "Ghost"})) == "invalid_param"


def test_param_guards():
    nid = mk("Basic", {"Front": "a", "Back": "b"})
    assert err_code(lambda: core.change_note_type(col, [], REVERSED)) == "invalid_param"
    assert err_code(lambda: core.change_note_type(col, "notalist", REVERSED)) == "invalid_param"
    assert err_code(lambda: core.change_note_type(col, [True], REVERSED)) == "invalid_param"
    assert err_code(lambda: core.change_note_type(col, [nid], "")) == "invalid_param"
    assert err_code(lambda: core.change_note_type(col, [nid], REVERSED, dry_run="yes")) == "invalid_param"
    assert err_code(lambda: core.change_note_type(col, [nid], REVERSED, field_map=[])) == "invalid_param"
    assert err_code(lambda: core.change_note_type(col, [nid], REVERSED, undo_label="   ")) == "invalid_param"


for _name, _fn in sorted((n, f) for n, f in globals().items()
                         if n.startswith("test_") and callable(f)):
    run(_name, _fn)

col.close()

failures = [r for r in RESULTS if not r[1]]
print("\n%d/%d passed" % (len(RESULTS) - len(failures), len(RESULTS)))
sys.exit(1 if failures else 0)
