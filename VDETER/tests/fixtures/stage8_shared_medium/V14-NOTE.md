# v1.4 policy transition

The six `.iq` files, `MANIFEST.sha256`, and original `PROVENANCE.md` are unchanged
historical evidence. Clean-source waveform regeneration and decoder positive/
negative controls remain tested. The three composite vectors are decoder input
fixtures only: they do not describe the current production collision policy.

The v1.3 `frontend.py` clipping implementation and composite fixture generator
were removed when `TX_INTERVAL_DROP_V1` replaced synthetic IQ addition/capture.
They are recoverable from Git at base commit `3254804`. No fixture was regenerated
to obtain a new PASS. New policy tests require both overlapping bursts to drop,
including identical and unequal-amplitude packets; isolated bytes stay identical.
