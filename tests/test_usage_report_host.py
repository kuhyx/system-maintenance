"""Tests for usage_report's host fingerprint: the /proc and nvidia-smi probes
behind _host_profile, and the Markdown bullets _fingerprint_section builds.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import _usage_report_render as render

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _fake_proc_files(
    monkeypatch: pytest.MonkeyPatch,
    contents: dict[str, str | OSError],
) -> None:
    """Route Path.open for /proc/cpuinfo and /proc/meminfo to canned text.

    _host_profile only ever opens those two paths, so every caller supplies
    both and there is no real-filesystem fallback to keep here.
    """

    def fake_open(
        self: Path,
        *_args: object,
        **_kwargs: object,
    ) -> io.StringIO:
        value = contents[str(self)]
        if isinstance(value, OSError):
            raise value
        return io.StringIO(value)

    monkeypatch.setattr(render.Path, "open", fake_open)


def _stub_platform(monkeypatch: pytest.MonkeyPatch, *, cpus: int | None) -> None:
    """Pin hostname, kernel and CPU count so assertions are machine-agnostic."""
    monkeypatch.setattr(render.platform, "node", lambda: "box")
    monkeypatch.setattr(render.platform, "release", lambda: "6.1.0")
    monkeypatch.setattr(render.os, "cpu_count", lambda: cpus)


# --------------------------------------------------------------------------- #
# _host_profile
# --------------------------------------------------------------------------- #
def test_host_profile_reads_cpu_model_and_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU model and total memory are lifted out of the /proc files."""
    _stub_platform(monkeypatch, cpus=8)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(
        monkeypatch,
        {
            "/proc/cpuinfo": "flags\t: x\nmodel name\t: Ryzen 9\nmodel name\t: other\n",
            "/proc/meminfo": "SwapTotal: 1 kB\nMemTotal:       32768000 kB\n",
        },
    )

    info = render._host_profile()

    assert info["hostname"] == "box"
    assert info["kernel"] == "6.1.0"
    assert info["cpus_online"] == "8"
    assert info["cpu_model"] == "Ryzen 9"
    assert info["memory_total_gib"] == "31.2"
    assert "gpu" not in info


def test_host_profile_includes_gpu_when_nvidia_smi_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-line nvidia-smi output is flattened onto one line."""
    _stub_platform(monkeypatch, cpus=1)
    monkeypatch.setattr(render, "_run", lambda _cmd: "GPU0, 8 GiB\nGPU1, 8 GiB\n")
    _fake_proc_files(monkeypatch, {"/proc/cpuinfo": "", "/proc/meminfo": ""})

    assert render._host_profile()["gpu"] == "GPU0, 8 GiB; GPU1, 8 GiB"


def test_host_profile_survives_unreadable_proc_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError on either /proc file leaves that key absent, not raising."""
    _stub_platform(monkeypatch, cpus=1)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(
        monkeypatch,
        {
            "/proc/cpuinfo": OSError("nope"),
            "/proc/meminfo": OSError("nope"),
        },
    )

    info = render._host_profile()

    assert "cpu_model" not in info
    assert "memory_total_gib" not in info


def test_host_profile_survives_meminfo_without_digits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MemTotal line with no parseable number is skipped, not fatal."""
    _stub_platform(monkeypatch, cpus=1)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(
        monkeypatch,
        {"/proc/cpuinfo": "", "/proc/meminfo": "MemTotal:       kB\n"},
    )

    assert "memory_total_gib" not in render._host_profile()


def test_host_profile_defaults_cpu_count_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """os.cpu_count() returning None renders as '0' rather than 'None'."""
    _stub_platform(monkeypatch, cpus=None)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(monkeypatch, {"/proc/cpuinfo": "", "/proc/meminfo": ""})

    assert render._host_profile()["cpus_online"] == "0"


# --------------------------------------------------------------------------- #
# _fingerprint_section
# --------------------------------------------------------------------------- #
def test_fingerprint_section_renders_each_fact_as_a_bullet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every host-profile key becomes one bold Markdown bullet."""
    monkeypatch.setattr(
        render,
        "_host_profile",
        lambda: {"hostname": "box", "kernel": "6"},
    )

    assert render._fingerprint_section() == [
        "## Host",
        "",
        "- **hostname**: box",
        "- **kernel**: 6",
        "",
    ]
