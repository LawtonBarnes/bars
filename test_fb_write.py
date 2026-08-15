#!/usr/bin/env python3
"""One-off test: does writing raw bytes directly to /dev/fb0 change what's
on screen, bypassing pygame's display.flip() entirely? Shows RED via the
normal pygame path, waits, then writes GREEN directly to the framebuffer
device. Watch the screen and note what you see and when."""
import mmap
import os
import time

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")

import pygame

pygame.init()
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
screen.fill((255, 0, 0))
pygame.display.flip()
print("RED shown via pygame flip(). Waiting 5s...")
time.sleep(5)

w, h, stride = 720, 480, 1440
size = stride * h
fd = os.open("/dev/fb0", os.O_RDWR)
mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
green565 = (0x07E0).to_bytes(2, "little")  # RGB565 pure green
mm.seek(0)
mm.write(green565 * (size // 2))
mm.flush()
print("Wrote GREEN directly to /dev/fb0 (bypassing pygame). Watch the screen. Waiting 8s...")
time.sleep(8)
mm.close()
os.close(fd)
print("done")
