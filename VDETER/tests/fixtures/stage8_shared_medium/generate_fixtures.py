"""Byte-pinned IQ golden-vector 생성기 (Stage 5 shared-medium, 명세 §11.1, WSM-G3).

이 스크립트는 참조 frozen decoder(WSM-REF-DECODER-1)와 IQ 합성(선형 복소 합산 + virtual
frontend WSM-VFE-1)에서 **결정적으로** golden vector를 생성한다. fixture를 시험 통과에 맞춰
임의로 만들지 않고, 문서화된 modem 정의에서 유도한다. expected decoder 결과와 waveform
provenance를 MANIFEST/PROVENANCE에 함께 기록한다.

재현: PYTHONPATH=src python tests/fixtures/stage8_shared_medium/generate_fixtures.py
생성물: *.iq (cf32le, byte-pinned), MANIFEST.sha256, PROVENANCE.md
"""

from __future__ import annotations

import hashlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vdeter.stage8.poc_api.types import PhysicalChannel  # noqa: E402
from vdeter.stage8.shared_medium import decoder as D  # noqa: E402
from vdeter.stage8.shared_medium import frontend as F  # noqa: E402
from vdeter.stage8.shared_medium import iq as IQ  # noqa: E402

# payload는 고정 ASCII. P1과 P3는 collision 정렬을 위해 같은 길이(16 byte).
P1 = b"WSM-POS-ROC-0001"
P2 = b"WSM-POS-MASSA-02"
P3 = b"WSM-NEG-ALT--003"


def _write(name: str, samples) -> tuple:
    data = IQ.to_cf32le(samples)
    path = os.path.join(_HERE, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return name, hashlib.sha256(data).hexdigest(), len(data), len(tuple(samples))


def build():
    b_p1_lower = D.build_burst(PhysicalChannel.LOWER, 0, P1)
    b_p2_upper = D.build_burst(PhysicalChannel.UPPER, 0, P2)
    b_p3_lower_0 = D.build_burst(PhysicalChannel.LOWER, 0, P3)
    b_p3_lower_40 = D.build_burst(PhysicalChannel.LOWER, 40, P3)

    # composite: 동일 두 burst 완전 겹침 -> capture(같은 payload), clipping 발생.
    cap = IQ.synthesize_same_channel(
        [b_p1_lower, D.build_burst(PhysicalChannel.LOWER, 0, P1)]
    )
    # composite: 서로 다른 두 burst 완전 겹침 -> collision(CRC 실패).
    col = IQ.synthesize_same_channel([b_p1_lower, b_p3_lower_0])
    # composite: 부분 겹침(같은 채널) -> collision.
    part = IQ.synthesize_same_channel([b_p1_lower, b_p3_lower_40])

    records = []
    records.append(_write("src_clean_lower_p1.iq", b_p1_lower.samples))
    records.append(_write("src_clean_upper_p2.iq", b_p2_upper.samples))
    records.append(_write("src_alt_lower_p3.iq", b_p3_lower_0.samples))
    records.append(_write("composite_capture_lower.iq", cap.samples))
    records.append(_write("composite_collision_lower.iq", col.samples))
    records.append(_write("composite_partial_lower.iq", part.samples))

    # MANIFEST.sha256
    manifest_lines = [f"{sha}  {name}" for name, sha, _size, _n in records]
    with open(os.path.join(_HERE, "MANIFEST.sha256"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(manifest_lines) + "\n")

    # expected decode 결과(문서화).
    def dec(name_samples):
        r = D.decode(name_samples)
        return r.status.value, (r.payload.decode("ascii", "replace") if r.payload else None)

    expected = {
        "src_clean_lower_p1.iq": dec(b_p1_lower.samples),
        "src_clean_upper_p2.iq": dec(b_p2_upper.samples),
        "src_alt_lower_p3.iq": dec(b_p3_lower_0.samples),
        "composite_capture_lower.iq": dec(cap.samples),
        "composite_collision_lower.iq": dec(col.samples),
        "composite_partial_lower.iq": dec(part.samples),
    }

    prov = []
    prov.append("# Stage 5 shared-medium byte-pinned IQ fixture provenance\n")
    prov.append(f"- reference decoder: `{D.REFERENCE_DECODER_VERSION}` "
                f"(QPSK, A={D.SYMBOL_AMPLITUDE}, {D.SAMPLES_PER_SYMBOL} sample/symbol, "
                f"hard-decision boundary 0, CRC32)\n")
    prov.append(f"- virtual frontend: `{F.VIRTUAL_FRONTEND_VERSION}` "
                f"(per-axis clip to +/-{F.FULL_SCALE}, quantization={F.QUANTIZATION})\n")
    prov.append(f"- sample format: `{IQ.SAMPLE_FORMAT}` (interleaved float32 LE, "
                f"{IQ.BYTES_PER_SAMPLE} bytes/sample)\n")
    prov.append("- collision arithmetic: linear complex sum on overlapping same-channel samples, "
                "passthrough (copy) on non-overlap; clip only composite (>=2 contributor) samples.\n")
    prov.append(f"- payloads: P1={P1!r}, P2={P2!r}, P3={P3!r} (P1/P3 동일 길이 정렬)\n\n")
    prov.append("| fixture | 생성 근거 | expected decode | SHA-256 |\n")
    prov.append("| --- | --- | --- | --- |\n")
    basis = {
        "src_clean_lower_p1.iq": "encode_payload(P1) 단일 clean burst (lower) — positive control",
        "src_clean_upper_p2.iq": "encode_payload(P2) 단일 clean burst (upper) — positive control",
        "src_alt_lower_p3.iq": "encode_payload(P3) 단일 clean burst (lower) — 대체 payload",
        "composite_capture_lower.iq": "P1+P1 완전 겹침 합성 (capture, clipping) — positive control",
        "composite_collision_lower.iq": "P1+P3 완전 겹침 합성 (collision) — negative control",
        "composite_partial_lower.iq": "P1+P3(offset 40) 부분 겹침 합성 — negative control",
    }
    by_name = {name: (sha, size, n) for name, sha, size, n in records}
    for name, _sha, _size, _n in records:
        st, pl = expected[name]
        sha, size, n = by_name[name]
        prov.append(f"| `{name}` | {basis[name]} | {st}{(' ' + repr(pl)) if pl else ''} | `{sha}` |\n")
    with open(os.path.join(_HERE, "PROVENANCE.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(prov))

    for name, sha, size, n in records:
        st, pl = expected[name]
        print(f"{name}: {size}B {n}samples sha={sha[:16]}... decode={st} {pl}")


if __name__ == "__main__":
    build()
