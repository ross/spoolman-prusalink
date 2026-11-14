"""What the stock PrusaSlicer profiles of the toolchangers and the MMU3 extrude outside the
model.

A print's segments are the model's own extrusion. Around them the stock start, tool-change
and end G-code prime each tool, ram and retract the outgoing tool at a change, purge the
incoming one, and park the last tool at the end. The firmware runs those moves as any other
print move, so they count on the picked tool through the filament tracker.

The MMU3 profiles differ: the firmware's own moves at a `T` and at `M702` aren't profile
data, and `model` runs them between the profile's (`Mmu` in `simulator.feed`).

This is slicer profile data, not firmware: `script/firmware-check` doesn't compare it. The
numbers come from PrusaSlicer 2.9.7 with the PrusaResearch vendor bundle 2.5.16, measured
in G-code sliced with the stock printer, print and PLA presets of the XL 2T and 5T
(`0.20mm SPEED @XLIS 0.4`, `Prusament PLA @XLIS`) and the Core One INDX 4T and 8T
(`0.20mm Balanced @COREONEINDX HF0.4`, `Prusament PLA @COREONEINDX HF0.4`). The bundle has
no Core One L INDX, so both INDX models use the Core One INDX numbers.

The MMU3's numbers come from the same PrusaSlicer and bundle, in G-code sliced with the
MK4S, Core One and Core One L MMU3 0.4 presets (`Original Prusa MK4S MMU3 0.4 nozzle`,
`Prusa CORE One MMU3 0.4 nozzle`, `Prusa CORE One L MMU3 0.4 nozzle`), their default print
presets (`0.20mm SPEED @MK4S 0.4`, `0.20mm SPEED @COREONE 0.4`, `0.20mm SPEED @COREONEL
0.4`), and `Prusament PLA` and `Prusament PETG` on the MK4S and Core One. The Core One L was
sliced with PLA alone and gave the Core One's moves. The MK4, MK3.9 and MK3.9S presets
weren't sliced, so those models take the MK4S numbers.

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


# The MMU3 wipe tower's unload of the old slot before the `T`, the same for every filament
# (`[printer:*MMU3*]`): 15 mm, then `cooling_tube_retraction` 45 + `cooling_tube_length`
# 20 / 2 - 15 = 40 mm split 70/20/10 %; then a cooling move of `cooling_tube_length` out and
# back, `filament_cooling_moves` times, with a stamping of `filament_stamping_distance` 45 +
# 20 / 2 = 55 mm between each two; last the park, `parking_pos_retraction` 84 - 45 - 10 =
# 29 mm. Whatever the cooling, the unload nets -84.
MMU_UNLOAD_RETRACTS: Moves = (-15.0, -28.0, -8.0, -4.0)
MMU_COOLING: Moves = (20.0, -20.0)
MMU_STAMPING: Moves = (50.0, 5.0, -55.0)
MMU_PARK = -29.0
# The new slot's load after the `T`: `parking_pos_retraction` 84 + `extra_loading_move` -52
# = 32 mm, split 20/70/10 %.
MMU_LOAD: Moves = (6.4, 22.4, 3.2)


@dataclass(frozen=True)
class MmuFilament:
    """One filament preset's moves in a stock MMU3 profile's wipe tower.

    The old slot's filament decides the ramming and the cooling moves, and the new slot's
    decides the purge after the load. A slot's first pick in the print stands for the
    first layer's change, whose new slot also prints the tower's first layer.
    """

    ramming: Moves  # the ramming's moves with X, before the unload
    cooling_moves: int  # `filament_cooling_moves`
    first_purge: float  # the purge of the first layer's first change: wipe and tower
    later_purge: float  # the purge of a later layer's first change: wipe and tower
    retract: float  # the print's last retract: `retract_length`, or the filament's own

    def park_out(self) -> Moves:
        """The old slot's moves before the `T`: the ramming, the unload's retracts, the
        cooling moves with a stamping between each two, and the park."""
        cooling: list[float] = []
        for i in range(self.cooling_moves):
            cooling += [*(MMU_STAMPING if i else ()), *MMU_COOLING]
        return (*self.ramming, *MMU_UNLOAD_RETRACTS, *cooling, MMU_PARK)

    def pick(self, first: bool) -> Moves:
        """The new slot's moves after the `T`: the load, then the purge."""
        return (*MMU_LOAD, self.first_purge if first else self.later_purge)


@dataclass(frozen=True)
class MmuSlicer:
    """A stock MMU3 profile's E moves outside the model.

    The start G-code retracts `cleanup` before any slot is selected, sends `T<first>`, then
    on the first slot loads `start_load` and extrudes the purge line, `purge_line`; it sends
    `T<first>` again after. A tool change runs the old slot's `park_out`, the `T`, then the
    new slot's `pick`. A print that used several slots ends with the tower's last unload,
    `park_out` with no `T`, on the last slot. Every print then retracts the last filament's
    `retract`, and the end G-code's `M702` unloads it into the MMU. Besides `T<n>` and
    `M702`, the profile sends the MMU nothing that moves filament.

    `pla` and `petg` are the stock `Prusament PLA` and `Prusament PETG` presets. Any other
    type prints with PLA's moves.
    """

    purge_line: Moves
    cleanup: Moves = (-2.0,)
    # `parking_pos_retraction` 84 + `extra_loading_move` -52 - 15.
    start_load: Moves = (17.0,)
    pla: MmuFilament = MmuFilament(
        # Eight moves summing to 20.9518 mm: 0.25 s at each of the five volumetric speeds
        # of `filament_ramming_parameters`, 50.39 mm³.
        ramming=(4.1743, 1.6973, 2.4971, 3.3745, 0.8299, 4.1944, 0.8472, 3.3371),
        cooling_moves=2,
        # 65 mm³ (`multimaterial_purging` 80 x `filament_purge_multiplier` 81.25 %), rounded
        # up to whole tower lines. The first layer's is 7.96 of wipe, the 15 mm³ minimum,
        # and 46.67 of the tower's first layer.
        first_purge=54.63,
        later_purge=29.95,
        retract=0.7,
    )
    petg: MmuFilament = MmuFilament(
        # Eight moves summing to 22.0450 mm, 53.02 mm³. Feeds in a row count as one.
        ramming=(22.045,),
        cooling_moves=3,
        # 80 mm³ (100 %). The first layer's is 18.03 of wipe, the 35 mm³ minimum, and
        # 39.78 + 0.64 of the tower's first layer.
        first_purge=58.45,
        later_purge=34.72,
        retract=0.8,
    )

    def filament(self, name: str | None) -> MmuFilament:
        """The moves of the filament type a file names: PETG's for PETG, else PLA's."""
        return self.petg if name == "PETG" else self.pla

    def overridden(self, prime_mm: float | None, purge_mm: float | None) -> MmuSlicer:
        """This profile with the print's own purge line and tool-change purge, where it sets
        them: `prime_mm` replaces the whole purge line, and `purge_mm` the purge after every
        load."""
        out = self
        if prime_mm is not None:
            out = replace(out, purge_line=(prime_mm,))
        if purge_mm is not None:
            out = replace(
                out,
                pla=replace(self.pla, first_purge=purge_mm, later_purge=purge_mm),
                petg=replace(self.petg, first_purge=purge_mm, later_purge=purge_mm),
            )
        return out


# The MK4S MMU3 profile: the purge line is the 2 mm back from the cleanup's retract, then
# `E7`, `E36`, `E4` and `E4`, 53 mm.
MK4S_MMU_SLICER = MmuSlicer(purge_line=(2.0, 7.0, 36.0, 4.0, 4.0))
# The Core One and Core One L MMU3 profiles, whose purge line starts with `E5`: 51 mm.
CORE_ONE_MMU_SLICER = MmuSlicer(purge_line=(2.0, 5.0, 36.0, 4.0, 4.0))
