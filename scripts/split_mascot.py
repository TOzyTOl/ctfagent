"""Split the mascot into two animatable layers.

The source is a flat composite with no alpha and an open outline, so a
contour-based cut is unreliable (the bear's stroke is drawn in segments and does
not close). Colour is reliable: the globe is the only cyan-and-green thing in the
image. So:

  globe = cyan | green, closed into a disc, with the bear's footprint diffused
          back in so the disc has no hole when it spins
  top   = everything that is not globe-coloured (bear, clouds, sky)

Both keep the full 225x225 canvas so they stack without offsetting anything.
"""

import numpy as np
from PIL import Image
from scipy import ndimage

SRC = "ctfagent/static/loading-bear.png"
OUT_GLOBE = "ctfagent/static/mascot-globe.png"
OUT_TOP = "ctfagent/static/mascot-top.png"

img = Image.open(SRC).convert("RGB")
a = np.asarray(img).astype(float)
h, w, _ = a.shape

r, g, b = a[..., 0], a[..., 1], a[..., 2]
ocean = (b > 180) & (g > 150) & (r < 160) & (b - r > 40)
land = (g > 170) & (r > 120) & (b < 170) & (g - b > 30)
globe_color = ocean | land

# The bear takes a bite out of the rim, so closing the shape leaves a notch that
# would sweep around with the spin. The globe is a sphere: fit the circle from
# the three extremes that are not covered — left, right and bottom.
# Cloud shading is also cyan-ish; only the largest blob is the globe.
lab, n = ndimage.label(globe_color)
sizes = ndimage.sum(globe_color, lab, range(1, n + 1))
globe_only = lab == (int(np.argmax(sizes)) + 1)
ys, xs = np.where(globe_only)
cx = (xs.min() + xs.max()) / 2.0
radius = (xs.max() - xs.min()) / 2.0
cy = ys.max() - radius
yy, xx = np.mgrid[0:h, 0:w]
disc = ((xx - cx) ** 2 + (yy - cy) ** 2) <= radius ** 2
print(f"拟合圆 r={radius:.1f}")
print(f"圆盘 bbox x{xs.min()}..{xs.max()} y{ys.min()}..{ys.max()}")
print(f"圆心 ({cx:.1f},{cy:.1f}) 占比 x {cx / w * 100:.2f}%  y {cy / h * 100:.2f}%")

# --- globe layer: the disc, with the bear's footprint filled back in --------
# Nearest-neighbour rather than averaged diffusion: averaging turns the whole
# footprint into one grey-blue smudge, and when the globe spins that smudge
# sweeps out from under the bear and is plainly wrong. Copying the nearest real
# pixel stretches the surrounding ocean and land inward, so what passes by still
# reads as globe.
fill = a.copy()
known = disc & globe_color
hole = disc & ~globe_color
print("要补的洞:", int(hole.sum()), "像素")

if hole.any():
    # Mirror the lower hemisphere up over the footprint: the result is real
    # terrain (green blobs, ocean) rather than a stretched fan, so what rotates
    # past the bear still looks like a planet. Nearest-neighbour covers the few
    # pixels the mirror cannot reach.
    yy, xx = np.mgrid[0:h, 0:w]
    my = np.clip((2 * cy - yy).astype(int), 0, h - 1)
    mirror_ok = hole & known[my, xx]
    fill[mirror_ok] = a[my[mirror_ok], xx[mirror_ok]]

    rest = hole & ~mirror_ok
    if rest.any():
        _, idx = ndimage.distance_transform_edt(~known, return_indices=True)
        fill[rest] = a[idx[0][rest], idx[1][rest]]

    blurred = np.dstack([ndimage.gaussian_filter(fill[..., c], 0.9) for c in range(3)])
    fill[hole] = blurred[hole]

globe_alpha = np.where(disc, 255.0, 0.0)
Image.fromarray(
    np.dstack([np.clip(fill, 0, 255), globe_alpha]).astype(np.uint8), "RGBA"
).save(OUT_GLOBE)

# --- top layer: everything that is not globe-coloured -------------------------
top_alpha = np.where(globe_color, 0.0, 255.0)
Image.fromarray(
    np.dstack([a, top_alpha]).astype(np.uint8), "RGBA"
).save(OUT_TOP)

print("saved", OUT_GLOBE, OUT_TOP)
