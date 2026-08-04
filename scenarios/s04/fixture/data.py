def flatten(rows):
    return [x for row in rows for x in row]

def dedupe(xs):
    return list(dict.fromkeys(xs))
