#!/usr/bin/env python3
"""Diagnostic: logs precise timestamps for every pygame event to
/tmp/bars_input_diag.log so we can see whether event delivery itself
is delayed, independent of rendering. Press keys with pauses between
them, then press Q or ESC (or wait 30s) to finish."""
import os
import time

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ.setdefault("SDL_VIDEO_DOUBLE_BUFFER", "1")

import pygame

pygame.init()
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
screen.fill((0, 0, 0))
pygame.display.flip()

log_path = "/tmp/bars_input_diag.log"
log = open(log_path, "w")
start = time.time()
clock = pygame.time.Clock()
running = True
last_loop_time = start
loop_n = 0

while running and (time.time() - start) < 30:
    now = time.time()
    gap_ms = (now - last_loop_time) * 1000
    last_loop_time = now
    loop_n += 1
    events = pygame.event.get()
    for e in events:
        key = getattr(e, "key", None)
        keyname = pygame.key.name(key) if key is not None else None
        log.write(
            f"t={now - start:8.3f}s  loop#{loop_n:5d}  loop_gap={gap_ms:6.1f}ms  "
            f"event={pygame.event.event_name(e.type)}  key={keyname}\n"
        )
        log.flush()
        if e.type == pygame.KEYDOWN and key in (pygame.K_q, pygame.K_ESCAPE):
            running = False
    clock.tick(30)

log.write(f"finished after {time.time() - start:.3f}s, {loop_n} loop iterations\n")
log.close()
pygame.quit()
print(f"wrote {log_path}")
