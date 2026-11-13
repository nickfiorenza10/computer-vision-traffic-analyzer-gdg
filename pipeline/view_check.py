"""Does the camera still point where the lanes were learned?

Lanes are learned once from daytime footage and then reused, including at night when a
camera can pan or zoom without anyone noticing. This module answers one question about a
frame from the video being counted: is it the same view as the reference image saved with
the lanes, has the view moved, or can we not tell?

The method is deliberately plain: CLAHE for contrast, ORB features, a ratio test, a RANSAC
homography, and then the transform is judged as a translation, a scale and a rotation
relative to the frame size. Three things make it trustworthy rather than merely working:

* **Overlays never move.** Caltrans burns the camera name into the top of the frame and a
  timestamp into the bottom, and both stay put when the camera pans, so a naive matcher
  says "same" for a moved camera. The overlay regions are masked out before features are
  taken (default: the top 25% and bottom 10%, overridable per camera with `view_mask` in
  the lanes file).
* **Day versus night is expected to be unknown, not wrong.** A night frame is dark and
  full of glare, so matching often fails. Failing is reported as `unknown` with a reason,
  never as `same` or `moved`.
* **Brightness and weather are not movement.** A brightness change with no geometric
  change comes out as `same`; a change so large that features vanish comes out as
  `unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, degrees, hypot
from pathlib import Path

import cv2
import numpy as np

SAME = "same"
MOVED = "moved"
UNKNOWN = "unknown"

# Fractions of the frame height, used when a lanes file does not say otherwise.
DEFAULT_MASK_TOP = 0.25
DEFAULT_MASK_BOTTOM = 0.10

# How far the view may drift before it counts as moved. The shift is a fraction of the
# frame's longer side; the scale is relative (0.05 = 5%); the rotation is in degrees.
DEFAULT_MAX_SHIFT = 0.03
DEFAULT_MAX_SCALE = 0.05
DEFAULT_MAX_ROTATION = 2.0

# A background is the per-pixel median of frames spread over a few seconds: vehicles and
# headlights move, the road does not. Single dark frames match on headlights and glare,
# which is how a still camera was once reported as moved.
BACKGROUND_FRAMES = 15
BACKGROUND_SECONDS = 5.0

# A pan or zoom this large is far more likely to be mismatched features than a real move,
# so it is reported as unknown rather than as "moved".
DEFAULT_MIN_PLAUSIBLE_SCALE = 0.67
DEFAULT_MAX_PLAUSIBLE_SCALE = 1.5
DEFAULT_MAX_PLAUSIBLE_ROTATION = 15.0

# Where to take the samples, and how closely several "moved" estimates must agree before
# the word is used at all.
SAMPLE_OFFSETS = (0.0, 8.0, 18.0)
AGREEMENT_SHIFT = 0.01
AGREEMENT_SCALE = 0.02
AGREEMENT_ROTATION = 1.0

# Below these the answer is "unknown" rather than a guess.
DEFAULT_MIN_MATCHES = 12
DEFAULT_MIN_INLIERS = 10
DEFAULT_MIN_INLIER_RATIO = 0.35
DEFAULT_MIN_KEYPOINTS = 40

# Feature matching runs on a downscaled frame; the answer does not need full resolution.
ANALYSIS_WIDTH = 640
RATIO_TEST = 0.75
RANSAC_THRESHOLD_PX = 3.0


@dataclass(frozen=True)
class ViewVerdict:
    """What the check decided, and enough detail to argue with it."""

    verdict: str
    reason: str
    reference: str | None = None
    shift: tuple[float, float] | None = None
    shift_fraction: float | None = None
    scale: float | None = None
    rotation_deg: float | None = None
    matches: int = 0
    inliers: int = 0

    @property
    def moved(self) -> bool:
        return self.verdict == MOVED

    def describe(self) -> str:
        parts = [f"camera view check: {self.verdict} ({self.reason})"]
        if self.reference:
            parts.append(f"reference {self.reference}")
        if self.shift is not None:
            parts.append(
                f"shift {self.shift[0]:+.1f},{self.shift[1]:+.1f} px "
                f"({(self.shift_fraction or 0) * 100:.1f}% of the frame)"
            )
        if self.scale is not None:
            parts.append(f"scale {self.scale:.3f}")
        if self.rotation_deg is not None:
            parts.append(f"rotation {self.rotation_deg:+.2f} deg")
        if self.matches:
            parts.append(f"{self.inliers}/{self.matches} inliers")
        return "; ".join(parts)


def mask_for(shape: tuple[int, int], *, top: float = DEFAULT_MASK_TOP, bottom: float = DEFAULT_MASK_BOTTOM) -> np.ndarray:
    """A 0/255 mask with the top `top` and bottom `bottom` fractions of the frame removed."""
    height, width = shape[:2]
    mask = np.full((height, width), 255, dtype=np.uint8)
    mask[: max(0, int(round(height * float(top)))), :] = 0
    if bottom > 0:
        mask[height - max(0, int(round(height * float(bottom)))) :, :] = 0
    return mask


def _prepare(image: np.ndarray, width: int = ANALYSIS_WIDTH) -> tuple[np.ndarray, np.ndarray]:
    """Return (raw grayscale, CLAHE-equalised), both downscaled.

    The raw copy is what the "is there anything here" guard looks at: equalising first
    would stretch a black night frame back to full contrast and make noise look like
    detail, which is exactly the mistake this check must not make.
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, image_width = gray.shape[:2]
    if image_width > width:
        scale = width / image_width
        gray = cv2.resize(gray, (width, max(1, int(round(height * scale)))), interpolation=cv2.INTER_AREA)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return gray, clahe.apply(gray)


def _detail(processed: np.ndarray, mask: np.ndarray) -> tuple[float, int]:
    """A blunt measure of how much there is to match: contrast, and keypoints found."""
    middle = processed if mask is None else cv2.bitwise_and(processed, processed, mask=mask)
    contrast = float(np.std(middle[mask > 0])) if mask is not None else float(np.std(middle))
    detector = cv2.ORB_create(nfeatures=3000)
    keypoints = detector.detect(middle, mask)
    return contrast, len(keypoints)


def compare_frames(
    reference: np.ndarray,
    frame: np.ndarray,
    *,
    mask_top: float = DEFAULT_MASK_TOP,
    mask_bottom: float = DEFAULT_MASK_BOTTOM,
    max_shift: float = DEFAULT_MAX_SHIFT,
    max_scale: float = DEFAULT_MAX_SCALE,
    max_rotation: float = DEFAULT_MAX_ROTATION,
    min_matches: int = DEFAULT_MIN_MATCHES,
    min_inliers: int = DEFAULT_MIN_INLIERS,
    min_inlier_ratio: float = DEFAULT_MIN_INLIER_RATIO,
    min_keypoints: int = DEFAULT_MIN_KEYPOINTS,
    min_plausible_scale: float = DEFAULT_MIN_PLAUSIBLE_SCALE,
    max_plausible_scale: float = DEFAULT_MAX_PLAUSIBLE_SCALE,
    max_plausible_rotation: float = DEFAULT_MAX_PLAUSIBLE_ROTATION,
    reference_name: str | None = None,
) -> ViewVerdict:
    """Compare two images (usually two backgrounds) and say same, moved or unknown."""
    if reference is None or frame is None:
        return ViewVerdict(UNKNOWN, "no frame to compare", reference_name)

    left_raw, left = _prepare(reference)
    right_raw, right = _prepare(frame)
    if left.shape != right.shape:
        right = cv2.resize(right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA)
        right_raw = cv2.resize(
            right_raw, (left_raw.shape[1], left_raw.shape[0]), interpolation=cv2.INTER_AREA
        )

    mask = mask_for(left.shape, top=mask_top, bottom=mask_bottom)
    if not np.any(mask):
        return ViewVerdict(UNKNOWN, "the overlay mask covers the whole frame", reference_name)

    left_contrast, left_keypoints = _detail(left_raw, mask)
    right_contrast, right_keypoints = _detail(right_raw, mask)
    if min(left_contrast, right_contrast) < 8.0 or min(left_keypoints, right_keypoints) < min_keypoints:
        return ViewVerdict(
            UNKNOWN,
            f"too little detail to match (contrast {left_contrast:.1f}/{right_contrast:.1f}, "
            f"keypoints {left_keypoints}/{right_keypoints}); a dark or blank frame looks like this",
            reference_name,
        )

    orb = cv2.ORB_create(nfeatures=3000)
    left_points, left_descriptors = orb.detectAndCompute(left, mask)
    right_points, right_descriptors = orb.detectAndCompute(right, mask)
    if left_descriptors is None or right_descriptors is None:
        return ViewVerdict(UNKNOWN, "no features outside the overlay regions", reference_name)

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(left_descriptors, right_descriptors, k=2)
    good = [first for first, second in pairs if first.distance < RATIO_TEST * second.distance]
    if len(good) < min_matches:
        return ViewVerdict(
            UNKNOWN,
            f"only {len(good)} feature matches outside the overlays (need {min_matches}); "
            "day against night usually fails here",
            reference_name,
            matches=len(good),
        )

    source = np.float32([left_points[match.queryIdx].pt for match in good]).reshape(-1, 1, 2)
    target = np.float32([right_points[match.trainIdx].pt for match in good]).reshape(-1, 1, 2)
    homography, inlier_mask = cv2.findHomography(source, target, cv2.RANSAC, RANSAC_THRESHOLD_PX)
    if homography is None or inlier_mask is None:
        return ViewVerdict(UNKNOWN, "no consistent transform", reference_name, matches=len(good))

    inliers = int(inlier_mask.sum())
    ratio = inliers / len(good)
    if inliers < min_inliers or ratio < min_inlier_ratio:
        return ViewVerdict(
            UNKNOWN,
            f"{inliers} of {len(good)} matches agree (need {min_inliers} and "
            f"{min_inlier_ratio:.0%}); the view may have changed too much to measure",
            reference_name,
            matches=len(good),
            inliers=inliers,
        )

    height, width = left.shape[:2]
    corners = np.float32([[0, 0], [width, 0], [width, height], [0, height]]).reshape(-1, 1, 2)
    mapped = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
    centre = cv2.perspectiveTransform(
        np.float32([[[width / 2, height / 2]]]), homography
    ).reshape(2)
    shift_x = float(centre[0] - width / 2)
    shift_y = float(centre[1] - height / 2)
    longer = float(max(width, height))
    shift_fraction = hypot(shift_x, shift_y) / longer
    scale = float(hypot(*(mapped[1] - mapped[0])) / max(1.0, float(width)))
    rotation = degrees(atan2(mapped[1][1] - mapped[0][1], mapped[1][0] - mapped[0][0]))

    # A transform this far from identity is not a pan somebody made: it is features
    # agreeing on the wrong thing. Saying "moved" here is what blocked night counting.
    if not (min_plausible_scale <= scale <= max_plausible_scale) or abs(rotation) > max_plausible_rotation:
        return ViewVerdict(
            UNKNOWN,
            f"implausible transform (scale {scale:.3f}, rotation {rotation:+.2f} deg) - "
            "more likely mismatched features than a real move",
            reference_name,
            shift=(shift_x, shift_y),
            shift_fraction=shift_fraction,
            scale=scale,
            rotation_deg=rotation,
            matches=len(good),
            inliers=inliers,
        )

    moved_because = []
    if shift_fraction > max_shift:
        moved_because.append(f"shifted {shift_fraction:.1%} (limit {max_shift:.0%})")
    if abs(scale - 1.0) > max_scale:
        moved_because.append(f"zoomed {scale:.3f} (limit {1 + max_scale:.2f})")
    if abs(rotation) > max_rotation:
        moved_because.append(f"rotated {rotation:+.2f} deg (limit {max_rotation:g})")

    return ViewVerdict(
        MOVED if moved_because else SAME,
        "; ".join(moved_because) if moved_because else "the view matches the reference",
        reference_name,
        shift=(shift_x, shift_y),
        shift_fraction=shift_fraction,
        scale=scale,
        rotation_deg=rotation,
        matches=len(good),
        inliers=inliers,
    )


def reference_name_for(lanes_path: Path) -> str:
    """The reference image that belongs next to a lanes file: <lanes>.reference.jpg."""
    return lanes_path.with_suffix(".reference.jpg").name


def median_background(frames) -> np.ndarray | None:
    """The per-pixel median of several frames: the scene with the moving things removed.

    Vehicles, headlights and glare move; the road, the poles and the camera's own overlay
    do not. In a dark scene the matchable detail is almost entirely headlights, which is
    why a single-frame comparison reported a still camera as moved.
    """
    if frames is None:
        return None
    if isinstance(frames, np.ndarray) and frames.ndim in (2, 3):
        return frames if frames.ndim == 2 else cv2.cvtColor(frames, cv2.COLOR_BGR2GRAY)
    stack = [frame for frame in frames if frame is not None]
    if not stack:
        return None
    if len(stack) == 1:
        alone = stack[0]
        return alone if alone.ndim == 2 else cv2.cvtColor(alone, cv2.COLOR_BGR2GRAY)
    grey = [frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) for frame in stack]
    shape = grey[0].shape
    for frame in grey:
        if frame.shape != shape:
            raise ValueError("every frame of a background must be the same size")
    return np.median(np.stack(grey), axis=0).astype(np.uint8)


def background_from_video(
    video_path: Path,
    *,
    start_seconds: float = 0.0,
    seconds: float = BACKGROUND_SECONDS,
    frames: int = BACKGROUND_FRAMES,
) -> np.ndarray | None:
    """Median background from about `frames` frames spread over `seconds` of video."""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return None
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        first = max(0, int(round(fps * max(0.0, start_seconds))))
        span = max(1, int(round(fps * max(0.1, seconds))))
        step = max(1, span // max(1, frames))
        wanted = set(range(first, first + span, step))
        picked: list[np.ndarray] = []
        index = 0
        while len(picked) < frames:
            ok, frame = capture.read()
            if not ok:
                break
            if index in wanted:
                picked.append(frame)
            index += 1
            if index > first + span + step:
                break
    finally:
        capture.release()
    return median_background(picked)


def save_reference(frame, lanes_path: Path, *, name: str | None = None) -> str:
    """Write the lanes file's reference image next to it and return its file name.

    `frame` may be one image or several; several are reduced to their median background
    first. Grayscale and at most 640 px wide, because it is committed alongside the lanes.
    """
    frame = median_background(frame)
    if frame is None:
        raise ValueError("no frames to save as a reference")
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    if width > ANALYSIS_WIDTH:
        gray = cv2.resize(
            gray, (ANALYSIS_WIDTH, max(1, int(round(height * ANALYSIS_WIDTH / width))))
        )
    target = lanes_path.with_name(name or reference_name_for(lanes_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(target), gray, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return target.name


def _load_image(path: Path) -> np.ndarray | None:
    if not path.is_file():
        return None
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    return image if image is not None and image.size else None


def load_reference(lanes_path: Path, config: dict, frame: np.ndarray | None = None) -> tuple[np.ndarray, str] | None:
    """The reference image to compare against, and its name.

    A night reference (`reference_image_night`) is used when the frame's mean brightness
    is closer to it than to the daytime one, which is what makes "learn by day, count by
    night" work without matching two very different pictures.
    """
    candidates: list[tuple[str, np.ndarray]] = []
    for key in ("reference_image", "reference_image_night"):
        name = config.get(key)
        if not name:
            continue
        image = _load_image(lanes_path.with_name(str(name)))
        if image is not None:
            candidates.append((str(name), image))
    if not candidates:
        return None
    if len(candidates) == 1 or frame is None:
        return candidates[0]
    brightness = float(np.mean(frame))
    return min(candidates, key=lambda item: abs(float(np.mean(item[1])) - brightness))


def duration_seconds(video_path: Path) -> float:
    capture = cv2.VideoCapture(str(video_path))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        counted = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    finally:
        capture.release()
    return float(counted) / float(fps) if fps else 0.0


def combine_verdicts(verdicts: list[ViewVerdict]) -> ViewVerdict:
    """One answer from several samples: agreement is what makes "moved" trustworthy."""
    if not verdicts:
        return ViewVerdict(UNKNOWN, "could not read a background from the video")

    unknown = next((verdict for verdict in verdicts if verdict.verdict == UNKNOWN), None)
    if unknown is not None:
        suffix = f" (1 of {len(verdicts)} samples)" if len(verdicts) > 1 else ""
        return ViewVerdict(UNKNOWN, unknown.reason + suffix, unknown.reference)

    moved = [verdict for verdict in verdicts if verdict.verdict == MOVED]
    if not moved:
        return ViewVerdict(
            SAME,
            f"the view matches the reference in {len(verdicts)} sample(s)",
            verdicts[0].reference,
            shift=verdicts[0].shift,
            shift_fraction=verdicts[0].shift_fraction,
            scale=verdicts[0].scale,
            rotation_deg=verdicts[0].rotation_deg,
            matches=verdicts[0].matches,
            inliers=verdicts[0].inliers,
        )

    if len(moved) < len(verdicts):
        return ViewVerdict(
            UNKNOWN,
            f"samples disagree ({len(moved)} moved, {len(verdicts) - len(moved)} not), "
            "so no verdict is claimed",
            moved[0].reference,
        )

    if len(moved) == 1:
        return ViewVerdict(
            UNKNOWN,
            f"one sample says moved ({moved[0].reason}), but a move is only reported when "
            "samples agree and this clip was too short for a second",
            moved[0].reference,
            matches=moved[0].matches,
            inliers=moved[0].inliers,
        )

    first = moved[0]
    for other in moved[1:]:
        agrees = (
            abs((other.shift_fraction or 0.0) - (first.shift_fraction or 0.0)) <= AGREEMENT_SHIFT
            and abs((other.scale or 1.0) - (first.scale or 1.0)) <= AGREEMENT_SCALE
            and abs((other.rotation_deg or 0.0) - (first.rotation_deg or 0.0)) <= AGREEMENT_ROTATION
        )
        if not agrees:
            return ViewVerdict(
                UNKNOWN,
                "the samples measured different moves "
                f"({first.reason} vs {other.reason}), so neither is reported",
                first.reference,
            )

    shift_x = sum((verdict.shift or (0.0, 0.0))[0] for verdict in moved) / len(moved)
    shift_y = sum((verdict.shift or (0.0, 0.0))[1] for verdict in moved) / len(moved)
    return ViewVerdict(
        MOVED,
        f"{first.reason}; the same move in all {len(moved)} samples",
        first.reference,
        shift=(shift_x, shift_y),
        shift_fraction=sum((verdict.shift_fraction or 0.0) for verdict in moved) / len(moved),
        scale=sum((verdict.scale or 1.0) for verdict in moved) / len(moved),
        rotation_deg=sum((verdict.rotation_deg or 0.0) for verdict in moved) / len(moved),
        matches=sum(verdict.matches for verdict in moved),
        inliers=sum(verdict.inliers for verdict in moved),
    )


def check_video(
    lanes_path: Path,
    video_path: Path,
    *,
    at_seconds: float = 2.0,
    seconds: float = BACKGROUND_SECONDS,
    frames: int = BACKGROUND_FRAMES,
    sample_offsets=SAMPLE_OFFSETS,
    mask_top: float | None = None,
    mask_bottom: float | None = None,
    **thresholds,
) -> ViewVerdict:
    """Compare median backgrounds, at several points in the video, with the reference.

    Both sides are backgrounds, not single frames: in a dark scene a single frame's only
    matchable detail is headlights, and RANSAC will happily fit their motion and call a
    still camera moved. Several samples are taken so that "moved" is only said when they
    agree; a clip too short for two samples can never produce it.

    `mask_top` and `mask_bottom` win when given; otherwise the lanes file's own
    `"view_mask"` is used, and the module defaults if it has none. A camera with an
    unusually tall name plate should carry that in its lanes file, not in a command.
    """
    import json

    config = json.loads(lanes_path.read_text(encoding="utf-8"))
    if not config.get("reference_image") and not config.get("reference_image_night"):
        return ViewVerdict(UNKNOWN, "the lanes file has no reference image")

    total = duration_seconds(video_path)
    starts = [
        at_seconds + offset
        for offset in sample_offsets
        if total <= 0 or at_seconds + offset + 0.2 < total
    ]
    if not starts:
        # A clip shorter than the first sample still gets checked, from its first frame:
        # one sample can say same but can never say moved.
        starts = [0.0]

    backgrounds = []
    for start in starts:
        background = background_from_video(
            video_path, start_seconds=start, seconds=seconds, frames=frames
        )
        if background is not None:
            backgrounds.append((start, background))
    if not backgrounds:
        return ViewVerdict(UNKNOWN, f"could not read a background from {video_path.name}")

    chosen = load_reference(lanes_path, config, backgrounds[0][1])
    if chosen is None:
        return ViewVerdict(UNKNOWN, "the reference images named in the lanes file are missing")
    name, reference = chosen
    view_mask = config.get("view_mask") or {}
    top = mask_top if mask_top is not None else float(view_mask.get("top", DEFAULT_MASK_TOP))
    bottom = (
        mask_bottom if mask_bottom is not None else float(view_mask.get("bottom", DEFAULT_MASK_BOTTOM))
    )
    verdicts = [
        compare_frames(
            reference,
            background,
            reference_name=name,
            mask_top=top,
            mask_bottom=bottom,
            **thresholds,
        )
        for _start, background in backgrounds
    ]
    return combine_verdicts(verdicts)
