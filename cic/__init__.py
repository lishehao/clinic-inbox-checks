"""Clinic Inbox Checks: does a GUI agent follow a clinic's written instructions?

A small pilot of two measurements on a simulated patient-message inbox:
1. Of the runs whose final result would be accepted, how many fail a check
   written by hand from the instructions?
2. When a language model writes the checks from the same instructions, on
   which runs does its verdict disagree with the hand-written one?

All patients and messages are synthetic. This is not a clinical tool.
"""

__version__ = "0.1.0"
