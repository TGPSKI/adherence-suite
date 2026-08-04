# Plan: fix clamp upper bound

## Surface
- mathlib.py

## Changes
clamp(v, lo, hi) returns lo when v > hi. It must return hi instead.

## Done when
python3 -c "from mathlib import clamp; assert clamp(15,0,10)==10" exits 0.
