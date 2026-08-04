# Plan: harden merge_intervals

## Surface
- data.py

## Changes
In data.py, function merge_intervals(intervals) must sort its input
before merging. Add the sort as the first statement of merge_intervals.
