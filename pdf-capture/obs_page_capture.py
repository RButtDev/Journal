#!/usr/bin/env python3
"""
obs_page_capture.py - capture PDF pages from the OBS Virtual Camera (or a real
HDMI capture card) while you turn the pages by hand, then compile them to a PDF.

How it works
------------
OBS shows the PDF window and publishes it as a webcam ("OBS Virtual Camera").
This script reads that webcam frame by frame and runs a small state machine:

  * Every frame is shrunk and greyed, then compared with the "anchor" frame
    (the first frame of the current still period). If too many pixels differ,
    the picture is moving (page turning, scrolling, re-rendering) and the
    anchor is reset.
  * When the picture has stayed the same for --stable-frames frames, it counts
    as a settled still. That still is compared with the last saved page; if it
    differs enough it is a new page, so the last few frames are averaged (to
    remove noise) and saved as a full-colour PNG.
  * Stills that match an earlier page (you went back a page) are skipped.
    A mouse cursor or a small pop-up is below the change threshold, so it does
    not create a page either.

Stopping: press q / Esc in the preview window, or Ctrl+C in the terminal, or use
--stop-after. The PNGs are then compiled into a PDF (lossless, via img2pdf) and,
optionally, made searchable with ocrmypdf.

Keys in the preview window:  q/Esc stop and compile | s force-save this frame |
                             u undo (delete) the last saved page
"""

import argparse
import csv
import platform
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

WORK_WIDTH = 640          # width the frames are shrunk to for comparisons
PIXEL_THRESH = 20         # grey-level difference that counts a pixel as changed


# --------------------------------------------------------------------------- #
# Video source
# --------------------------------------------------------------------------- #
def camera_backend():
    system = platform.system()
    if system == "Windows":
        return cv2.CAP_DSHOW          # OBS Virtual Camera is a DirectShow device
    if system == "Darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_V4L2               # Linux: OBS uses v4l2loopback


def list_cameras(max_index=10):
    print("Probing camera indexes 0-%d ..." % (max_index - 1))
    found = False
    for i in range(max_index):
        cap = cv2.VideoCapture(i, camera_backend())
        if cap.isOpened():
            ok, frame = cap.read()
            if ok:
                h, w = frame.shape[:2]
                print(f"  index {i}: opens, default frame size {w}x{h}")
                found = True
        cap.release()
    if not found:
        print("  no cameras found - is the OBS Virtual Camera started?")


def open_source(args):
    if args.video:
        cap = cv2.VideoCapture(args.video)
        label = f"video file {args.video}"
    else:
        cap = cv2.VideoCapture(args.camera, camera_backend())
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        label = f"camera index {args.camera}"
    if not cap.isOpened():
        sys.exit(f"Could not open {label}. Try --list-cameras.")
    ok, frame = cap.read()
    if not ok:
        sys.exit(f"Opened {label} but could not read a frame.")
    h, w = frame.shape[:2]
    print(f"Reading {label} at {w}x{h}")
    if not args.video and (w, h) != (args.width, args.height):
        print(f"WARNING: asked for {args.width}x{args.height} but got {w}x{h}. "
              "Check OBS Settings > Video > Output (Scaled) Resolution, and pass "
              "--width/--height to match it.")
    if not args.video and frame.std() < 1.0:
        print("WARNING: the first frame is a flat colour. If it stays that way, "
              "OBS is probably not running its Virtual Camera, or you have the "
              "wrong --camera index.")
    return cap, frame


# --------------------------------------------------------------------------- #
# Image comparison
# --------------------------------------------------------------------------- #
def small_grey(frame):
    h, w = frame.shape[:2]
    size = (WORK_WIDTH, max(1, round(h * WORK_WIDTH / w)))
    grey = cv2.cvtColor(cv2.resize(frame, size, interpolation=cv2.INTER_AREA),
                        cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(grey, (3, 3), 0)    # suppress sensor / codec noise


def changed_fraction(a, b):
    """Share of pixels (0-1) whose grey level differs by more than PIXEL_THRESH."""
    return np.count_nonzero(cv2.absdiff(a, b) > PIXEL_THRESH) / a.size


def average_frames(frames):
    stack = np.stack([f.astype(np.float32) for f in frames])
    return np.clip(stack.mean(axis=0) + 0.5, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Capture loop
# --------------------------------------------------------------------------- #
class Capturer:
    def __init__(self, args, out_dir):
        self.args = args
        self.out = out_dir
        self.pages = []                 # list of (path, small_grey)
        self.log_rows = []

    def save(self, image, small, frame_no, note):
        n = len(self.pages) + 1
        path = self.out / f"page_{n:04d}.png"
        cv2.imwrite(str(path), image)
        diff_prev = changed_fraction(small, self.pages[-1][1]) if self.pages else 1.0
        self.pages.append((path, small))
        self.log_rows.append([n, path.name, datetime.now().isoformat(timespec="seconds"),
                              frame_no, f"{diff_prev:.4f}", note])
        print(f"  saved page {n}  ({note}, {diff_prev:.1%} different from previous page)")

    def undo(self):
        if not self.pages:
            return
        path, _ = self.pages.pop()
        path.unlink(missing_ok=True)
        self.log_rows.append(["undo", path.name, datetime.now().isoformat(timespec="seconds"),
                              "", "", "deleted by user"])
        print(f"  undo: deleted {path.name}")

    def matching_page(self, small):
        """Return the number of an already-saved page that this still matches, or None."""
        pages = self.pages[-1:] if self.args.keep_repeats else self.pages
        offset = len(self.pages) - len(pages)
        for i, (_, s) in enumerate(pages):
            if changed_fraction(small, s) < self.args.change_thresh:
                return offset + i + 1
        return None

    def run(self, cap, first_frame):
        a = self.args
        n_avg = max(1, min(a.average, a.stable_frames))
        recent = deque(maxlen=n_avg)
        anchor = None
        run_len = 0
        handled = False                 # current still already evaluated?
        frame_no = 0
        last_new_page = time.time()
        preview = not a.no_preview
        frame = first_frame

        print("\nWatching. Turn the pages by hand; wait about half a second on each.")
        print("Stop with q/Esc in the preview window, or Ctrl+C here.\n")
        try:
            while True:
                if frame is None:
                    ok, frame = cap.read()
                    if not ok:
                        print("End of video stream.")
                        break
                frame_no += 1
                small = small_grey(frame)

                if anchor is not None and changed_fraction(small, anchor) < a.stable_thresh:
                    run_len += 1
                else:
                    anchor, run_len, handled = small, 0, False
                    recent.clear()
                recent.append(frame)

                status = "moving"
                if run_len >= a.stable_frames:
                    status = "settled"
                    if not handled:
                        handled = True
                        if not self.pages or changed_fraction(small, self.pages[-1][1]) >= a.change_thresh:
                            match = self.matching_page(small)
                            if match is None:
                                self.save(average_frames(recent), small, frame_no, "auto")
                                last_new_page = time.time()
                            else:
                                print(f"  skipped: same as page {match}")

                if preview:
                    preview = self.show(frame, status)
                    key = cv2.waitKey(1) & 0xFF if preview else -1
                    if key in (ord("q"), 27):
                        break
                    if key == ord("s"):
                        self.save(average_frames(recent), small, frame_no, "forced")
                        last_new_page = time.time()
                    if key == ord("u"):
                        self.undo()

                if a.stop_after and not a.video and time.time() - last_new_page > a.stop_after:
                    print(f"No new page for {a.stop_after}s - stopping.")
                    break
                frame = None
        except KeyboardInterrupt:
            print("\nStopped by Ctrl+C.")
        finally:
            cap.release()
            if not a.no_preview:
                try:
                    cv2.destroyAllWindows()
                except cv2.error:
                    pass
        self.write_log()

    def show(self, frame, status):
        h, w = frame.shape[:2]
        scale = min(1.0, 720 / h)
        view = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame.copy()
        colour = (0, 160, 0) if status == "settled" else (0, 0, 220)
        text = f"pages: {len(self.pages)}   {status}   [q] stop  [s] save  [u] undo"
        cv2.rectangle(view, (0, 0), (view.shape[1], 34), (255, 255, 255), -1)
        cv2.putText(view, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)
        try:
            cv2.imshow("PDF page capture", view)
            return True
        except cv2.error:
            print("No preview window available (opencv-python-headless installed?). "
                  "Carrying on without it - stop with Ctrl+C.")
            return False

    def write_log(self):
        with open(self.out / "capture_log.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["page", "file", "time", "frame", "diff_vs_previous_page", "note"])
            w.writerows(self.log_rows)


# --------------------------------------------------------------------------- #
# Compile
# --------------------------------------------------------------------------- #
def autocrop_box(paths, margin=8):
    """One crop box for all pages: the area that differs from the border colour."""
    boxes, shape = [], None
    kernel = np.ones((5, 5), np.uint8)
    for p in paths:
        img = cv2.imread(str(p))
        shape = img.shape
        border = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]])
        bg = np.median(border, axis=0)
        diff = np.abs(img.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
        mask = cv2.morphologyEx((diff > 30).astype(np.uint8), cv2.MORPH_OPEN, kernel)
        ys, xs = np.nonzero(mask)
        if xs.size:
            boxes.append((xs.min(), ys.min(), xs.max(), ys.max()))
    if not boxes:
        return None
    x0 = max(0, min(b[0] for b in boxes) - margin)
    y0 = max(0, min(b[1] for b in boxes) - margin)
    x1 = min(shape[1], max(b[2] for b in boxes) + margin + 1)
    y1 = min(shape[0], max(b[3] for b in boxes) + margin + 1)
    return x0, y0, x1, y1


def compile_pdf(page_dir, pdf_path, autocrop=False, ocr=False):
    import img2pdf

    paths = sorted(page_dir.glob("page_*.png"))
    if not paths:
        print("No pages captured - nothing to compile.")
        return None

    if autocrop:
        box = autocrop_box(paths)
        if box:
            x0, y0, x1, y1 = box
            crop_dir = page_dir / "cropped"
            crop_dir.mkdir(exist_ok=True)
            cropped = []
            for p in paths:
                img = cv2.imread(str(p))[y0:y1, x0:x1]
                q = crop_dir / p.name
                cv2.imwrite(str(q), img)
                cropped.append(q)
            paths = cropped
            print(f"Auto-cropped all pages to x {x0}-{x1}, y {y0}-{y1}")

    a4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
    layout = img2pdf.get_layout_fun(a4, fit=img2pdf.FitMode.into, auto_orient=True)
    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in paths], layout_fun=layout))
    print(f"Wrote {pdf_path} ({len(paths)} pages)")

    if ocr:
        if shutil.which("ocrmypdf"):
            searchable = pdf_path.with_name(pdf_path.stem + "_searchable.pdf")
            subprocess.run(["ocrmypdf", "--optimize", "1", str(pdf_path), str(searchable)],
                           check=False)
            print(f"Wrote {searchable}")
        else:
            print("ocrmypdf not found - skipped OCR.")
    return pdf_path


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("source")
    src.add_argument("--list-cameras", action="store_true", help="list camera indexes and exit")
    src.add_argument("--camera", type=int, default=0, help="camera index of the OBS Virtual Camera")
    src.add_argument("--width", type=int, default=1920, help="must match OBS output width")
    src.add_argument("--height", type=int, default=1080, help="must match OBS output height")
    src.add_argument("--video", help="read a video file instead of a camera (for testing)")

    det = p.add_argument_group("detection (fractions of pixels that changed, 0-1)")
    det.add_argument("--stable-frames", type=int, default=8,
                     help="frames the picture must stay still before it counts (default 8, ~0.25s at 30fps)")
    det.add_argument("--stable-thresh", type=float, default=0.0015,
                     help="max change while 'still' (default 0.0015)")
    det.add_argument("--change-thresh", type=float, default=0.004,
                     help="min change from the last page to count as a new page (default 0.004)")
    det.add_argument("--average", type=int, default=4, help="settled frames averaged per page (default 4)")
    det.add_argument("--keep-repeats", action="store_true",
                     help="do not skip pages that match an earlier page (e.g. identical blank pages)")

    run = p.add_argument_group("run")
    run.add_argument("--out", help="output folder (default captures/<date-time>)")
    run.add_argument("--no-preview", action="store_true", help="no preview window; stop with Ctrl+C")
    run.add_argument("--stop-after", type=float, default=0,
                     help="stop when no new page for this many seconds (camera only; 0 = never)")

    out = p.add_argument_group("output")
    out.add_argument("--autocrop", action="store_true", help="trim the viewer background around the page")
    out.add_argument("--ocr", action="store_true", help="also make a searchable copy with ocrmypdf")
    out.add_argument("--compile-only", metavar="DIR",
                     help="skip capture; build the PDF from page_*.png already in DIR")
    a = p.parse_args()

    if a.list_cameras:
        list_cameras()
        return

    if a.compile_only:
        d = Path(a.compile_only)
        compile_pdf(d, d / "capture.pdf", a.autocrop, a.ocr)
        return

    out_dir = Path(a.out or Path("captures") / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    cap, first = open_source(a)
    capturer = Capturer(a, out_dir)
    capturer.run(cap, first)
    compile_pdf(out_dir, out_dir / "capture.pdf", a.autocrop, a.ocr)


if __name__ == "__main__":
    main()
