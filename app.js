/* ─────────────────────────────────────────────────────────────────────────
   CONFIG
   ───────────────────────────────────────────────────────────────────────── */

/* Root path for all image files. Both `jpg` and `avif` subfolders are
   expected to live inside this directory, each containing the
   ff{number}.{ext} files. Change in one place if the layout moves. */
const IMAGES_BASE = 'images';

/* Root path for video files. Looks for `${VIDEO_BASE}/ff{n}.mp4`
   starting from 1, same batched discovery as images. Videos coexist
   with images in the rotation — pairs can be photo+photo, photo+video,
   or video+video, scored by the same colour-analysis on a single
   representative frame extracted from each clip. */
const VIDEO_BASE = 'videos';

/* Image formats in preference order. The <picture> element auto-picks
   the first format the browser supports; if no AVIF support, the JPG
   fallback in <img> is used. loadOne tries the same order during
   analysis so the cache is shared between analysis and display. */
const FORMATS       = ['avif', 'jpg'];
/* Responsive widths. The browser uses these together with `sizes="50vw"`
   on each <picture> source/img to pick the smallest variant that still
   covers `50vw × devicePixelRatio` device pixels. A 3× DPR phone at
   430 CSS px wide needs ~645 device px for 50vw → grabs the 600w (and
   looks fine, since AVIF compresses gracefully when slightly under
   target). A retina laptop or large monitor reaches for 1000 or 1500.
   Without this array `srcset` collapses to a single URL and every
   visitor downloads the largest file regardless of screen size.
   Must be sorted ascending — loadOne uses SIZES[0] as the analysis
   variant assuming it's the smallest. Variants must exist on disk
   at `images/{format}/ff{n}-{width}.{ext}` — see build-variants.mjs. */
const SIZES         = [600, 1000, 1500];

/* Interlude cadence — show a contact/share/welcome card every N user
   clicks, with N randomised in [CONTACT_MIN, CONTACT_MAX] so the
   interludes feel organic rather than clockwork. The next trigger
   point is rolled fresh after every interlude (see nextInterludeAt
   in advance()). Range is intentionally tight (4-7) so the visitor
   encounters all three cards early in the session — each card is
   a single-use punctuation, not a recurring beat. Set CONTACT_MIN
   === CONTACT_MAX for the old fixed-cadence behaviour, or CONTACT_MIN
   = 0 to disable interludes entirely. */
const CONTACT_MIN   = 4;
const CONTACT_MAX   = 7;
function rollNextInterlude() {
  if (CONTACT_MIN <= 0 || CONTACT_MAX <= 0) return Infinity;
  return CONTACT_MIN + Math.floor(Math.random() * (CONTACT_MAX - CONTACT_MIN + 1));
}

/* After an image appears in a diptych, block it from re-appearing for
   this many clicks. Each click shows two images, so internally we keep
   a buffer of the last 2 × RECENT_CLICKS_BLOCK image references. At
   25, ~50 images stay off-limits at any time (in a ~100-image pool).
   Higher values stretch out how often the same pair (or its swapped-
   orientation twin) can re-appear, which matters when the pool is
   small and the quality-bias concentrates picks at the top — without
   this, the top ~10 pairs cycle through fast and feel repetitive
   within a session. Tuning: above ~30 with a ~100-image pool starts
   starving the guarantee branch (too many of each image's top-K
   partners get filtered out as "recent"); below ~15 the same pair
   can recur within 30 seconds of casual tapping. */
const RECENT_CLICKS_BLOCK = 25;

/* Sibling groups — declare images that should be treated as the
   same image for recent-block purposes. When any image in a group
   appears, all its siblings get marked recently-shown too, so they
   can't appear together or in close succession. Use this for
   near-duplicates from the same shoot, sequence shots, or any
   pairing that's visually too similar for the colour algorithm
   to catch. Format: each inner array is a group; entries are bare
   numbers for images, 'v'-prefixed for videos (e.g. ['97','98']
   or ['12','v3']). Group size is unlimited — you can chain three
   or more siblings together. */
const SIBLING_GROUPS = [
  ['97', '98'],
];

/* Favorite images: get extra rotation priority. Their staleness is
   multiplied by FAVORITE_BOOST inside the per-image guarantee branch,
   so they cycle back more often than non-favorites would on the
   algorithm's own judgement.

   This is the right knob when the scorer "correctly" deprioritises
   a photo you genuinely love — usually because it's hard to pair
   (palette-narrow, very desaturated, very dark) and so its top-K
   partners are mid-tier even though the photo itself is great. The
   favorites mechanism doesn't override the scorer's choice of WHICH
   partner to use; it just makes those favorite-led pairings come up
   more often in rotation.

   How it works in practice (rough math, ~100 image pool, GUARANTEE_RATE
   at 0.45, FAVORITE_BOOST at 4):
   - A non-favorite at average staleness gets ~1/100 of a guarantee
     click, ≈ 0.45% of total clicks
   - A favorite at the same staleness gets ~4/100 of a guarantee click,
     ≈ 1.8% of total clicks — roughly one appearance every ~55 clicks
   - So a favorite appears ~4× as often as it would unprompted

   Boost only applies AFTER the recent-block window has cleared, so it
   doesn't force back-to-back appearances. It's a long-term rotation
   tilt, not a "show this next" override.

   Format: bare image numbers as strings (e.g. '11', '14'). Videos are
   not currently supported — let me know if you want that too. Use
   sparingly — listing 30+ favorites cancels the effect since they all
   compete with each other for the same boosted weight. */
const FAVORITE_IMAGES = new Set([
  '11',  '14',  '17',  '51',
  '58',  '64',  '69',  '75',  '81',
  '109', '112', '114', '117', '122', '124',
]);
const FAVORITE_BOOST  = 4.0;

/* Splash timing.

   PROGRESS_RATE_PER_SEC caps how fast the "Loading… X%" counter can
   climb. Whole numbers tick up at this rate when actual loading is
   instantaneous (warm cache), and rise in real time when actual
   loading is slower than the cap. 60%/s gives a minimum splash
   duration of ~1.7s — enough to register the title and the count.

   SPLASH_FADE_MS is the opacity transition. The CSS `#splash` rule
   must match this value; change in both places if retiming.

   SPLASH_MAX_WAIT_MS is a safety cap. On a stuck or extremely slow
   connection the splash would otherwise wait forever for image
   loads to complete. After this many ms the splash fades anyway and
   the gallery starts with whatever has loaded so far — pair scoring
   gracefully degrades when fewer images have signatures. */
const PROGRESS_RATE_PER_SEC = 60;
const SPLASH_FADE_MS        = 1800;
const SPLASH_MAX_WAIT_MS    = 30000;

/* Video load timeouts. Both paths (analysis and display) await media
   events that can simply never fire — corrupt MP4, misconfigured
   server, connection dropping between TCP handshake and data, etc.
   Without these, the UI would silently hang forever. On timeout, we
   resolve gracefully: the video is skipped for analysis (signature
   never registered), or the display panel resolves with whatever
   frame state the element has reached. */
const VIDEO_DISPLAY_TIMEOUT_MS  = 8000;
const VIDEO_ANALYSIS_TIMEOUT_MS = 10000;

/* Image decode timeout. img.decode() can stop progressing indefinitely
   when the page is suspended mid-load — phone screen locking, OS
   suspending an idle tab, iOS Safari freezing a backgrounded page —
   and may fail to resume cleanly when the page wakes up. Without a
   timeout the entire load promise hangs, leaving loadingDiptych true
   forever and silently disabling every subsequent click. The setTimeout
   fires when the page is foregrounded again (browsers run overdue
   timers shortly after restoring visibility), so recovery is automatic
   from the user's perspective — the diptych just becomes responsive
   again on the next click rather than appearing to be broken. */
const IMAGE_DECODE_TIMEOUT_MS   = 5000;

/* Image discovery: probe in parallel batches of this size; stop after a
   batch returns nothing. Tolerates gaps in numbering up to this size.
   Always probes the JPG path since that's the universal fallback —
   every image is expected to have a JPG copy even if it also has AVIF. */
const DISCOVER_BATCH = 20;

/* Pair ranking. Of all the possible pairs (sorted by quality), the
   picker chooses from the top N unordered pairs using a quality-biased
   random (see pickPair). Each can appear in either left/right
   orientation, so the visible catalogue ends up at TOP_PAIRS_POOL × 2
   distinct diptychs. A hard floor: pairs ranked worse than N can
   never surface, no matter what the random roll does — so use this
   to set the worst-case quality you're willing to accept.
   At 250, with ~100 items in the pool, that's the top ~5% of the
   ~4950 possible pairs — strict enough that nothing weak surfaces,
   loose enough to give long sessions genuine variety (500 distinct
   diptychs). */
const TOP_PAIRS_POOL   = 250;

/* ─── RANKING WEIGHTS ───
   The ranker rewards two kinds of contrast that must BOTH be
   present: palette contrast (different dominant colours) and
   compositional density contrast (busy + uniform). Crucially,
   density contrast is multiplied by palette contrast in pairScore
   — a pair with strong density difference but a shared palette
   gets little reward, because that's where "fake contrast"
   pairings come from (e.g. two pale-blue-dominated images, one
   busier than the other).

   Lightness contrast is a separate reward — explicit "one bright,
   one dark" preference, since two pairs with identical palette/
   density signatures can read very differently if one is light-
   and-light vs. one bright + one dim. Captures the "up and down"
   editorial dimension that pure hue/density don't.

   Tonal cohesion is kept as a faint tiebreaker, deliberately low.
   It directly conflicts with what the eye values here (contrast
   over cohesion), so we don't want it competing with the contrast
   signals — but a small weight stops perfectly tonal-similar
   pairs from being penalised for that alone. Sum ≈ 1.0. */
const TONAL_WEIGHT     = 0.05;
const PALETTE_WEIGHT   = 0.45;
const DENSITY_WEIGHT   = 0.35;
const LIGHTNESS_WEIGHT = 0.20;
const SAT_WEIGHT       = 0.05;

/* Palette-contrast curve. The raw paletteContrast value is in [0, 1].
   Powering it by an exponent > 1 concentrates reward at the top:
   pairs with weak-to-moderate contrast lose more ground, pairs with
   strong contrast are barely affected. With exponent 2.0:
     1.0 → 1.0  (full reward)
     0.8 → 0.64 (20% lower)
     0.5 → 0.25 (50% lower — halves)
     0.3 → 0.09 (70% lower)
   At this setting, only pairs with strong palette contrast meaningfully
   score on the palette term — moderate similarity (warm-on-warm,
   cool-on-cool) gets pushed firmly below the surface threshold and
   relies on density or lightness contrast to qualify. Set to 1.0 to
   disable and revert to a flat linear reward. 1.5 is a gentler
   intermediate. 3.0 would be very aggressive — only near-opposite
   palettes would clear the palette gate. */
const PALETTE_CONTRAST_POWER = 2.0;

/* How hard to penalise pairs whose dominant colour matches (whether
   that's a saturated palette[0] sharing a hue family, or a blank-canvas
   neutral background). 0 disables; 1.1 is firm — pushes two-brick /
   two-blue / two-red pairs cleanly out of the top pool. Previously
   was 0.9, which let some near-misses through (oranges paired with
   golds, where hue families overlap at the boundary). */
const REPETITION_PENALTY = 1.1;

/* Joint-desaturation penalty. Fires when BOTH images are overall low
   in saturation; the diptych wants colour life from at least one
   panel. Threshold lifted from 0.25 to 0.30 so it catches more
   "everything is dust-grey" cases that just barely cleared the old
   bar. One colourful side is still enough to keep the pair alive. */
const JOINT_DESAT_PENALTY = 0.5;
const JOINT_DESAT_THRESHOLD = 0.30;

/* Joint-fullness penalty. Mirrors jointDesat for the density axis —
   when BOTH images are highly busy (above the threshold), penalise.
   Two-full pairs feel claustrophobic regardless of palette contrast;
   the eye wants somewhere to rest in at least one panel. Ramp starts
   at density 0.55 (firmly "full") and reaches full penalty by 1.0.
   Without this, two busy compositions could surface on palette
   contrast alone, even when there's no negative space anywhere. */
const JOINT_FULL_PENALTY   = 0.45;
const JOINT_FULL_THRESHOLD = 0.55;

/* Joint-emptiness penalty. The other end of the density axis —
   two near-empty frames (single subject on void, abstract texture,
   monochrome surface with sparse detail) read as repetitive even
   when palettes differ. Milder than fullness because some
   minimalist pairs work; the penalty is meant to suppress the
   "two empty similar surfaces with text/object" cases. Ramp starts
   at density 0.35 (firmly "empty") and reaches full by 0. */
const JOINT_EMPTY_PENALTY   = 0.30;
const JOINT_EMPTY_THRESHOLD = 0.35;

/* Trust penalty for signatures without real colour data — videos
   that haven't had a poster generated yet (see videoPosterUrl) and
   fall back to the neutral-grey signature. The fallback's
   mid-saturation, mid-lightness, flat-histogram values can
   "accidentally" pair well with achromatic photos under the OKLab
   colour distance — neither side has any strong colour signal, so
   `paletteContrast` reads as moderate, no `jointDesat` fires
   (fallback avgSat is 0.3, exactly at threshold), and the pair
   sneaks into the top pool with no real basis for the matching.
   This penalty pushes any pair involving a fallback signature out
   of the global top-N ranking — they can still appear via the
   per-image guarantee branch (which doesn't read pair scores), so
   videos still rotate, just less likely to surface via the main
   draw until you give them a real signature via poster image.
   0.40 is firm: a pair with otherwise solid scoring (~0.30) goes
   firmly negative once this fires. Set to 0 to disable the
   safeguard entirely. */
const FALLBACK_TRUST_PENALTY = 0.40;

/* Video pacing — probabilistic, with a minimum gap between videos so
   they never appear back-to-back. VIDEO_RATE is the per-click chance
   once eligible; VIDEO_MIN_GAP is how many photo-only clicks must
   follow a video before another can land. Average gap with the
   defaults is ~1 + 1/0.35 ≈ 3.9 clicks, so videos feel "every few
   duos" without being mechanical and without ever clustering. Set
   VIDEO_RATE = 0 to disable videos entirely. */
const VIDEO_RATE    = 0.35;
const VIDEO_MIN_GAP = 1;

/* Per-image guarantee. With this probability per click, the picker
   draws from a per-image-best list (each image's highest-scored pair)
   instead of the global top-N. Inside the guarantee branch the pick
   is staleness-weighted, so images that haven't appeared in a while
   dominate over those just shown — converting the guarantee from a
   coin flip into an active long-term rotation engine.

   At 0.45, roughly 1 in 2 clicks rotates the catalogue while the
   remaining ~1 in 2 follow the quality-biased top-250 selection.
   This keeps freshness high — most clicks bring back an under-shown
   image — without entirely abandoning the global quality ranking.
   Higher values surface rare images faster at the cost of pulling
   more weight from the quality-ranked main pool. */
const GUARANTEE_RATE = 0.45;

/* Colour analysis. COLOR_SAMPLE_SIZE is the side length of the
   downsampled canvas each image is drawn to before histogram /
   palette / saturation extraction. Larger = more accurate (small
   subjects represented by more pixels, palette filter sees finer
   colour distinctions), but quadratic in cost. 64 was the original
   compromise; 128 quadruples the pixel count (4,096 → 16,384) with
   analysis still running comfortably under 20ms per image and a
   noticeable improvement in catching small-area dominant colours.
   Going to 256+ is diminishing returns for top-4 palette extraction. */
const COLOR_SAMPLE_SIZE = 128;
const PALETTE_SIZE      = 4;
const HIST_BINS         = 7;

function altFor(num) {
  return `Federico Ferrari — Random Diptych ${num}`;
}

/* ─────────────────────────────────────────────────────────────────────────
   STATE
   ───────────────────────────────────────────────────────────────────────── */

let   images          = [];
const validImages     = [];
const colorSignatures = new Map();    // src → { histogram, palette, avgSat }
let   topPairs        = [];           // globally-ranked pair list (best first)
let   imageBests      = [];           // per-image first-best pair (kept for debugging)
let   bestsPerImage   = new Map();    // src → top K best pairs, used by pickPair guarantee branch
let   lastShown       = new Map();    // src → clickCount when last appeared; drives staleness weighting
let   recent          = new Map();    // src → clickCount when last marked recent; checked via isRecent()
let   clickCount       = 0;
let   clicksSinceVideo = Infinity;  // gap counter for VIDEO_MIN_GAP
let   interludePreload = null;
let   currentInterlude = null;
let   lastInterlude    = null;

/* ─────────────────────────────────────────────────────────────────────────
   PATH HELPERS
   ───────────────────────────────────────────────────────────────────────── */

function path(num, format, width) {
  return width
    ? `${IMAGES_BASE}/${format}/ff${num}-${width}.${format}`
    : `${IMAGES_BASE}/${format}/ff${num}.${format}`;
}
function srcset(num, format) {
  if (!SIZES.length) return path(num, format);
  return SIZES.map(w => `${path(num, format, w)} ${w}w`).join(', ');
}
/* Pick the variant width this device will most likely display. The
   image renders at most 50vw wide (`object-fit: contain` in a
   50vw × 50vh box). devicePixelRatio converts CSS px to device px.
   Returning the smallest variant ≥ that target matches what the
   browser's own srcset algorithm picks given `sizes="50vw"`, so the
   startup preload warms the SAME URL the <picture> element will
   later fetch — no wasted bandwidth, no second download on first
   display. If nothing covers the need, fall back to the largest
   available. Returns null when SIZES is empty (single-size mode). */
function pickDisplayVariant() {
  if (!SIZES.length) return null;
  const dpr    = window.devicePixelRatio || 1;
  const needed = (window.innerWidth * 0.5) * dpr;
  return SIZES.find(w => w >= needed) || SIZES[SIZES.length - 1];
}
/* Match canonical or variant URLs across any image format. Previously
   this only matched `ff{n}.jpg`, which silently returned null for any
   variant- or AVIF-suffixed path. Today every caller passes canonical
   JPGs, but it was one future code path away from breaking quietly. */
function srcToNum(src) { const m = src && src.match(/ff(\d+)(?:-\d+)?\.(?:jpg|avif|webp)$/i); return m ? m[1] : null; }
function numToSrc(n)   { return `${IMAGES_BASE}/jpg/ff${n}.jpg`; }

/* Video helpers. Videos live at `${VIDEO_BASE}/ff{n}.mp4` and share
   no numeric namespace with images (ff1.mp4 is a different asset
   than ff1.jpg). The hash uses a 'v' prefix to distinguish — e.g.
   `#5,v12` means image 5 paired with video 12. Bare-number hashes
   still parse as images, so existing share links keep working. */
function isVideo(src)     { return /\.mp4$/i.test(src); }
function videoNumToSrc(n) { return `${VIDEO_BASE}/ff${n}.mp4`; }

/* Poster companion image for a video — same directory, same name,
   '-poster.jpg' suffix. The site loads and analyses this with the
   regular image-analysis path (analyzeImage) so videos get a real
   colour signature derived from the same algorithm as the photos,
   instead of the neutral grey fallback. Reliable on every browser
   — none of iOS Safari's offscreen-video / canvas-tainting issues
   that plague runtime frame extraction.

   Generate one per video with ffmpeg. Pick a representative frame
   roughly 25% in:

     for f in videos/ff*.mp4; do
       N="${f%.mp4}"
       ffmpeg -ss 1 -i "$f" -frames:v 1 -q:v 3 "${N}-poster.jpg"
     done

   Videos without a poster fall back to runtime frame extraction
   (loadVideoForAnalysis), then to the neutral signature on failure.
   So adding posters is opt-in per video; everything still works
   without them, just with worse pairing for those videos. */
function videoPosterUrl(videoSrc) {
  return videoSrc.replace(/\.mp4$/i, '-poster.jpg');
}
function srcToId(src) {
  if (isVideo(src)) {
    const m = src.match(/ff(\d+)\.mp4$/i);
    return m ? 'v' + m[1] : null;
  }
  return srcToNum(src);
}
function idToSrc(id) {
  if (!id) return null;
  return id[0] === 'v' ? videoNumToSrc(id.slice(1)) : numToSrc(id);
}

/* SIBLINGS — flat src → [sibling srcs] lookup derived from
   SIBLING_GROUPS. Built once at script init since the groups are
   static config. Used in markPairRecent() to extend the recent[]
   block to every sibling of an image when its pair is shown. */
const SIBLINGS = new Map();
for (const group of SIBLING_GROUPS) {
  const srcs = group.map(idToSrc).filter(Boolean);
  for (const s of srcs) {
    SIBLINGS.set(s, srcs.filter(x => x !== s));
  }
}

/* ─────────────────────────────────────────────────────────────────────────
   IMAGE DISCOVERY
   ───────────────────────────────────────────────────────────────────────── */

async function discoverBy(urlFor) {
  const found = [];
  let start = 1;
  while (true) {
    const batch = Array.from({ length: DISCOVER_BATCH }, (_, i) => start + i);
    const results = await Promise.all(
      batch.map(n =>
        fetch(urlFor(n), { method: 'HEAD' })
          .then(r => (r.ok ? n : null))
          .catch(() => null)
      )
    );
    const present = results.filter(n => n !== null);
    found.push(...present);
    if (present.length === 0) break;

    /* Partial-hit batch usually means we've crossed the end of the
       catalogue, but it could also be a hole (e.g. ff100 deleted,
       ff101-ff147 still present). Probe one item far ahead to tell
       the two apart. If the far probe also 404s, we're at the end
       and break — saves the ~20 wasted HEADs the old logic spent
       on a confirming full batch. If it hits, there's genuinely a
       hole; continue normal batched probing. Trade-off: a hole
       exactly between this batch and the far probe would fool us,
       but that's a contrived pattern; in practice catalogues are
       contiguous or have small isolated gaps. */
    if (present.length < batch.length) {
      const farN = start + DISCOVER_BATCH * 2;
      const farExists = await fetch(urlFor(farN), { method: 'HEAD' })
        .then(r => r.ok).catch(() => false);
      if (!farExists) break;
    }

    start += DISCOVER_BATCH;
  }
  return found.sort((a, b) => a - b);
}

const discoverImages = () => discoverBy(n => `${IMAGES_BASE}/jpg/ff${n}.jpg`);
const discoverVideos = () => discoverBy(n => `${VIDEO_BASE}/ff${n}.mp4`);

/* ─────────────────────────────────────────────────────────────────────────
   COLOUR ANALYSIS
   Per image: a normalised lightness histogram, a small filtered palette,
   and the average saturation. These three signals feed pairScore.
   ───────────────────────────────────────────────────────────────────────── */

function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l };
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  switch (max) {
    case r: h = ((g - b) / d + (g < b ? 6 : 0)); break;
    case g: h = ((b - r) / d + 2);               break;
    default: h = ((r - g) / d + 4);
  }
  return { h: h * 60, s, l };
}

/* OKLab — a perceptually uniform colour space (Björn Ottosson, 2020).
   Euclidean distance between two points in OKLab tracks how the eye
   actually reads the difference between the two colours, unlike HSL
   where a soft pink and a deep red can look "close" by hue but read
   as unrelated, and two greys at different lightnesses can score as
   "far" despite both being grey. This is what powers colorSimilarity,
   so the precision of the entire pair score depends on it.

   Pipeline: sRGB (0–255) → normalised → linearised (inverse gamma) →
   LMS cone response matrix → cube-root non-linearity → OKLab matrix.
   Constants are the canonical ones from the OKLab spec. Output L is
   roughly [0, 1] (black to white); a is roughly [-0.4, +0.4] (green
   to red); b is roughly [-0.4, +0.4] (blue to yellow). Maximum
   Euclidean distance between sRGB-renderable points is ~1 (black/
   white axis); typical "very different" photo colours sit at ~0.4. */
function rgbToOklab(r, g, b) {
  /* sRGB normalize + linearize (undo the gamma curve). */
  const lin = c => {
    c /= 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const lr = lin(r), lg = lin(g), lb = lin(b);

  /* Linear-RGB → LMS cone responses. */
  const l = 0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb;
  const m = 0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb;
  const s = 0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb;

  /* Cube-root for the perceptual non-linearity. */
  const l_ = Math.cbrt(l), m_ = Math.cbrt(m), s_ = Math.cbrt(s);

  /* LMS' → OKLab. */
  return {
    L: 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
    a: 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
    b: 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
  };
}

/* Compositional density via lightness-histogram entropy. Returns
   [0, 1] — 0 = all pixels in one tonal bin (uniform / "empty"),
   1 = pixels perfectly spread across all bins ("full" / busy).
   Real photos fall roughly between 0.2 (sky / wall) and 0.85
   (varied scene). Used by pairScore to reward density CONTRAST,
   not density itself. Precomputed once per image and cached on
   the signature — pairScore reads sig.density. */
function histogramDensity(histogram) {
  let H = 0;
  for (const p of histogram) {
    if (p > 0) H -= p * Math.log2(p);
  }
  return H / Math.log2(HIST_BINS);
}

/* L2 norm of the histogram, cached so pairScore's cosine similarity
   doesn't recompute it on every pair. The sqrt is paid once per image
   instead of twice per pair. */
function histogramMagnitude(histogram) {
  let m = 0;
  for (const p of histogram) m += p * p;
  return Math.sqrt(m);
}

function analyzeImage(img) {
  try {
    const N = COLOR_SAMPLE_SIZE;
    const canvas = document.createElement('canvas');
    canvas.width = N; canvas.height = N;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0, N, N);
    const data = ctx.getImageData(0, 0, N, N).data;

    const histogram = new Array(HIST_BINS).fill(0);
    const buckets   = new Map();
    let totalWeight = 0, satWeighted = 0, lumWeighted = 0;

    /* Centre-weighting parameters. Subject of a photo is almost always
       near the middle of the frame; edges carry background or peripheral
       content. Giving centre pixels more vote in palette / histogram /
       avgSat / meanL construction makes the colour signature better
       reflect what the photo is *about*, not just what it has the most
       surface area of. Linear ramp from CENTER_WEIGHT at the centre
       down to 1.0 at the corners, measured by max(|dx|, |dy|) — a
       square gradient that matches the rectangular pixel grid (rounder
       L2 distance would over-penalise the corners). 1.6 was chosen as
       a moderate setting: meaningfully changes pairing for portraits
       and centred subjects without overpowering background colour when
       that genuinely matters (wide landscapes, full-bleed textures).
       Set to 1.0 to disable centre weighting entirely. */
    const CENTER_WEIGHT = 1.6;
    const half = N / 2;

    for (let i = 0, px = 0; i < data.length; i += 4, px++) {
      const r = data[i], g = data[i + 1], b = data[i + 2];

      /* Centre-weighted pixel vote. px = pixel index in row-major
         order; x and y reconstructed from it. Distance from centre
         normalised to [0, 1] via max-axis. */
      const x = px % N;
      const y = (px / N) | 0;
      const dx = Math.abs(x - half) / half;
      const dy = Math.abs(y - half) / half;
      const distFromCenter = Math.max(dx, dy);                  // 0 = centre, 1 = edge
      const w = 1 + (CENTER_WEIGHT - 1) * (1 - distFromCenter); // CENTER_WEIGHT → 1
      totalWeight += w;

      const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      histogram[Math.min(HIST_BINS - 1, Math.floor(lum * HIST_BINS))] += w;
      lumWeighted += lum * w;

      const key = (r >> 3) << 10 | (g >> 3) << 5 | (b >> 3);
      buckets.set(key, (buckets.get(key) || 0) + w);

      const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      const L = (mx + mn) / 510;
      const sat = mx === mn ? 0 : (L > 0.5 ? (mx - mn) / (510 - mx - mn) : (mx - mn) / (mx + mn));
      satWeighted += sat * w;
    }
    for (let i = 0; i < HIST_BINS; i++) histogram[i] /= totalWeight;
    const avgSat = satWeighted / totalWeight;
    const meanL  = lumWeighted / totalWeight;   // mean lightness, [0, 1]

    const sorted  = [...buckets.entries()].sort((a, b) => b[1] - a[1]);
    const palette = [];

    for (const [key, count] of sorted) {
      if (palette.length >= PALETTE_SIZE) break;
      const r = ((key >> 10) & 31) << 3;
      const g = ((key >>  5) & 31) << 3;
      const b =  (key        & 31) << 3;
      const hsl = rgbToHsl(r, g, b);

      if (hsl.l < 0.06 || hsl.l > 0.94 || hsl.s < 0.08) continue;

      let merged = false;
      for (const p of palette) {
        let hueDiff = Math.abs(p.hsl.h - hsl.h);
        if (hueDiff > 180) hueDiff = 360 - hueDiff;
        if (hueDiff < 18 && Math.abs(p.hsl.l - hsl.l) < 0.12) {
          p.weight += count / totalWeight;
          merged = true;
          break;
        }
      }
      /* Each palette entry carries BOTH coordinate systems:
         - hsl: used by the palette-merge heuristic above and by
           dominantRepetition's hue-family detection (hue distance
           is the right idea there — two reds at different lightnesses
           ARE related as a "red repetition", which is what we want
           to catch).
         - oklab: used by colorSimilarity in pair scoring, where
           perceptual distance is what matters. */
      if (!merged) palette.push({
        hsl,
        oklab: rgbToOklab(r, g, b),
        weight: count / totalWeight,
      });
    }

    if (palette.length === 0) {
      /* Edge case: every dominant bucket was filtered out by the
         too-dark / too-light / too-desaturated gate. Use the overall
         mean colour as a last-resort palette of one. Uses simple
         pixel-count means (not centre-weighted) for robustness — it's
         already a fallback path. */
      let tr = 0, tg = 0, tb = 0, n = 0;
      for (let i = 0; i < data.length; i += 4) {
        tr += data[i]; tg += data[i + 1]; tb += data[i + 2]; n++;
      }
      const mr = tr / n, mg = tg / n, mb = tb / n;
      palette.push({
        hsl:    rgbToHsl(mr, mg, mb),
        oklab:  rgbToOklab(mr, mg, mb),
        weight: 1,
      });
    }
    const wTotal = palette.reduce((s, p) => s + p.weight, 0);
    palette.forEach(p => p.weight /= wTotal);

    /* Cache per-image scalars that pairScore would otherwise recompute
       O(N²) times — entropy of the lightness histogram (density), the
       L2 norm of the histogram (for the tonalScore cosine), and the
       mean lightness (for lightness contrast). With N≈100 these add
       up: ~10k entropy calls, ~10k sqrt calls per full computeTopPairs
       pass collapse to N each. */
    return {
      histogram,
      palette,
      avgSat,
      meanL,
      density: histogramDensity(histogram),
      histMag: histogramMagnitude(histogram),
    };
  } catch {
    return null;
  }
}

/* Pure similarity measure between two palette entries. 1 = effectively
   the same colour, 0 = at opposite ends of the perceivable gamut.
   Uses Euclidean distance in OKLab — a perceptually uniform space
   where distance correlates with how the eye reads the difference.
   This replaces the previous HSL hue/lightness mix, which gave
   misleading scores in both directions (e.g. soft pink vs deep red
   reading as close because hue agrees; two greys at different
   lightnesses reading as far despite both being grey).

   Normalisation: max practical distance between sRGB-renderable
   points is ~1.0 (black to white axis). Typical "very different"
   photo colours sit around 0.3–0.5. Clamping at 1.0 means similarity
   = max(0, 1 - dist) maps the full sensible range to [0, 1] without
   needing a custom curve. */
function colorSimilarity(c1, c2) {
  const dL = c1.oklab.L - c2.oklab.L;
  const da = c1.oklab.a - c2.oklab.a;
  const db = c1.oklab.b - c2.oklab.b;
  const dist = Math.sqrt(dL * dL + da * da + db * db);
  return Math.max(0, 1 - dist);
}

/* Detects "blank canvas" repetition — both images are mostly dominated
   by the same low-saturation tone (pale sky, white wall, grey backdrop),
   with that tone often too desaturated for the palette filter to
   register it as the dominant colour. Catches the case dominantRepetition
   misses, where palette[0] ends up being a tiny accent colour while the
   actual visual dominant is a neutral.

   The saturation gate is SOFT — it scales linearly from full effect at
   avgSat 0 down to no effect at avgSat 0.3 — so borderline cases
   (knife photo with a small saturated handle on a pale wall, sky with
   wispy clouds) still get caught when they share the same tonal cluster
   with another image. A hard cutoff at 0.15 was missing these. */
function blankCanvasRepetition(a, b) {
  const satGateA = Math.max(0, 1 - a.avgSat / 0.3);
  const satGateB = Math.max(0, 1 - b.avgSat / 0.3);
  const satGate  = Math.min(satGateA, satGateB);
  if (satGate === 0) return 0;

  /* Find each image's strongest 2-adjacent-bin slab and its position. */
  const concentration = sig => {
    let best = 0, bestIdx = 0;
    for (let i = 0; i < HIST_BINS - 1; i++) {
      const adjacent = sig.histogram[i] + sig.histogram[i + 1];
      if (adjacent > best) { best = adjacent; bestIdx = i; }
    }
    return { val: best, idx: bestIdx };
  };

  const ca = concentration(a);
  const cb = concentration(b);

  /* Both must be heavily dominated by their neutral mass (>50% in two
     adjacent bins) AND that mass must sit at roughly the same lightness. */
  if (ca.val < 0.5 || cb.val < 0.5) return 0;
  if (Math.abs(ca.idx - cb.idx) > 1) return 0;

  return Math.min(ca.val, cb.val) * satGate;
}

/* Detects when both images share essentially the same predominant
   colour. PART 1 catches saturated palette[0] matches by hue family —
   a deep blue paired with a pale blue still counts as "two blues",
   regardless of lightness difference. PART 2 catches blank-canvas
   images where the dominant area is a neutral the palette filter
   stripped out. */
function dominantRepetition(a, b) {
  const cA = a.palette[0];
  const cB = b.palette[0];

  /* PART 1: palette[0] hue-family match */
  let palRep = 0;
  if (cA && cB) {
    let hueDiff = Math.abs(cA.hsl.h - cB.hsl.h);
    if (hueDiff > 180) hueDiff = 360 - hueDiff;

    const satGate = Math.min(cA.hsl.s, cB.hsl.s);

    /* Saturated dominants: same hue family (within 30°) = repetition,
       regardless of lightness. Desaturated dominants (greys): fall
       back to lightness proximity, since hue is meaningless. */
    const closeness = satGate > 0.15
      ? Math.max(0, 1 - hueDiff / 30)
      : Math.max(0, 1 - Math.abs(cA.hsl.l - cB.hsl.l) / 0.15);

    /* Weight by prominence — a tiny matching patch doesn't qualify. */
    palRep = closeness * Math.min(cA.weight, cB.weight);
  }

  /* PART 2: blank-canvas neutral match */
  const blankRep = blankCanvasRepetition(a, b);

  return Math.max(palRep, blankRep);
}

/* Pair score: rewards palette contrast (perceptual, OKLab-based),
   compositional density contrast (full-vs-empty), lightness contrast
   (bright-vs-dark), tonal cohesion as a faint signal, and saturation
   match as a tiebreaker. Minus penalties for predominant-colour
   repetition, two-empty pairs (joint-desat, joint-empty-density),
   and two-full pairs (joint-full-density). */
function pairScore(a, b) {
  if (!a || !b) return 0;

  /* Tonal cohesion via histogram cosine similarity. Kept as a low
     weight — it directly conflicts with density contrast, since a
     full scene and an empty sky have very different histograms.
     Magnitudes are precomputed once per image (see histogramMagnitude
     in analyzeImage), so only the dot product runs per pair. */
  let dot = 0;
  for (let i = 0; i < HIST_BINS; i++) {
    dot += a.histogram[i] * b.histogram[i];
  }
  const tonalScore = (a.histMag && b.histMag) ? dot / (a.histMag * b.histMag) : 0;

  /* Palette overlap: for each colour in A, find its best match in B
     and weight by A's weight. Symmetric average over both directions.
     colorSimilarity reads .oklab from each palette entry. */
  const directional = (pA, pB) => {
    let total = 0;
    for (const cA of pA) {
      let best = 0;
      for (const cB of pB) {
        const s = colorSimilarity(cA, cB);
        if (s > best) best = s;
      }
      total += best * cA.weight;
    }
    return total;
  };
  const paletteOverlap  = (directional(a.palette, b.palette) + directional(b.palette, a.palette)) / 2;
  const paletteContrast = 1 - paletteOverlap;

  /* Concentrate reward at the top of the contrast range. With
     PALETTE_CONTRAST_POWER = 2.0: a pair scoring 1.0 keeps full
     reward, a pair at 0.5 drops to 0.25 (halves), at 0.3 drops to
     0.09. Effect: only pairs with strong palette contrast score
     meaningfully on the palette term; moderate palette similarity
     (warm-on-warm, cool-on-cool, near-tonal-match pairs) drops
     hard. See the constant declaration above for the full curve. */
  const paletteContrastShaped = Math.pow(paletteContrast, PALETTE_CONTRAST_POWER);

  /* Compositional density contrast: Shannon entropy of each image's
     lightness histogram, normalised to [0, 1]. Cached on the signature
     (a.density / b.density) so this is just a subtraction per pair
     instead of two entropy passes. The score rewards the ABSOLUTE
     DIFFERENCE — pairing one full and one empty image gets a strong
     boost, while two-full or two-empty pairs contribute nothing from
     this term. Multiplying by paletteContrast (not its shaped form)
     keeps the original interlock: density bonus only when there's
     also some palette difference, to avoid the "two-blue / one busy
     one calm" fake-contrast trap. */
  const densityContrast = Math.abs(a.density - b.density);

  /* Lightness contrast — perceptual mean L (from OKLab pipeline,
     stored as meanL [0, 1]). Rewards "one bright, one dark" pairings
     independently of palette and density. Two photos can have the
     same warm palette and similar density yet read very differently
     if one is high-key (bright noon) and the other low-key (golden
     hour); this term captures that "up-and-down" dimension explicitly.
     Unlike densityContrast it is NOT gated by paletteContrast — a
     bright-dark pair with a shared family of colours still scores
     well here, since the lightness shift alone creates visual
     dialogue. Max value ~0.8 (white-vs-black photo); typical
     "noticeable" lightness contrast is 0.25–0.4. */
  const lightnessContrast = Math.abs(a.meanL - b.meanL);

  const satMatch = 1 - Math.abs(a.avgSat - b.avgSat);

  const repetition = dominantRepetition(a, b);

  /* Joint-desaturation penalty: fires when BOTH images sit below
     JOINT_DESAT_THRESHOLD avgSat. Threshold raised from 0.25 → 0.30
     so it catches more "everything is dust-grey" cases. One
     colourful side is enough to keep the pair alive. */
  const maxSat     = Math.max(a.avgSat, b.avgSat);
  const jointDesat = Math.max(0, 1 - maxSat / JOINT_DESAT_THRESHOLD);

  /* Joint-fullness penalty: ramps up when BOTH images are busy —
     i.e. the minimum of the two densities is above the threshold.
     Two highly-composed images compete for the eye; somewhere
     should be quiet. Uses min(a, b) so the penalty fires only when
     BOTH cross into "full" territory — a busy image paired with
     an empty one is exactly what densityContrast rewards. */
  const minDensity = Math.min(a.density, b.density);
  const jointFull  = Math.max(0, (minDensity - JOINT_FULL_THRESHOLD) /
                                 (1 - JOINT_FULL_THRESHOLD));

  /* Joint-emptiness penalty: mirror of fullness, ramps up when
     BOTH images are near-empty — max(a, b) below the threshold.
     Catches "two minimal surfaces with sparse detail" pairs that
     are technically distinct in palette/saturation but feel
     visually similar (both quiet). Milder than fullness because
     some minimalist pairs work intentionally. */
  const maxDensity = Math.max(a.density, b.density);
  const jointEmpty = Math.max(0, (JOINT_EMPTY_THRESHOLD - maxDensity) /
                                 JOINT_EMPTY_THRESHOLD);

  /* Fallback-signature trust penalty. Either side carrying the
     isFallback marker (video without a poster yet) deducts a fixed
     amount, large enough to keep the pair out of the global top-N
     pool. See FALLBACK_TRUST_PENALTY declaration for rationale.
     Boolean OR — penalty doesn't double up if both sides are
     fallback (extremely rare anyway since first pair is photo-only). */
  const trustPenalty = (a.isFallback || b.isFallback) ? FALLBACK_TRUST_PENALTY : 0;

  return tonalScore                                * TONAL_WEIGHT
       + paletteContrastShaped                     * PALETTE_WEIGHT
       + paletteContrast * densityContrast         * DENSITY_WEIGHT
       + lightnessContrast                         * LIGHTNESS_WEIGHT
       + satMatch                                  * SAT_WEIGHT
       - repetition                                * REPETITION_PENALTY
       - jointDesat                                * JOINT_DESAT_PENALTY
       - jointFull                                 * JOINT_FULL_PENALTY
       - jointEmpty                                * JOINT_EMPTY_PENALTY
       - trustPenalty;
}

function computeTopPairs() {
  if (validImages.length < 2) { topPairs = []; imageBests = []; bestsPerImage = new Map(); return; }
  const arr   = validImages.filter(s => colorSignatures.has(s));
  const pairs = [];
  for (let i = 0; i < arr.length; i++) {
    for (let j = i + 1; j < arr.length; j++) {
      pairs.push({
        a: arr[i],
        b: arr[j],
        score: pairScore(colorSignatures.get(arr[i]), colorSignatures.get(arr[j]))
      });
    }
  }
  pairs.sort((x, y) => y.score - x.score);
  topPairs = pairs;

  /* Per-image best pairs. Two parallel structures from one pass:
     - bestsPerImage: Map<src, Pair[]> with each image's top
       BESTS_PER_IMAGE highest-scoring partner-pairs. The guarantee
       branch consults this so an image with a single popular best
       partner (often in recent[]) still has fallback options. With
       K=1 (the previous design), modest-scoring images whose top
       partner was a heavy-hitter got systematically starved: their
       single imageBests row was filtered out whenever the partner
       appeared recently, and they weren't in the global top-N pool
       either. K=5 means recent[] would need to block five separate
       best-pairings before an image disappears from the guarantee
       pool — much less common.
     - imageBests: flat list of first-pair-per-image, kept purely
       for debugging via window.__imageBests so existing console
       workflows still work. Not consulted by pickPair anymore. */
  const BESTS_PER_IMAGE = 5;
  bestsPerImage = new Map();
  for (const pair of pairs) {
    for (const src of [pair.a, pair.b]) {
      const list = bestsPerImage.get(src);
      if (!list) bestsPerImage.set(src, [pair]);
      else if (list.length < BESTS_PER_IMAGE) list.push(pair);
    }
  }

  const seen  = new Set();
  const bests = [];
  for (const pair of pairs) {
    if (!seen.has(pair.a) || !seen.has(pair.b)) {
      bests.push(pair);
      seen.add(pair.a);
      seen.add(pair.b);
    }
    if (seen.size >= arr.length) break;
  }
  imageBests = bests;

  window.__topPairs      = topPairs;
  window.__imageBests    = imageBests;
  window.__bestsPerImage = bestsPerImage;
}

/* Debounced wrapper. During initial parallel discovery, every image
   that finishes loading would trigger a full O(N²) recompute — with
   30 images that's 30 back-to-back recomputes of ~400 pair scores
   each, all on the main thread. Coalescing into one recompute per
   frame keeps the splash animation smooth without changing semantics:
   pickPair only consults topPairs after the splash dismisses anyway. */
let topPairsPending = 0;
function scheduleTopPairs() {
  if (topPairsPending) return;
  topPairsPending = requestAnimationFrame(() => {
    topPairsPending = 0;
    computeTopPairs();
  });
}

/* ─────────────────────────────────────────────────────────────────────────
   SHAREABLE URL HASH
   ───────────────────────────────────────────────────────────────────────── */

function pairToHash(p) { return '#' + p.map(srcToId).join(','); }
function hashToPair() {
  const m = location.hash.match(/^#(v?\d+),(v?\d+)$/);
  if (!m || m[1] === m[2]) return null;
  const a = idToSrc(m[1]), b = idToSrc(m[2]);
  return (validImages.includes(a) && validImages.includes(b)) ? [a, b] : null;
}

/* ─────────────────────────────────────────────────────────────────────────
   IMAGE LOADING & SWAPPING
   ───────────────────────────────────────────────────────────────────────── */

/* Staleness — how many clicks since an image last appeared. Never-shown
   images return a large constant so they dominate any weighted selection
   until they've appeared at least once. The picker uses this to weight
   the guarantee branch toward images that haven't surfaced recently. */
function staleness(src) {
  const last = lastShown.get(src);
  return last === undefined ? clickCount + 1000 : clickCount - last;
}

/* Recent-block check. An image is considered "recent" if its last
   marking was within RECENT_CLICKS_BLOCK clicks. Map-based lookup
   replaces the previous array+slice approach so adding siblings to
   the block doesn't shrink the effective window (each sibling-push
   used to trim older entries early, making the block window expire
   faster on clicks that happened to involve grouped images). */
function isRecent(src) {
  const last = recent.get(src);
  return last !== undefined && (clickCount - last) < RECENT_CLICKS_BLOCK;
}

/* Mark a pair as just-shown — and propagate the block to every
   sibling declared in SIBLING_GROUPS. Without sibling propagation,
   near-duplicates like ff97/ff98 (same shoot, near-identical
   colour signature) could appear in adjacent clicks because the
   scorer sees them as independent images. */
function markPairRecent(pair) {
  for (const src of pair) {
    recent.set(src, clickCount);
    const sibs = SIBLINGS.get(src);
    if (sibs) for (const s of sibs) recent.set(s, clickCount);
  }
}

/* Weighted random pick from a list. Each item's chance of selection is
   proportional to weightFn(item). Falls back to uniform if all weights
   are zero (defensive). Used by the guarantee branch to favor rarely-
   shown images, but generic enough to reuse elsewhere if needed. */
function weightedPick(items, weightFn) {
  let total = 0;
  const weights = new Array(items.length);
  for (let i = 0; i < items.length; i++) {
    weights[i] = Math.max(0, weightFn(items[i]));
    total += weights[i];
  }
  if (total === 0) return items[Math.floor(Math.random() * items.length)];
  let r = Math.random() * total;
  for (let i = 0; i < items.length; i++) {
    r -= weights[i];
    if (r <= 0) return items[i];
  }
  return items[items.length - 1];
}

function pickPair(arr, opts) {
  /* allowVideos defaults to true so existing call-sites keep their
     behaviour. The first-pair caller in start() passes false because
     videos aren't preloaded by the splash and a video pick would
     leave the consent card waiting on a fresh MP4 fetch (2-4s on
     decent connections). Affects BOTH the guarantee branch and the
     main top-N draw — previously only the `arr` argument was filtered,
     and pickPair ignored that for everything except the random
     fallback below, so the safety claimed by the call-site was never
     actually realised. */
  const allowVideos = !opts || opts.allowVideos !== false;

  if (topPairs.length === 0) {
    const pool = allowVideos ? arr : arr.filter(s => !isVideo(s));
    const src  = pool.length ? pool : arr;
    const i = Math.floor(Math.random() * src.length);
    let   j = Math.floor(Math.random() * src.length);
    while (j === i && src.length > 1) j = Math.floor(Math.random() * src.length);
    return [src[i], src[j]];
  }

  /* Per-image guarantee draw. With GUARANTEE_RATE probability, pick
     via bestsPerImage — each image's top BESTS_PER_IMAGE highest-
     scoring pairings, with weighted selection by staleness of the
     source image. Previously this branch used `imageBests` (one
     row per image, that image's single highest-scoring pair). The
     failure mode was: a modest-score image's single best partner
     was often a globally-popular image, which appeared often in
     recent[]; the filter then removed the modest image's only row,
     and since its global rank was below TOP_PAIRS_POOL, it didn't
     appear in the main pool either — silently starved across
     sessions. With K=5 best partners per image, we'd need recent[]
     to be blocking all five before the image vanishes from the
     guarantee pool. */
  if (Math.random() < GUARANTEE_RATE && bestsPerImage.size > 0) {
    /* Build per-image candidates: for each image not currently in
       recent[], find its highest-scoring pair where neither side
       is in recent[]. Iteration is over the Map's keys, so each
       image contributes at most ONE candidate row, weighted by
       that image's own staleness. When allowVideos is false (first
       pair), the candidate must be photo-only on both sides AND
       the source image itself must not be a video. */
    const candidates = [];
    for (const [src, pairList] of bestsPerImage) {
      if (isRecent(src)) continue;
      if (!allowVideos && isVideo(src)) continue;
      const avail = pairList.find(p =>
        !isRecent(p.a) && !isRecent(p.b) &&
        (allowVideos || (!isVideo(p.a) && !isVideo(p.b)))
      );
      if (avail) candidates.push({ src, pair: avail });
    }
    if (candidates.length > 0) {
      const gHasVideos = allowVideos && candidates.some(c => isVideo(c.pair.a) || isVideo(c.pair.b));
      const gEligible  = clicksSinceVideo >= VIDEO_MIN_GAP;
      const gWantVideo = gHasVideos && gEligible && Math.random() < VIDEO_RATE;
      const gFiltered  = gWantVideo
        ? candidates.filter(c =>  isVideo(c.pair.a) ||  isVideo(c.pair.b))
        : candidates.filter(c => !isVideo(c.pair.a) && !isVideo(c.pair.b));
      const gPool      = gFiltered.length > 0 ? gFiltered : candidates;
      /* Weight by the source image's staleness — never-shown images
         return very large staleness, so they dominate until they
         surface, then normalize. Note: a pair (A,B) may appear as
         a candidate twice (once with src=A, once with src=B) if
         it's in both images' top K. That's intentional — pairs
         where both sides are stale get effectively doubled weight.

         Favorites get their staleness multiplied by FAVORITE_BOOST,
         so listed images cycle back more often once their recent-
         block window has cleared. See FAVORITE_IMAGES near the top
         of this file. */
      const chosen = weightedPick(gPool, c => {
        const base = staleness(c.src);
        const num  = srcToNum(c.src);
        return (num && FAVORITE_IMAGES.has(num)) ? base * FAVORITE_BOOST : base;
      });
      const pair       = chosen.pair;
      const drewVideo  = isVideo(pair.a) || isVideo(pair.b);
      clicksSinceVideo = drewVideo ? 0 : clicksSinceVideo + 1;
      return Math.random() < 0.5 ? [pair.a, pair.b] : [pair.b, pair.a];
    }
    /* Every image's K best partners blocked by recent[] AND every
       image itself in recent[]. Vanishingly rare. Falls through. */
  }

  /* Filter out pairs containing any image shown in the last
     RECENT_CLICKS_BLOCK clicks. If that filter empties the pool
     (e.g. early in the session when fewer images have loaded), fall
     back to the full ranked list rather than getting stuck. */
  let available = topPairs.filter(p => !isRecent(p.a) && !isRecent(p.b));
  if (!allowVideos) {
    available = available.filter(p => !isVideo(p.a) && !isVideo(p.b));
  }
  /* Fallback pool also respects allowVideos so a tiny early-session
     pool doesn't smuggle a video into the first pair. */
  const fallback  = allowVideos ? topPairs : topPairs.filter(p => !isVideo(p.a) && !isVideo(p.b));
  const pool      = available.length > 0 ? available : fallback;

  /* Probabilistic video selection with a minimum-gap guard. The
     gap check ensures videos never appear back-to-back; once past
     the gap, VIDEO_RATE decides per click. Non-video clicks are
     filtered to photo-only pairs so the rate is exact — without
     this, videos could still slip in via the top-N pool. The
     fallback to `pool` covers the (rare) case where filtering
     leaves nothing, e.g. very early in the session. */
  const hasVideos = allowVideos && pool.some(p => isVideo(p.a) || isVideo(p.b));
  const eligible  = clicksSinceVideo >= VIDEO_MIN_GAP;
  const wantVideo = hasVideos && eligible && Math.random() < VIDEO_RATE;
  const drawPool  = wantVideo
    ? pool.filter(p =>  isVideo(p.a) ||  isVideo(p.b))
    : pool.filter(p => !isVideo(p.a) && !isVideo(p.b));
  const finalPool = drawPool.length > 0 ? drawPool : pool;

  /* Update the gap counter based on what we're actually going to
     draw, not what we wanted — if drawPool collapsed and we fell
     back to `pool`, the result may or may not include a video. */
  const drawingVideo = finalPool === drawPool && wantVideo;
  clicksSinceVideo   = drawingVideo ? 0 : clicksSinceVideo + 1;

  const poolSize  = Math.min(TOP_PAIRS_POOL, finalPool.length);
  /* Quality-biased pick: squaring Math.random() stretches the
     distribution toward 0 so the highest-scored pairs dominate.
     With the current pool of 150, the top 10% (15 pairs) receives
     ~32% of picks and the bottom half receives ~29%. The hard
     floor on quality comes from TOP_PAIRS_POOL itself (no pair
     ranked worse than 150 ever appears); this bias just shifts
     the rotation toward the very top within that pool. Bump to
     ** 3 for stronger top-emphasis, ** 1 for uniform. */
  const chosen    = finalPool[Math.floor(Math.random() ** 2 * poolSize)];

  return Math.random() < 0.5 ? [chosen.a, chosen.b] : [chosen.b, chosen.a];
}

function preparePanel(panelEl, src) {
  const layers = panelEl.querySelectorAll('.layer');
  const active = panelEl.querySelector('.layer.loaded');
  const back   = (active === layers[0]) ? layers[1] : layers[0];

  if (isVideo(src)) {
    /* Reuse a video element in the back layer if one's already there,
       otherwise rebuild. The element gets `muted` + `playsinline` so
       iOS won't go fullscreen and so autoplay isn't blocked. `autoplay`
       attribute is belt-and-braces alongside the explicit play() call
       — some iOS Safari versions prefer the attribute over the JS
       call, others vice versa. Setting muted as both attribute AND
       property defends against iOS quirks where the property can be
       silently reset when src changes. */
    let video = back.querySelector('video');
    if (!video) {
      back.innerHTML = '<video muted autoplay loop playsinline preload="auto"></video>';
      video = back.querySelector('video');
    }
    /* Re-assert muted + playsinline state every time, because some
       iOS versions reset these on src change. Cheap and harmless. */
    video.muted       = true;
    video.playsInline = true;
    return new Promise(resolve => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        /* play() returns a Promise that rejects when autoplay is
           blocked (iOS Low Power Mode, some Android battery saver
           modes). Catch it explicitly — a sync try/catch wouldn't
           catch the rejection. Resolved either way so the diptych
           still appears, just with the first frame frozen until a
           user gesture unblocks playback on the next click. */
        video.play().catch(() => {});
        resolve({ back, active });
      };
      /* Timeout safety net. If the video file is unreachable or
         corrupt enough that loadeddata + error both never fire,
         resolve anyway after VIDEO_DISPLAY_TIMEOUT_MS so the
         gallery stays responsive. */
      const timer = setTimeout(() => {
        if (!settled) console.warn('Diptych: video load timeout', src);
        done();
      }, VIDEO_DISPLAY_TIMEOUT_MS);
      video.addEventListener('loadeddata', done, { once: true });
      video.addEventListener('error',      done, { once: true });
      video.src = src;
      video.load();
    });
  }

  /* Image path — reuse the existing picture element if present,
     otherwise rebuild it (the layer may currently hold a video). */
  let picture = back.querySelector('picture');
  if (!picture) {
    /* If a video occupied this layer, stop its download / playback
       before the markup is replaced. Without this, the previous video
       can keep streaming bytes in the background while its DOM element
       is destroyed, wasting bandwidth on slow connections. */
    const oldVideo = back.querySelector('video');
    if (oldVideo) {
      try {
        oldVideo.pause();
        oldVideo.removeAttribute('src');
        oldVideo.load();
      } catch {}
    }
    back.innerHTML =
      '<picture>' +
        '<source type="image/avif" sizes="50vw">' +
        '<img alt="" sizes="50vw">' +
      '</picture>';
    picture = back.querySelector('picture');
  }

  const num     = srcToNum(src);
  const avifSrc = picture.querySelector('source[type="image/avif"]');
  const img     = picture.querySelector('img');

  avifSrc.srcset = FORMATS.includes('avif') ? srcset(num, 'avif') : '';
  img.srcset     = SIZES.length ? srcset(num, 'jpg') : '';
  img.src        = path(num, 'jpg');
  img.alt        = altFor(num);

  /* Race decode against a timeout — see IMAGE_DECODE_TIMEOUT_MS for
     the reasoning. The image still appears whenever decode actually
     finishes (img.src is set independently and the browser paints it
     on the next frame regardless of whether we awaited the promise);
     the race just ensures the surrounding load flow never wedges. */
  return Promise.race([
    img.decode().catch(() => {}),
    new Promise(r => setTimeout(r, IMAGE_DECODE_TIMEOUT_MS)),
  ]).then(() => ({ back, active }));
}

async function loadDiptych(forcedPair) {
  if (validImages.length < 2) return;
  const pair = forcedPair || pickPair(validImages);
  history.replaceState(null, '', pairToHash(pair));

  const sides = await Promise.all([
    preparePanel(document.querySelector('.panel.left'),  pair[0]),
    preparePanel(document.querySelector('.panel.right'), pair[1])
  ]);

  requestAnimationFrame(() => {
    sides.forEach(({ back, active }) => {
      back.classList.add('loaded');
      if (active && active !== back) {
        active.classList.remove('loaded');
        /* Pause any video in the layer that just became hidden so we
           don't accumulate background playback as the user clicks
           through. The video stays in the DOM ready to be reused on
           its next turn. */
        const oldVideo = active.querySelector('video');
        if (oldVideo) { try { oldVideo.pause(); } catch {} }
      }
    });
    /* Record the pair as "recently shown" only AFTER the swap commits.
       Doing this earlier (before await) marked preloaded-but-hidden
       interlude pairs as seen, thinning the candidate set unnecessarily.
       markPairRecent also propagates the block to any declared siblings
       (SIBLING_GROUPS). lastShown drives the staleness weighting in
       the guarantee branch: the more clicks since an image last
       appeared, the more likely the guarantee will pick a pair
       containing it next time. */
    markPairRecent(pair);
    lastShown.set(pair[0], clickCount);
    lastShown.set(pair[1], clickCount);
  });

  if (window.gaEnabled && typeof gtag !== 'undefined') {
    gtag('event', 'diptych_view', { left: pair[0], right: pair[1] });
  }
}

/* Load an image or video for analysis. Images: tries each format in
   FORMATS until one succeeds, so the browser fetches AVIF first
   (matching what <picture> will serve to AVIF-capable browsers) and
   falls back to JPG on error. Videos: routed to a separate path that
   loads the file, seeks to ~25% in, and extracts a representative
   frame to feed to analyzeImage. In both cases the src arg is the
   canonical identifier used in validImages and colorSignatures. */
function loadOne(src) {
  if (isVideo(src)) {
    /* Poster-first path: try `ff{n}-poster.jpg` next to the video.
       If present, it's analysed exactly like an image — fast, reliable,
       same colour algorithm. If absent (or fails to load), fall through
       to runtime video frame extraction, which works on most desktops
       but is unreliable on iOS Safari. If both fail, the video keeps
       its neutral fallback signature from the bulk pre-registration in
       start() and still appears in the rotation. */
    return loadVideoPosterForAnalysis(src).then(ok =>
      ok ? true : loadVideoForAnalysis(src)
    );
  }
  return new Promise(resolve => {
    const num = srcToNum(src);
    let attemptIdx = 0;

    const attempt = () => {
      if (attemptIdx >= FORMATS.length) {
        console.warn('Diptych: failed to load', src);
        resolve(false);
        return;
      }
      const format = FORMATS[attemptIdx++];
      const img = new Image();
      img.onload = () => {
        if (!validImages.includes(src)) validImages.push(src);
        const sig = analyzeImage(img);
        if (sig) {
          colorSignatures.set(src, sig);
          scheduleTopPairs();
        }
        resolve(true);
      };
      img.onerror = () => attempt();
      /* Use the variant this device will actually display, not the
         smallest one. That makes a single fetch serve two purposes:
         the analysis pass gets pixels for the colour histogram, and
         the browser cache is warmed for when <picture> later renders
         the same URL — so the splash's "loading" wait IS the display
         preload. Falls back to the base path if SIZES is unset. */
      img.src = SIZES.length ? path(num, format, pickDisplayVariant()) : path(num, format);
    };

    attempt();
  });
}

/* Neutral fallback colour signature for videos that can't be analysed
   (mostly iOS Safari, which refuses to load video data without user
   gesture). Using a flat-uniform histogram and a mid-grey palette
   means the pair scorer treats the video as neither strongly matching
   nor strongly clashing with anything — pairing quality drops, but
   the video appears in the rotation. Without this, videos that fail
   analysis are silently dropped and never shown. Includes the same
   cached scalars as analyzeImage's output (oklab, meanL, density,
   histMag) so pairScore can read them uniformly. */
function fallbackSignature() {
  const histogram = new Array(HIST_BINS).fill(1 / HIST_BINS);
  return {
    histogram,
    /* Mid-grey palette entry with both colour-space representations.
       OKLab for rgb(128,128,128) is approximately (0.6, 0, 0) — a is
       0 (no green-red tilt), b is 0 (no blue-yellow tilt), L sits in
       the middle of the lightness range. Neutral against everything. */
    palette:   [{
      hsl:    { h: 0, s: 0, l: 0.5 },
      oklab:  { L: 0.6, a: 0, b: 0 },
      weight: 1,
    }],
    avgSat:    0.3,
    meanL:     0.5,
    density:   histogramDensity(histogram),
    histMag:   histogramMagnitude(histogram),
    /* Marker read by pairScore. Any pair where one side is a fallback
       signature gets FALLBACK_TRUST_PENALTY subtracted from its score
       so it doesn't sneak into the top-N pool on accidental neutral-
       neutral matching. The flag is cleared automatically when a real
       signature replaces this one (via poster image or runtime video
       frame extraction), since the replacement object doesn't carry
       the flag. */
    isFallback: true,
  };
}

/* Poster-image path for video colour analysis. The user can generate
   one JPG per video (ff{n}-poster.jpg, same directory as the .mp4) and
   the site analyses that with analyzeImage exactly like a photo — same
   colour pipeline, same centre weighting, same OKLab conversion. This
   is the recommended way to give videos accurate colour signatures.
   See videoPosterUrl above for the ffmpeg one-liner. Returns true on
   success (signature stored), false on missing poster or analysis
   failure — loadOne then falls through to runtime extraction. */
function loadVideoPosterForAnalysis(src) {
  return new Promise(resolve => {
    const img = new Image();
    img.onload = () => {
      try {
        if (!validImages.includes(src)) validImages.push(src);
        const sig = analyzeImage(img);
        if (sig) {
          colorSignatures.set(src, sig);
          scheduleTopPairs();
          resolve(true);
        } else {
          resolve(false);
        }
      } catch {
        resolve(false);
      }
    };
    img.onerror = () => resolve(false);
    img.src = videoPosterUrl(src);
  });
}

/* Frame extraction for video colour analysis. Creates a hidden
   <video>, waits for loadeddata, seeks to a representative frame
   (~25% in, capped at 1s for short clips), and passes the video
   element to analyzeImage — canvas.drawImage accepts video elements
   directly, so the same histogram/palette/saturation signature gets
   computed without converting to an image first. The element is
   attached to the DOM (offscreen) because iOS Safari refuses to
   load video data on detached elements. If analysis fails or times
   out (iOS still being awkward, corrupt file, etc.), the video is
   registered with a neutral fallback signature so it still appears
   in the rotation — the alternative would be the video silently
   vanishing on iOS. */
function loadVideoForAnalysis(src) {
  return new Promise(resolve => {
    const v = document.createElement('video');
    v.muted       = true;
    v.playsInline = true;
    v.preload     = 'auto';
    /* Attach to DOM offscreen — iOS Safari won't load detached
       <video> elements, so analysis silently failed on iPhone
       without this. 1×1px, fully transparent, no pointer events. */
    v.setAttribute('aria-hidden', 'true');
    v.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;opacity:0;pointer-events:none;';
    document.body.appendChild(v);

    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      try { v.pause(); } catch {}
      v.removeAttribute('src');
      v.load();
      v.remove();
      /* If analysis didn't produce a signature, register the video
         with the fallback so it still appears in the rotation. The
         file existence is already verified by discovery, so this is
         safe even on failure. */
      if (!colorSignatures.has(src)) {
        if (!validImages.includes(src)) validImages.push(src);
        colorSignatures.set(src, fallbackSignature());
        scheduleTopPairs();
      }
      resolve(true);
    };

    const analyze = () => {
      try {
        if (!validImages.includes(src)) validImages.push(src);
        const sig = analyzeImage(v);
        if (sig) {
          colorSignatures.set(src, sig);
          scheduleTopPairs();
        }
        finish(true);
      } catch {
        finish(false);
      }
    };

    /* Watchdog: if neither loadeddata nor error fires within the
       timeout (iOS Safari off-DOM, corrupt file, network drop),
       fall through to finish() which registers the video with a
       fallback signature so it still shows up. */
    const watchdog = setTimeout(() => {
      console.warn('Diptych: video analysis timeout, using fallback signature', src);
      finish(false);
    }, VIDEO_ANALYSIS_TIMEOUT_MS);

    v.addEventListener('loadeddata', () => {
      if (v.duration && isFinite(v.duration) && v.duration > 0.5) {
        v.addEventListener('seeked', analyze, { once: true });
        v.currentTime = Math.min(v.duration * 0.25, 1);
      } else {
        analyze();
      }
    }, { once: true });

    v.addEventListener('error', () => {
      console.warn('Diptych: failed to load video, using fallback signature', src);
      finish(false);
    }, { once: true });

    v.src = src;
  });
}

/* ─────────────────────────────────────────────────────────────────────────
   INTERLUDE SLIDES

   Three full-screen white cards — contact, share, welcome — appear
   every CONTACT_MIN..CONTACT_MAX clicks (cadence rolled fresh after
   each interlude in advance()). Each card is SINGLE-USE per session
   (seenInterludes) — once shown, removed from the pool. After all
   three have been seen the rotation stops entirely and the gallery
   becomes a pure sequence of diptychs.

   All three require a deliberate click to dismiss. Clicking anywhere
   on the card dismisses it and reveals the next diptych (preloaded
   while the card was visible).
   ───────────────────────────────────────────────────────────────────────── */

const INTERLUDES = ['contact', 'share', 'welcome'];

(function buildContact() {
  const u = 'ciao', d = ['thisisfed', 'xyz'].join('.');
  const email = u + String.fromCharCode(64) + d;
  const phoneTel     = '+' + '44' + '7547026300';
  const phoneDisplay = '+44' + ' (0) ' + '7547 ' + '02 ' + '63 ' + '00';

  const e = document.getElementById('contact-email');
  e.setAttribute('href', 'mailto:' + email);
  e.textContent = email;

  const p = document.getElementById('contact-phone');
  p.setAttribute('href', 'tel:' + phoneTel);
  p.textContent = phoneDisplay;
})();

/* Single-use interludes. Each card (contact, share, welcome) appears
   AT MOST ONCE per session — once shown, it's removed from the pool
   and never returns. After all three have been seen the rotation
   stops entirely and the gallery becomes a pure sequence of diptychs.
   This makes the interludes feel like deliberate punctuation rather
   than recurring interruptions: the visitor learns the share gesture,
   sees the contact info, reads the iPhone-shot statement, and then
   it's just photography from there. */
const seenInterludes = new Set();

function pickInterlude() {
  const unseen = INTERLUDES.filter(s => !seenInterludes.has(s));
  if (unseen.length === 0) return null;

  /* First interlude of the session is ALWAYS the share card (if still
     unseen) — teaches the share gesture (Long press / Press S) before
     the user has any way to discover it. */
  if (lastInterlude === null && unseen.includes('share')) {
    lastInterlude = 'share';
    return 'share';
  }

  /* Random pick from unseen cards, excluding the previous so the same
     one never appears twice in a row. If only one unseen remains and
     it happens to equal lastInterlude, return it anyway — the no-repeat
     rule yields to the must-be-unseen rule. */
  const pool   = unseen.filter(s => s !== lastInterlude);
  const choice = (pool.length ? pool : unseen)[Math.floor(Math.random() * (pool.length || unseen.length))];
  lastInterlude = choice;
  return choice;
}

function showInterlude() {
  const which = pickInterlude();
  if (!which) return;  /* All interludes seen — caller falls through to loadDiptych */
  seenInterludes.add(which);
  currentInterlude = document.getElementById(which);

  currentInterlude.classList.add('visible');
  currentInterlude.setAttribute('aria-hidden', 'false');
  captureFocus(currentInterlude);
  interludePreload = loadDiptych();

  if (window.gaEnabled && typeof gtag !== 'undefined') {
    gtag('event', 'interlude_shown', { interlude: which });
  }
}

/* Remove the initial gate that hides the diptych during the welcome
   and (when needed) analytics cards. Called once, from whichever
   dismissal is the last of the gate sequence — see hideInterlude
   and dismissConsent. Idempotent: classList.remove is a no-op if
   the class isn't present. */
function liftGate() {
  document.documentElement.classList.remove('gated');
}

/* ── OVERLAY FOCUS MANAGEMENT ──
   Move keyboard focus into an overlay when it appears, and restore
   it on dismiss. Without this, screen readers and keyboard users
   have no idea the overlay opened — focus stays on whatever invisible
   element the user last interacted with, and Tab navigation reaches
   the panels behind the card. Cheap a11y improvement.

   Implementation notes:
   - We focus the `.inner` block with tabindex="-1" (set inline below)
     rather than the first interactive child, because not every overlay
     has interactive children (welcome and share are plain text).
   - previouslyFocused is captured once per show, restored once per
     hide. Stacking (interlude → privacy → back) is handled by tracking
     a stack rather than a single slot, but for this site only one
     overlay is ever active at a time, so a single slot suffices. */
let previouslyFocused = null;

function captureFocus(targetEl) {
  previouslyFocused = document.activeElement;
  const inner = targetEl.querySelector('.inner') || targetEl;
  if (!inner.hasAttribute('tabindex')) inner.setAttribute('tabindex', '-1');
  /* Defer focus to the next frame so the visibility transition has
     started — focusing a still-display:none element is a no-op in
     some browsers, and we want the screen-reader announcement to
     coincide with the visible appearance. */
  requestAnimationFrame(() => { try { inner.focus({ preventScroll: true }); } catch {} });
}

function restoreFocus() {
  const target = previouslyFocused;
  previouslyFocused = null;
  if (target && typeof target.focus === 'function' && document.contains(target)) {
    try { target.focus({ preventScroll: true }); } catch {}
  } else if (document.body) {
    try { document.body.focus({ preventScroll: true }); } catch {}
  }
}

function hideInterlude() {
  if (currentInterlude) {
    currentInterlude.classList.remove('visible');
    currentInterlude.setAttribute('aria-hidden', 'true');
    currentInterlude = null;
    restoreFocus();
  }
}

/* One delegated click handler per interlude. Anchors in the contact
   slide (mailto:/tel:) navigate normally and don't dismiss. The
   share card has no in-card trigger — sharing happens via the S key
   (desktop) or long-press (mobile), so any click on the share card
   simply dismisses it. Background clicks dismiss for most interludes.

   Two exceptions:
   - #consent never dismisses on background click — the user must
     explicitly Accept or Decline (those handlers run their own
     dismiss path). Privacy regulations and basic UX both want a
     deliberate choice rather than an accidental dismissal.
   - #welcome dismisses normally but, if consent is still needed,
     hands off to the analytics card instead of revealing the
     diptych. The diptych keeps loading behind both cards; the
     interludePreload await happens at the *final* card before the
     diptych is shown (analytics if consent is needed, welcome
     otherwise). */
document.querySelectorAll('.interlude').forEach(el => {
  el.addEventListener('click', async (e) => {
    if (el.id === 'consent') return;

    if (e.target.closest('a')) {
      return; /* let mailto: / tel: links fire without dismissing */
    }

    /* Cap the interludePreload await at 300ms. The await exists so
       the interlude doesn't dismiss faster than the next pair can
       decode — but if something goes wrong (slow video, network
       hiccup, unexpected delay), the card shouldn't sit at full
       opacity for seconds waiting. 300ms is well above any cached-
       image decode time, so the normal case never trips the cap;
       the safety net only fires when something is genuinely slow,
       in which case the diptych may appear mid-decode behind the
       card fade-out — far better than the card appearing stuck. */
    if (interludePreload) {
      await Promise.race([
        interludePreload,
        new Promise(r => setTimeout(r, 300))
      ]);
    }
    hideInterlude();
  });
});

/* ─────────────────────────────────────────────────────────────────────────
   SPLASH + IMAGE PRELOAD

   Three static lines (title, gallery count, loading percentage), with
   the percentage animating 0 → 100 to reflect real image-load progress.
   A rAF loop drives the percentage display: actual progress increases
   in chunks each time an image completes, but the rendered number rises
   at a steady rate (PROGRESS_RATE_PER_SEC) so a warm-cache visit still
   shows a smooth climb rather than a flicker from 0% to 100%. The
   splash dismisses the moment the displayed number reaches 100% — so
   visual completion and gallery-ready always coincide.

   Videos are NOT preloaded; they get a neutral colour signature so the
   pair scorer keeps them in rotation, and the actual file fetch happens
   lazily in preparePanel when their pair is selected. There is no
   click or keyboard skip; the safety cap SPLASH_MAX_WAIT_MS bounds the
   worst case so the gallery never gets stranded behind a stuck splash.
   ───────────────────────────────────────────────────────────────────────── */

(async function start() {
  const splashEl   = document.getElementById('splash');
  const subtitleEl = splashEl.querySelector('.subtitle');
  const loadingEl  = splashEl.querySelector('.loading');

  let target            = 500;
  let imageSrcs         = null;
  let splashFinished    = false;
  let splashSafetyTimer = null;

  /* Idempotent dismissal. Called from two places: the progress
     animation (when the displayed % reaches 100), and the safety
     timer (when MAX_WAIT_MS elapses without loading completing).
     The splashFinished flag makes subsequent calls no-ops. There
     is intentionally no click or keyboard path here — once the
     splash is on screen, only loading completion or the safety
     cap will dismiss it. */
  const finishSplash = () => {
    if (splashFinished) return;
    splashFinished = true;
    clearTimeout(splashSafetyTimer);
    splashEl.classList.add('hidden');
    setTimeout(() => splashEl.remove(), SPLASH_FADE_MS + 50);
  };

  splashSafetyTimer = setTimeout(finishSplash, SPLASH_MAX_WAIT_MS);

  const [imageIndices, videoIndices] = await Promise.all([
    discoverImages(),
    discoverVideos()
  ]);
  const totalCount = imageIndices.length + videoIndices.length;
  if (totalCount < 2) {
    subtitleEl.textContent = 'No media could be loaded.';
    loadingEl.textContent  = '';
    /* Cancel the safety timer — without this it would fire ~30s later,
       fade the splash, and leave the user staring at a permanently
       blank page (the diptych is still hidden by html.gated and no
       loadDiptych ever runs to populate it). Better to keep the
       message on screen indefinitely than to silently disappear it. */
    clearTimeout(splashSafetyTimer);
    return;
  }
  images = imageIndices.map(numToSrc).concat(videoIndices.map(videoNumToSrc));

  /* Register every video with a neutral signature up front — without
     loading the file. That keeps videos eligible for pickPair (they
     end up in validImages and colorSignatures from the start) while
     deferring the actual byte fetch to preparePanel's video path,
     which runs only when a pair containing the video is selected.
     The neutral signature means pair quality is slightly degraded
     for video-containing pairs, but the gallery feels lazy-on-demand
     rather than waiting for tens of megabytes of MP4s up front. */
  for (const videoSrc of videoIndices.map(videoNumToSrc)) {
    if (!validImages.includes(videoSrc)) validImages.push(videoSrc);
    if (!colorSignatures.has(videoSrc)) colorSignatures.set(videoSrc, fallbackSignature());
  }

  /* Update the static subtitle to the real pair count if discovery
     reveals a different total than the HTML's placeholder "500". */
  const unorderedPairs = totalCount * (totalCount - 1) / 2;
  const realTotal      = Math.min(TOP_PAIRS_POOL, unorderedPairs) * 2;
  if (realTotal !== target) {
    target = realTotal;
    subtitleEl.textContent = `${target} Random Diptychs`;
  }

  /* Hash-driven entry loads JUST the deep-linked pair (which may
     include a video) and dismisses the splash immediately — the
     linked diptych is the whole point of that URL and shouldn't
     wait on the full image catalogue. The 0→100 progress animation
     only runs in the no-hash branch below. */
  const hashMatch = location.hash.match(/^#(v?\d+),(v?\d+)$/);

  if (hashMatch && hashMatch[1] !== hashMatch[2]) {
    const firstPair = [idToSrc(hashMatch[1]), idToSrc(hashMatch[2])];
    const results = await Promise.all(firstPair.map(loadOne));
    if (!results.every(Boolean)) {
      /* Hash points to a missing/corrupt item — walk until 2 valid. */
      for (const src of images) {
        if (validImages.length >= 2) break;
        if (!validImages.includes(src)) await loadOne(src);
      }
    }
    if (validImages.length < 2) {
      subtitleEl.textContent = 'No media could be loaded.';
      loadingEl.textContent  = '';
      /* Keep the splash on screen with its message; without this the
         safety timer would fade it to a blank page. */
      clearTimeout(splashSafetyTimer);
      return;
    }
    /* Background-fill the rest of the IMAGES (videos stay lazy). */
    Promise.all(
      imageIndices.map(numToSrc)
        .filter(src => !validImages.includes(src))
        .map(loadOne)
    );
    const startPair = firstPair.every(s => validImages.includes(s)) ? firstPair : null;
    finishSplash();
    /* First visit (consent still required) → show consent card on
       top of the loading diptych; consent's Accept/Decline handlers
       await interludePreload before dismissing. Return visit → lift
       the gate immediately so the diptych appears behind the splash
       fade-out. Either way, no welcome card sits between splash and
       gallery; the welcome statement now appears later as one of the
       rotation interludes. */
    if (consentEl && consentEl.isConnected) {
      interludePreload = loadDiptych(startPair);
      showAnalytics();
    } else {
      liftGate();
      loadDiptych(startPair);
    }
    return;
  }

  /* No hash — preload every image at the device's display variant
     size before dismissing the splash. The "Loading… X%" counter
     reflects real progress against the full catalogue, so the
     splash has a substantive moment and by the time it fades out
     every diptych the user will ever click through is already
     cached. Trade-off: splash takes proportionally longer on slow
     connections (5-30s on 4G with 150 images) — but every
     transition after is instant, which is what a photography
     gallery should feel like. Videos remain lazy (loaded on demand
     in preparePanel) because their files are 10-100x larger than
     images and waiting for them would push the splash into
     unreasonable territory. */
  imageSrcs = imageIndices.map(numToSrc);

  /* Count ATTEMPTS (success or failure), not just successful loads.
     loadOne resolves either way — success pushes to validImages,
     failure quietly returns false — and counting both means a 404
     on one image doesn't permanently strand the percentage at 99
     waiting for a load that will never happen. */
  let imageLoadsAttempted = 0;
  imageSrcs.forEach(src => {
    loadOne(src).finally(() => { imageLoadsAttempted++; });
  });

  /* Smooth progress animation, gated on the full image catalogue.
     displayPct chases an "effective" load fraction that snaps to
     100 once real progress is within a hair of complete — that
     absorbs the last 1-2 stragglers without holding the splash on
     a slow image. The rate cap keeps the climb legible even on a
     warm cache where every image is already in memory.

     The dt cap (100ms) is a guard against the browser deferring rAF
     during a tab switch — without it, the first frame after returning
     focus would advance displayPct by the full elapsed delta and look
     like a jump. */
  let displayPct = 0;
  let lastTickMs = performance.now();

  await new Promise(resolve => {
    const tick = () => {
      if (splashFinished) { resolve(); return; }

      const now = performance.now();
      const dt  = Math.min(0.1, (now - lastTickMs) / 1000);
      lastTickMs = now;

      const total           = imageSrcs.length;
      const actualPct       = total > 0 ? (imageLoadsAttempted / total) * 100 : 0;
      const effectiveActual = actualPct >= 99 ? 100 : actualPct;

      if (effectiveActual > displayPct) {
        displayPct = Math.min(effectiveActual, displayPct + PROGRESS_RATE_PER_SEC * dt);
      }

      /* Only show "X%" once the counter has actually started moving.
         Frozen "Loading… 0%" during the initial discovery phase reads
         as a stuck splash on warm-cache visits — the hardcoded HTML
         value sits there for ~100-500ms while discoverImages probes,
         then jumps from 0 to 1 to 2 …. Showing plain "Loading…" until
         there's real progress to display avoids that. */
      const shown = Math.floor(displayPct);
      loadingEl.textContent = shown > 0 ? `Loading… ${shown}%` : 'Loading…';

      if (displayPct >= 100) {
        loadingEl.textContent = 'Loading… 100%';
        resolve();
      } else {
        requestAnimationFrame(tick);
      }
    };
    requestAnimationFrame(tick);
  });

  if (validImages.length < 2) {
    subtitleEl.textContent = 'No media could be loaded.';
    loadingEl.textContent  = '';
    /* Keep the splash visible with its message rather than letting
       the safety timer fade it to a permanently-blank page. See the
       earlier no-media branch for the same reasoning. */
    clearTimeout(splashSafetyTimer);
    return;
  }

  /* Loading complete (or safety-capped). Trigger the fade now so the
     gallery transition begins immediately, before computeTopPairs
     and pickPair do their work — those run in milliseconds and
     finish well within the 1.8s fade. */
  finishSplash();

  /* scheduleTopPairs is rAF-debounced, so the latest analyses may
     not yet be reflected. Force a sync compute before picking — and
     cancel the pending rAF so it doesn't redundantly recompute the
     same pairs one frame later. */
  if (topPairsPending) {
    cancelAnimationFrame(topPairsPending);
    topPairsPending = 0;
  }
  computeTopPairs();

  /* Same selection logic as every other click — bias toward
     top-scoring pairs, with the per-image guarantee mixing in.
     recent[] is empty on first paint, so no filtering to dodge.

     IMPORTANT: pass allowVideos:false for the FIRST pair. Videos are
     pre-registered to validImages with fallback colour signatures so
     they can appear in the rotation from click 2 onwards, but they're
     not preloaded by the splash — picking a video for the first pair
     means preparePanel has to fetch the MP4 fresh, which can take
     2-4s even on decent connections. Consent's Accept/Decline await
     interludePreload (so the diptych isn't revealed half-decoded),
     and a video first pair would leave the consent card stuck waiting
     for the video load. All images are fully cached by the time the
     splash dismisses, so img.decode() on the first pair is sub-ms. */
  const firstPair = pickPair(validImages, { allowVideos: false });
  /* First visit (consent still required) → show consent card on top
     of the loading diptych; consent's Accept/Decline handlers await
     interludePreload before dismissing. Return visit → lift the gate
     immediately so the diptych appears behind the splash fade-out.
     Either way, no welcome card sits between splash and gallery; the
     welcome statement now appears later as one of the rotation
     interludes (CONTACT_MIN..CONTACT_MAX clicks in). */
  if (consentEl && consentEl.isConnected) {
    interludePreload = loadDiptych(firstPair);
    showAnalytics();
  } else {
    liftGate();
    loadDiptych(firstPair);
  }
})();

/* ─────────────────────────────────────────────────────────────────────────
   CLICK / TAP HANDLING
   ───────────────────────────────────────────────────────────────────────── */

/* In-flight guard for loadDiptych. Without this, a click landing while
   a previous load is still awaiting img.decode would overwrite the
   in-flight image's src and call history.replaceState twice with
   potentially mismatched pairs — observable as flicker or a hash that
   disagrees with what's painted. */
let loadingDiptych = false;

/* Roll the first interlude target up front. clicksSinceInterlude
   counts user clicks since the last interlude (or since session
   start); when it crosses nextInterludeAt, an interlude fires and
   both are reset. */
let clicksSinceInterlude = 0;
let nextInterludeAt      = rollNextInterlude();

/* Shared advance step — used by both the diptych click handler and
   the keyboard nav listener. Same in-flight guard, same interlude
   gating logic; defining it once keeps the two input paths in sync.
   Once all three interludes have been shown for this session,
   seenInterludes.size === INTERLUDES.length and the gate skips the
   interlude branch entirely — gallery becomes pure diptychs. */
async function advance() {
  if (loadingDiptych) return;
  clickCount++;
  clicksSinceInterlude++;
  const interludesRemaining = seenInterludes.size < INTERLUDES.length;
  if (interludesRemaining && clicksSinceInterlude >= nextInterludeAt) {
    clicksSinceInterlude = 0;
    nextInterludeAt      = rollNextInterlude();
    showInterlude();
  } else {
    loadingDiptych = true;
    try { await loadDiptych(); } finally { loadingDiptych = false; }
  }
}

/* Touch-landscape devices get one fullscreen attempt per session.
   Without the latch, deliberately exiting fullscreen via the
   browser's own gesture would be immediately reversed by the next
   tap on the diptych — the site fighting the user. The flag is
   session-scoped (resets on refresh), which feels right: one
   refresh re-arms the request. */
let triedFullscreen = false;
document.getElementById('diptych').addEventListener('click', () => {
  if (!triedFullscreen
      && matchMedia('(hover: none) and (orientation: landscape)').matches
      && !document.fullscreenElement
      && document.documentElement.requestFullscreen) {
    triedFullscreen = true;
    document.documentElement.requestFullscreen().catch(() => {});
  }
  advance();
});

/* ─────────────────────────────────────────────────────────────────────────
   KEYBOARD NAVIGATION

   Space / Enter / Right-arrow advance the diptych or dismiss whatever
   overlay is currently on top; Escape dismisses overlays only. The
   handler is delegated to document so it works regardless of focus,
   but it bails when the user is typing in an input or holding a
   modifier (so Ctrl+R, Cmd+L and friends still work as expected).

   Priority order matches z-index: splash → privacy → interlude →
   diptych. Whichever is currently visible consumes the key.
   ───────────────────────────────────────────────────────────────────────── */

document.addEventListener('keydown', async (e) => {
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  const isAdvance = (e.key === ' ' || e.key === 'Enter' || e.key === 'ArrowRight');
  const isEscape  = (e.key === 'Escape');
  if (!isAdvance && !isEscape) return;

  /* Splash still on screen — absorb the keypress so it doesn't leak
     through to the diptych underneath, but DO NOT dismiss. The splash
     has no skip path anymore; it dismisses on loading completion or
     the SPLASH_MAX_WAIT_MS safety cap. Re-querying the element rather
     than capturing once at script parse: finishSplash removes it from
     the DOM after the fade, so a stale reference would never read as
     "not hidden" again. */
  const splashEl = document.getElementById('splash');
  if (splashEl && !splashEl.classList.contains('hidden')) {
    e.preventDefault();
    return;
  }

  /* Privacy modal — Escape only; the Accept/Decline buttons handle
     their own keyboard activation since they're focusable. */
  if (privacyEl && privacyEl.classList.contains('visible')) {
    if (isEscape) { e.preventDefault(); hidePrivacy(); }
    return;
  }

  /* Consent card — like the privacy slide, requires an explicit
     Accept or Decline choice. Absorb the keypress so it doesn't
     leak through to anything underneath, but don't dismiss. */
  if (currentInterlude && currentInterlude.id === 'consent') {
    e.preventDefault();
    return;
  }

  /* Interlude visible — any handled key dismisses, mirroring the
     click-anywhere-to-dismiss behavior. Await the preloaded next
     pair so the photo behind is ready when the card fades out.

     Welcome → analytics chain mirrors the click handler: if the
     welcome is on screen and consent is still needed, dismiss the
     welcome and bring up the consent card instead of awaiting the
     preload and revealing the diptych. */
  if (currentInterlude) {
    e.preventDefault();
    if (currentInterlude.id === 'welcome' && consentEl && consentEl.isConnected) {
      hideInterlude();
      showAnalytics();
      return;
    }
    if (interludePreload) await interludePreload;
    hideInterlude();
    return;
  }

  /* Diptych on screen — advance on advance keys; Escape is a no-op
     (there's nothing to dismiss). */
  if (isAdvance) {
    e.preventDefault();
    advance();
  }
});

/* ─────────────────────────────────────────────────────────────────────────
   ANALYTICS CONSENT GATE

   Originally a bottom banner; now a centered interlude-style card
   that's shown after the welcome dismisses (only when consent
   isn't yet stored). Flow:

     splash → welcome → [consent? if needed] → diptych

   The welcome's click handler (above) chains into showAnalytics
   when consent is still needed. Once the user picks Accept or
   Decline (either on the consent card directly, or via the
   privacy slide reached via "Why?"), the choice is saved and the
   card dismisses — awaiting the first-diptych preload first so
   the reveal lands on a ready image rather than a blank panel.
   ───────────────────────────────────────────────────────────────────────── */

const GA_ID       = 'G-J2Q38DS42K';
const CONSENT_KEY = 'ff-analytics-consent';
const consentEl   = document.getElementById('consent');
const privacyEl   = document.getElementById('privacy');

function loadAnalytics() {
  if (window.gaEnabled) return;
  window.gaEnabled = true;
  const s = document.createElement('script');
  s.async = true;
  s.src   = `https://www.googletagmanager.com/gtag/js?id=${GA_ID}`;
  document.head.appendChild(s);
  window.dataLayer = window.dataLayer || [];
  window.gtag = function () { dataLayer.push(arguments); };
  gtag('js', new Date());
  gtag('config', GA_ID, { anonymize_ip: true });
}

/* Show the consent card as an interlude. Called by the welcome's
   click handler when consent is still needed. Sets currentInterlude
   so hideInterlude could dismiss it (though we don't use the
   generic dismiss path for consent — see the click handler's early
   return on el.id === 'consent'). The single-use removal of the
   element from the DOM happens in dismissConsent after the fade. */
function showAnalytics() {
  if (!consentEl) return;
  currentInterlude = consentEl;
  consentEl.classList.add('visible');
  consentEl.setAttribute('aria-hidden', 'false');
  captureFocus(consentEl);
}

/* Synchronous dismissal — fade out + DOM removal after the fade.
   Callers await interludePreload BEFORE calling this so the diptych
   is decoded and ready when the card disappears. The timeout (1s)
   comfortably covers the .interlude fade-out (0.85s) plus a small
   buffer; once removed, the gate's element references become stale
   but no further code reads them. */
function dismissConsent() {
  if (!consentEl) return;
  /* Lift the initial gate BEFORE starting the consent fade-out. The
     diptych becomes visibility:visible immediately, but is still
     covered by the consent card at opacity 1; as the card fades, the
     diptych is revealed smoothly underneath. Lifting AFTER the fade
     would mean the diptych pops in at the moment the card finishes
     disappearing — visible discontinuity. */
  liftGate();
  consentEl.classList.remove('visible');
  consentEl.setAttribute('aria-hidden', 'true');
  if (currentInterlude === consentEl) {
    currentInterlude = null;
    restoreFocus();
  }
  setTimeout(() => consentEl.remove(), 1000);
}

function showPrivacy() {
  privacyEl.classList.add('visible');
  privacyEl.setAttribute('aria-hidden', 'false');
  captureFocus(privacyEl);
}
function hidePrivacy() {
  privacyEl.classList.remove('visible');
  privacyEl.setAttribute('aria-hidden', 'true');
  restoreFocus();
}

const stored = (() => { try { return localStorage.getItem(CONSENT_KEY); } catch { return null; } })();
if (stored === 'granted') {
  loadAnalytics();
  consentEl.remove();
  privacyEl.remove();
} else if (stored === 'denied') {
  consentEl.remove();
  privacyEl.remove();
} else {
  /* No stored decision — consent card stays in the DOM, hidden
     (opacity:0 via .interlude default), and will be revealed by
     showAnalytics() after the welcome dismisses. Handlers below
     wait for the first-pair preload before dismissing so the
     diptych reveal lands cleanly. */
  document.getElementById('consent-accept').addEventListener('click', async (e) => {
    e.stopPropagation();
    try { localStorage.setItem(CONSENT_KEY, 'granted'); } catch {}
    loadAnalytics();
    if (interludePreload) await interludePreload;
    dismissConsent();
  });
  document.getElementById('consent-decline').addEventListener('click', async (e) => {
    e.stopPropagation();
    try { localStorage.setItem(CONSENT_KEY, 'denied'); } catch {}
    if (interludePreload) await interludePreload;
    dismissConsent();
  });
  document.getElementById('consent-why').addEventListener('click', (e) => {
    e.stopPropagation();
    showPrivacy();
  });

  /* Privacy slide — clicking outside the action anchors dismisses
     back to the consent card. The action anchors themselves complete
     the consent flow (both the privacy slide and the consent card
     go away). Action anchors live inside .privacy-actions so that
     selector is the differentiator — a click on the long privacy
     paragraph above shouldn't be mistaken for a button press. */
  privacyEl.addEventListener('click', (e) => {
    if (e.target.closest('.privacy-actions a')) return;
    hidePrivacy();
  });
  document.getElementById('privacy-accept').addEventListener('click', async (e) => {
    e.stopPropagation();
    try { localStorage.setItem(CONSENT_KEY, 'granted'); } catch {}
    loadAnalytics();
    if (interludePreload) await interludePreload;
    hidePrivacy();
    dismissConsent();
  });
  document.getElementById('privacy-decline').addEventListener('click', async (e) => {
    e.stopPropagation();
    try { localStorage.setItem(CONSENT_KEY, 'denied'); } catch {}
    if (interludePreload) await interludePreload;
    hidePrivacy();
    dismissConsent();
  });
}

/* ─────────────────────────────────────────────────────────────────────────
   SHAREABLE PAIRS

   Every pair has a URL — history.replaceState fires inside loadDiptych
   on every advance, so location.href always reflects what's on screen.
   This block makes that shareable without adding visible chrome:

     Desktop:  S key             →  copy URL  →  brief "Link copied" toast
     Mobile:   long-press diptych →  copy URL  →  brief "Link copied" toast

   The toast is a screen-wide semi-transparent dim with large centred
   text; auto-dismisses in ~1s. The existing share interlude (every
   CONTACT_MIN..CONTACT_MAX clicks) keeps using its own Web-Share-API path —
   untouched here.
   ───────────────────────────────────────────────────────────────────────── */

const shareToastEl     = document.getElementById('share-toast');
const shareToastTextEl = document.getElementById('share-toast-text');
const LONG_PRESS_MS    = 550;
const LONG_PRESS_MOVE_TOLERANCE = 12; // px — beyond this, treat as scroll/drag, not press

let shareToastTimer = null;
let longPressFired  = false;
let touchActive     = false;
let touchStartTime  = 0;
let touchStartX     = 0;
let touchStartY     = 0;
let touchMoved      = false;

function showShareToast(text) {
  shareToastTextEl.textContent = text;
  shareToastEl.classList.add('visible');
  shareToastEl.setAttribute('aria-hidden', 'false');
  clearTimeout(shareToastTimer);
  /* Hold 500ms then trigger the CSS fade-out (120ms). A "blip" —
     fast enough to feel like a flash, slow enough that "Link copied"
     reads on a single glance. Total visible cycle ~740ms.
     Previously 1500ms (standard transient-toast duration), which felt
     too settled-in for this site's lightweight pop-and-go interaction.
     Going under ~400ms hits the floor of comfortable recognition for
     a 10-character message; over ~800ms starts feeling like a dwell. */
  shareToastTimer = setTimeout(() => {
    shareToastEl.classList.remove('visible');
    shareToastEl.setAttribute('aria-hidden', 'true');
  }, 500);
}

function shareCurrentPair() {
  /* loadDiptych updates the URL hash via history.replaceState, so
     location.href is the live "what's currently shown" URL. */
  const url   = location.href;
  const title = 'Federico Ferrari — Random Diptychs';

  /* MOBILE PATH — native share sheet.
     iOS Safari has become unreliable about clipboard access; both
     navigator.clipboard.writeText and document.execCommand('copy')
     silently fail on plenty of real-world devices (in-app webviews,
     Lockdown Mode, recent permission tightening). navigator.share
     uses the same user-gesture machinery but works reliably across
     iOS Safari, Android Chrome, and most webviews. The native sheet
     IS the feedback — the user picks Copy / Messages / AirDrop /
     email and the OS handles the rest. No toast needed; the sheet
     opening is the success signal. */
  const isMobile = matchMedia('(hover: none) and (pointer: coarse)').matches;
  if (isMobile && navigator.share) {
    navigator.share({ url, title }).catch(() => { /* user cancelled — silent */ });
    if (window.gaEnabled && typeof gtag !== 'undefined') {
      gtag('event', 'pair_shared', { url, source: 'shortcut-mobile' });
    }
    return;
  }

  /* DESKTOP PATH — clipboard + toast.
     Modern Clipboard API works reliably on desktop browsers and
     reports success/failure honestly. The legacy execCommand path
     (document.execCommand('copy') via off-screen textarea) is
     deprecated, notoriously dishonest about success on some engines,
     and pollutes the DOM each call — so it's only the fallback for
     ancient browsers without navigator.clipboard. We only show
     "Link copied" on a real success signal. */
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(
      () => showShareToast('Link copied'),
      () => {
        /* Modern API rejected (permission, focus, lockdown). Try
           the legacy path as a last resort before giving up. */
        if (legacyCopy(url)) showShareToast('Link copied');
        else                 showShareToast('Couldn’t copy');
      }
    );
  } else if (legacyCopy(url)) {
    showShareToast('Link copied');
  } else {
    showShareToast('Couldn’t copy');
  }

  if (window.gaEnabled && typeof gtag !== 'undefined') {
    gtag('event', 'pair_shared', { url, source: 'shortcut' });
  }
}

function legacyCopy(text) {
  /* Off-screen textarea — must be in the DOM and have non-zero
     size for iOS Safari to honour the selection. font-size:16px
     prevents iOS from zooming the viewport if the textarea ever
     briefly takes focus. opacity:0 hides it visually without
     making it inert (pointer-events:none would, so we don't set
     it — iOS sometimes refuses to operate on truly-inert elements). */
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText =
    'position:fixed;top:0;left:0;width:2em;height:2em;' +
    'padding:0;border:none;outline:none;box-shadow:none;' +
    'background:transparent;font-size:16px;opacity:0;';
  document.body.appendChild(ta);

  let ok = false;
  try {
    /* iOS-family detection. Modern iPads (iPadOS 13+) report
       themselves as Mac in the user-agent string by default, so
       a UA-only check misses them. The combination of MacIntel
       platform and maxTouchPoints > 1 is the standard Apple-
       recommended way to spot iPadOS. */
    const isAppleMobile =
      /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    if (isAppleMobile) {
      /* iOS-specific selection ritual. Plain .select() is silently
         ignored — Safari only honours a Range that's been added
         to the live Selection, followed by an explicit
         setSelectionRange on the textarea itself. The textarea
         also has to be editable (not readOnly, contentEditable
         true) at the moment of selection. */
      ta.contentEditable = 'true';
      ta.readOnly        = false;
      const range = document.createRange();
      range.selectNodeContents(ta);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      ta.setSelectionRange(0, text.length);
    } else {
      ta.focus();
      ta.select();
    }
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

/* ── LONG-PRESS DETECTION (mobile) ──
   iOS Safari refuses to honour navigator.clipboard.writeText
   from inside a setTimeout callback — the user-gesture
   activation has lapsed by the time the timer fires. The fix
   is to do the duration check on touchend instead, where we're
   still inside the synchronous user-gesture handler.

   Flow:
     touchstart → record time + position, mark touchActive
     touchmove  → if displacement exceeds tolerance, mark
                  touchMoved (it's a drag or scroll, not a press)
     touchend   → if duration ≥ LONG_PRESS_MS and not moved,
                  fire share synchronously. The capture-phase
                  click handler below swallows the synthetic
                  click that touchend produces, so the diptych
                  doesn't advance after a share. */
const diptychEl = document.getElementById('diptych');

diptychEl.addEventListener('touchstart', (e) => {
  /* Multi-touch (pinch, two-finger) is never a long-press —
     bail and let the browser handle it. */
  if (!e.touches || e.touches.length !== 1) {
    touchActive = false;
    return;
  }
  touchActive    = true;
  touchStartTime = performance.now();
  touchStartX    = e.touches[0].clientX;
  touchStartY    = e.touches[0].clientY;
  touchMoved     = false;
}, { passive: true });

diptychEl.addEventListener('touchmove', (e) => {
  if (!touchActive || touchMoved) return;
  if (!e.touches || e.touches.length === 0) return;
  const dx = e.touches[0].clientX - touchStartX;
  const dy = e.touches[0].clientY - touchStartY;
  /* Squared-distance compare — no Math.sqrt per touchmove.
     12px tolerance matches iOS's own tap-vs-drag threshold
     closely enough to feel native. */
  if (dx * dx + dy * dy > LONG_PRESS_MOVE_TOLERANCE * LONG_PRESS_MOVE_TOLERANCE) {
    touchMoved = true;
  }
}, { passive: true });

diptychEl.addEventListener('touchend', () => {
  if (!touchActive) return;
  touchActive = false;
  if (touchMoved) return;
  const duration = performance.now() - touchStartTime;
  if (duration >= LONG_PRESS_MS) {
    /* Mark BEFORE calling shareCurrentPair: the synthetic click
       that follows touchend needs to see the flag set so the
       capture-phase handler below can swallow it. */
    longPressFired = true;
    shareCurrentPair();
  }
}, { passive: true });

diptychEl.addEventListener('touchcancel', () => {
  touchActive = false;
}, { passive: true });

/* Capture-phase click handler — fires BEFORE the existing
   bubble-phase advance handler on the same element. When a long
   press just fired, swallow the synthetic click that touchend
   produces via stopImmediatePropagation, so the diptych doesn't
   advance. The longPressFired flag resets after each use. */
diptychEl.addEventListener('click', (e) => {
  if (longPressFired) {
    longPressFired = false;
    e.stopImmediatePropagation();
    e.preventDefault();
  }
}, true);

/* ── KEYBOARD 'S' SHORTCUT (desktop) ──
   Separate keydown listener from the main one — keeps share
   logic local to this block and avoids editing the existing
   keys/overlays flow. Mirrors the same overlay-gating: bail
   if splash / privacy / consent / any interlude is up so 'S'
   isn't reachable until the diptych is actually on screen. */
document.addEventListener('keydown', (e) => {
  if (e.key !== 's' && e.key !== 'S') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  const splashEl = document.getElementById('splash');
  if (splashEl && !splashEl.classList.contains('hidden')) return;
  if (privacyEl && privacyEl.classList.contains('visible')) return;
  if (currentInterlude) return;
  e.preventDefault();
  shareCurrentPair();
});

/* ── SHARE INTERLUDE LABEL ──
   The share interlude card (shown every CONTACT_MIN..CONTACT_MAX clicks)
   uses different wording per input mode. Touch-primary devices
   get "long press to share"; pointer-primary get "press S to share"
   (teaches the keyboard shortcut).

   Detection: hover:none AND pointer:coarse — the standard CSS
   media-query pair for "this device's primary input is touch".
   Avoids 'ontouchstart' in window false-positives on hybrid
   laptops that have both a touchscreen and a precise pointer.
   Re-runs on input-mode change so a 2-in-1 flipping between
   laptop and tablet posture gets the right wording without a
   reload. */
(function setShareTriggerLabel() {
  const trigger = document.getElementById('share-trigger');
  if (!trigger) return;
  const mq = matchMedia('(hover: none) and (pointer: coarse)');
  const apply = () => {
    trigger.textContent = mq.matches ? 'Long press to share' : 'Press S to share';
  };
  apply();
  /* addEventListener is the modern API; older Safari only supports
     the deprecated addListener. Try the modern one first. */
  if (mq.addEventListener) mq.addEventListener('change', apply);
  else if (mq.addListener) mq.addListener(apply);
})();
