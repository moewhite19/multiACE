#!/usr/bin/env python3
import zipfile
CELL = 20.0
GAP = 2.0
BORDER = 5.0
COLS = 4
ROWS = 3
LAYER_H = 0.2
Z_MID = LAYER_H
Z_TOP = 2 * LAYER_H
PX = 2.0
N_CELLS = COLS * ROWS
OUT = '/home/user/dirk-3d/testmatrix.3mf'
FONT = {'0': ['111', '101', '101', '101', '111'], '1': ['010', '110', '010', '010', '111'], '2': ['111', '001', '111', '100', '111'], '3': ['111', '001', '111', '001', '111'], '4': ['101', '101', '111', '001', '001'], '5': ['111', '100', '111', '001', '111'], '6': ['111', '100', '111', '101', '111'], '7': ['111', '001', '010', '010', '010'], '8': ['111', '101', '111', '101', '111'], '9': ['111', '101', '111', '001', '111']}

def box_mesh(x0, y0, z0, dx, dy, dz):
    x1, y1, z1 = (x0 + dx, y0 + dy, z0 + dz)
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    t = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return (v, t)

def combine(meshes):
    V, T = ([], [])
    for v, t in meshes:
        off = len(V)
        V.extend(v)
        T.extend([(a + off, b + off, c + off) for a, b, c in t])
    return (V, T)

def glyph_layout(number):
    digits = str(number)
    glyph_w_px = 3 * len(digits) + (len(digits) - 1)
    total_w = glyph_w_px * PX
    total_h = 5 * PX
    x_off = (CELL - total_w) / 2
    y_off = (CELL - total_h) / 2
    pixels = set()
    for di, d in enumerate(digits):
        for row in range(5):
            for col in range(3):
                if FONT[d][row][col] == '1':
                    bbox_col = di * 4 + col
                    bbox_row = 4 - row
                    pixels.add((bbox_col, bbox_row))
    return (x_off, y_off, glyph_w_px, pixels)

def glyph_rects(number):
    x_off, y_off, _, pixels = glyph_layout(number)
    return [(x_off + c * PX, y_off + r * PX) for c, r in pixels]

def body_layer2_rects(number):
    x_off, y_off, glyph_w_px, pixels = glyph_layout(number)
    total_w = glyph_w_px * PX
    total_h = 5 * PX
    rects = []
    if y_off > 0:
        rects.append((0.0, 0.0, CELL, y_off))
    if y_off + total_h < CELL:
        rects.append((0.0, y_off + total_h, CELL, CELL - (y_off + total_h)))
    if x_off > 0:
        rects.append((0.0, y_off, x_off, total_h))
    if x_off + total_w < CELL:
        rects.append((x_off + total_w, y_off, CELL - (x_off + total_w), total_h))
    for r in range(5):
        for c in range(glyph_w_px):
            if (c, r) not in pixels:
                rects.append((x_off + c * PX, y_off + r * PX, PX, PX))
    return rects

def mesh_to_xml(v, t, indent='      '):
    lines = ['<mesh>', ' <vertices>']
    for x, y, z in v:
        lines.append('  <vertex x="%.4f" y="%.4f" z="%.4f"/>' % (x, y, z))
    lines.append(' </vertices>')
    lines.append(' <triangles>')
    for a, b, c in t:
        lines.append('  <triangle v1="%d" v2="%d" v3="%d"/>' % (a, b, c))
    lines.append(' </triangles>')
    lines.append('</mesh>')
    return '\n'.join((indent + line for line in lines))
objects_xml = []
items_xml = []
model_settings = ['<?xml version="1.0" encoding="UTF-8"?>', '<config>']
LEAF_BASE = 1
ASM_BASE = 100
for idx in range(N_CELLS):
    col = idx % COLS
    row = idx // COLS
    cell_x = BORDER + col * (CELL + GAP)
    cell_y = BORDER + row * (CELL + GAP)
    t_body = idx
    t_glyph = (idx + 1) % N_CELLS
    body_meshes = [box_mesh(0, 0, 0, CELL, CELL, LAYER_H)]
    for bx, by, bdx, bdy in body_layer2_rects(idx):
        body_meshes.append(box_mesh(bx, by, Z_MID, bdx, bdy, LAYER_H))
    body_v, body_t = combine(body_meshes)
    glyph_meshes = [box_mesh(gx, gy, Z_MID, PX, PX, LAYER_H) for gx, gy in glyph_rects(idx)]
    glyph_v, glyph_t = combine(glyph_meshes)
    body_id = LEAF_BASE + 2 * idx
    glyph_id = LEAF_BASE + 2 * idx + 1
    asm_id = ASM_BASE + idx
    objects_xml.append('  <object id="%d" type="model">\n%s\n  </object>' % (body_id, mesh_to_xml(body_v, body_t)))
    objects_xml.append('  <object id="%d" type="model">\n%s\n  </object>' % (glyph_id, mesh_to_xml(glyph_v, glyph_t)))
    objects_xml.append('  <object id="%d" type="model">\n    <components>\n      <component objectid="%d"/>\n      <component objectid="%d"/>\n    </components>\n  </object>' % (asm_id, body_id, glyph_id))
    transform = '1 0 0 0 1 0 0 0 1 %.4f %.4f 0' % (cell_x, cell_y)
    items_xml.append('    <item objectid="%d" transform="%s"/>' % (asm_id, transform))
    model_settings.append('  <object id="%d">' % asm_id)
    model_settings.append('    <metadata key="name" value="cell_%02d"/>' % idx)
    model_settings.append('    <metadata key="extruder" value="%d"/>' % (t_body + 1))
    model_settings.append('    <part id="%d" subtype="normal_part">' % body_id)
    model_settings.append('      <metadata key="name" value="body_T%d"/>' % t_body)
    model_settings.append('      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>')
    model_settings.append('      <metadata key="extruder" value="%d"/>' % (t_body + 1))
    model_settings.append('    </part>')
    model_settings.append('    <part id="%d" subtype="normal_part">' % glyph_id)
    model_settings.append('      <metadata key="name" value="glyph_T%d"/>' % t_glyph)
    model_settings.append('      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>')
    model_settings.append('      <metadata key="extruder" value="%d"/>' % (t_glyph + 1))
    model_settings.append('    </part>')
    model_settings.append('  </object>')
model_settings.append('</config>')
model_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n  <metadata name="Application">multiACE testmatrix generator</metadata>\n  <resources>\n%s\n  </resources>\n  <build>\n%s\n  </build>\n</model>\n' % ('\n'.join(objects_xml), '\n'.join(items_xml))
content_types = '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n</Types>\n'
rels = '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n'
with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('[Content_Types].xml', content_types)
    z.writestr('_rels/.rels', rels)
    z.writestr('3D/3dmodel.model', model_xml)
    z.writestr('Metadata/model_settings.config', '\n'.join(model_settings))
print('Wrote', OUT)
print('Cells:', N_CELLS)
print('Leaf objects: %d, Assembly objects: %d' % (2 * N_CELLS, N_CELLS))
