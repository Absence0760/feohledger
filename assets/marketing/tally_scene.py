#!/usr/bin/env python3
"""Blender scene for the marketing hero ornament: the split Exchequer tally.

Run through Blender, never as a plain script:

    blender -b --python assets/marketing/tally_scene.py

The brand mark is feoh written as a split tally stick (docs/decisions.md §173).
This renders the object the mark abstracts: a notched hazel stick sawn down its
length into the two halves the Exchequer gave to each party, held apart so the
notches can be read across the cut. That is the act the product performs — an
invoice is settled when the two halves agree — so the ornament carries the
argument rather than decorating around it.

Geometry is derived, not modelled by hand: NOTCHES drives both the cut count and
their spacing, so the render can be re-derived from this file alone. Everything
is authored in metres at roughly the scale the object had in life (a ~24 cm
stick), which is what keeps the bevel widths and the depth-of-field plausible.

Output: assets/marketing/render/tally-split.png (RGBA, transparent film), which
gen-marketing.sh downsamples into frontend/static/marketing/.
"""
import math
import os
import sys

import bmesh
import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(bpy.data.filepath or __file__))
# `blender --python` leaves __file__ pointing at the script, which is what we
# want; the filepath fallback above only matters if this is ever run from a
# saved .blend.
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "render", "tally-split.png")

# ── the stick ────────────────────────────────────────────────────────────────
LENGTH = 0.24          # 24 cm, a working Exchequer tally's order of magnitude
WIDTH = 0.026          # across the face
DEPTH = 0.020          # front to back before the split
KERF = 0.013           # how far the two halves are drawn apart
# One notch per unit of debt. Widths are the real vocabulary: the Dialogus de
# Scaccario sizes a notch by the sum it records, so a mixed set reads as a
# number rather than as a texture.
NOTCHES = [
    (-0.086, 0.0042),
    (-0.058, 0.0022),
    (-0.038, 0.0022),
    (-0.004, 0.0052),
    (0.028, 0.0022),
    (0.048, 0.0022),
    (0.076, 0.0032),
]
NOTCH_DEPTH = 0.0050

GOLD = (0.941, 0.639, 0.180, 1.0)    # the gilt #E7B95E, pushed warmer to survive AgX
GOLD_DIM = (0.569, 0.376, 0.125, 1.0)  # its #8F7440 companion, the same push

RES = (1600, 1200)
SAMPLES = 256


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def box(name, size, location=(0, 0, 0), rotation=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location, rotation=rotation)
    ob = bpy.context.active_object
    ob.name = name
    ob.scale = Vector(size)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return ob


def notch_cutter(name, x, width, z_top, depth):
    """A V-groove across the face, as an explicit triangular prism.

    Built from vertices rather than from a rotated cube. The cube version cut
    the stick into a row of loose blocks: a 45° cube's reach along the stick is
    a function of its scale on THREE axes at once, so "how deep" and "how wide"
    could not be set independently and the depth always won. Here the triangle
    is the cross-section and the extrusion is the width of the stick, so both
    numbers mean what they say.
    """
    half = width / 2
    # A hair above the face so the boolean has a clean entry rather than a
    # coplanar one, which EXACT resolves as a zero-area face.
    over = 0.0004
    y = WIDTH
    section = [(x - half, z_top + over), (x + half, z_top + over), (x, z_top - depth)]
    verts = [(px, -y, pz) for px, pz in section] + [(px, y, pz) for px, pz in section]
    faces = [(0, 1, 2), (5, 4, 3), (0, 3, 4, 1), (1, 4, 5, 2), (2, 5, 3, 0)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    # `from_pydata` takes the winding on trust, and a hand-written face list
    # gets some of it backwards. An EXACT boolean reads an inward-facing normal
    # as "the solid is everything OUTSIDE this prism", so a difference against it
    # removes nothing and reports success: the notches were in the mesh — the
    # vertices were at the right depths — and the top face rendered perfectly
    # smooth. Recalculating is one line and removes the whole class of error.
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(mesh)
    bm.free()
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def boolean(target, cutter):
    mod = target.modifiers.new(name=f"cut-{cutter.name}", type="BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.object = cutter
    mod.solver = "EXACT"
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def bevel(ob, width=0.0012, segments=3):
    """Break every edge so it catches a highlight.

    Left flat-shaded on purpose: the object is a sawn stick, and a beveled edge
    rendered flat gives the thin bright line along each arris that makes the
    notches legible at the size this ships. Smooth shading rounds that line away
    and the whole thing reads as extruded plastic.
    """
    mod = ob.modifiers.new(name="bevel", type="BEVEL")
    mod.width = width
    mod.segments = segments
    mod.limit_method = "ANGLE"
    mod.angle_limit = math.radians(35)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.modifier_apply(modifier=mod.name)


def gold_material(name, base, roughness):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = base
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = roughness
    # A trace of anisotropy along the grain: the stick was sawn, and a perfectly
    # isotropic highlight on a long thin object reads as plastic.
    if "Anisotropic" in bsdf.inputs:
        bsdf.inputs["Anisotropic"].default_value = 0.35
    return mat


def build_half(name, mirror):
    """One half of the sawn stick.

    Both halves are cut with the SAME notch set, because in life they were one
    blank notched before it was split — that identity is the whole security
    property of a tally, and it is why this takes NOTCHES rather than a seed.
    """
    half_depth = DEPTH / 2
    ob = box(name, (LENGTH, WIDTH, half_depth))
    for i, (x, width) in enumerate(NOTCHES):
        boolean(ob, notch_cutter(f"{name}-notch-{i}", x, width,
                                 z_top=half_depth / 2, depth=NOTCH_DEPTH))
    bevel(ob)
    # A BOOLEAN modifier copies the CUTTER's material slots onto the result, and
    # the cutters carry none — so the stick came out of the loop owning an empty
    # slot at index 0 that every face pointed at. The caller's
    # `materials.append()` then landed at index 1 and was never drawn: the first
    # three renders of this scene were gold sticks rendered in Blender's default
    # grey, and nothing in the API reports it. Clear the slots and pin every face
    # to index 0, so whatever the caller appends is what the faces use.
    ob.data.materials.clear()
    for polygon in ob.data.polygons:
        polygon.material_index = 0
    if mirror:
        ob.rotation_euler = (math.radians(180), 0, 0)
    return ob


def area_light(name, location, size, energy, color=(1, 1, 1), target=(0, 0, 0)):
    """An area light aimed at `target` by constraint rather than by Euler maths.

    Hand-written rotations are why the first pass of this scene rendered a solid
    white bar: a light pointed a few degrees off is invisible in the numbers and
    obvious in the image. A TRACK_TO constraint makes aim a consequence of
    position, so moving a light can only change where it comes from.
    """
    bpy.ops.object.light_add(type="AREA", location=location)
    light = bpy.context.active_object
    light.name = name
    light.data.size = size
    light.data.energy = energy
    light.data.color = color
    aim(light, target)
    return light


def aim(ob, target):
    empty = bpy.data.objects.new(f"{ob.name}-target", None)
    bpy.context.scene.collection.objects.link(empty)
    empty.location = target
    constraint = ob.constraints.new(type="TRACK_TO")
    constraint.target = empty
    constraint.track_axis = "TRACK_NEGATIVE_Z"
    constraint.up_axis = "UP_Y"
    return empty


def camera_distance(frame_width, lens, sensor=36.0):
    """How far back an unrotated `lens` mm camera must sit to frame that width.

    Derived rather than dialled in, because the object's length is a constant at
    the top of this file: change LENGTH and the framing follows instead of
    silently cropping.
    """
    return frame_width * lens / sensor


def studio_world(scene):
    """A vertical gradient environment, bright above and near-black below.

    A flat world colour gives a mirror-finish metal one constant to reflect, so
    it renders as one constant. The gradient is what puts a horizon in the
    reflection — and a notch wall tilted 45° out of the face crosses that
    horizon, which is why the notches read as notches.
    """
    world = bpy.data.worlds.new("world")
    world.use_nodes = True
    tree = world.node_tree
    background = tree.nodes["Background"]
    background.inputs[1].default_value = 0.55

    coord = tree.nodes.new("ShaderNodeTexCoord")
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.30
    ramp.color_ramp.elements[0].color = (0.016, 0.020, 0.043, 1)
    ramp.color_ramp.elements[1].position = 0.92
    ramp.color_ramp.elements[1].color = (0.62, 0.68, 0.86, 1)

    tree.links.new(coord.outputs["Generated"], separate.inputs["Vector"])
    tree.links.new(separate.outputs["Z"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], background.inputs[0])
    scene.world = world
    return world


def main():
    reset()
    scene = bpy.context.scene

    stock = build_half("tally-stock", mirror=False)
    foil = build_half("tally-foil", mirror=True)

    # Draw the halves apart along the saw cut. The foil is pushed back and given
    # a little yaw so the gap reads as one stick opened, not two sticks laid out.
    stock.location = (0, -(KERF + WIDTH * 0.62), DEPTH / 4)
    stock.rotation_euler = (0, 0, math.radians(-2.4))
    foil.location = (0.012, KERF + WIDTH * 0.62, -DEPTH / 4)
    foil.rotation_euler = (math.radians(180), 0, math.radians(3.2))

    stock.data.materials.append(gold_material("gold-stock", GOLD, 0.34))
    foil.data.materials.append(gold_material("gold-foil", GOLD_DIM, 0.42))

    # ── lighting ─────────────────────────────────────────────────────────────
    # A key raking ALONG the stick so each notch wall is lit from one side and
    # shadowed on the other — a light square-on flattens the notches into a
    # stripe, which is the whole subject gone.
    area_light("key", (0.34, -0.30, 0.40), size=0.26, energy=11,
               color=(1.0, 0.95, 0.86))
    # The fill is the product's own blue: it keeps the shadow side from going to
    # black on --bg without adding a second warm source.
    area_light("fill", (-0.42, 0.22, 0.10), size=0.60, energy=2.4,
               color=(0.52, 0.63, 1.0))
    area_light("rim", (-0.16, -0.40, -0.26), size=0.35, energy=7,
               color=(0.70, 0.80, 1.0))

    studio_world(scene)

    # A softbox the metal can REFLECT. This is the difference between a render
    # and a photograph of nothing: at metallic 1 the stick has no diffuse term
    # at all, so every pixel of it is an image of the environment. In an empty
    # world the notch walls reflect the void and the object renders as a smooth
    # bar with the notches perfectly invisible — which is exactly what the first
    # four passes produced, geometry present and unlit.
    panel = box("softbox", (0.9, 0.5, 0.001), location=(0.05, -0.18, 0.46))
    panel.rotation_euler = (math.radians(-28), 0, 0)
    emitter = bpy.data.materials.new("softbox-emitter")
    emitter.use_nodes = True
    tree = emitter.node_tree
    tree.nodes.remove(tree.nodes["Principled BSDF"])
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (1.0, 0.97, 0.92, 1.0)
    emission.inputs["Strength"].default_value = 4.0
    tree.links.new(emission.outputs["Emission"], tree.nodes["Material Output"].inputs["Surface"])
    panel.data.materials.append(emitter)
    panel.visible_camera = False

    # ── camera ───────────────────────────────────────────────────────────────
    # Frame ~1.5 stick-lengths so the object sits IN space rather than against
    # the edges of it, then drop the whole rig onto a 3/4 view.
    lens = 85.0
    distance = camera_distance(LENGTH * 1.55, lens)
    azimuth, elevation = math.radians(-58), math.radians(34)
    bpy.ops.object.camera_add(location=(
        distance * math.cos(elevation) * math.cos(azimuth),
        distance * math.cos(elevation) * math.sin(azimuth),
        distance * math.sin(elevation),
    ))
    camera = bpy.context.active_object
    camera.data.lens = lens
    camera.data.dof.use_dof = True
    camera.data.dof.focus_object = stock
    # f/22, not f/5.6. An 85 mm lens 0.6 m from a 24 cm object is working at
    # near-macro magnification, where depth of field collapses to millimetres —
    # the first two passes rendered a smooth gold bar because every notch was
    # inside the blur. The stop is wide open only in the sense that the number
    # sounds closed; here it buys back just enough falloff at the ends to keep
    # the object in space.
    camera.data.dof.aperture_fstop = 22
    aim(camera, (0, 0, 0))
    scene.camera = camera

    # ── render ───────────────────────────────────────────────────────────────
    scene.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "OPTIX"
    prefs.get_devices()
    for device in prefs.devices:
        device.use = device.type in {"OPTIX", "CPU"}
    scene.cycles.device = "GPU"
    scene.cycles.samples = int(os.environ.get("TALLY_SAMPLES", SAMPLES))
    scene.cycles.use_denoising = True
    scene.cycles.max_bounces = 8
    scene.render.film_transparent = True
    scene.render.resolution_x, scene.render.resolution_y = RES
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    # AgX, not Standard: gold at a grazing angle clips instantly under a linear
    # transform, which is exactly how the first render came out as a white bar.
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Punchy"
    scene.view_settings.exposure = 0.0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    scene.render.filepath = OUT
    bpy.ops.render.render(write_still=True)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
