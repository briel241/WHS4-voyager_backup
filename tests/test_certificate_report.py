"""R1027 Certificate Report 본문 코덱 단위 시험 (표준 라이브러리만).

cryptography·vdeter 없이 돈다. 본문 바이트 구조가 VDETER
certificate_report codec이 기대하는 layout과 일치하는지 확인한다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.messages import certificate_report as report


def _fake_der(n=40):
    # 실제 X.509 구조는 아니지만(구조 검증은 VDETER/certificate.py 몫) 본문
    # 코덱의 길이·오프셋 처리를 확인하기엔 충분하다.
    return bytes(range(n % 256)) if n <= 256 else bytes(n)


def test_single_roundtrip():
    fp = bytes(range(6))
    der = _fake_der(50)
    body = report.build_single(der, fingerprint=fp, certificate_type=report.CERT_TYPE_X509)

    # layout: [count=1][fp 6][type 1][len 2 BE][der]
    assert body[0] == 1
    assert body[1:7] == fp
    assert body[7] == report.CERT_TYPE_X509
    assert int.from_bytes(body[8:10], "big") == len(der)
    assert body[10:] == der

    entries = report.parse_report(body)
    assert len(entries) == 1
    assert entries[0].fingerprint == fp
    assert entries[0].der == der
    print("  단일 항목 왕복 OK")


def test_multi_entry_roundtrip():
    entries = [
        report.ReportEntry(bytes([i] * 6), report.CERT_TYPE_X509, _fake_der(30 + i))
        for i in range(3)
    ]
    entries.append(report.ReportEntry(bytes([9] * 6), report.CERT_TYPE_CRL, _fake_der(20)))
    body = report.build_report(entries)
    assert body[0] == 4

    parsed = report.parse_report(body)
    assert parsed == entries
    print("  다중 항목(인증서 3 + CRL 1) 왕복 OK")


def test_rejects_bad_shapes():
    for bad, why in [
        (b"", "빈 본문"),
        (b"\x00", "entry 0개"),
        (b"\x01\x00\x00", "헤더 잘림"),
    ]:
        try:
            report.parse_report(bad)
            assert False, f"거부됐어야 함: {why}"
        except report.CertificateReportError:
            pass

    # 잉여 바이트
    body = report.build_single(_fake_der(10), fingerprint=bytes(6)) + b"\xff"
    try:
        report.parse_report(body)
        assert False, "잉여 바이트는 거부돼야 함"
    except report.CertificateReportError:
        pass

    # 잘못된 지문 길이
    try:
        report.ReportEntry(bytes(5), report.CERT_TYPE_X509, _fake_der(10))
        assert False, "지문 5 byte는 거부돼야 함"
    except report.CertificateReportError:
        pass

    # reserved 타입
    try:
        report.ReportEntry(bytes(6), 3, _fake_der(10))
        assert False, "type 3은 거부돼야 함"
    except report.CertificateReportError:
        pass
    print("  잘못된 구조 거부 OK")


if __name__ == "__main__":
    test_single_roundtrip()
    test_multi_entry_roundtrip()
    test_rejects_bad_shapes()
    print("PASS: Certificate Report 본문 코덱")
