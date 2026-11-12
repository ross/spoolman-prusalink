from dataclasses import replace

import pytest

from spoolman_prusalink.models import Material, Spool
from spoolman_prusalink.tags import (
    Decision,
    SeenTag,
    decide,
    leftover,
    loaded_tag,
    unloaded,
    without_uuid,
)

TAG = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
OTHER = "1f0e7d2a-3f6c-4d7b-9a43-6c1f0e7d2a11"
MATERIAL_UUID = "0b1c6b8e-8d0f-4f8e-9a63-2b1f0d6c7e11"

# The bridge watched another UUID with spool 7 certain, so the next one appears.
WATCHED = SeenTag("before", 7)


def tag_material(instance=TAG, from_tag=True):
    return Material(
        "PLA",
        "#73767d",
        instance,
        MATERIAL_UUID if from_tag else None,
        nozzle_temperature=215,
        preheat_temperature=170,
        bed_temperature=60,
    )


def spool(id, uuid=None):
    return Spool(id, "Core One L+", "Grey", "PLA", "73767d", 1.75, 1.24, instance_uuid=uuid)


def run(seen, material, active, *, spools=None, certain=True, uuids=True, **inputs):
    """Call `decide()` for a certain spool that Spoolman lists alone, unless told otherwise."""
    spools = [active] if spools is None else spools
    return decide(seen, material, active, certain=certain, spools=spools, uuids=uuids, **inputs)


@pytest.mark.parametrize(
    ("material", "spools", "inputs", "want"),
    [
        # A tag on a spool with no UUID: adopt it.
        (tag_material(), [spool(7)], {}, Decision.ADOPT),
        # The case doesn't matter.
        (tag_material(TAG.upper()), [spool(7)], {}, Decision.ADOPT),
        # Not a tag: the bridge mints a UUID of its own.
        (tag_material(from_tag=False), [spool(7)], {}, Decision.NONE),
        # Not a tag, but the bridge's last write kept it.
        (tag_material(from_tag=False), [spool(7)], {"written_uuid": TAG}, Decision.KEEP),
        (tag_material(from_tag=False), [spool(7)], {"written_uuid": OTHER}, Decision.NONE),
        # A tag with no instance UUID.
        (tag_material(None), [spool(7)], {}, Decision.NONE),
        # The active spool isn't certain: staged, unconfirmed, or provisional. The tag
        # isn't adopted, and needn't be kept: a tool loaded from a tag isn't written.
        (tag_material(), [spool(7)], {"certain": False}, Decision.NONE),
        # Not certain and a different UUID in Spoolman: drift, not a conflict.
        (tag_material(), [spool(7, "other")], {"certain": False}, Decision.NONE),
        # Another spool has the UUID: S12 or drift, not this spool's.
        (tag_material(), [spool(7), spool(8, TAG.upper())], {}, Decision.NONE),
        # The spool has it already, or a different one.
        (tag_material(), [spool(7, TAG)], {}, Decision.NONE),
        (tag_material(), [spool(7, "other")], {}, Decision.CONFLICT),
        # An archived spool has it, so it was refused before: not proposed again.
        (tag_material(), [spool(7)], {"refused": {(7, TAG)}}, Decision.NONE),
        (tag_material(), [spool(7)], {"refused": {(8, TAG), (7, OTHER)}}, Decision.ADOPT),
        # With UUIDs off, nothing is decided.
        (tag_material(), [spool(7)], {"uuids": False}, Decision.NONE),
        (tag_material(), [spool(7, "other")], {"uuids": False}, Decision.NONE),
    ],
)
def test_decide(material, spools, inputs, want):
    active = next(s for s in spools if s.id == 7)
    assert run(WATCHED, material, active, spools=spools, **inputs)[1] is want


def test_decide_records_the_uuid_seen_with_a_certain_spool():
    seen, _ = run(WATCHED, tag_material(TAG.upper()), spool(7))
    assert seen == SeenTag(TAG, 7, appeared=True)
    # The same poll again changes nothing.
    assert run(seen, tag_material(), spool(7)) == (seen, Decision.ADOPT)
    # Recorded with UUIDs off too, so a runout leftover is still found.
    assert run(WATCHED, tag_material(), spool(7), uuids=False)[0] == seen


@pytest.mark.parametrize(
    ("material", "active", "inputs"),
    [
        (tag_material(None), spool(7), {}),
        (tag_material(), spool(7), {"certain": False}),
        (tag_material(), spool(7), {"spools": [spool(7), spool(8, TAG)]}),
    ],
)
def test_decide_keeps_the_record_when_the_uuid_says_nothing(material, active, inputs):
    """No UUID, an uncertain spool, or another spool's UUID: nothing is recorded."""
    assert run(WATCHED, material, active, **inputs) == (WATCHED, Decision.NONE)


def test_a_tag_already_loaded_at_start_is_not_trusted():
    """The bridge didn't see the tag load: the spool assigned may not be the tag's."""
    seen = SeenTag()
    for _ in range(2):
        # Not adopted, and no conflict either.
        seen, decision = run(seen, tag_material(), spool(7))
        assert (seen, decision) == (SeenTag(TAG, 7), Decision.NONE)
        assert run(seen, tag_material(), spool(7, "x")) == (seen, Decision.NONE)
    # A reload of the same tag that the bridge watched: adopted.
    seen, decision = run(without_uuid(seen), tag_material(), spool(7))
    assert (seen, decision) == (SeenTag(TAG, 7, appeared=True), Decision.ADOPT)
    # So is a different tag, and the first one after it.
    seen, decision = run(seen, tag_material(OTHER), spool(7))
    assert (seen, decision) == (SeenTag(OTHER, 7, appeared=True), Decision.ADOPT)
    assert run(seen, tag_material(), spool(7))[1] is Decision.ADOPT


def test_a_tag_shown_after_an_empty_tool_appeared():
    """With nothing recorded, a tool the bridge saw empty and then tagged is trusted."""
    seen = without_uuid(SeenTag())
    assert seen == SeenTag(gap=True) and without_uuid(seen) is seen
    assert run(seen, tag_material(), spool(7)) == (SeenTag(TAG, 7, appeared=True), Decision.ADOPT)


def test_a_tag_seen_with_another_spool_is_not_adopted():
    """A runout reload keeps the old spool's tag: the next spool doesn't take its UUID."""
    old, new = spool(6), spool(7)
    # Spool 6's tag, seen while 6 was certainly loaded; its UUID isn't in Spoolman.
    seen, decision = run(SeenTag("before", 6), tag_material(), old)
    assert decision is Decision.ADOPT
    # A runout, a swap to 7 and a reload: the printer still shows 6's tag.
    seen = unloaded(seen, runout=True)
    for _ in range(2):
        assert run(without_uuid(seen), tag_material(), new) == (
            without_uuid(seen),
            Decision.NONE,
        )
    # A different tag on 7 is adopted.
    assert run(seen, tag_material(OTHER), new) == (
        SeenTag(OTHER, 7, appeared=True),
        Decision.ADOPT,
    )


def test_a_written_uuid_seen_with_another_spool_is_not_kept():
    """A UUID the bridge wrote for spool 6 isn't kept for spool 7 after a runout reload."""
    seen = SeenTag(TAG, 6, runout=True)
    material = tag_material(from_tag=False)
    assert run(seen, material, spool(7), written_uuid=TAG) == (seen, Decision.NONE)


def test_a_claim_or_correction_makes_the_tag_the_new_spools_but_not_trusted():
    """The active spool changed with no unload: the tag was recorded with the wrong one."""
    seen = SeenTag(TAG, 6, appeared=True)
    assert run(seen, tag_material(), spool(7)) == (SeenTag(TAG, 7), Decision.NONE)
    # Unless the tool read without the UUID in between: then the bridge saw it appear.
    assert run(without_uuid(seen), tag_material(), spool(7)) == (
        SeenTag(TAG, 7, appeared=True),
        Decision.ADOPT,
    )


@pytest.mark.parametrize(
    ("material", "want"),
    [
        (tag_material(), True),
        # A tag with no instance UUID is still a tag.
        (tag_material(None), True),
        # A menu pick or the bridge's own write.
        (tag_material(from_tag=False), False),
    ],
)
def test_loaded_tag(material, want):
    assert loaded_tag(SeenTag(), material, spool(7)) is want


def test_a_runout_leftover_is_not_a_loaded_tag():
    """A runout reload keeps spool 6's tag with spool 7 active: that tag isn't 7's."""
    seen = SeenTag(TAG, 6, appeared=True)
    # With no unload since 6, a claim or correction made 7 active: the tag is 7's.
    assert loaded_tag(seen, tag_material(), spool(7)) is True
    seen = unloaded(seen, runout=True)
    assert leftover(seen, TAG.upper(), spool(7)) is True
    assert loaded_tag(seen, tag_material(TAG.upper()), spool(7)) is False
    # Spoolman gives 7 the tag's UUID: its own tag, never a leftover.
    assert loaded_tag(seen, tag_material(), spool(7, TAG.upper())) is True
    # Spool 6's own reload of it is its tag, as is another tag for spool 7.
    assert loaded_tag(seen, tag_material(), spool(6)) is True
    assert loaded_tag(seen, tag_material(OTHER), spool(7)) is True
    # With no instance UUID, a leftover can't be told apart: it counts as loaded.
    assert loaded_tag(seen, tag_material(None), spool(7)) is True


def test_an_unload_outside_a_print_makes_the_tags_spool_unknown():
    """Only a runout reload keeps a type: after any other unload the tag isn't a leftover,
    and it isn't trusted either, since the spool it was seen with may have been wrong."""
    seen = unloaded(SeenTag(TAG, 6, appeared=True, runout=True), runout=False)
    assert seen == SeenTag(TAG)
    assert loaded_tag(seen, tag_material(), spool(7)) is True
    # The tool read empty in between, and the same tag still didn't appear.
    assert run(without_uuid(seen), tag_material(), spool(7)) == (SeenTag(TAG, 7), Decision.NONE)
    # A runout after that doesn't make the unknown spool's tag a leftover.
    assert unloaded(seen, runout=True) is seen
    assert loaded_tag(seen, tag_material(), spool(7)) is True
    # A different tag after the unload appears as usual.
    assert run(seen, tag_material(OTHER), spool(7)) == (
        SeenTag(OTHER, 7, appeared=True),
        Decision.ADOPT,
    )


@pytest.mark.parametrize("appeared", [True, False])
def test_a_runout_reload_of_the_same_spool_keeps_its_tag_as_it_was(appeared):
    """A runout and a reload with spool 7 certain throughout: the tag is still 7's, and
    trusted only if the bridge saw it appear before the runout."""
    before = SeenTag(TAG, 7, appeared=appeared)
    seen = unloaded(before, runout=True)
    assert seen == replace(before, runout=True)
    assert unloaded(seen, runout=True) is seen  # A second runout changes nothing.
    # The reload is seen: a later unload is a new one.
    want = Decision.ADOPT if appeared else Decision.NONE
    assert run(seen, tag_material(), spool(7)) == (before, want)


def test_an_unload_with_nothing_recorded_changes_nothing():
    for seen in (SeenTag(), SeenTag(gap=True)):
        assert unloaded(seen, runout=False) is seen
        assert unloaded(seen, runout=True) is seen
