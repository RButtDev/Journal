#!/usr/bin/env python3
"""
make_test_video.py - build a fake "OBS feed" video for testing obs_page_capture.py
without OBS. It imitates a PDF viewer on a 1920x1080 screen while someone turns
pages by hand, including the awkward cases:

  page 1 -> 2 (instant)      mouse cursor moves over page 2 while it is shown
  page 3 -> 4                viewer shows a blurry render first, then sharpens
  page 4 -> 5                slow cross-fade
  5 -> back to 4 -> 5        going back a page (both repeats should be skipped)
  page 6 -> 7                scrolls up rather than jumping
  plus sensor-style noise and MJPEG compression on every frame.

Expected result: 7 pages saved. Going back to page 4 is reported as "skipped:
same as page 4"; returning to page 5 matches the last saved page, so it is
ignored without a message (the same rule that ignores cursor movement).

Usage:  python make_test_video.py test_feed.avi
        python obs_page_capture.py --video test_feed.avi --no-preview
"""
import sys

import cv2
import numpy as np

W, H, FPS, HOLD = 1920, 1080, 30, 45
BG = (89, 86, 82)                                # PDF-viewer grey (BGR)
PAGE_H = 1040
PAGE_W = int(PAGE_H * 210 / 297)
X0, Y0 = (W - PAGE_W) // 2, (H - PAGE_H) // 2
rng = np.random.default_rng(1)
WORDS = ("contract tender clause supplier council works payment notice period "
         "completion defects employer contractor schedule award standstill").split()


def make_page(n):
    page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    cv2.putText(page, f"Document title - section {n}", (50, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
    r = np.random.default_rng(100 + n)
    for line in range(34):
        words = " ".join(r.choice(WORDS, size=r.integers(5, 9)))
        cv2.putText(page, words, (50, 130 + line * 26), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(page, f"{n}", (PAGE_W // 2 - 8, PAGE_H - 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    return page


def screen(page, dy=0):
    s = np.full((H, W, 3), BG, np.uint8)
    y = Y0 + dy
    top, bot = max(0, y), min(H, y + PAGE_H)
    if bot > top:
        s[top:bot, X0:X0 + PAGE_W] = page[top - y:bot - y]
    return s


def cursor(img, x, y):
    pts = np.array([[x, y], [x, y + 22], [x + 6, y + 17], [x + 15, y + 17]], np.int32)
    cv2.fillPoly(img, [pts], (0, 0, 0))
    cv2.polylines(img, [pts], True, (255, 255, 255), 1)
    return img


def main(path):
    pages = {n: make_page(n) for n in range(1, 8)}
    frames = []
    hold = lambda img, n=HOLD: frames.extend([img] * n)

    hold(screen(pages[1]))
    s2 = screen(pages[2])
    for i in range(25):                            # cursor sweeps across page 2
        frames.append(cursor(s2.copy(), 800 + i * 12, 400 + i * 6))
    hold(cursor(s2.copy(), 1100, 550))
    hold(screen(pages[3]))
    for _ in range(4):                             # blurry first render of page 4
        frames.append(cv2.GaussianBlur(screen(pages[4]), (0, 0), 4))
    hold(screen(pages[4]))
    a, b = screen(pages[4]).astype(np.float32), screen(pages[5]).astype(np.float32)
    for i in range(1, 16):                         # slow cross-fade 4 -> 5
        t = i / 16
        frames.append((a * (1 - t) + b * t).astype(np.uint8))
    hold(screen(pages[5]))
    hold(screen(pages[4]))                         # back a page (repeat)
    hold(screen(pages[5]))                         # forward again (repeat)
    hold(screen(pages[6]))
    combo = np.vstack([pages[6], np.full((40, PAGE_W, 3), BG, np.uint8), pages[7]])
    for i in range(1, 13):                         # scroll 6 -> 7
        dy = int(i / 12 * (PAGE_H + 40))
        s = np.full((H, W, 3), BG, np.uint8)
        s[Y0:Y0 + PAGE_H, X0:X0 + PAGE_W] = combo[dy:dy + PAGE_H]
        frames.append(s)
    hold(screen(pages[7]), HOLD * 2)

    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    for f in frames:
        noisy = np.clip(f.astype(np.int16) + rng.normal(0, 2, f.shape), 0, 255).astype(np.uint8)
        out.write(noisy)
    out.release()
    print(f"Wrote {path}: {len(frames)} frames ({len(frames) / FPS:.1f}s). "
          "Expected: 7 pages saved, 1 'skipped' message.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test_feed.avi")
