"""环境指纹单元测试

采集部分用临时 os-release 文件替代真实 /etc/os-release，
匹配与分级为纯函数，可在任意平台验证。
"""
from __future__ import annotations

import pytest

from memory.fingerprint import (
    capture,
    describe,
    is_hard_incompatible,
    match,
    normalize,
    parse_os,
    read_os_release,
)
from memory.types import EnvFingerprint

KYLIN_RELEASE = """NAME="Kylin Linux Advanced Server"
VERSION="V11 (SP3)"
ID="kylin"
VERSION_ID="V11"
PRETTY_NAME="Kylin Linux Advanced Server V11 (SP3)"
"""

UBUNTU_RELEASE = """NAME="Ubuntu"
VERSION="22.04.4 LTS (Jammy Jellyfish)"
ID=ubuntu
VERSION_ID="22.04"
PRETTY_NAME="Ubuntu 22.04.4 LTS"
"""


def fp(**kw) -> EnvFingerprint:
    base = dict(
        os_id="kylin", os_version="v11", os_sp="sp3",
        kernel="4.19.90-25.10.v2101", arch="x86_64",
        deps={}, captured_at="2026-11-17T03:00:00+00:00",
        verify_result="unknown",
    )
    base.update(kw)
    return EnvFingerprint(**base)


# ── os-release 解析 ───────────────────────────────────────────────────
def test_read_os_release_parses_quoted_values(tmp_path):
    p = tmp_path / "os-release"
    p.write_text(KYLIN_RELEASE, encoding="utf-8")
    raw = read_os_release(p)
    assert raw["ID"] == "kylin"
    assert raw["VERSION_ID"] == "V11"
    assert 'SP3' in raw["PRETTY_NAME"]


def test_parse_os_kylin_extracts_sp(tmp_path):
    p = tmp_path / "os-release"
    p.write_text(KYLIN_RELEASE, encoding="utf-8")
    os_id, ver, sp = parse_os(read_os_release(p))
    assert os_id == "kylin"
    assert ver == "v11"
    assert sp == "sp3"


def test_parse_os_ubuntu_without_sp(tmp_path):
    p = tmp_path / "os-release"
    p.write_text(UBUNTU_RELEASE, encoding="utf-8")
    os_id, ver, sp = parse_os(read_os_release(p))
    assert os_id == "ubuntu"
    assert ver == "22.04"
    assert sp == ""


def test_parse_os_missing_fields_are_safe():
    assert parse_os({}) == ("unknown", "unknown", "")


# ── 匹配分级 ──────────────────────────────────────────────────────────
def test_match_identical_is_exact_everywhere():
    levels = match(fp(), fp())
    assert levels == {"os": "exact", "arch": "exact", "kernel": "exact", "deps": "exact"}
    assert describe(levels) == "环境完全匹配"


def test_match_sp_diff_is_minor():
    levels = match(fp(os_sp="sp2"), fp(os_sp="sp3"))
    assert levels["os"] == "minor"


def test_match_version_diff_is_major():
    levels = match(fp(os_version="v10"), fp(os_version="v11"))
    assert levels["os"] == "major"


def test_match_different_distro_is_none():
    levels = match(fp(os_id="ubuntu"), fp(os_id="kylin"))
    assert levels["os"] == "none"


def test_match_arch_diff_is_none_and_hard_incompatible():
    levels = match(fp(arch="loongarch64"), fp(arch="x86_64"))
    assert levels["arch"] == "none"
    bad, reason = is_hard_incompatible(levels)
    assert bad is True
    assert "架构" in reason


def test_match_kernel_major_and_minor():
    assert match(fp(kernel="4.19.90"), fp(kernel="5.10.0"))["kernel"] == "major"
    assert match(fp(kernel="4.19.90"), fp(kernel="4.19.91"))["kernel"] == "minor"
    assert match(fp(kernel="4.19.90"), fp(kernel="4.19.90"))["kernel"] == "exact"


def test_match_deps_missing_is_none():
    exp = fp(deps={"nginx": "1.24"})
    levels = match(exp, fp(deps={}))
    assert levels["deps"] == "none"
    bad, reason = is_hard_incompatible(levels)
    assert bad is True
    assert "依赖" in reason


def test_match_deps_version_diff_is_minor():
    exp = fp(deps={"nginx": "1.24"})
    levels = match(exp, fp(deps={"nginx": "1.20"}))
    assert levels["deps"] == "minor"
    assert is_hard_incompatible(levels)[0] is False


def test_match_empty_deps_requirement_is_exact():
    assert match(fp(deps={}), fp(deps={"nginx": "1.24"}))["deps"] == "exact"


def test_describe_lists_only_non_exact():
    text = describe({"os": "minor", "arch": "exact", "kernel": "exact", "deps": "exact"})
    assert "操作系统" in text
    assert "次版本差异" in text
    assert "架构" not in text


# ── 归一化与采集 ──────────────────────────────────────────────────────
def test_normalize_from_raw_dict():
    got = normalize({
        "ID": "Kylin", "VERSION_ID": "V11", "os_sp": "SP3",
        "kernel": "4.19.90", "arch": "X86_64",
        "deps": {"nginx": "1.24"},
    })
    assert got.os_id == "kylin"
    assert got.arch == "x86_64"
    assert got.os_sp == "sp3"
    assert got.deps["nginx"] == "1.24"


def test_capture_does_not_raise_off_linux():
    """Windows 上采集应降级返回 unknown，而不是抛异常。"""
    got = capture(with_deps=False)
    assert got.os_id
    assert got.arch
    assert got.verify_result == "unknown"
