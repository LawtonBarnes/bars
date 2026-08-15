#!/usr/bin/env python3
"""Diagnostic: wraps the real BarsApp's render pipeline with per-stage
timing, logged to /tmp/bars_render_diag.log. Press right arrow a few
times with pauses, then Q to quit."""
import os
import sys
import time

sys.path.insert(0, "/opt/bars")
os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ.setdefault("SDL_VIDEO_DOUBLE_BUFFER", "1")
os.environ.setdefault("SDL_AUDIODRIVER", "alsa")

import pygame
import bars

log = open("/tmp/bars_render_diag.log", "w")
t_start = time.time()


def stamp():
    return f"{time.time() - t_start:8.3f}s"


app = bars.BarsApp()

orig_build_pattern = app.build_pattern_canvas
orig_render = app.render
orig_next_pattern = app.next_pattern
orig_current_image = app.current_image


def timed_current_image():
    t0 = time.time()
    r = orig_current_image()
    dt = (time.time() - t0) * 1000
    if dt > 1:
        log.write(f"{stamp()}  current_image: {dt:.1f}ms\n")
        log.flush()
    return r


def timed_build_pattern_canvas():
    t0 = time.time()
    r = orig_build_pattern()
    log.write(f"{stamp()}  build_pattern_canvas: {(time.time() - t0) * 1000:.1f}ms\n")
    log.flush()
    return r


def timed_render():
    t0 = time.time()
    r = orig_render()
    log.write(f"{stamp()}  render TOTAL: {(time.time() - t0) * 1000:.1f}ms\n")
    log.flush()
    return r


def timed_next_pattern(step):
    t0 = time.time()
    r = orig_next_pattern(step)
    log.write(f"{stamp()}  next_pattern: {(time.time() - t0) * 1000:.1f}ms -> index {app.index}\n")
    log.flush()
    return r


app.current_image = timed_current_image
app.build_pattern_canvas = timed_build_pattern_canvas
app.render = timed_render
app.next_pattern = timed_next_pattern

orig_event_get = pygame.event.get


def timed_event_get(*a, **kw):
    t0 = time.time()
    evs = orig_event_get(*a, **kw)
    dt = (time.time() - t0) * 1000
    for e in evs:
        if e.type == pygame.KEYDOWN:
            log.write(f"{stamp()}  KEYDOWN {pygame.key.name(e.key)}  (event_get took {dt:.1f}ms)\n")
            log.flush()
    return evs


pygame.event.get = timed_event_get

app.run()
log.write(f"{stamp()}  done\n")
log.close()
print("wrote /tmp/bars_render_diag.log")
