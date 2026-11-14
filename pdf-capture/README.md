# PDF page capture via OBS (capture-card simulation)

This tool simulates the two-computer setup on a single machine:

| Real setup | Simulation |
|---|---|
| Computer 1 shows the PDF | A PDF viewer window on this computer |
| HDMI capture card | OBS Studio's **Virtual Camera** |
| Computer 2 runs the script | `obs_page_capture.py` on this computer |

You turn the pages by hand. The script saves each page once it has settled and
builds a PDF when you stop. To move to real hardware later, plug in the capture
card and point `--camera` at it instead of OBS. Nothing else changes.

## 1. Install

- **Python 3.9+**
- **OBS Studio 30 or later** from obsproject.com (the Virtual Camera is built in on Windows and macOS)
- Python packages, from this folder:
  ```
  pip install -r requirements.txt
  ```
  Use `opencv-python`, **not** `opencv-python-headless`. The headless version has no preview window.
- Optional: `ocrmypdf` for searchable PDFs (`--ocr`).

## 2. Set up OBS (one-off, about 5 minutes)

1. **Open the PDF** in your viewer. Hide toolbars and sidebars and set the zoom to *fit page*.
   Use a light theme so the page stands out from the background.
2. **Settings → Video**: set *Base (Canvas) Resolution* and *Output (Scaled) Resolution*
   to your screen's native resolution (e.g. 1920x1080). Set FPS to 30. Click OK.
3. **Sources → + → Window Capture** and choose the PDF viewer window.
   - Windows: if it comes out black, set *Capture Method* to "Windows 10 (1903 and up)".
   - macOS: use *macOS Screen Capture* and pick the window. Grant Screen Recording permission when asked.
   - Tick **Capture Cursor** off, so the mouse pointer doesn't appear.
4. Right-click the source → **Transform → Fit to screen**.
5. **Crop away everything except the page**: hold **Alt** (Option on Mac) and drag the
   red edges of the source in the preview. You can also right-click → Filters → + Crop/Pad.
6. Click **Start Virtual Camera** (Controls panel, bottom right).

Use Window Capture, not Display Capture. With Display Capture, the script's own
preview window ends up in the feed and appears in every page.

## 3. Find the camera index

```
python obs_page_capture.py --list-cameras
```

The OBS Virtual Camera is usually the highest index if you also have a webcam.
Your webcam's image looks like a camera feed; OBS's looks like your PDF.

## 4. Capture

```
python obs_page_capture.py --camera 1 --width 1920 --height 1080
```

(Use your own index and your OBS output resolution.)

- A preview window opens, showing the page count and **moving** or **settled**.
- Click on the PDF viewer and turn pages with Page Down or the arrow keys. **Pause about half a second on each page** until the preview says *settled* and the terminal prints `saved page N`.
- Keys in the preview window: **q** / Esc to stop and build the PDF; **s** to force-save the current frame; **u** to delete the last saved page.
- Output goes to `captures/<date-time>/`: `page_0001.png`, … , `capture.pdf` and `capture_log.csv`.

Useful options:

| Option | Effect |
|---|---|
| `--autocrop` | Trims any viewer background left around the page |
| `--ocr` | Also writes `capture_searchable.pdf` (needs ocrmypdf) |
| `--stop-after 20` | Stops automatically after 20 seconds with no new page |
| `--keep-repeats` | Keeps pages identical to an earlier page (e.g. several blank pages) |
| `--compile-only DIR` | Rebuilds the PDF from the PNGs in DIR (e.g. after deleting a bad page by hand) |

## 5. Test without OBS

```
python make_test_video.py test_feed.avi
python obs_page_capture.py --video test_feed.avi --no-preview --autocrop
```

The test video imitates someone turning pages by hand: a cursor moving over the page,
a blurry first render, a cross-fade, going back a page, and a scroll. The correct result
is 7 pages and one "skipped: same as page 4" message.

## How it decides what is a page

- Each frame is shrunk to 640 px wide, converted to grey and lightly blurred. A pixel counts as
  changed if its grey level differs by more than 20.
- **Settled**: fewer than 0.15% of pixels (`--stable-thresh`) have changed compared with the
  start of the still period, for 8 frames in a row (`--stable-frames`, about 0.25 s).
  Comparing with the start, not just the previous frame, means a slow fade can't pass as
  still.
- **New page**: a settled image is saved if at least 0.4% of pixels (`--change-thresh`) differ
  from the last saved page, and it doesn't match an earlier page. A mouse cursor or a small
  pop-up falls below that threshold, so it doesn't create a page.
- The last 4 settled frames are averaged (`--average`) to remove noise, then saved in full colour.

## Known limits

- **Pages that differ only very slightly** (e.g. only the page number changes on an otherwise
  identical form) may fall below the 0.4% threshold and be skipped. Press **s** to force-save
  them, or lower `--change-thresh` (e.g. 0.001).
- **Identical pages** (e.g. two blank pages) are treated as repeats unless you pass `--keep-repeats`.
- **Resolution is limited to what's on screen.** A portrait page fitted to a landscape
  1080p screen is roughly 110–130 dpi. Rotating the monitor to portrait and setting the OBS
  canvas to 1080x1920 roughly doubles the detail.
- If the viewer animates page turns slowly, raise `--stable-frames`.
- The PDF pages are fitted onto A4 (auto-rotated for landscape pages), without re-compressing the images.
