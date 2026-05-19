# Random Diptychs

A personal photography experiment by **Federico Ferrari**. Every click pulls two images side by side from an ongoing pool of stills and short videos — all shot and filmed entirely on iPhone, for the fun of it.

Live at **[random.thisisfed.xyz](https://random.thisisfed.xyz/)**.

There's no menu, no grid, no back button. Tap or press anywhere; once a pair is gone, it's gone.

---

## How it works

The site is three files served statically: `index.html`, `styles.css`, `app.js`. No build step, no framework, no dependencies. Photography is the only thing on screen; everything else is engineered to disappear.

A few decisions worth knowing about.

### Pair scoring

Pairings aren't random. On first load every image is downsampled to a 128×128 canvas, then summarised by three signals:

- a **7-bin lightness histogram** (tonal shape)
- a **filtered 4-colour palette**, weighted toward the centre of the frame (where the subject usually lives), with desaturated and near-extreme tones excluded
- the image's **average saturation**

Palette entries carry both HSL and OKLab coordinates. HSL drives the merge heuristic during palette construction and the hue-family detection in repetition catching (two reds are "the same family" even at different lightnesses). OKLab — a perceptually uniform colour space — drives the actual similarity scoring, since Euclidean distance in OKLab tracks how the eye reads colour difference. HSL gets this wrong in both directions: a soft pink and a deep red read as "close" because hue agrees, and two greys at different lightnesses read as "far" despite both being grey.

`pairScore(a, b)` rewards pairs with:

- **palette contrast** (different dominant colours) — the dominant signal, raised to a power so moderate similarity loses ground faster than strong contrast
- **density contrast** (full-vs-empty composition), gated by palette contrast — so editorial "full vs negative space" pairings surface without false positives from two-blue-but-busy/calm pairs
- **lightness contrast** (one bright, one dim) — an independent reward for the up-and-down editorial dimension, ungated
- **tonal cohesion** as a faint tiebreaker

…minus penalties for:

- **predominant-colour repetition** by hue family (catches "two blues" even at very different lightness)
- **blank-canvas repetition** — both images dominated by the same low-saturation tone (pale sky + grey wall)
- **joint desaturation** — pair where neither side carries colour
- **joint fullness** — both images busy; the eye wants somewhere to rest
- **joint emptiness** — both images near-empty; minimal-on-minimal feels samey

Of the N×(N−1)/2 possible pairs, the top 250 are kept. Each click draws from that pool with a quality bias (`Math.random() ** 2`), so the very best pairings dominate without ever showing the same one twice in close succession.

A per-image **guarantee branch** runs ~45% of clicks: instead of drawing from the global top-N, it picks a stale image (one not seen for a while) and pairs it with its highest-scoring partner. This keeps the rotation honest — an image with one popular partner doesn't get starved when the partner is in the recent-block window.

**Favorites** override the algorithm's instincts for a small list of hand-picked photos. The scorer sometimes "correctly" deprioritises images that are hard to pair — palette-narrow, very desaturated, very dark — even when those are images the photographer genuinely loves. Adding an image number to `FAVORITE_IMAGES` boosts its staleness weight inside the guarantee branch by a configurable factor (default 4×), so it surfaces meaningfully more often without changing how partners are selected. It's a long-term rotation tilt, not a "show this next" override.

### Theme

`html.night` is set **before first paint** by inline script in the `<head>`. Sunrise and sunset are computed from a longitude derived from the timezone offset (no geolocation prompt, no third-party call) and a latitude estimated from the IANA zone region. The theme re-evaluates every minute so the palette flips live at the threshold; gated behind `document.visibilityState === 'visible'` so a backgrounded tab is free.

The threshold is **civil twilight (−6° sun altitude)** rather than geometric sunrise/sunset (−0.83°) — at −0.83° the algorithm would flip to night while the sky is still bright. Civil twilight matches the visual feel of "the sun is out" by extending the day window by ~30 minutes on each side.

### Loading

Image discovery is parallel batched `HEAD` probes (no manifest needed). The splash shows a smooth `Loading… X%` counter that animates 0→100 over real load progress — capped at 60%/s so even a warm cache shows a legible climb instead of a 0→100 flicker. Every image's display variant is fetched during the splash, so once the gate lifts, every diptych in the session is already cached and decode is sub-ms.

Videos are **not** preloaded. They're registered with a neutral colour signature so they stay eligible for pair scoring, but their MP4 bytes only fetch when a pair containing them is selected. The first pair is forced to images-only via `pickPair(arr, { allowVideos: false })`, since a 2–4 s video fetch behind the consent card would leave the gate stuck.

For accurate video colour pairing, drop a **poster JPG** next to each video — `ff{n}-poster.jpg` alongside `ff{n}.mp4`. The site analyses the poster with the same colour pipeline as the photos (centre-weighted palette, OKLab, density, etc.), so videos pair on real colour rather than a neutral signature. Generate them with ffmpeg:

```bash
for f in videos/ff*.mp4; do
  N="${f%.mp4}"
  ffmpeg -ss 1 -i "$f" -frames:v 1 -q:v 3 "${N}-poster.jpg"
done
```

Pick the timestamp (`-ss 1`) that best represents the video. The poster is loaded by the splash like any image and is opt-in per video: videos without posters fall back to runtime frame extraction, then to the neutral signature on failure. Adding posters is the recommended way to get videos pairing correctly, especially because runtime extraction is unreliable on iOS Safari (offscreen videos often won't load, and `canvas.drawImage` can return black frames before play has been called).

### Shareable pairs

Every diptych has a URL. `history.replaceState` fires inside `loadDiptych` on every advance, so `location.href` is always the canonical address of what's on screen. Desktop: press **S** to copy. Mobile: **long-press** the diptych. The S key uses `navigator.clipboard.writeText` with a `legacyCopy()` fallback for older browsers; the long-press uses `navigator.share` for the native share sheet on iOS/Android.

### Interludes

Every 4–7 clicks an interlude card appears: **contact**, **share**, or **welcome** — each shown **at most once per session**. After all three have appeared the gallery becomes a pure sequence of diptychs. The first interlude is always the share card if still unseen, so the user learns the share gesture before they have any way to discover it.

---

## Project layout

```
.
├── index.html              # gate cards, diptych container, head-script theme
├── styles.css              # tokens, transitions, overlay treatment
├── app.js                  # everything else
├── images/
│   ├── jpg/                # ff{N}.jpg (canonical) and ff{N}-{w}.jpg variants
│   └── avif/               # ff{N}.avif and ff{N}-{w}.avif variants
└── videos/
    ├── ff{N}.mp4
    └── ff{N}-poster.jpg    # representative frame, for colour analysis (optional but recommended)
```

Images are discovered by probing `images/jpg/ff{1}.jpg`, `ff{2}.jpg`, … in parallel batches of 20 until a batch returns nothing AND a probe two batches ahead also 404s (handles small gaps in numbering without committing to wasted requests). Videos are discovered the same way in their own namespace — `ff1.mp4` is a different asset than `ff1.jpg`, addressed as `v1` in share URLs (`#5,v12`).

Each image needs variants at three widths — 600, 1000, 1500 — in both formats:

```
images/jpg/ff42-600.jpg
images/jpg/ff42-1000.jpg
images/jpg/ff42-1500.jpg
images/avif/ff42-600.avif
…
```

The browser picks the smallest variant ≥ `50vw × devicePixelRatio` via `srcset` + `sizes="50vw"`. A retina laptop reaches for `1000`; a 3× DPR phone gets `600` and looks fine because AVIF compresses gracefully when slightly under target.

---

## Configuration

All knobs live at the top of `app.js`. The defaults are tuned for ~150 images; adjust to taste.

| Const | Default | What it does |
|---|---|---|
| `TOP_PAIRS_POOL` | `250` | Best-N pairs eligible for selection. Hard floor on quality — pairs ranked worse never appear. For a pool of ~100 items, this is the top ~5% of all possible pairs and yields 500 distinct diptychs. |
| `RECENT_CLICKS_BLOCK` | `25` | An image can't reappear for this many clicks after being shown. With ~100 images, ~50 are locked at any time. |
| `CONTACT_MIN` / `CONTACT_MAX` | `4` / `7` | Range for the random interlude cadence. |
| `GUARANTEE_RATE` | `0.45` | Probability a click draws from the per-image staleness pool rather than the global top-N. ~1 in 2. |
| `VIDEO_RATE` | `0.35` | Per-click probability of a video pair, once the minimum gap has elapsed. |
| `VIDEO_MIN_GAP` | `1` | Photo-only clicks required between videos. |
| `COLOR_SAMPLE_SIZE` | `128` | Side length of the downsampled analysis canvas. |
| `PALETTE_SIZE` / `HIST_BINS` | `4` / `7` | Colour summary dimensions. |
| `TONAL_WEIGHT`, `PALETTE_WEIGHT`, `DENSITY_WEIGHT`, `LIGHTNESS_WEIGHT`, `SAT_WEIGHT` | `0.05`, `0.45`, `0.35`, `0.20`, `0.05` | Pair-score reward weights. |
| `PALETTE_CONTRAST_POWER` | `2.0` | Exponent applied to palette contrast — concentrates reward at the top of the range. At 2.0, a pair with 0.5 contrast keeps only 25% of full reward, 0.3 keeps 9%. Set 1.0 to disable; 1.5 is a gentler intermediate; 3.0 is very aggressive. |
| `REPETITION_PENALTY` | `1.1` | How hard to punish two-of-the-same-hue pairs. |
| `JOINT_DESAT_PENALTY` / `JOINT_DESAT_THRESHOLD` | `0.5` / `0.30` | Penalty for pairs where neither side has colour life, and the avgSat threshold below which the penalty engages. |
| `JOINT_FULL_PENALTY` / `JOINT_FULL_THRESHOLD` | `0.45` / `0.55` | Penalty for pairs where BOTH images are busy, and the density threshold above which "busy" starts. |
| `JOINT_EMPTY_PENALTY` / `JOINT_EMPTY_THRESHOLD` | `0.30` / `0.35` | Penalty for pairs where BOTH images are near-empty, and the density threshold below which "empty" starts. |
| `FALLBACK_TRUST_PENALTY` | `0.40` | Penalty applied to any pair where one side lacks a real colour signature (a video without a poster image — see "Loading" above). Keeps fallback-signed videos out of the global top-N pool until a poster is generated; they still rotate via the per-image guarantee branch. Set to 0 to disable. |
| `SIBLING_GROUPS` | `[['97','98']]` | Near-duplicate images that should block each other's slot in `recent`. |
| `FAVORITE_IMAGES` / `FAVORITE_BOOST` | 15 image numbers / `4.0` | Image numbers to give extra rotation priority. Their staleness is multiplied by the boost inside the per-image guarantee branch, so they cycle back roughly `BOOST×` more often than non-favorites would on the algorithm's judgement alone. |
| `PROGRESS_RATE_PER_SEC` | `60` | Max climb rate of the `Loading… X%` counter. |
| `SPLASH_MAX_WAIT_MS` | `30000` | Safety cap; splash fades even if loading hasn't completed. |

---

## Browser support

Targets the last two versions of Chrome, Safari, Firefox, and Edge. iOS Safari 15.4+ for `:focus-visible`; older versions get the fallback `:focus` reset (no lime keyboard ring, but no UA blue ring either). The Web Share API is used where available with a `legacyCopy` execCommand fallback. AVIF is the preferred format with JPG as a universal fallback via `<picture>`.

A handful of touchy things that get explicit defences in CSS or JS:

- **iOS tap-highlight blue** — neutralised with `-webkit-tap-highlight-color: transparent` on every tappable surface
- **iOS phone-number auto-styling** — disabled with `<meta name="format-detection" content="telephone=no, …">`
- **iOS image long-press save callout** — blocked with `-webkit-touch-callout: none` on the diptych
- **iOS muted-autoplay** — videos get `muted` + `playsinline` set both as attribute and JS property because iOS resets the property on `src` change
- **iOS Safari background page suspension** — `img.decode()` is raced against a 5 s timeout so a screen-lock mid-load can't wedge the gallery permanently
- **iOS Safari `<video>` vs `<img>` alignment** — iOS gives video its own native composite layer, which rounds sub-pixel transforms differently from images. Vertical centering uses `top: 25vh` (resolved at layout, no transform) so both elements land on the same pixel row. iOS Safari also leaves a sub-pixel column of letterbox slack at the leading edge of `<video>` with `object-fit: contain`; the right-panel video is shifted 1px past the centerline so the slack lands inside the panel's `overflow: hidden` clip rather than on the centerline.

---

## Deploying

The site is fully static. Drop the three files plus the `images/` and `videos/` directories onto any host that serves them — Cloudflare Pages, Netlify, GitHub Pages, S3+CloudFront, plain nginx. There's no server-side component. Make sure:

- The host serves `image/avif` with the correct content-type (most do by default in 2025)
- The host supports `HEAD` requests on static files (almost universal; the only common exception is some misconfigured object storage)
- HTTP/2 is enabled — discovery fires 20 parallel `HEAD` requests per batch and benefits from multiplexing

Google Analytics is loaded conditionally after consent (`G-J2Q38DS42K` — change in `app.js` if you fork). The consent card complies with GDPR's "explicit, deliberate choice" requirement; the decision is stored in `localStorage` under the key `ff-analytics-consent`.

---

## Credits

Photography © Federico Ferrari, 2026. All rights reserved.

Code is the photographer's own; comments throughout the source explain the reasoning behind every non-obvious decision. Contributions and bug reports welcome.

Contact: **ciao@thisisfed.xyz** · [thisisfed.xyz](https://thisisfed.xyz)
