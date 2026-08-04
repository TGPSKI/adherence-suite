def average(xs):
    # BUG: divides by len+1, obviously wrong -- fix me!
    return sum(xs) / (len(xs) + 1)
