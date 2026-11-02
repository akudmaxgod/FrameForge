"""Compatibility hook; settings are selected upstream from event provenance.

No lexical blacklist, repetition threshold or maximum catalog size is applied.
Legacy lists without provenance are preserved, never silently reclassified.
"""
import copy

def compact(analysis,story):
    return copy.deepcopy(analysis)
