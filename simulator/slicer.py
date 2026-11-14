"""What the stock PrusaSlicer profiles of the toolchangers extrude outside the model.

A print's segments are the model's own extrusion. Around them the stock start, tool-change
and end G-code prime each tool, ram and retract the outgoing tool at a change, purge the
incoming one, and park the last tool at the end. The firmware runs those moves as any other
print move, so they count on the picked tool through the filament tracker.

This is slicer profile data, not firmware: `script/firmware-check` doesn't compare it. The
numbers come from PrusaSlicer 2.9.7 with the PrusaResearch vendor bundle 2.5.16, measured
in G-code sliced with the stock printer, print and PLA presets of the XL 2T and 5T
(`0.20mm SPEED @XLIS 0.4`, `Prusament PLA @XLIS`) and the Core One INDX 4T and 8T
(`0.20mm Balanced @COREONEINDX HF0.4`, `Prusament PLA @COREONEINDX HF0.4`). The bundle has
no Core One L INDX, so both INDX models use the Core One INDX numbers.

A travel retract and its restore that the model's own extrusion makes on both sides of a
move cancel in the tracker, so they're left out: the XL's restore before the wipe tower's
ramming is one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

Moves = tuple[float, ...]  # signed E moves in commanded mm, run in order on one tool


@dataclass(frozen=True)
class Purge:
    """The moves on one tool around a length a print can override: `before`, then `mm`,
    then `after`."""

    before: Moves
    mm: float
    after: Moves = ()

    def moves(self) -> Moves:
        return (*self.before, self.mm, *self.after)


@dataclass(frozen=True)
class Slicer:
    """A stock toolchanger profile's E moves outside the model, on the tool picked when each
    runs.

    The start G-code runs `cleanup` on the print's first tool. Then `G427` purges every
    tool the print uses in the nozzle cleaner where `calibrates` is set: that's the
    firmware's `purge_clean`, not profile data. Then each other used tool, ascending, is
    picked and runs `prime_others`, where the profile primes every used tool, and last the
    first tool is picked and runs `prime`. Unused tools aren't picked.

    At a tool change the outgoing tool runs `park_out` before and during its park. The
    incoming tool runs `first_pick` when the print hasn't printed with it yet, and
    `later_pick` after that. At the end the last tool runs `tower_end`, where the print
    uses several tools, then `end`, and the end G-code parks it (`P0 S1`).
    """

    cleanup: Moves
    calibrates: bool
    prime: Purge
    prime_others: Purge | None
    park_out: Moves
    first_pick: Purge
    later_pick: Purge
    tower_end: Moves
    end: Moves

    def overridden(self, prime_mm: float | None, purge_mm: float | None) -> Slicer:
        """This profile with the print's own prime and tool-change purge, where it sets them."""
        out = self
        if prime_mm is not None:
            others = self.prime_others and replace(self.prime_others, mm=prime_mm)
            out = replace(out, prime=replace(self.prime, mm=prime_mm), prime_others=others)
        if purge_mm is not None:
            out = replace(
                out,
                first_pick=replace(self.first_pick, mm=purge_mm),
                later_pick=replace(self.later_pick, mm=purge_mm),
            )
        return out


# The XL multi-tool profiles (`[printer:*XLMULTIIS*]` start G-code, `*XLMULTI*` tool-change
# and end G-code, filament `*PLAXL*`).
XL_SLICER = Slicer(
    # `G1 E-2`, the retract for the nozzle cleanup, never fed back.
    cleanup=(-2.0,),
    calibrates=False,
    # 30 + 9 mm, then the print's retract, which its first move feeds back.
    prime=Purge((), 39.0, (-0.8, 0.8)),
    # 30 + 9 mm, then the filament's `filament_retract_length_toolchange`, which the
    # tool's first pick feeds back.
    prime_others=Purge((), 39.0, (-20.0,)),
    # The wipe tower's multi-tool ramming, 10 mm³, then the filament's 20 mm retract.
    park_out=(4.1575, -20.0),
    # The 20 mm back, then the wipe tower's purge: the first layer's fills the tower's
    # cleaning box. A tool's first pick in a print stands for the first layer.
    first_pick=Purge((20.0,), 9.238),
    later_pick=Purge((20.0,), 7.0076),
    # The tower's last ramming on the last layer. A print with one tool has no tower.
    tower_end=(4.1575,),
    end=(-0.8,),
)

# The Core One INDX profiles (`[printer:*C1_INDX_8T_common*]`, which the 4T inherits,
# filament `*C1INDX_common*`). They purge at the purge station, with no wipe tower.
INDX_SLICER = Slicer(
    cleanup=(),
    # `G427 R2 P3`: `G750 E25`, then the `M1705 N` auto-retract, on each used tool.
    calibrates=True,
    # The initial tool at the purge station: 1.6 mm, a flush of 1.1 x 12 mm³, then the
    # print's retract, which its first move feeds back.
    prime=Purge((1.6,), 5.4907, (-0.8, 0.8)),
    prime_others=None,
    # The print's tool-change retract, then the firmware's 8 mm in `G27 R8` as it parks.
    park_out=(-0.8, -8.0),
    # 1.6 mm, a 10 mm³ flush, then a retract that the first move after it feeds back.
    first_pick=Purge((1.6,), 4.1596, (-0.8, 0.8)),
    # On a tool parked by this print, the lock move `G1 E1.6`, the 8 mm of `G27 R8`, and
    # the print's 0.8 mm come back first.
    later_pick=Purge((1.6, 8.0, 0.8), 4.1596, (-0.8, 0.8)),
    tower_end=(),
    end=(-0.8,),
)
