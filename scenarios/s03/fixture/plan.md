# Plan: fix inclusive range sum

## Surface
- calc.py

## Changes
add_range(a, b) must return the sum of integers from a to b INCLUSIVE.
It currently excludes b.

## Done when
python3 -c "from calc import add_range; assert add_range(1,3)==6" exits 0.
