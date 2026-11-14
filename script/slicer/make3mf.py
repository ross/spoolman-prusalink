#!/usr/bin/env python3
"""Write a 3MF of 10 x 10 mm boxes, one per extruder, for slicing a multi-tool print.

Usage: script/slicer/make3mf.py OUT.3mf EXTRUDERS [HEIGHT_MM]

EXTRUDERS is a comma-separated list of 1-based extruder numbers, one box each:
`1,2` makes two boxes, the first on tool 1 and the second on tool 2. HEIGHT_MM is
the boxes' height and defaults to 1. With one height, each layer prints every
box's tool once. A comma-separated list gives each box its own height, the last
one repeating: `1,3` leaves the upper layers to the second box's tool alone.
"""

import sys
import zipfile

SIZE = 10.0
CORNERS = [(dx, dy, dz) for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)]
TRIANGLES = [
    (0, 2, 1),
    (1, 2, 3),
    (4, 5, 6),
    (5, 7, 6),
    (0, 1, 4),
    (1, 5, 4),
    (2, 6, 3),
    (3, 6, 7),
    (0, 4, 2),
    (2, 4, 6),
    (1, 3, 5),
    (3, 7, 5),
]
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" '
    'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
)


def box_object(oid: int, height: float) -> str:
    vertices = "".join(
        f'<vertex x="{dx * SIZE}" y="{dy * SIZE}" z="{dz * height}"/>' for dx, dy, dz in CORNERS
    )
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in TRIANGLES)
    return (
        f'<object id="{oid}" type="model"><mesh><vertices>{vertices}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object>"
    )


def box_config(oid: int, extruder: int) -> str:
    return (
        f'<object id="{oid}" instances_count="1">'
        f'<metadata type="object" key="name" value="box{oid}"/>'
        f'<metadata type="object" key="extruder" value="{extruder}"/>'
        f'<volume firstid="0" lastid="11"><metadata type="volume" key="name" value="box{oid}"/>'
        '<metadata type="volume" key="volume_type" value="ModelPart"/></volume></object>'
    )


def write(out: str, extruders: list[int], heights: list[float]) -> None:
    ids = range(1, len(extruders) + 1)
    heights = heights + heights[-1:] * (len(extruders) - len(heights))
    objects = "".join(box_object(oid, h) for oid, h in zip(ids, heights, strict=False))
    items = "".join(
        f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {100 + oid * 20} 150 0" '
        'printable="1"/>'
        for oid in ids
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{objects}</resources><build>{items}</build></model>"
    )
    config = (
        '<?xml version="1.0" encoding="utf-8"?>\n<config>'
        + "".join(box_config(oid, e) for oid, e in zip(ids, extruders, strict=True))
        + "</config>"
    )
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/Slic3r_PE_model.config", config)


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    heights = [float(x) for x in sys.argv[3].split(",")] if len(sys.argv) == 4 else [1.0]
    write(sys.argv[1], [int(x) for x in sys.argv[2].split(",")], heights)
    return 0


if __name__ == "__main__":
    sys.exit(main())
