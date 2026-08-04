def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return lo  # BUG: should return hi
    return v
